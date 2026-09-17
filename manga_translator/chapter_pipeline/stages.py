from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

from .artifacts import ArtifactStore, hash_json
from .config import PipelineSettings
from .models import ChapterStage
from .storage import PipelineStorage


DEFAULT_STAGES: tuple[ChapterStage, ...] = (
    ChapterStage.OCR,
    ChapterStage.TRANSLATE,
    ChapterStage.INPAINT,
    ChapterStage.RENDER,
    ChapterStage.SUMMARIZE,
    ChapterStage.EMBED,
)

STAGE_DEPENDENCIES: dict[ChapterStage, tuple[ChapterStage, ...]] = {
    ChapterStage.OCR: (),
    ChapterStage.VISION: (ChapterStage.OCR,),
    ChapterStage.KNOWLEDGE: (ChapterStage.VISION,),
    ChapterStage.TRANSLATE: (ChapterStage.OCR,),
    ChapterStage.INPAINT: (ChapterStage.OCR,),
    ChapterStage.RENDER: (
        ChapterStage.INPAINT,
        ChapterStage.TRANSLATE,
    ),
    ChapterStage.SUMMARIZE: (ChapterStage.TRANSLATE,),
    ChapterStage.EMBED: (ChapterStage.SUMMARIZE,),
}


@dataclass(slots=True)
class StageExecution:
    job_id: str
    series_id: str
    source_root: str
    chapter: dict[str, Any]
    pages: list[dict[str, Any]]
    options: dict[str, Any]
    version_hash: str
    version_hashes: dict[str, str]
    artifacts: ArtifactStore
    storage: PipelineStorage
    settings: PipelineSettings
    report_progress: Callable[[str, int, int], Awaitable[None]]
    check_cancelled: Callable[[], Awaitable[bool]]
    page_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class StageResult:
    output_hash: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class StageRuntime(Protocol):
    async def run_ocr(self, execution: StageExecution) -> StageResult: ...

    async def run_ocr_batch(
        self,
        executions: list[StageExecution],
    ) -> dict[str, StageResult]: ...

    async def run_vision(self, execution: StageExecution) -> StageResult: ...

    async def run_knowledge(self, execution: StageExecution) -> StageResult: ...

    async def run_translate(self, execution: StageExecution) -> StageResult: ...

    async def run_inpaint(self, execution: StageExecution) -> StageResult: ...

    async def run_render(self, execution: StageExecution) -> StageResult: ...

    async def run_summarize(self, execution: StageExecution) -> StageResult: ...

    async def run_embed(self, execution: StageExecution) -> StageResult: ...


class StageRuntimeDispatcher:
    def __init__(self, runtime: StageRuntime):
        self.runtime = runtime

    async def run(
        self,
        stage: ChapterStage,
        execution: StageExecution,
    ) -> StageResult:
        method = getattr(self.runtime, f"run_{stage.value}")
        return await method(execution)


def normalize_stages(
    stages: Sequence[ChapterStage | str] | None,
    *,
    enable_embeddings: bool = False,
) -> tuple[ChapterStage, ...]:
    if stages:
        requested = {ChapterStage(stage) for stage in stages}
        changed = True
        while changed:
            changed = False
            for stage in tuple(requested):
                for dependency in STAGE_DEPENDENCIES[stage]:
                    if dependency not in requested:
                        requested.add(dependency)
                        changed = True
        result = tuple(stage for stage in DEFAULT_STAGES if stage in requested)
    else:
        result = DEFAULT_STAGES
    if not enable_embeddings:
        result = tuple(stage for stage in result if stage != ChapterStage.EMBED)
    return result


def affected_stages(stage: ChapterStage | str) -> tuple[ChapterStage, ...]:
    changed = ChapterStage(stage)
    affected = {changed}
    changed_any = True
    while changed_any:
        changed_any = False
        for candidate, dependencies in STAGE_DEPENDENCIES.items():
            if candidate in affected:
                continue
            if any(dependency in affected for dependency in dependencies):
                affected.add(candidate)
                changed_any = True
    return tuple(stage for stage in DEFAULT_STAGES if stage in affected)


def stage_model_id(
    stage: ChapterStage,
    *,
    settings: PipelineSettings,
    options: dict[str, Any],
    chapter: dict[str, Any],
) -> str:
    overrides = options.get("models") or {}
    if stage == ChapterStage.OCR:
        return settings.ocr_model_for(chapter, options=options)
    if stage == ChapterStage.VISION:
        return str(overrides.get("vision", settings.character_model))
    if stage == ChapterStage.KNOWLEDGE:
        return str(overrides.get("knowledge", settings.character_model))
    if stage == ChapterStage.TRANSLATE:
        provider = str(
            overrides.get(
                "translation_provider",
                options.get(
                    "translation_provider",
                    settings.translation_provider,
                ),
            )
        )
        model = str(
            overrides.get("translation", settings.translation_model)
            if provider == "local"
            else overrides.get(
                "translation",
                settings.online_openai_model or settings.translation_model,
            )
        )
        return f"{provider}:{model}"
    if stage == ChapterStage.INPAINT:
        return str(overrides.get("inpaint", settings.inpainter_model))
    if stage == ChapterStage.RENDER:
        return str(overrides.get("render", settings.renderer_model))
    if stage == ChapterStage.SUMMARIZE:
        return str(overrides.get("summary", settings.translation_model))
    if stage == ChapterStage.EMBED:
        return str(overrides.get("embedding", settings.embedding_model_id))
    return stage.value


def ocr_filter_rules() -> dict[str, list[str]]:
    """Effective OCR text filter rules, used for stage invalidation.

    Read straight from ``config/filter_list.json`` instead of going through
    ``manga_translator.utils.text_filter`` so stage planning stays free of the
    OCR/model stack. Shaping mirrors that module: trimmed and lowercased.
    """

    from ..runtime_paths import get_config_path

    def clean(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return sorted(
            text
            for text in (str(value).strip().lower() for value in values)
            if text
        )

    try:
        payload = json.loads(
            Path(get_config_path("filter_list.json")).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {"contains": [], "exact": []}
    if not isinstance(payload, dict):
        return {"contains": [], "exact": []}
    return {
        "contains": clean(payload.get("contains")),
        "exact": clean(payload.get("exact")),
    }


def stage_config(
    stage: ChapterStage,
    *,
    settings: PipelineSettings,
    options: dict[str, Any],
) -> dict[str, Any]:
    overrides = options.get("stage_config") or {}
    value = overrides.get(stage.value) or {}
    if stage == ChapterStage.OCR:
        include = {
            "correction_threshold": options.get(
                "ocr_correction_threshold"
            ),
            "detector": options.get("detector"),
            "bubble_filter": options.get("bubble_filter"),
            "ignore_bubble": (options.get("stage_config") or {})
            .get("ocr", {})
            .get("ignore_bubble"),
            "ocr_options": (options.get("stage_config") or {}).get(
                "ocr",
                {},
            ),
            "ocr_pages_per_batch": options.get(
                "ocr_pages_per_batch",
                8,
            ),
            "korean_webtoon": options.get("korean_webtoon", False),
            # The OCR stage drops filtered text lines, so the filter rules
            # change its output and must take part in the version hash.
            "ocr_filter_rules": ocr_filter_rules(),
        }
    elif stage in {ChapterStage.VISION, ChapterStage.KNOWLEDGE}:
        include = {
            "speaker_confidence_threshold": options.get(
                "speaker_confidence_threshold"
            ),
            "character_context": options.get("character_context", True),
        }
    elif stage == ChapterStage.TRANSLATE:
        include = {
            "target_language": options.get(
                "target_language",
                settings.target_language,
            ),
            "taiwan_wording": True,
            "translation_mode": options.get("translation_mode", "hq"),
            "hq_batch_size": options.get("hq_batch_size", 2),
            "hq_max_regions_per_request": options.get(
                "hq_max_regions_per_request",
                60,
            ),
            "translation_confidence_threshold": options.get(
                "translation_confidence_threshold"
            ),
        }
    elif stage == ChapterStage.INPAINT:
        include = {
            "mask_dilation": options.get("mask_dilation"),
            "inpainting_size": options.get("inpainting_size"),
        }
    elif stage == ChapterStage.RENDER:
        include = {
            "font_family": options.get("font_family"),
            "layout_mode": options.get("layout_mode", "smart_scaling"),
            "direction": options.get("direction", "auto"),
            "alignment": options.get("alignment", "auto"),
        }
    elif stage == ChapterStage.SUMMARIZE:
        include = {"citation_required": True}
    else:
        include = {}
    return {**include, **value}


def input_hash_for_stage(
    stage: ChapterStage,
    *,
    pages: list[dict[str, Any]],
    version_hashes: dict[str, str],
) -> str:
    page_inputs = [
        {
            "id": page["id"],
            "sha256": page.get("sha256", ""),
        }
        for page in pages
    ]
    dependency_inputs = {
        dependency.value: version_hashes.get(dependency.value, "")
        for dependency in STAGE_DEPENDENCIES[stage]
    }
    return hash_json(
        {
            "pages": page_inputs,
            "dependencies": dependency_inputs,
        }
    )
