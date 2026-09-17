"""Per-series configuration for the chapter pipeline.

Everything that used to be hardcoded for a single manga lives here instead:
where a series keeps its RAW pages and results, how a chapter number maps to a
source language (and therefore to an OCR model), the target language, and the
default stage parameters.

The module deliberately depends on nothing heavier than PyYAML so that
``PipelineSettings`` can import it without pulling in torch/OpenCV.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping


class SeriesConfigError(ValueError):
    """Raised when a series configuration is missing or malformed."""


SERIES_CONFIG_SUFFIXES = (".yaml", ".yml", ".json")

# Reserved per-rule language keys; every other key in a language rule is
# treated as a job-options override for the chapters that rule matches.
LANGUAGE_RULE_KEYS = frozenset(
    {"chapters", "source", "ocr_model", "ocr_hint", "translator_code"}
)

_SPAN_RE = re.compile(r"^(\d+(?:\.\d+)?)?\s*-\s*(\d+(?:\.\d+)?)?$")
_FIRST_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def merge_options(
    base: Mapping[str, Any] | None,
    override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Deep merge two job-options mappings; ``override`` wins on conflicts."""

    result = copy.deepcopy(dict(base or {}))
    for key, value in (override or {}).items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = merge_options(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``$VAR`` references from the environment.

    Undefined variables are left untouched so the resulting path surfaces the
    mistake instead of silently becoming empty. Only POSIX-style references are
    understood, which keeps behaviour identical on Windows and in the container.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return _ENV_RE.sub(replace, value)


def chapter_number(chapter: Mapping[str, Any] | None) -> float | None:
    """Best-effort chapter number from a chapter record.

    Storage rows expose the numeric part of the chapter as ``sort_key``; other
    callers may carry ``number`` or only a name.
    """

    if not chapter:
        return None
    for key in ("sort_key", "number", "chapter_number"):
        value = chapter.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    name = chapter.get("name")
    if isinstance(name, str):
        match = _FIRST_NUMBER_RE.search(name)
        if match:
            return float(match.group(1))
    return None

# Fallback bridge from a chapter's source language to the code the translators
# expect. Series rules can override it with an explicit ``translator_code``.
# Kept local so this module stays import-light; mirrors the subset of
# ``manga_translator.translators.common.ISO_639_1_TO_VALID_LANGUAGES``.
_TRANSLATOR_CODES = {
    "zh": "CHS",
    "zh-hans": "CHS",
    "zh-hant": "CHT",
    "ja": "JPN",
    "en": "ENG",
    "ko": "KOR",
    "es": "ESP",
    "pt": "PTB",
    "fr": "FRA",
    "de": "DEU",
    "it": "ITA",
    "ru": "RUS",
    "uk": "UKR",
    "pl": "POL",
    "nl": "NLD",
    "cs": "CSY",
    "hu": "HUN",
    "ro": "ROM",
    "tr": "TRK",
    "vi": "VIN",
    "th": "THA",
    "id": "IND",
    "ar": "ARA",
    "tl": "FIL",
    "sr": "SRP",
    "hr": "HRV",
    "cnr": "CNR",
}


def slugify(value: str) -> str:
    """Return a filesystem/identifier safe slug for a series name."""

    return "-".join(
        part
        for part in "".join(
            char.casefold() if char.isalnum() else "-" for char in value
        ).split("-")
        if part
    )


@dataclass(slots=True, frozen=True)
class ChapterSpan:
    """An inclusive chapter number interval; ``None`` means unbounded."""

    lower: float | None = None
    upper: float | None = None

    def matches(self, number: float) -> bool:
        if self.lower is not None and number < self.lower:
            return False
        if self.upper is not None and number > self.upper:
            return False
        return True


@dataclass(slots=True, frozen=True)
class ChapterSelector:
    """A comma separated set of chapter numbers and intervals.

    Accepted syntax: ``*`` (everything), ``"1-90"``, ``"91-"``, ``"-14"``,
    ``"114.5"`` or any comma separated combination such as ``"1-14,24.5"``.
    """

    spans: tuple[ChapterSpan, ...]
    raw: str = "*"

    @classmethod
    def parse(cls, spec: Any) -> "ChapterSelector":
        if spec is None:
            return cls((ChapterSpan(),), "*")
        if isinstance(spec, bool):
            raise SeriesConfigError(f"invalid chapter selector: {spec!r}")
        if isinstance(spec, (int, float)):
            value = float(spec)
            return cls((ChapterSpan(value, value),), str(spec))
        text = str(spec).strip()
        if not text or text == "*":
            return cls((ChapterSpan(),), text or "*")
        spans: list[ChapterSpan] = []
        for token in text.split(","):
            token = token.strip()
            if not token:
                continue
            if token == "*":
                spans = [ChapterSpan()]
                break
            span_match = _SPAN_RE.match(token)
            if span_match:
                lower_raw, upper_raw = span_match.group(1), span_match.group(2)
                if lower_raw is None and upper_raw is None:
                    raise SeriesConfigError(
                        f"chapter selector {token!r} does not bound any chapter"
                    )
                spans.append(
                    ChapterSpan(
                        float(lower_raw) if lower_raw else None,
                        float(upper_raw) if upper_raw else None,
                    )
                )
                continue
            try:
                value = float(token)
            except ValueError as exc:
                raise SeriesConfigError(
                    f"invalid chapter selector: {token!r}"
                ) from exc
            spans.append(ChapterSpan(value, value))
        if not spans:
            raise SeriesConfigError(f"empty chapter selector: {spec!r}")
        return cls(tuple(spans), text)

    def matches(self, number: float) -> bool:
        return any(span.matches(number) for span in self.spans)


@dataclass(slots=True, frozen=True)
class LanguageRule:
    """What a matching chapter is written in and how to read it.

    Keys other than the reserved language keys are collected into ``options``
    and deep-merged over the series ``defaults`` for every chapter this rule
    matches, so a series can tune OCR/inpaint parameters per language block
    (for example Korean chapters 1-90 versus Spanish chapters 91+).
    """

    chapters: ChapterSelector
    source: str
    ocr_model: str | None = None
    ocr_hint: str | None = None
    translator_code: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def resolved_translator_code(self) -> str | None:
        if self.translator_code:
            return self.translator_code
        return _TRANSLATOR_CODES.get(self.source.casefold())


@dataclass(slots=True, frozen=True)
class LanguageRuleSet:
    """Ordered language rules; the first matching rule wins."""

    rules: tuple[LanguageRule, ...]

    def resolve(self, number: float, *, chapter_name: str = "") -> LanguageRule:
        for rule in self.rules:
            if rule.chapters.matches(number):
                return rule
        label = chapter_name or f"number {number}"
        raise SeriesConfigError(
            f"no language rule matches chapter {label}; add a catch-all "
            "'chapters: \"*\"' rule to the series configuration"
        )

    @property
    def sources(self) -> tuple[str, ...]:
        seen: list[str] = []
        for rule in self.rules:
            if rule.source not in seen:
                seen.append(rule.source)
        return tuple(seen)

    def as_public_list(self) -> list[dict[str, Any]]:
        return [
            {
                "chapters": rule.chapters.raw,
                "source": rule.source,
                "ocr_model": rule.ocr_model,
                "ocr_hint": rule.ocr_hint,
                "translator_code": rule.resolved_translator_code(),
            }
            for rule in self.rules
        ]


@dataclass(slots=True)
class SeriesPaths:
    """Container/host paths for one series."""

    raw: Path
    results: Path
    database: Path | None = None
    uploads: Path | None = None

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "raw": str(self.raw),
            "results": str(self.results),
            "database": str(self.database) if self.database else None,
            "uploads": str(self.uploads) if self.uploads else None,
        }


@dataclass(slots=True)
class SeriesConfig:
    """Resolved configuration for a single manga series."""

    name: str
    slug: str
    paths: SeriesPaths
    languages: LanguageRuleSet
    target_language: str = "zh-Hans"
    target_code: str = "CHS"
    expected_chapter_count: int = 0
    expected_page_count: int = 0
    chapter_pattern: str | None = None
    defaults: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    def language_for(self, number: float, *, chapter_name: str = "") -> LanguageRule:
        return self.languages.resolve(number, chapter_name=chapter_name)

    def compiled_chapter_pattern(self) -> re.Pattern[str] | None:
        if not self.chapter_pattern:
            return None
        try:
            pattern = re.compile(self.chapter_pattern)
        except re.error as exc:
            raise SeriesConfigError(
                f"invalid chapter_pattern {self.chapter_pattern!r}: {exc}"
            ) from exc
        if pattern.groups < 1:
            raise SeriesConfigError(
                "chapter_pattern must capture the chapter number in group 1"
            )
        return pattern

    @property
    def upload_prefixes(self) -> tuple[str, ...]:
        """Leading path components to drop from browser upload paths."""

        values = {self.slug, self.name.casefold(), "raw"}
        raw_name = self.paths.raw.name
        if raw_name:
            values.add(raw_name.casefold())
        return tuple(sorted(value for value in values if value))

    def stage_defaults(self) -> dict[str, Any]:
        """Job ``options`` defaults, deep copied so callers can mutate them."""

        return copy.deepcopy(self.defaults)

    def options_for(
        self,
        number: float | None,
        *,
        chapter_name: str = "",
    ) -> dict[str, Any]:
        """Job-options baseline for one chapter.

        ``defaults`` deep-merged with the matching language rule's overrides.
        The rule's ``ocr_model`` is mirrored into ``models.ocr`` so the OCR
        model keeps a single source of truth per rule while still losing to an
        explicit job-level ``models.ocr`` override.
        """

        base = copy.deepcopy(self.defaults)
        if number is None:
            return base
        for rule in self.languages.rules:
            if not rule.chapters.matches(number):
                continue
            merged = merge_options(base, rule.options)
            if rule.ocr_model:
                models = merged.get("models")
                models = dict(models) if isinstance(models, Mapping) else {}
                models["ocr"] = rule.ocr_model
                merged["models"] = models
            return merged
        return base

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "series_name": self.name,
            "series_slug": self.slug,
            "series_config": str(self.source_path) if self.source_path else None,
            "paths": self.paths.as_public_dict(),
            "languages": self.languages.as_public_list(),
            "target_language": self.target_language,
            "target_code": self.target_code,
            "expected_chapter_count": self.expected_chapter_count,
            "expected_page_count": self.expected_page_count,
            "chapter_pattern": self.chapter_pattern,
        }


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeriesConfigError(f"cannot read series config {path}: {exc}") from exc
    suffix = path.suffix.casefold()
    try:
        if suffix == ".json":
            payload = json.loads(text)
        else:
            import yaml

            payload = yaml.safe_load(text)
    except Exception as exc:
        raise SeriesConfigError(f"cannot parse series config {path}: {exc}") from exc
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise SeriesConfigError(f"series config {path} must be a mapping")
    return payload


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SeriesConfigError(f"{label} must be a mapping")
    return dict(value)


def _resolve_path(value: Any, *, label: str, base: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise SeriesConfigError(f"{label} must be a non-empty path")
    candidate = Path(expand_env(str(value))).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate


def _optional_path(value: Any, *, label: str, base: Path) -> Path | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _resolve_path(value, label=label, base=base)


def _optional_int(value: Any, *, label: str, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SeriesConfigError(f"{label} must be an integer") from exc


def _parse_language_rules(
    raw_rules: Any,
    *,
    default_ocr_model: str | None,
    label: str,
) -> LanguageRuleSet:
    if not isinstance(raw_rules, list) or not raw_rules:
        raise SeriesConfigError(f"{label} must be a non-empty list of rules")
    rules: list[LanguageRule] = []
    for index, entry in enumerate(raw_rules):
        if not isinstance(entry, Mapping):
            raise SeriesConfigError(f"{label}[{index}] must be a mapping")
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise SeriesConfigError(f"{label}[{index}].source must be a string")
        chapters = entry.get("chapters", "*")
        ocr_model = entry.get("ocr_model", default_ocr_model)
        translator_code = entry.get("translator_code")
        overrides = {
            key: value
            for key, value in entry.items()
            if key not in LANGUAGE_RULE_KEYS
        }
        rules.append(
            LanguageRule(
                chapters=ChapterSelector.parse(chapters),
                source=source.strip(),
                ocr_model=str(ocr_model).strip() if ocr_model else None,
                ocr_hint=(
                    str(entry["ocr_hint"]).strip()
                    if entry.get("ocr_hint")
                    else None
                ),
                translator_code=(
                    str(translator_code).strip() if translator_code else None
                ),
                options=overrides,
            )
        )
    return LanguageRuleSet(tuple(rules))


def load_series_config(path: str | Path) -> SeriesConfig:
    """Load and validate one series configuration file."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise SeriesConfigError(f"series config not found: {config_path}")
    payload = _read_mapping(config_path)
    base = config_path.parent

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SeriesConfigError(f"series config {config_path} requires a name")
    name = name.strip()
    slug = payload.get("slug") or slugify(name)
    if not isinstance(slug, str) or not slug.strip():
        raise SeriesConfigError(f"series config {config_path} has an invalid slug")

    paths_payload = _require_mapping(payload.get("paths"), label="paths")
    # ``languages`` accepts either a bare rule list or a mapping with a
    # ``rules`` key plus a shared ``default_ocr_model``.
    languages_raw = payload.get("languages")
    if isinstance(languages_raw, Mapping):
        rules_payload = languages_raw.get("rules")
        default_ocr_model = languages_raw.get("default_ocr_model")
    else:
        rules_payload = languages_raw
        default_ocr_model = payload.get("default_ocr_model")

    inventory = _require_mapping(payload.get("inventory"), label="inventory")
    translation = _require_mapping(payload.get("translation"), label="translation")
    defaults = _require_mapping(payload.get("defaults"), label="defaults")

    results = _resolve_path(
        paths_payload.get("results", "results"),
        label="paths.results",
        base=base,
    )
    raw = _resolve_path(
        paths_payload.get("raw", "RAW"),
        label="paths.raw",
        base=base,
    )
    database = _optional_path(
        paths_payload.get("database"),
        label="paths.database",
        base=base,
    )
    uploads = _optional_path(
        paths_payload.get("uploads"),
        label="paths.uploads",
        base=base,
    )

    target_language = str(translation.get("target") or "zh-Hans")
    target_code = str(translation.get("target_code") or "CHS")

    return SeriesConfig(
        name=name,
        slug=slug.strip(),
        paths=SeriesPaths(
            raw=raw,
            results=results,
            database=database,
            uploads=uploads,
        ),
        languages=_parse_language_rules(
            rules_payload,
            default_ocr_model=(
                str(default_ocr_model).strip() if default_ocr_model else None
            ),
            label="languages",
        ),
        target_language=target_language,
        target_code=target_code,
        expected_chapter_count=_optional_int(
            inventory.get("expected_chapters"),
            label="inventory.expected_chapters",
        ),
        expected_page_count=_optional_int(
            inventory.get("expected_pages"),
            label="inventory.expected_pages",
        ),
        chapter_pattern=(
            str(inventory["chapter_pattern"]).strip()
            if inventory.get("chapter_pattern")
            else None
        ),
        defaults=defaults,
        source_path=config_path,
    )


def iter_series_files(directory: str | Path) -> Iterator[Path]:
    """Yield candidate series config files, sorted by name."""

    root = Path(directory).expanduser()
    if not root.is_dir():
        return
    for path in sorted(root.iterdir()):
        if path.is_file() and path.suffix.casefold() in SERIES_CONFIG_SUFFIXES:
            yield path


def discover_series(directory: str | Path) -> dict[str, Path]:
    """Map slug -> config path for every series file in ``directory``."""

    discovered: dict[str, Path] = {}
    for path in iter_series_files(directory):
        try:
            payload = _read_mapping(path)
        except SeriesConfigError:
            continue
        name = payload.get("name")
        slug = payload.get("slug") or (slugify(name) if isinstance(name, str) else "")
        if not isinstance(slug, str) or not slug.strip():
            slug = slugify(path.stem)
        if slug:
            discovered[slug.strip().casefold()] = path
    return discovered
