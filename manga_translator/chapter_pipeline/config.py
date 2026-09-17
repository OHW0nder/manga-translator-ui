from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..runtime_paths import get_config_path
from .series_config import (
    LanguageRule,
    SeriesConfig,
    SeriesConfigError,
    chapter_number,
    discover_series,
    load_series_config,
    merge_options,
)


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value and value.strip() else None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _coerce_path(value: Any) -> Path | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return value
    text = str(value).strip()
    return Path(text).expanduser() if text else None


@dataclass(slots=True)
class PipelineSettings:
    """Runtime settings for the chapter pipeline.

    Series specific behaviour (paths, chapter -> language mapping, target
    language, default stage parameters) comes from a :class:`SeriesConfig`.
    Environment variables only pick which series is active and describe the
    deployment; the legacy ``MT_MANGA_*`` variables still work as explicit
    overrides of the series paths.
    """

    # --- active series ---------------------------------------------------
    series: SeriesConfig | None = None
    series_dir: Path | None = None

    # --- explicit overrides (beat both the series config and the env) -----
    data_root: Path | None = None
    raw_dir: Path | None = None
    results_dir: Path | None = None
    upload_dir: Path | None = None
    database_path: Path | None = None
    expected_chapter_count: int | None = None
    expected_page_count: int | None = None

    # --- model manager / endpoints ---------------------------------------
    model_manager_url: str = field(
        default_factory=lambda: os.environ.get(
            "MT_MODEL_MANAGER_URL", "http://manga-translator-model-manager:8090"
        ).rstrip("/")
    )
    external_qwen_url: str = field(
        default_factory=lambda: os.environ.get(
            "MT_EXTERNAL_QWEN_URL", ""
        ).rstrip("/")
    )
    model_manager_token: str = field(
        default_factory=lambda: os.environ.get(
            "MT_MODEL_MANAGER_TOKEN", "manga-translator-internal"
        )
    )
    local_openai_base: str = field(
        default_factory=lambda: os.environ.get(
            "OPENAI_API_BASE", "http://manga-translator-model-manager:8080/v1"
        ).rstrip("/")
    )
    local_openai_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENAI_API_KEY", "local")
    )
    local_openai_model: str = field(
        default_factory=lambda: os.environ.get(
            "OPENAI_MODEL", "local-qwen35-4b-vl"
        )
    )
    online_openai_base: str = field(
        default_factory=lambda: os.environ.get("MT_ONLINE_OPENAI_BASE", "").rstrip("/")
    )
    online_openai_api_key: str = field(
        default_factory=lambda: os.environ.get("MT_ONLINE_OPENAI_API_KEY", "")
    )
    online_openai_model: str = field(
        default_factory=lambda: os.environ.get("MT_ONLINE_OPENAI_MODEL", "")
    )
    qwen_model_id: str = field(
        default_factory=lambda: os.environ.get("MT_QWEN_MODEL_ID", "qwen35-4b-vl")
    )
    qwen_context_size: int = field(
        default_factory=lambda: int(
            os.environ.get("MT_QWEN_CONTEXT_SIZE", "24576")
        )
    )
    embedding_model_id: str = field(
        default_factory=lambda: os.environ.get(
            "MT_EMBEDDING_MODEL_ID", "local-embedding"
        )
    )
    inpainter_model: str = field(
        default_factory=lambda: os.environ.get("MT_INPAINTER_MODEL", "lama_large")
    )
    renderer_model: str = field(
        default_factory=lambda: os.environ.get("MT_RENDERER_MODEL", "default")
    )
    character_model: str = field(
        default_factory=lambda: os.environ.get(
            "MT_CHARACTER_MODEL", "local-qwen35-4b-vl"
        )
    )
    translation_model: str = field(
        default_factory=lambda: os.environ.get(
            "MT_TRANSLATION_MODEL", "local-qwen35-4b-vl"
        )
    )
    translation_provider: str = field(
        default_factory=lambda: os.environ.get("MT_TRANSLATION_PROVIDER", "local")
    )
    ocr_correction_threshold: float = field(
        default_factory=lambda: _env_float("MT_OCR_CORRECTION_THRESHOLD", 0.72)
    )
    speaker_confidence_threshold: float = field(
        default_factory=lambda: _env_float("MT_SPEAKER_CONFIDENCE_THRESHOLD", 0.78)
    )
    translation_confidence_threshold: float = field(
        default_factory=lambda: _env_float(
            "MT_TRANSLATION_CONFIDENCE_THRESHOLD", 0.8
        )
    )
    enable_embeddings: bool = field(
        default_factory=lambda: _env_bool("MT_ENABLE_EMBEDDINGS", False)
    )
    max_page_concurrency: int = field(
        default_factory=lambda: int(os.environ.get("MT_PIPELINE_PAGE_CONCURRENCY", "1"))
    )
    ocr_pages_per_batch: int = field(
        default_factory=lambda: int(os.environ.get("MT_OCR_PAGES_PER_BATCH", "8"))
    )
    max_stage_attempts: int = field(
        default_factory=lambda: int(os.environ.get("MT_PIPELINE_STAGE_ATTEMPTS", "2"))
    )

    def __post_init__(self) -> None:
        self.series_dir = _coerce_path(self.series_dir) or _coerce_path(
            os.environ.get("MT_SERIES_DIR")
        ) or Path(get_config_path("series"))
        if self.series is None:
            self.series = self._resolve_series()
        if not isinstance(self.series, SeriesConfig):
            raise SeriesConfigError(
                "series must be a SeriesConfig; pass a config path via "
                "MT_SERIES_CONFIG instead"
            )
        series = self.series

        self.data_root = (
            _coerce_path(self.data_root)
            or _coerce_path(os.environ.get("MT_MANGA_DATA_ROOT"))
            or series.paths.results.parent
        )
        self.raw_dir = (
            _coerce_path(self.raw_dir)
            or _coerce_path(os.environ.get("MT_MANGA_RAW_DIR"))
            or series.paths.raw
        )
        self.results_dir = (
            _coerce_path(self.results_dir)
            or _coerce_path(os.environ.get("MT_MANGA_RESULTS_DIR"))
            or series.paths.results
        )
        self.upload_dir = (
            _coerce_path(self.upload_dir)
            or _coerce_path(os.environ.get("MT_PIPELINE_UPLOAD_DIR"))
            or series.paths.uploads
            or self.data_root / "uploads"
        )
        self.database_path = (
            _coerce_path(self.database_path)
            or _coerce_path(os.environ.get("MT_PIPELINE_DB"))
            or series.paths.database
            or self.data_root / "pipeline.db"
        )

        if self.expected_chapter_count is None:
            self.expected_chapter_count = _env_int(
                "MT_EXPECTED_CHAPTERS",
                series.expected_chapter_count,
            )
        if self.expected_page_count is None:
            self.expected_page_count = _env_int(
                "MT_EXPECTED_PAGES",
                series.expected_page_count,
            )
        if "MT_OCR_PAGES_PER_BATCH" not in os.environ:
            configured = series.defaults.get("ocr_pages_per_batch")
            if isinstance(configured, int) and not isinstance(configured, bool):
                self.ocr_pages_per_batch = configured

    # ------------------------------------------------------------------
    # series resolution
    # ------------------------------------------------------------------
    def _resolve_series(self) -> SeriesConfig:
        explicit = _env("MT_SERIES_CONFIG")
        if explicit:
            return load_series_config(explicit)

        available = discover_series(self.series_dir)
        wanted = _env("MT_SERIES")
        if wanted:
            key = wanted.strip().casefold()
            path = available.get(key)
            if path is None:
                candidate = Path(wanted).expanduser()
                if candidate.is_file():
                    return load_series_config(candidate)
                known = ", ".join(sorted(available)) or "none"
                raise SeriesConfigError(
                    f"series {wanted!r} was not found in {self.series_dir} "
                    f"(available: {known}); set MT_SERIES or MT_SERIES_CONFIG"
                )
            return load_series_config(path)

        if len(available) == 1:
            return load_series_config(next(iter(available.values())))
        if not available:
            raise SeriesConfigError(
                f"no series configuration found in {self.series_dir}. "
                "Add a <slug>.yaml file there (see doc/SERIES_CONFIG.md) or "
                "point MT_SERIES_CONFIG at one."
            )
        known = ", ".join(sorted(available))
        raise SeriesConfigError(
            f"multiple series configurations found in {self.series_dir} "
            f"({known}); set MT_SERIES to choose one"
        )

    def _require_series(self) -> SeriesConfig:
        if self.series is None:  # pragma: no cover - guarded in __post_init__
            raise SeriesConfigError("no series configuration is loaded")
        return self.series

    # ------------------------------------------------------------------
    # series derived accessors
    # ------------------------------------------------------------------
    @property
    def series_name(self) -> str:
        return self._require_series().name

    @property
    def series_slug(self) -> str:
        return self._require_series().slug

    @property
    def target_language(self) -> str:
        return self._require_series().target_language

    @property
    def target_code(self) -> str:
        return self._require_series().target_code

    @property
    def languages(self):
        return self._require_series().languages

    def language_rule_for_chapter(
        self,
        chapter: Mapping[str, Any] | None,
    ) -> LanguageRule | None:
        """Resolve the rule for a chapter record (SQLite row or dict)."""

        if chapter is None:
            return None
        number = chapter_number(chapter)
        if number is not None:
            try:
                return self._require_series().language_for(
                    number,
                    chapter_name=str(chapter.get("name") or ""),
                )
            except SeriesConfigError:
                pass
        return self.language_rule_for_source(chapter.get("source_language"))

    def language_rule_for_source(
        self,
        source_language: Any,
    ) -> LanguageRule | None:
        """Resolve a rule by source language alone.

        Used where only the stored language is available (for example when an
        OCR batch is grouped by language). The first matching rule wins.
        """

        if not source_language:
            return None
        key = str(source_language).strip().casefold()
        if not key:
            return None
        for rule in self._require_series().languages.rules:
            if rule.source.casefold() == key:
                return rule
        return None

    def translator_code_for_chapter(
        self,
        chapter: Mapping[str, Any] | None,
    ) -> str:
        """Source language code expected by the translators (e.g. ``KOR``)."""

        rule = self.language_rule_for_chapter(chapter)
        if rule is None:
            label = chapter.get("name") if chapter else None
            raise SeriesConfigError(
                f"no language rule matches chapter {label!r}; add one to the "
                "series configuration"
            )
        code = rule.resolved_translator_code()
        if not code:
            raise SeriesConfigError(
                f"no translator code is known for source language "
                f"{rule.source!r}; set translator_code on that language rule"
            )
        return code

    def ocr_model_for(
        self,
        chapter: Mapping[str, Any] | None = None,
        *,
        source_language: Any = None,
        options: Mapping[str, Any] | None = None,
    ) -> str:
        """OCR model for a chapter, honouring an explicit job override.

        Both the stage version hash and the OCR runtime resolve the model
        through here so the recorded hash always matches what actually ran.
        """

        override = ((options or {}).get("models") or {}).get("ocr")
        if override:
            return str(override)
        rule = (
            self.language_rule_for_chapter(chapter)
            if chapter is not None
            else self.language_rule_for_source(source_language)
        )
        if rule is None or not rule.ocr_model:
            label = source_language
            if chapter is not None:
                label = chapter.get("source_language") or chapter.get("name")
            raise SeriesConfigError(
                f"no OCR model is configured for source language {label!r}; "
                "add ocr_model to the matching language rule"
            )
        return rule.ocr_model

    # ------------------------------------------------------------------
    def stage_defaults(self) -> dict[str, Any]:
        """Job ``options`` defaults declared by the series configuration."""

        return self._require_series().stage_defaults()

    def chapter_options(
        self,
        chapter: Mapping[str, Any] | None,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolved job options for one chapter.

        Precedence, lowest first: series ``defaults`` -> the matching language
        rule's overrides -> the job's ``options``. Both the stage version hash
        and the stage execution consume this single result, so a chapter can
        never be hashed with one parameter set and run with another.
        """

        number = chapter_number(chapter) if chapter is not None else None
        base = self._require_series().options_for(
            number,
            chapter_name=str((chapter or {}).get("name") or ""),
        )
        return merge_options(base, options)

    def compiled_chapter_pattern(self) -> re.Pattern[str] | None:
        """Optional series override for the chapter directory pattern."""

        return self._require_series().compiled_chapter_pattern()

    def upload_prefixes(self) -> tuple[str, ...]:
        return self._require_series().upload_prefixes

    def ensure_writable_directories(self) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def as_public_dict(self) -> dict[str, Any]:
        series = self._require_series()
        return {
            **series.as_public_dict(),
            "data_root": str(self.data_root),
            "raw_dir": str(self.raw_dir),
            "results_dir": str(self.results_dir),
            "upload_dir": str(self.upload_dir),
            "database_path": str(self.database_path),
            "series_dir": str(self.series_dir),
            "model_manager_url": self.model_manager_url,
            "external_qwen_url": self.external_qwen_url,
            "local_openai_base": self.local_openai_base,
            "local_openai_model": self.local_openai_model,
            "translation_provider": self.translation_provider,
            "inpainter_model": self.inpainter_model,
            "renderer_model": self.renderer_model,
            "enable_embeddings": self.enable_embeddings,
        }
