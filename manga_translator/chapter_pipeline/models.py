from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class PageSchemaVersion(int, Enum):
    LEGACY = 1
    CHAPTER_PIPELINE = 2


class SourceLanguage(str, Enum):
    """Well known source languages.

    Which language a chapter actually uses is decided by the series
    configuration, not by this enum. Values outside this set are carried
    through as plain strings; use :func:`source_language_value` to read them.
    """

    KOREAN = "ko"
    JAPANESE = "ja"
    ENGLISH = "en"
    SPANISH = "es"
    THAI = "th"
    SIMPLIFIED_CHINESE = "zh-Hans"
    TRADITIONAL_CHINESE = "zh-Hant"
    UNKNOWN = "unknown"


def coerce_source_language(value: Any) -> SourceLanguage | str:
    """Return an enum member when the code is known, else the raw string."""

    if isinstance(value, SourceLanguage):
        return value
    if value is None:
        return SourceLanguage.UNKNOWN
    text = str(value).strip()
    if not text:
        return SourceLanguage.UNKNOWN
    try:
        return SourceLanguage(text)
    except ValueError:
        return text


def source_language_value(value: Any) -> str:
    """Normalize an enum member or plain string to its language code."""

    if isinstance(value, SourceLanguage):
        return value.value
    if value is None:
        return SourceLanguage.UNKNOWN.value
    text = str(value).strip()
    return text or SourceLanguage.UNKNOWN.value


class ChapterStage(str, Enum):
    OCR = "ocr"
    VISION = "vision"
    KNOWLEDGE = "knowledge"
    TRANSLATE = "translate"
    INPAINT = "inpaint"
    RENDER = "render"
    SUMMARIZE = "summarize"
    EMBED = "embed"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class ReviewStatus(str, Enum):
    UNREVIEWED = "unreviewed"
    AUTO_ACCEPTED = "auto_accepted"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SpeechType(str, Enum):
    DIALOGUE = "dialogue"
    THOUGHT = "thought"
    NARRATION = "narration"
    SFX = "sfx"
    SIGN = "sign"
    UNKNOWN = "unknown"


@dataclass(slots=True, frozen=True)
class PageInventory:
    relative_path: str
    chapter_name: str
    chapter_number: float
    filename: str
    source_language: SourceLanguage | str
    sha256: str
    size_bytes: int
    source_mtime_ns: int = 0
    width: int | None = None
    height: int | None = None

    @property
    def page_id(self) -> str:
        return canonical_relative_path(self.relative_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "relative_path": self.relative_path,
            "chapter_name": self.chapter_name,
            "chapter_number": self.chapter_number,
            "filename": self.filename,
            "source_language": source_language_value(self.source_language),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
        }


@dataclass(slots=True)
class ChapterInventory:
    name: str
    number: float
    source_language: SourceLanguage | str
    pages: list[PageInventory] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "number": self.number,
            "source_language": source_language_value(self.source_language),
            "page_count": len(self.pages),
            "pages": [page.to_dict() for page in self.pages],
        }


@dataclass(slots=True)
class InventoryReport:
    series_name: str
    root: str
    chapters: list[ChapterInventory]
    series_slug: str = ""
    ignored_directories: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    expected_chapter_count: int = 0
    expected_page_count: int = 0

    @property
    def page_count(self) -> int:
        return sum(len(chapter.pages) for chapter in self.chapters)

    @property
    def valid(self) -> bool:
        return (
            not self.errors
            and (
                self.expected_chapter_count <= 0
                or len(self.chapters) == self.expected_chapter_count
            )
            and (
                self.expected_page_count <= 0
                or self.page_count == self.expected_page_count
            )
        )

    def to_dict(self, include_pages: bool = True) -> dict[str, Any]:
        chapters = []
        for chapter in self.chapters:
            data = chapter.to_dict()
            if not include_pages:
                data.pop("pages", None)
            chapters.append(data)
        return {
            "series_name": self.series_name,
            "series_slug": self.series_slug,
            "root": self.root,
            "chapter_count": len(self.chapters),
            "page_count": self.page_count,
            "expected_chapter_count": self.expected_chapter_count,
            "expected_page_count": self.expected_page_count,
            "valid": self.valid,
            "ignored_directories": self.ignored_directories,
            "errors": self.errors,
            "chapters": chapters,
        }


@dataclass(slots=True)
class RegionRecord:
    page_id: str
    region_index: int
    bbox: list[float] | None = None
    source_text: str = ""
    translated_text: str = ""
    source_language: str = SourceLanguage.UNKNOWN.value
    ocr_confidence: float | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    speaker_confidence: float | None = None
    speech_type: str = SpeechType.UNKNOWN.value
    review_status: str = ReviewStatus.UNREVIEWED.value
    artifact_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "region_index": self.region_index,
            "bbox": self.bbox,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "source_language": self.source_language,
            "ocr_confidence": self.ocr_confidence,
            "speaker_id": self.speaker_id,
            "speaker_name": self.speaker_name,
            "speaker_confidence": self.speaker_confidence,
            "speech_type": self.speech_type,
            "review_status": self.review_status,
            "artifact_version": self.artifact_version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_page_region(
        cls,
        page_id: str,
        region_index: int,
        region: dict[str, Any],
        *,
        source_language: str,
        artifact_version: str,
    ) -> "RegionRecord":
        bbox = None
        lines = region.get("lines")
        if isinstance(lines, list) and lines:
            try:
                points = [
                    point
                    for line in lines
                    for point in line
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                if points:
                    xs = [float(point[0]) for point in points]
                    ys = [float(point[1]) for point in points]
                    bbox = [min(xs), min(ys), max(xs), max(ys)]
            except (TypeError, ValueError):
                bbox = None
        return cls(
            page_id=page_id,
            region_index=region_index,
            bbox=bbox,
            source_text=str(region.get("text") or ""),
            translated_text=str(region.get("translation") or ""),
            source_language=str(region.get("source_language") or source_language),
            ocr_confidence=_optional_float(
                region.get("ocr_confidence", region.get("prob"))
            ),
            speaker_id=region.get("speaker_id"),
            speaker_name=region.get("speaker_name"),
            speaker_confidence=_optional_float(region.get("speaker_confidence")),
            speech_type=str(region.get("speech_type") or SpeechType.UNKNOWN.value),
            review_status=str(
                region.get("review_status") or ReviewStatus.UNREVIEWED.value
            ),
            artifact_version=artifact_version,
            metadata={
                key: value
                for key, value in region.items()
                if key
                not in {
                    "lines",
                    "text",
                    "translation",
                    "source_language",
                    "ocr_confidence",
                    "prob",
                    "speaker_id",
                    "speaker_name",
                    "speaker_confidence",
                    "speech_type",
                    "review_status",
                }
            },
        )


_CHAPTER_RE = re.compile(r"^Chapter\s+(\d+(?:\.\d+)?)$", re.IGNORECASE)


def parse_chapter_name(
    name: str,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, float] | None:
    """Parse a chapter directory name into ``(normalized_name, number)``.

    ``pattern`` must fullmatch the directory name and capture the chapter
    number in group 1. It defaults to the project convention
    ``Chapter <number>``; a series can override it via ``inventory.chapter_pattern``.
    """

    regex = pattern or _CHAPTER_RE
    match = regex.fullmatch(name.strip())
    if not match:
        return None
    try:
        number = float(match.group(1))
    except (TypeError, ValueError):
        return None
    normalized = f"Chapter {int(number) if number.is_integer() else number:g}"
    return normalized, number


def chapter_sort_key(name: str) -> tuple[float, str]:
    parsed = parse_chapter_name(name)
    if parsed:
        return parsed[1], name.casefold()
    return float("inf"), name.casefold()


def canonical_relative_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def json_dumps_canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
