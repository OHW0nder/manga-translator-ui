from __future__ import annotations

import asyncio
import base64
import gc
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .artifacts import (
    ArtifactStore,
    hash_file,
    hash_json,
    upgrade_page_payload,
)
from .config import PipelineSettings
from .knowledge import KnowledgeService
from .model_control import (
    ExternalModelManagerClient,
    ModelLease,
    ModelManagerClient,
    ModelManagerError,
    unload_web_models,
)
from .models import (
    ChapterStage,
    RegionRecord,
    ReviewStatus,
    SpeechType,
)
from .paths import is_relative_to
from .providers import (
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    ProviderError,
    gather_limited,
)
from .series_config import LanguageRule
from .stages import StageExecution, StageResult
from .storage import PipelineStorage
from .translation import TranslationAdapter, TranslationRequest
from .vision import VisionAnalyzer


logger = logging.getLogger("manga_translator.chapter_pipeline")


class DefaultChapterRuntime:
    """Adapter from chapter stages to the existing model pipeline."""

    def __init__(
        self,
        settings: PipelineSettings,
        storage: PipelineStorage,
    ):
        self.settings = settings
        self.storage = storage
        if settings.external_qwen_url:
            self.model_manager = ExternalModelManagerClient(
                settings.external_qwen_url
            )
        else:
            self.model_manager = ModelManagerClient(
                settings.model_manager_url,
                settings.model_manager_token,
            )

    async def run_ocr(self, execution: StageExecution) -> StageResult:
        results = await self.run_ocr_batch([execution])
        return results[execution.chapter["id"]]

    async def run_ocr_batch(
        self,
        executions: list[StageExecution],
    ) -> dict[str, StageResult]:
        """Run OCR for all chapters with one resident OCR model per language."""

        if not executions:
            return {}
        started = time.monotonic()
        await self._prepare_web_stage()
        grouped: dict[tuple[str, str], list[StageExecution]] = {}
        for execution in executions:
            chapter = execution.chapter
            ocr_model = self.settings.ocr_model_for(
                chapter,
                options=execution.options,
            )
            key = (str(chapter.get("source_language") or ""), ocr_model)
            grouped.setdefault(key, []).append(execution)
        try:
            for group in grouped.values():
                await self._run_ocr_language_group(group)
        finally:
            await unload_web_models()

        results: dict[str, StageResult] = {}
        total_pages = 0
        for execution in executions:
            page_count = len(execution.pages)
            total_pages += page_count
            results[execution.chapter["id"]] = StageResult(
                output_hash=execution.artifacts.hash_stage(
                    execution.chapter["name"],
                    ChapterStage.OCR,
                    execution.version_hash,
                ),
                checkpoint={"pages": page_count},
                metrics=self._resource_metrics(started, page_count),
            )
        logger.info(
            "Batch OCR completed: %s chapters, %s pages",
            len(executions),
            total_pages,
        )
        return results

    async def _run_ocr_language_group(
        self,
        executions: list[StageExecution],
    ) -> None:
        from manga_translator.manga_translator import MangaTranslator
        from manga_translator.utils import open_pil_image
        from manga_translator.utils.path_manager import get_json_path

        options = executions[0].options
        chapter = executions[0].chapter
        source_language = str(chapter.get("source_language") or "")
        rule = self.settings.language_rule_for_chapter(chapter)
        config = self._new_config()
        config.ocr.ocr = self._ocr_model(chapter, options)
        self._apply_ocr_options(config, rule, options)
        config.translator.target_lang = self.settings.target_code
        config.cli.save_text = True
        config.cli.overwrite = True
        config.cli.batch_concurrent = False
        config.cli.use_gpu = True
        detector_override = (options.get("stage_config") or {}).get(
            "ocr", {}
        ).get("detector")
        if detector_override:
            config.detector.detector = detector_override

        translator = MangaTranslator(
            {
                "use_gpu": True,
                "verbose": False,
                "models_ttl": 0,
                "attempts": 1,
                "template": True,
                "save_text": True,
                "batch_size": 1,
                # Explicit: the chapter pipeline always honours
                # config/filter_list.json. MangaTranslator would default to
                # True anyway, but config.json's filter_text_enabled is a
                # traditional-WebUI switch and must not silently win here.
                "filter_text_enabled": True,
            }
        )
        page_refs: list[tuple[StageExecution, dict[str, Any], Path, int]] = []
        skipped = 0
        for execution in executions:
            for page_index, page in enumerate(execution.pages):
                existing = execution.artifacts.read_json(
                    execution.chapter["name"],
                    ChapterStage.OCR,
                    execution.version_hash,
                    page["relative_path"],
                )
                if existing is not None and isinstance(
                    existing.get("regions"),
                    list,
                ):
                    skipped += 1
                    continue
                page_refs.append(
                    (
                        execution,
                        page,
                        self._work_path(
                            execution,
                            ChapterStage.OCR,
                            page,
                        ),
                        page_index,
                    )
                )
        batch_size = max(
            1,
            int(
                options.get(
                    "ocr_pages_per_batch",
                    self.settings.ocr_pages_per_batch,
                )
            ),
        )
        try:
            for batch_start in range(0, len(page_refs), batch_size):
                if executions and await executions[0].check_cancelled():
                    return
                chunk = page_refs[batch_start : batch_start + batch_size]
                loaded_images = []
                offsets: dict[int, int | None] = {}
                try:
                    for position, (
                        execution,
                        page,
                        work_path,
                        page_index,
                    ) in enumerate(chunk):
                        source_path = self._source_path(execution, page)
                        overlap = _boundary_overlap(execution.options)
                        stitched = None
                        if overlap > 0:
                            stitched = self._stitch_boundary(
                                execution,
                                page_index,
                                overlap=overlap,
                            )
                        if stitched is None:
                            shutil.copy2(source_path, work_path)
                            image_path = work_path
                            offsets[position] = None
                        else:
                            stitched_image, offset = stitched
                            # Scratch for OCR only; JPEG keeps it ~0.4MB/page
                            # instead of ~2MB/page for a lossless PNG.
                            image_path = work_path
                            stitched_image.save(
                                image_path, format="JPEG", quality=95
                            )
                            stitched_image.close()
                            offsets[position] = offset
                        image = open_pil_image(image_path, eager=False)
                        image.name = str(image_path)
                        loaded_images.append(image)
                        chunk[position] = (
                            execution,
                            page,
                            image_path,
                            page_index,
                        )
                    batch_items = [
                        (image, config.model_copy(deep=True))
                        for image in loaded_images
                    ]
                    contexts = await translator.translate_batch(
                        batch_items,
                        batch_size=1,
                    )
                    if len(contexts) != len(chunk):
                        raise RuntimeError(
                            "OCR batch returned an unexpected number of contexts"
                        )
                    for index, (execution, page, work_path, page_index) in enumerate(
                        chunk
                    ):
                        context = contexts[index]
                        if getattr(context, "translation_error", None):
                            raise RuntimeError(str(context.translation_error))
                        json_path = Path(
                            get_json_path(str(work_path), create_dir=False)
                        )
                        if not json_path.is_file():
                            raise FileNotFoundError(
                                f"OCR did not create page JSON: {json_path}"
                            )
                        data = json.loads(json_path.read_text(encoding="utf-8"))
                        page_data = next(iter(data.values()))
                        if isinstance(page_data, list):
                            page_data = {"regions": page_data}
                        if not isinstance(page_data, dict):
                            raise ValueError(
                                f"invalid OCR page payload: {json_path}"
                            )
                        payload = upgrade_page_payload(
                            page_data,
                            relative_path=page["relative_path"],
                            source_language=source_language,
                            artifact_version=execution.version_hash,
                        )
                        payload = self._restore_page_coordinates(
                            payload,
                            execution,
                            page,
                            offset=offsets.get(index),
                        )
                        page_id = page["id"]
                        self._write_page_artifact(
                            execution,
                            ChapterStage.OCR,
                            page["relative_path"],
                            payload,
                        )
                        self._index_regions(
                            execution,
                            page_id,
                            payload,
                            ChapterStage.OCR,
                            execution.version_hash,
                        )
                        self.storage.update_page_artifact(
                            page_id,
                            stage=ChapterStage.OCR,
                            artifact_version=execution.version_hash,
                            review_status=_page_review_status(payload),
                        )
                        await execution.report_progress(
                            ChapterStage.OCR.value,
                            batch_start + index + 1,
                            len(page_refs),
                        )
                finally:
                    for image in loaded_images:
                        try:
                            image.close()
                        except Exception:
                            pass
            if skipped:
                logger.info(
                    "OCR %s reused %s completed page artifacts",
                    source_language,
                    skipped,
                )
        finally:
            try:
                await translator.unload_models()
            except Exception as exc:
                logger.debug("batch OCR model unload skipped: %s", exc)
            del translator
            gc.collect()

    async def run_vision(self, execution: StageExecution) -> StageResult:
        started = time.monotonic()
        await unload_web_models()
        characters = self.storage.list_characters(execution.series_id)
        processed = 0
        async with ModelLease(
            self.model_manager,
            self.settings.qwen_model_id,
        ):
            analyzer = VisionAnalyzer(self._local_client())
            for index, page in enumerate(execution.pages, start=1):
                if await execution.check_cancelled():
                    break
                payload = self._read_page_artifact(
                    execution,
                    ChapterStage.OCR,
                    page,
                )
                if payload is None:
                    raise FileNotFoundError(
                        f"OCR artifact missing for {page['relative_path']}"
                    )
                source_path = self._source_path(execution, page)
                result = await analyzer.attribute_speakers(
                    source_path,
                    payload,
                    known_characters=characters,
                    source_language=execution.chapter["source_language"],
                )
                assignments = {
                    int(item["region_id"]): item
                    for item in result.get("regions", [])
                    if isinstance(item, dict) and "region_id" in item
                }
                threshold = float(
                    execution.options.get(
                        "speaker_confidence_threshold",
                        self.settings.speaker_confidence_threshold,
                    )
                )
                for region_index, region in enumerate(payload.get("regions") or []):
                    assignment = assignments.get(region_index, {})
                    confidence = _confidence(assignment.get("confidence"))
                    speaker_id = assignment.get("speaker_id")
                    if confidence is None or confidence < threshold:
                        speaker_id = None
                    region["speaker_id"] = speaker_id
                    region["speaker_name"] = (
                        assignment.get("speaker_name")
                        if speaker_id
                        else None
                    )
                    region["speaker_confidence"] = confidence
                    region["speech_type"] = _speech_type(
                        assignment.get("speech_type")
                    )
                    region["visual_evidence"] = str(
                        assignment.get("visual_evidence") or ""
                    )
                    region["review_status"] = (
                        ReviewStatus.AUTO_ACCEPTED.value
                        if speaker_id and confidence >= threshold
                        else ReviewStatus.NEEDS_REVIEW.value
                    )
                payload = upgrade_page_payload(
                    payload,
                    relative_path=page["relative_path"],
                    source_language=execution.chapter["source_language"],
                    artifact_version=execution.version_hash,
                    review_status=_page_review_status(payload),
                )
                self._write_page_artifact(
                    execution,
                    ChapterStage.VISION,
                    page["relative_path"],
                    payload,
                )
                self._index_regions(
                    execution,
                    page["id"],
                    payload,
                    ChapterStage.VISION,
                    execution.version_hash,
                )
                self.storage.update_page_artifact(
                    page["id"],
                    stage=ChapterStage.VISION,
                    artifact_version=execution.version_hash,
                    review_status=_page_review_status(payload),
                )
                self._persist_observations(
                    execution,
                    result.get("new_characters")
                    or result.get("observations", []),
                    page,
                )
                characters = self.storage.list_characters(execution.series_id)
                processed += 1
                await execution.report_progress(
                    ChapterStage.VISION.value,
                    index,
                    len(execution.pages),
                )
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.VISION,
                execution.version_hash,
            ),
            checkpoint={"pages": processed},
            metrics=self._resource_metrics(started, processed),
        )

    async def run_knowledge(self, execution: StageExecution) -> StageResult:
        started = time.monotonic()
        await unload_web_models()
        pages = [
            self._read_page_artifact(execution, ChapterStage.VISION, page)
            for page in execution.pages
        ]
        pages = [page for page in pages if page is not None]
        async with ModelLease(
            self.model_manager,
            self.settings.qwen_model_id,
        ):
            service = KnowledgeService(
                self.storage,
                self._local_client(),
            )
            knowledge = await service.extract_chapter_knowledge(
                chapter_name=execution.chapter["name"],
                source_language=execution.chapter["source_language"],
                pages=pages,
                known_characters=self.storage.list_characters(
                    execution.series_id
                ),
            )
        self._persist_knowledge(execution, knowledge)
        self._write_chapter_artifact(
            execution,
            ChapterStage.KNOWLEDGE,
            "_knowledge.json",
            knowledge,
        )
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.KNOWLEDGE,
                execution.version_hash,
            ),
            checkpoint={
                "characters": len(knowledge.get("characters") or []),
                "terms": len(knowledge.get("terms") or []),
            },
            metrics=self._resource_metrics(started, len(pages)),
        )

    async def run_translate(self, execution: StageExecution) -> StageResult:
        started = time.monotonic()
        from PIL import Image

        from manga_translator.config import Translator
        from manga_translator.translators.openai_hq import (
            OpenAIHighQualityTranslator,
        )
        from manga_translator.utils import Context, TextBlock

        page_entries: list[dict[str, Any]] = []
        opened_images = []
        translator = None
        processed = 0
        from_code = self.settings.translator_code_for_chapter(execution.chapter)
        to_code = self.settings.target_code
        try:
            for page in execution.pages:
                payload = self._read_page_artifact(
                    execution,
                    ChapterStage.OCR,
                    page,
                    version_hashes=execution.version_hashes,
                )
                if payload is None:
                    raise FileNotFoundError(
                        f"OCR artifact missing for {page['relative_path']}"
                    )
                source_path = self._source_path(execution, page)
                image = Image.open(source_path).convert("RGB")
                opened_images.append(image)
                regions = []
                for region_data in payload.get("regions") or []:
                    if not isinstance(region_data, dict):
                        continue
                    region = dict(region_data)
                    region.setdefault("target_lang", self.settings.target_code)
                    regions.append(TextBlock(**region))
                original_texts = [
                    str(region.text or "")
                    for region in regions
                    if str(region.text or "").strip()
                ]
                text_indices = [
                    region_index
                    for region_index, region in enumerate(regions)
                    if str(region.text or "").strip()
                ]
                text_order = list(range(1, len(original_texts) + 1))
                page_entries.append(
                    {
                        "page": page,
                        "payload": payload,
                        "image": image,
                        "batch_data": {
                            "image": image,
                            "text_regions": regions,
                            "text_order": text_order,
                            "upscaled_size": (image.height, image.width),
                            "original_texts": original_texts,
                        },
                        "text_indices": text_indices,
                        "translations": {},
                    }
                )
            config = self._new_config()
            config.translator.translator = Translator.openai_hq
            config.translator.target_lang = self.settings.target_code
            config.cli.attempts = 1
            translator = OpenAIHighQualityTranslator()
            translator.parse_args(config)
            max_images = max(
                1,
                int(execution.options.get("hq_batch_size", 2)),
            )
            max_regions = max(
                20,
                int(execution.options.get("hq_max_regions_per_request", 60)),
            )
            chunks: list[list[dict[str, Any]]] = []
            current_chunk: list[dict[str, Any]] = []
            current_regions = 0
            for entry in page_entries:
                region_count = len(entry["text_indices"])
                if current_chunk and (
                    len(current_chunk) >= max_images
                    or current_regions + region_count > max_regions
                ):
                    chunks.append(current_chunk)
                    current_chunk = []
                    current_regions = 0
                current_chunk.append(entry)
                current_regions += region_count
            if current_chunk:
                chunks.append(current_chunk)

            await unload_web_models()
            async with ModelLease(
                self.model_manager,
                self.settings.qwen_model_id,
            ):
                async def translate_entries(
                    entries: list[dict[str, Any]],
                ) -> list[str]:
                    queries = [
                        text
                        for entry in entries
                        for text in entry["batch_data"]["original_texts"]
                    ]
                    if not queries:
                        return []
                    ctx = Context()
                    ctx.input = entries[0]["image"]
                    ctx.text_regions = [
                        region
                        for entry in entries
                        for region in entry["batch_data"]["text_regions"]
                    ]
                    ctx.high_quality_batch_data = [
                        entry["batch_data"] for entry in entries
                    ]
                    ctx.from_lang = from_code
                    translated = await translator._translate(
                        ctx.from_lang,
                        to_code,
                        queries,
                        ctx,
                    )
                    if len(translated) != len(queries):
                        raise ProviderError(
                            "HQ translation returned an unexpected number of regions"
                        )
                    return translated

                async def translate_single_region(
                    entry: dict[str, Any],
                    region_index: int,
                ) -> str:
                    region = entry["batch_data"]["text_regions"][region_index]
                    query = str(region.text or "")
                    ctx = Context()
                    ctx.input = entry["image"]
                    ctx.text_regions = [region]
                    ctx.high_quality_batch_data = [
                        {
                            "image": entry["image"],
                            "text_regions": [region],
                            "text_order": [1],
                            "upscaled_size": entry["batch_data"][
                                "upscaled_size"
                            ],
                            "original_texts": [query],
                        }
                    ]
                    ctx.from_lang = from_code
                    translated = await translator._translate(
                        ctx.from_lang,
                        to_code,
                        [query],
                        ctx,
                    )
                    if len(translated) != 1:
                        raise ProviderError(
                            "HQ translation failed for a single region"
                        )
                    return str(translated[0] or "")

                async def translate_entries_with_fallback(
                    entries: list[dict[str, Any]],
                ) -> None:
                    try:
                        translated = await translate_entries(entries)
                        cursor = 0
                        for entry in entries:
                            count = len(entry["text_indices"])
                            for region_index, target in zip(
                                entry["text_indices"],
                                translated[cursor : cursor + count],
                            ):
                                entry["translations"][region_index] = (
                                    _to_simplified(str(target or "").strip())
                                )
                            cursor += count
                        return
                    except Exception:
                        if len(entries) > 1:
                            midpoint = len(entries) // 2
                            await translate_entries_with_fallback(
                                entries[:midpoint]
                            )
                            await translate_entries_with_fallback(
                                entries[midpoint:]
                            )
                            return
                        entry = entries[0]
                        for region_index in entry["text_indices"]:
                            entry["translations"][region_index] = (
                                _to_simplified(
                                    await translate_single_region(
                                        entry,
                                        region_index,
                                    )
                                )
                            )

                for chunk in chunks:
                    await translate_entries_with_fallback(chunk)

            for entry in page_entries:
                page = entry["page"]
                payload = entry["payload"]
                regions = list(payload.get("regions") or [])
                for region_index, region in enumerate(regions):
                    if not isinstance(region, dict):
                        continue
                    if not str(region.get("text") or "").strip():
                        region["translation"] = ""
                        continue
                    text = entry["translations"].get(region_index, "")
                    if not text:
                        raise ProviderError(
                            "HQ translation omitted "
                            f"{page['relative_path']} region {region_index}"
                        )
                    region["translation"] = text
                    region["translation_confidence"] = None
                    region["review_status"] = ReviewStatus.AUTO_ACCEPTED.value
                payload = upgrade_page_payload(
                    payload,
                    relative_path=page["relative_path"],
                    source_language=execution.chapter["source_language"],
                    artifact_version=execution.version_hash,
                    review_status=ReviewStatus.AUTO_ACCEPTED,
                )
                self._write_page_artifact(
                    execution,
                    ChapterStage.TRANSLATE,
                    page["relative_path"],
                    payload,
                )
                self._index_regions(
                    execution,
                    page["id"],
                    payload,
                    ChapterStage.TRANSLATE,
                    execution.version_hash,
                )
                self.storage.update_page_artifact(
                    page["id"],
                    stage=ChapterStage.TRANSLATE,
                    artifact_version=execution.version_hash,
                    review_status=ReviewStatus.AUTO_ACCEPTED.value,
                )
                processed += 1
        finally:
            for image in opened_images:
                try:
                    image.close()
                except Exception:
                    pass
            if translator is not None:
                try:
                    await translator._cleanup()
                except Exception:
                    pass
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.TRANSLATE,
                execution.version_hash,
            ),
            checkpoint={"pages": processed},
            metrics=self._resource_metrics(started, processed),
        )

    async def run_inpaint(self, execution: StageExecution) -> StageResult:
        started = time.monotonic()
        await self._prepare_web_stage()
        processed = 0
        try:
            for index, page in enumerate(execution.pages, start=1):
                if await execution.check_cancelled():
                    break
                source_path = self._source_path(execution, page)
                payload = self._read_page_artifact(
                    execution,
                    ChapterStage.OCR,
                    page,
                )
                if payload is None:
                    raise FileNotFoundError(
                        f"OCR artifact missing for {page['relative_path']}"
                    )
                await self._inpaint_page(
                    execution,
                    page,
                    source_path,
                    payload,
                )
                processed += 1
                await execution.report_progress(
                    ChapterStage.INPAINT.value,
                    index,
                    len(execution.pages),
                )
        finally:
            await self._unload_inpainter()
            await unload_web_models()
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.INPAINT,
                execution.version_hash,
            ),
            checkpoint={"pages": processed},
            metrics=self._resource_metrics(started, processed),
        )

    async def run_render(self, execution: StageExecution) -> StageResult:
        started = time.monotonic()
        await self._prepare_web_stage()
        processed = 0
        try:
            for index, page in enumerate(execution.pages, start=1):
                if await execution.check_cancelled():
                    break
                source_path = self._source_path(execution, page)
                payload = self._read_page_artifact(
                    execution,
                    ChapterStage.TRANSLATE,
                    page,
                )
                if payload is None:
                    raise FileNotFoundError(
                        f"translation artifact missing for {page['relative_path']}"
                    )
                rendered_payload = await self._render_page(
                    execution,
                    page,
                    source_path,
                    payload,
                )
                self._write_page_artifact(
                    execution,
                    ChapterStage.RENDER,
                    page["relative_path"],
                    rendered_payload,
                )
                self.storage.update_page_artifact(
                    page["id"],
                    stage=ChapterStage.RENDER,
                    artifact_version=execution.version_hash,
                    review_status=_page_review_status(rendered_payload),
                )
                processed += 1
                await execution.report_progress(
                    ChapterStage.RENDER.value,
                    index,
                    len(execution.pages),
                )
        finally:
            await unload_web_models()
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.RENDER,
                execution.version_hash,
            ),
            checkpoint={"pages": processed},
            metrics=self._resource_metrics(started, processed),
        )

    async def run_summarize(self, execution: StageExecution) -> StageResult:
        started = time.monotonic()
        pages = [
            {
                "relative_path": page["relative_path"],
                "regions": (
                    self._read_page_artifact(
                        execution,
                        ChapterStage.TRANSLATE,
                        page,
                    )
                    or {}
                ).get("regions", []),
            }
            for page in execution.pages
        ]
        await unload_web_models()
        async with ModelLease(
            self.model_manager,
            self.settings.qwen_model_id,
        ):
            client = self._local_client()
            result = await client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Summarize only facts present in the supplied "
                            "translated manga pages. Every event must cite a "
                            "relative page path and region id."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Return JSON: {\"chapter_summary\":\"\","
                            "\"events\":[{\"description\":\"\",\"confidence\":0.0,"
                            "\"source_refs\":[{\"relative_path\":\"\",\"region_id\":0}]}],"
                            "\"character_updates\":[]}. "
                            f"Chapter {execution.chapter['name']}: {pages}"
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=8192,
            )
        self._write_chapter_artifact(
            execution,
            ChapterStage.SUMMARIZE,
            "_summary.json",
            result,
        )
        self.storage.upsert_summary(
            series_id=execution.series_id,
            chapter_id=execution.chapter["id"],
            kind="chapter",
            content=str(result.get("chapter_summary") or ""),
            confidence=_confidence(
                (result.get("chapter_summary_confidence"))
            ),
            source_ref={
                "chapter": execution.chapter["name"],
                "pages": [page["relative_path"] for page in execution.pages],
            },
        )
        for event in result.get("events") or []:
            if not isinstance(event, dict):
                continue
            refs = event.get("source_refs") or []
            self.storage.upsert_event(
                series_id=execution.series_id,
                chapter_id=execution.chapter["id"],
                page_id=None,
                description=str(event.get("description") or ""),
                confidence=_confidence(event.get("confidence")),
                source_ref={"references": refs},
            )
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.SUMMARIZE,
                execution.version_hash,
            ),
            checkpoint={
                "events": len(result.get("events") or []),
            },
            metrics=self._resource_metrics(started, 1),
        )

    async def run_embed(self, execution: StageExecution) -> StageResult:
        if not execution.options.get(
            "enable_embeddings",
            self.settings.enable_embeddings,
        ):
            return StageResult(
                output_hash=hash_json({"enabled": False}),
                checkpoint={"enabled": False},
            )
        started = time.monotonic()
        await unload_web_models()
        summary = self._read_chapter_artifact(
            execution,
            ChapterStage.SUMMARIZE,
            "_summary.json",
        ) or {}
        contents = [
            str(summary.get("chapter_summary") or ""),
            *[
                str(event.get("description") or "")
                for event in summary.get("events") or []
                if isinstance(event, dict)
            ],
        ]
        contents = [value for value in contents if value.strip()]
        async with ModelLease(
            self.model_manager,
            self.settings.embedding_model_id,
        ):
            client = self._embedding_client()
            vectors = await client.embeddings(contents)
        for index, (content, vector) in enumerate(zip(contents, vectors)):
            self.storage.upsert_embedding(
                entity_type="chapter_summary" if index == 0 else "event",
                entity_id=f"{execution.chapter['id']}:{index}",
                model_id=self.settings.embedding_model_id,
                vector=vector,
                content_hash=hash_json(content),
            )
        self._write_chapter_artifact(
            execution,
            ChapterStage.EMBED,
            "_embeddings.json",
            {"model_id": self.settings.embedding_model_id, "count": len(vectors)},
        )
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                ChapterStage.EMBED,
                execution.version_hash,
            ),
            checkpoint={"embeddings": len(vectors)},
            metrics=self._resource_metrics(started, len(vectors)),
        )

    async def _correct_low_confidence_regions(
        self,
        analyzer: VisionAnalyzer,
        payload: dict[str, Any],
        source_path: Path,
        source_language: str,
        threshold: float,
    ) -> None:
        try:
            corrections = await analyzer.correct_ocr(
                source_path,
                payload,
                source_language=source_language,
            )
        except ProviderError as exc:
            logger.warning("Qwen OCR correction failed: %s", exc)
            for region in payload.get("regions") or []:
                if _confidence(
                    region.get("ocr_confidence", region.get("prob"))
                ) < threshold:
                    region["review_status"] = ReviewStatus.NEEDS_REVIEW.value
            payload["review_status"] = ReviewStatus.NEEDS_REVIEW.value
            return
        by_id = {
            int(item["region_id"]): item
            for item in corrections
            if isinstance(item, dict) and "region_id" in item
        }
        for index, region in enumerate(payload.get("regions") or []):
            current = _confidence(
                region.get("ocr_confidence", region.get("prob"))
            )
            if current is not None and current >= threshold:
                continue
            correction = by_id.get(index)
            if not correction:
                region["review_status"] = ReviewStatus.NEEDS_REVIEW.value
                continue
            corrected_text = str(correction.get("text") or "").strip()
            if corrected_text:
                region["text"] = corrected_text
            region["ocr_confidence"] = _confidence(
                correction.get("confidence")
            )
            region["ocr_corrected_by"] = self.settings.qwen_model_id
            region["review_status"] = ReviewStatus.NEEDS_REVIEW.value
        payload["review_status"] = _page_review_status(payload)

    async def _inpaint_page(
        self,
        execution: StageExecution,
        page: dict[str, Any],
        source_path: Path,
        payload: dict[str, Any],
    ) -> Path:
        import cv2
        import numpy as np
        from PIL import Image

        from manga_translator.config import Inpainter
        from manga_translator.inpainting import dispatch as dispatch_inpainting

        with Image.open(source_path) as source_image:
            image = np.asarray(source_image.convert("RGB"))
        mask = _decode_mask(payload.get("mask_raw"))
        if mask is None:
            mask = _mask_from_regions(payload, image.shape[:2], cv2)
        if mask.shape[:2] != image.shape[:2]:
            mask = cv2.resize(
                mask,
                (image.shape[1], image.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            )
        dilation = int(execution.options.get("mask_dilation", 0) or 0)
        if dilation > 0:
            mask = cv2.dilate(
                mask,
                np.ones((3, 3), dtype=np.uint8),
                iterations=max(1, dilation // 3),
            )
        config = self._new_config()
        inpainter_name = str(
            (execution.options.get("models") or {}).get(
                "inpaint",
                self.settings.inpainter_model,
            )
        )
        config.inpainter.inpainter = Inpainter(inpainter_name)
        result = await dispatch_inpainting(
            config.inpainter.inpainter,
            image,
            mask,
            config.inpainter,
            config.inpainter.inpainting_size,
            device="cuda" if config.cli.use_gpu else "cpu",
            verbose=False,
        )
        target = execution.artifacts.artifact_path(
            execution.chapter["name"],
            ChapterStage.INPAINT,
            execution.version_hash,
            page["relative_path"],
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(result).save(target)
        return target

    async def _render_page(
        self,
        execution: StageExecution,
        page: dict[str, Any],
        source_path: Path,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        from manga_translator.manga_translator import MangaTranslator
        from manga_translator.utils import open_pil_image
        from manga_translator.utils.path_manager import (
            get_inpainted_path,
            get_json_path,
        )

        work_path = self._work_path(
            execution,
            ChapterStage.RENDER,
            page,
        )
        shutil.copy2(source_path, work_path)
        inpainted_path = execution.artifacts.artifact_path(
            execution.chapter["name"],
            ChapterStage.INPAINT,
            execution.version_hashes[ChapterStage.INPAINT.value],
            page["relative_path"],
        )
        if not inpainted_path.is_file():
            raise FileNotFoundError(
                f"inpaint artifact missing for {page['relative_path']}"
            )
        render_inpainted_path = Path(get_inpainted_path(str(work_path)))
        render_inpainted_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(inpainted_path, render_inpainted_path)
        json_path = Path(get_json_path(str(work_path)))
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(
                {str(work_path): payload},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        config = self._new_config()
        config.translator.target_lang = self.settings.target_code
        from manga_translator.config import Renderer

        renderer_name = str(
            (execution.options.get("models") or {}).get(
                "render",
                self.settings.renderer_model,
            )
        )
        config.render.renderer = Renderer(renderer_name)
        config.cli.save_text = True
        config.cli.overwrite = True
        config.cli.batch_concurrent = False
        config.cli.use_gpu = True
        _apply_render_options(config, execution.options)
        translator = MangaTranslator(
            {
                "use_gpu": True,
                "verbose": False,
                "models_ttl": 0,
                "attempts": 1,
                "load_text": True,
                "save_text": True,
                "batch_size": 1,
            }
        )
        image = open_pil_image(work_path, eager=False)
        image.name = str(work_path)
        output_dir = execution.artifacts.stage_dir(
            execution.chapter["name"],
            ChapterStage.RENDER,
            execution.version_hash,
        )
        try:
            results = await translator.translate_batch(
                [(image, config)],
                batch_size=1,
                save_info={
                    "output_folder": str(output_dir),
                    "overwrite": True,
                },
            )
        finally:
            try:
                image.close()
            except Exception:
                pass
            try:
                await translator.unload_models()
            except Exception as exc:
                logger.debug("ephemeral render model unload skipped: %s", exc)
            del translator
            gc.collect()
        if not results:
            raise RuntimeError(f"render returned no context for {page['filename']}")
        context = results[0]
        if getattr(context, "translation_error", None):
            raise RuntimeError(str(context.translation_error))
        output_path = Path(
            getattr(context, "output_path", "") or (output_dir / page["filename"])
        )
        if not output_path.is_file():
            raise FileNotFoundError(
                f"renderer did not create output image: {output_path}"
            )
        active_path = self._active_result_path(
            execution,
            page["relative_path"],
        )
        active_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_path, active_path)

        rendered_payload = payload
        if json_path.is_file():
            try:
                saved = json.loads(json_path.read_text(encoding="utf-8"))
                candidate = next(iter(saved.values()), payload)
                if isinstance(candidate, dict):
                    rendered_payload = candidate
            except (OSError, json.JSONDecodeError, StopIteration):
                pass
        return upgrade_page_payload(
            rendered_payload,
            relative_path=page["relative_path"],
            source_language=execution.chapter["source_language"],
            artifact_version=execution.version_hash,
            review_status=_page_review_status(rendered_payload),
        )

    def _write_page_artifact(
        self,
        execution: StageExecution,
        stage: ChapterStage,
        relative_path: str,
        payload: dict[str, Any],
    ) -> Path:
        return execution.artifacts.write_json(
            execution.chapter["name"],
            stage,
            execution.version_hash,
            relative_path,
            payload,
        )

    def _write_chapter_artifact(
        self,
        execution: StageExecution,
        stage: ChapterStage,
        filename: str,
        payload: dict[str, Any],
    ) -> Path:
        return execution.artifacts.write_json(
            execution.chapter["name"],
            stage,
            execution.version_hash,
            filename,
            payload,
        )

    def _read_page_artifact(
        self,
        execution: StageExecution,
        stage: ChapterStage,
        page: dict[str, Any],
        *,
        version_hashes: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        hashes = version_hashes or execution.version_hashes
        version_hash = hashes.get(stage.value, execution.version_hash)
        return execution.artifacts.read_json(
            execution.chapter["name"],
            stage,
            version_hash,
            page["relative_path"],
        )

    def _read_chapter_artifact(
        self,
        execution: StageExecution,
        stage: ChapterStage,
        filename: str,
    ) -> dict[str, Any] | None:
        return execution.artifacts.read_json(
            execution.chapter["name"],
            stage,
            execution.version_hashes.get(stage.value, execution.version_hash),
            filename,
        )

    def _index_regions(
        self,
        execution: StageExecution,
        page_id: str,
        payload: dict[str, Any],
        stage: ChapterStage,
        artifact_version: str,
    ) -> None:
        records = [
            RegionRecord.from_page_region(
                page_id,
                index,
                region,
                source_language=execution.chapter["source_language"],
                artifact_version=artifact_version,
            )
            for index, region in enumerate(payload.get("regions") or [])
            if isinstance(region, dict)
        ]
        self.storage.upsert_regions(page_id, records)

    def _persist_observations(
        self,
        execution: StageExecution,
        observations: list[dict[str, Any]],
        page: dict[str, Any],
    ) -> None:
        if not isinstance(observations, list):
            return
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            visual_id = str(observation.get("character_id") or "").strip()
            if not visual_id or visual_id.lower() == "unknown":
                continue
            character_id = _character_id(execution.series_id, visual_id)
            name = str(observation.get("name") or "").strip()
            confidence = _confidence(observation.get("confidence"))
            review_status = (
                ReviewStatus.AUTO_ACCEPTED
                if name and name.lower() != "unknown" and (confidence or 0) >= 0.9
                else ReviewStatus.NEEDS_REVIEW
            )
            self.storage.upsert_character(
                series_id=execution.series_id,
                character_id=character_id,
                canonical_name=name or visual_id,
                description=str(observation.get("appearance") or ""),
                confidence=confidence,
                review_status=review_status,
                source_ref={
                    "chapter": execution.chapter["name"],
                    "page": page["relative_path"],
                },
            )
            for alias in observation.get("aliases") or []:
                alias_text = str(alias or "").strip()
                if alias_text:
                    self.storage.upsert_alias(
                        character_id=character_id,
                        alias=alias_text,
                        language=execution.chapter["source_language"],
                        confidence=confidence,
                        review_status=review_status,
                        source_ref={
                            "chapter": execution.chapter["name"],
                            "page": page["relative_path"],
                        },
                    )

    def _persist_knowledge(
        self,
        execution: StageExecution,
        knowledge: dict[str, Any],
    ) -> None:
        for item in knowledge.get("characters") or []:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("character_id") or "").strip()
            if not raw_id:
                continue
            character_id = _character_id(execution.series_id, raw_id)
            confidence = _confidence(item.get("confidence"))
            name = str(item.get("name") or "").strip()
            review_status = (
                ReviewStatus.AUTO_ACCEPTED
                if name and name.lower() != "unknown" and (confidence or 0) >= 0.95
                else ReviewStatus.NEEDS_REVIEW
            )
            self.storage.upsert_character(
                series_id=execution.series_id,
                character_id=character_id,
                canonical_name=name or raw_id,
                description=str(item.get("description") or ""),
                confidence=confidence,
                review_status=review_status,
                source_ref={"references": item.get("source_refs") or []},
            )
            for alias in item.get("aliases") or []:
                alias_text = str(alias or "").strip()
                if alias_text:
                    self.storage.upsert_alias(
                        character_id=character_id,
                        alias=alias_text,
                        language=execution.chapter["source_language"],
                        confidence=confidence,
                        review_status=review_status,
                        source_ref={"references": item.get("source_refs") or []},
                    )
        for item in knowledge.get("terms") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = _to_simplified(str(item.get("target") or "").strip())
            if not source or not target:
                continue
            confidence = _confidence(item.get("confidence"))
            review_status = (
                ReviewStatus.AUTO_ACCEPTED
                if (confidence or 0) >= 0.95
                else ReviewStatus.NEEDS_REVIEW
            )
            self.storage.upsert_term(
                series_id=execution.series_id,
                source=source,
                target=target,
                category=str(item.get("category") or "term"),
                confidence=confidence,
                review_status=review_status,
                source_ref={"references": item.get("source_refs") or []},
            )
        for item in knowledge.get("voice_profiles") or []:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("character_id") or "").strip()
            if not raw_id:
                continue
            character_id = _character_id(execution.series_id, raw_id)
            confidence = _confidence(item.get("confidence"))
            self.storage.upsert_voice_profile(
                character_id=character_id,
                language=str(
                    item.get("language")
                    or execution.chapter["source_language"]
                ),
                profile=item.get("profile") or item,
                confidence=confidence,
                review_status=(
                    ReviewStatus.AUTO_ACCEPTED
                    if (confidence or 0) >= 0.95
                    else ReviewStatus.NEEDS_REVIEW
                ),
                source_ref={"references": item.get("source_refs") or []},
            )

    async def _prepare_web_stage(self) -> None:
        try:
            await self.model_manager.unload()
        except ModelManagerError:
            logger.warning("Qwen model manager was unavailable during web stage")
        await unload_web_models()

    async def _unload_ocr(self, ocr_model: Any) -> None:
        try:
            from manga_translator.ocr import unload as unload_ocr

            await unload_ocr(ocr_model)
        except Exception as exc:
            logger.debug("OCR unload skipped: %s", exc)

    async def _unload_inpainter(self) -> None:
        try:
            from manga_translator.config import Inpainter
            from manga_translator.inpainting import unload as unload_inpainter

            await unload_inpainter(Inpainter(self.settings.inpainter_model))
        except Exception as exc:
            logger.debug("Inpainter unload skipped: %s", exc)

    def _stitch_boundary(
        self,
        execution: StageExecution,
        page_index: int,
        *,
        overlap: int,
    ):
        """Stitched image for one page, or ``None`` when there is no neighbour."""

        pages = execution.pages
        if not pages or page_index >= len(pages):
            return None
        previous = pages[page_index - 1] if page_index > 0 else None
        following = (
            pages[page_index + 1] if page_index + 1 < len(pages) else None
        )
        if previous is None and following is None:
            return None
        return stitch_boundary_image(
            self._source_path(execution, pages[page_index]),
            self._source_path(execution, previous) if previous else None,
            self._source_path(execution, following) if following else None,
            overlap=overlap,
        )

    def _restore_page_coordinates(
        self,
        payload: dict[str, Any],
        execution: StageExecution,
        page: dict[str, Any],
        *,
        offset: int | None,
    ) -> dict[str, Any]:
        """Undo the boundary stitch so coordinates match the page image.

        ``None`` means the page was not stitched at all. An offset of ``0``
        still requires a remap when the chapter's first page got the next
        page's head appended below it: the payload is then taller than the
        page and its mask must be cropped back to the page box.
        """

        if offset is None:
            return payload
        from PIL import Image

        with Image.open(self._source_path(execution, page)) as image:
            width, height = image.size
        return remap_stitched_payload(
            payload,
            offset=offset,
            width=width,
            height=height,
        )

    def _source_path(
        self,
        execution: StageExecution,
        page: dict[str, Any],
    ) -> Path:
        source_root = Path(execution.source_root).resolve()
        source_path = (source_root / page["relative_path"]).resolve()
        if not is_relative_to(source_path, source_root) or not source_path.is_file():
            raise FileNotFoundError(
                f"source page not found inside input root: {page['relative_path']}"
            )
        return source_path

    def _work_path(
        self,
        execution: StageExecution,
        stage: ChapterStage,
        page: dict[str, Any],
    ) -> Path:
        work = (
            execution.artifacts.stage_dir(
                execution.chapter["name"],
                stage,
                execution.version_hash,
            )
            / "_work"
            / page["filename"]
        )
        work.parent.mkdir(parents=True, exist_ok=True)
        return work

    def _active_result_path(
        self,
        execution: StageExecution,
        relative_path: str,
    ) -> Path:
        return execution.artifacts.raw_root / relative_path

    def _new_config(self):
        from manga_translator.config import Config

        try:
            from manga_translator.server.core.config_manager import load_default_config

            return load_default_config().model_copy(deep=True)
        except Exception:
            return Config()

    def _ocr_model(
        self,
        chapter: dict[str, Any],
        options: dict[str, Any] | None = None,
    ):
        from manga_translator.config import Ocr

        return Ocr(
            self.settings.ocr_model_for(chapter, options=options)
        )

    @staticmethod
    def _apply_ocr_options(
        config: Any,
        rule: LanguageRule | None,
        options: dict[str, Any],
    ) -> None:
        stage_options = (options.get("stage_config") or {}).get("ocr") or {}
        scalar_keys = (
            "ignore_bubble",
            "min_text_length",
            "use_hybrid_ocr",
            "use_model_bubble_filter",
            "model_bubble_overlap_threshold",
            "use_model_bubble_repair_intersection",
            "limit_mask_dilation_to_bubble_mask",
            "merge_gamma",
            "merge_sigma",
            "merge_edge_ratio_threshold",
            "merge_special_require_full_wrap",
        )
        for key in scalar_keys:
            if key in stage_options:
                setattr(config.ocr, key, stage_options[key])
        detector_options = {
            "detection_size",
            "text_threshold",
            "box_threshold",
            "unclip_ratio",
            "min_box_area_ratio",
            "use_yolo_obb",
            "use_sfx_filter",
        }
        for key in detector_options:
            if key in stage_options:
                setattr(config.detector, key, stage_options[key])
        if stage_options.get("ocr_vl_language_hint"):
            config.ocr.ocr_vl_language_hint = str(
                stage_options["ocr_vl_language_hint"]
            )
        elif rule is not None and rule.ocr_hint:
            config.ocr.ocr_vl_language_hint = rule.ocr_hint

    def _local_client(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            OpenAICompatibleConfig(
                base_url=self.settings.local_openai_base,
                api_key=self.settings.local_openai_api_key,
                model=self.settings.local_openai_model,
            )
        )

    def _embedding_client(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            OpenAICompatibleConfig(
                base_url=self.settings.local_openai_base,
                api_key=self.settings.local_openai_api_key,
                model=self.settings.embedding_model_id,
            )
        )

    def _translation_client(
        self,
        options: dict[str, Any],
    ) -> tuple[str, OpenAICompatibleClient]:
        provider = str(
            options.get(
                "translation_provider",
                self.settings.translation_provider,
            )
        ).casefold()
        if provider == "online":
            if not (
                self.settings.online_openai_base
                and self.settings.online_openai_model
            ):
                raise ValueError(
                    "online translation provider is not configured"
                )
            return "online", OpenAICompatibleClient(
                OpenAICompatibleConfig(
                    base_url=self.settings.online_openai_base,
                    api_key=self.settings.online_openai_api_key,
                    model=self.settings.online_openai_model,
                )
            )
        if provider != "local":
            raise ValueError(f"unsupported translation provider: {provider}")
        return "local", self._local_client()

    @staticmethod
    def _resource_metrics(
        started: float,
        item_count: int,
    ) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "duration_ms": int((time.monotonic() - started) * 1000),
            "items": item_count,
        }
        try:
            import psutil

            metrics["peak_memory_mb"] = round(
                psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024,
                2,
            )
        except Exception:
            pass
        try:
            import torch

            if torch.cuda.is_available():
                metrics["peak_gpu_memory_mb"] = round(
                    torch.cuda.max_memory_allocated() / 1024 / 1024,
                    2,
                )
        except Exception:
            pass
        return metrics


def _apply_render_options(config: Any, options: dict[str, Any]) -> None:
    if options.get("font_family"):
        config.render.font_family = str(options["font_family"])
    if options.get("layout_mode"):
        config.render.layout_mode = str(options["layout_mode"])
    if options.get("direction"):
        config.render.direction = str(options["direction"])
    if options.get("alignment"):
        config.render.alignment = str(options["alignment"])


def _boundary_overlap(options: dict[str, Any] | None) -> int:
    """Pixels of the neighbouring pages to stitch onto each page (0 = off)."""

    try:
        value = int((options or {}).get("ocr_boundary_overlap", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def stitch_boundary_image(
    page_path: Path,
    previous_path: Path | None,
    next_path: Path | None,
    *,
    overlap: int,
):
    """Stack the previous page's tail and the next page's head onto a page.

    Webtoon strips are often sliced at arbitrary heights, so a text line can be
    cut in half by a page break. Neither half is recognisable on its own; only
    the joined image contains the whole line. Returns ``(image, offset)`` where
    ``offset`` is how many pixels were prepended above the page.
    """

    from PIL import Image

    def load(path: Path | None):
        if path is None:
            return None
        with Image.open(path) as handle:
            return handle.convert("RGB")

    own = load(page_path)
    if own is None or overlap <= 0:
        return None
    width = own.width
    previous = load(previous_path)
    following = load(next_path)
    head = tail = None
    if previous is not None:
        height = min(overlap, previous.height)
        if height > 0:
            head = previous.crop(
                (0, previous.height - height, min(width, previous.width), previous.height)
            )
    if following is not None:
        height = min(overlap, following.height)
        if height > 0:
            tail = following.crop(
                (0, 0, min(width, following.width), height)
            )
    if head is None and tail is None:
        own.close()
        if previous is not None:
            previous.close()
        if following is not None:
            following.close()
        return None
    head_height = head.height if head is not None else 0
    tail_height = tail.height if tail is not None else 0
    stitched = Image.new(
        "RGB",
        (width, head_height + own.height + tail_height),
        (255, 255, 255),
    )
    if head is not None:
        stitched.paste(head, (0, 0))
    stitched.paste(own, (0, head_height))
    if tail is not None:
        stitched.paste(tail, (0, head_height + own.height))
    own.close()
    for image in (previous, following, head, tail):
        if image is not None:
            image.close()
    return stitched, head_height


def _crop_mask_base64(
    value: str,
    *,
    offset: int,
    width: int,
    height: int,
) -> str:
    """Crop a stitched-image mask back down to the page's own rows."""

    import cv2
    import numpy as np

    try:
        decoded = cv2.imdecode(
            np.frombuffer(base64.b64decode(value), dtype=np.uint8),
            cv2.IMREAD_GRAYSCALE,
        )
    except Exception:
        return value
    if decoded is None:
        return value
    if decoded.shape[0] < offset + height:
        padded = np.zeros(
            (offset + height, decoded.shape[1]),
            dtype=decoded.dtype,
        )
        padded[: decoded.shape[0], : decoded.shape[1]] = decoded
        decoded = padded
    cropped = decoded[offset : offset + height, :width]
    ok, buffer = cv2.imencode(".png", cropped)
    if not ok:
        return value
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def remap_stitched_payload(
    payload: dict[str, Any],
    *,
    offset: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Move a stitched-image OCR payload back into page coordinates.

    Regions are clamped to the page box rather than dropped, so a line cut by
    the page break stays registered on both neighbouring pages and each side
    erases its own half.
    """

    if offset < 0:
        return payload
    result = dict(payload)
    regions: list[dict[str, Any]] = []
    for region in payload.get("regions") or []:
        if not isinstance(region, dict):
            continue
        item = dict(region)
        lines: list[list[list[float]]] = []
        for line in item.get("lines") or []:
            clamped: list[list[float]] = []
            for point in line:
                try:
                    x = float(point[0])
                    y = float(point[1]) - offset
                except (TypeError, ValueError, IndexError):
                    continue
                clamped.append(
                    [
                        min(max(x, 0.0), float(width)),
                        min(max(y, 0.0), float(height)),
                    ]
                )
            if len(clamped) >= 3:
                lines.append(clamped)
        if not lines:
            continue
        xs = [point[0] for line in lines for point in line]
        ys = [point[1] for line in lines for point in line]
        # Drop slivers that only overlap the page by a pixel or two; they come
        # from the neighbour and would erase a stray line.
        if (max(xs) - min(xs)) < 3 or (max(ys) - min(ys)) < 3:
            continue
        item["lines"] = lines
        item["center"] = [
            (min(xs) + max(xs)) / 2.0,
            (min(ys) + max(ys)) / 2.0,
        ]
        regions.append(item)
    result["regions"] = regions
    mask = payload.get("mask_raw")
    if isinstance(mask, str) and mask:
        result["mask_raw"] = _crop_mask_base64(
            mask,
            offset=offset,
            width=width,
            height=height,
        )
    result["original_width"] = width
    result["original_height"] = height
    return result


def _decode_mask(value: Any):
    import cv2
    import numpy as np

    if not isinstance(value, str) or not value:
        return None
    try:
        payload = base64.b64decode(value)
        array = np.frombuffer(payload, dtype=np.uint8)
        return cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
    except Exception:
        return None


def _mask_from_regions(payload: dict[str, Any], size: tuple[int, int], cv2):
    import numpy as np

    mask = np.zeros(size, dtype=np.uint8)
    polygons = []
    for region in payload.get("regions") or []:
        for line in region.get("lines") or []:
            try:
                polygon = np.asarray(line, dtype=np.int32).reshape((-1, 1, 2))
            except (TypeError, ValueError):
                continue
            if len(polygon) >= 3:
                polygons.append(polygon)
    if polygons:
        cv2.fillPoly(mask, polygons, 255)
    return mask


def _has_low_confidence_regions(
    payload: dict[str, Any],
    threshold: float,
) -> bool:
    for region in payload.get("regions") or []:
        confidence = _confidence(
            region.get("ocr_confidence", region.get("prob"))
        )
        if confidence is not None and confidence < threshold:
            return True
    return False


def _page_review_status(payload: dict[str, Any]) -> str:
    statuses = [
        str(region.get("review_status") or ReviewStatus.UNREVIEWED.value)
        for region in payload.get("regions") or []
    ]
    if any(
        status
        in {
            ReviewStatus.NEEDS_REVIEW.value,
            ReviewStatus.UNREVIEWED.value,
        }
        for status in statuses
    ):
        return ReviewStatus.NEEDS_REVIEW.value
    if statuses and all(
        status == ReviewStatus.CONFIRMED.value for status in statuses
    ):
        return ReviewStatus.CONFIRMED.value
    return ReviewStatus.AUTO_ACCEPTED.value


def _confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result > 1 and result <= 100:
        result /= 100
    return min(max(result, 0.0), 1.0)


def _speech_type(value: Any) -> str:
    try:
        return SpeechType(str(value or "unknown").casefold()).value
    except ValueError:
        return SpeechType.UNKNOWN.value


def _character_id(series_id: str, visual_id: str) -> str:
    if visual_id.startswith(f"{series_id}:"):
        return visual_id
    slug = "-".join(
        part
        for part in "".join(
            char.casefold() if char.isalnum() else "-"
            for char in visual_id
        ).split("-")
        if part
    )
    return f"{series_id}:character:{slug or hash_json(visual_id)[:12]}"


def _to_simplified(text: str) -> str:
    try:
        from opencc import OpenCC

        if not hasattr(_to_simplified, "_converter"):
            _to_simplified._converter = OpenCC("t2s")
        return _to_simplified._converter.convert(text)
    except Exception:
        return text
