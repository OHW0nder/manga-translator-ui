from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .models import (
    ChapterInventory,
    InventoryReport,
    PageInventory,
    canonical_relative_path,
    chapter_sort_key,
    parse_chapter_name,
)
from .paths import normalize_upload_relative_path, safe_join
from .series_config import LanguageRuleSet, SeriesConfigError


SUPPORTED_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".avif",
        ".heic",
        ".heif",
    }
)

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _natural_key(value: str) -> list[object]:
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in _NUMBER_RE.split(value)
    ]


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def scan_inventory(
    root: str | Path,
    *,
    series_name: str,
    languages: LanguageRuleSet,
    series_slug: str = "",
    expected_chapter_count: int = 0,
    expected_page_count: int = 0,
    include_hashes: bool = True,
    chapter_pattern: re.Pattern[str] | None = None,
) -> InventoryReport:
    """Scan chapter directories while ignoring every nested work directory.

    Chapter languages come from ``languages``, the active series' rule set;
    a chapter whose number matches no rule is reported as an error instead of
    being assigned a guessed language.
    """

    root_path = Path(root).resolve()
    report = InventoryReport(
        series_name=series_name,
        series_slug=series_slug,
        root=str(root_path),
        chapters=[],
        expected_chapter_count=expected_chapter_count,
        expected_page_count=expected_page_count,
    )
    if not root_path.is_dir():
        report.errors.append(f"input root does not exist: {root_path}")
        return report

    chapter_dirs: list[tuple[str, float, Path]] = []
    for child in root_path.iterdir():
        if not child.is_dir():
            continue
        parsed = parse_chapter_name(child.name, chapter_pattern)
        if parsed is None:
            report.ignored_directories.append(child.name)
            continue
        normalized_name, number = parsed
        chapter_dirs.append((normalized_name, number, child))

    chapter_dirs.sort(key=lambda item: (item[1], item[0].casefold()))
    seen_names: set[str] = set()
    for chapter_name, number, chapter_path in chapter_dirs:
        if chapter_name.casefold() in seen_names:
            report.errors.append(f"duplicate chapter directory: {chapter_name}")
            continue
        seen_names.add(chapter_name.casefold())
        try:
            language = languages.resolve(number, chapter_name=chapter_name).source
        except SeriesConfigError as exc:
            report.errors.append(str(exc))
            continue
        chapter = ChapterInventory(
            name=chapter_name,
            number=number,
            source_language=language,
        )
        top_level_files = sorted(
            (
                path
                for path in chapter_path.iterdir()
                if path.is_file()
                and path.suffix.casefold() in SUPPORTED_IMAGE_EXTENSIONS
            ),
            key=lambda path: _natural_key(path.name),
        )
        for image_path in top_level_files:
            relative_path = canonical_relative_path(
                image_path.relative_to(root_path)
            )
            try:
                digest = _sha256(image_path) if include_hashes else ""
                size = image_path.stat().st_size
            except OSError as exc:
                report.errors.append(f"failed to inspect {relative_path}: {exc}")
                continue
            chapter.pages.append(
                PageInventory(
                    relative_path=relative_path,
                    chapter_name=chapter_name,
                    chapter_number=number,
                    filename=image_path.name,
                    source_language=language,
                    sha256=digest,
                    size_bytes=size,
                )
            )
        report.chapters.append(chapter)

    if (
        expected_chapter_count > 0
        and len(report.chapters) != expected_chapter_count
    ):
        report.errors.append(
            "chapter count mismatch: "
            f"expected {expected_chapter_count}, found {len(report.chapters)}"
        )
    if expected_page_count > 0 and report.page_count != expected_page_count:
        report.errors.append(
            "page count mismatch: "
            f"expected {expected_page_count}, found {report.page_count}"
        )
    return report


@dataclass(slots=True)
class UploadValidation:
    accepted: list[tuple[str, Path]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.accepted) and not self.rejected


def validate_upload_relative_paths(
    relative_paths: Iterable[str],
    *,
    strip_prefixes: Iterable[str] | None = None,
) -> UploadValidation:
    result = UploadValidation()
    for raw_path in relative_paths:
        try:
            normalized = normalize_upload_relative_path(
                raw_path,
                strip_prefixes=strip_prefixes,
            )
        except ValueError as exc:
            result.rejected.append(
                {"relative_path": str(raw_path), "error": str(exc)}
            )
            continue
        result.accepted.append((normalized, safe_join(".", normalized)))
    return result


def extract_upload_zip(
    zip_path: str | Path,
    destination: str | Path,
    *,
    max_files: int = 10000,
    max_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024,
    strip_prefixes: Iterable[str] | None = None,
) -> UploadValidation:
    """Extract only files with safe relative paths."""

    destination_path = Path(destination).resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    result = UploadValidation()
    total_size = 0
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > max_files:
            raise ValueError(f"archive contains too many entries: {len(infos)}")
        for info in infos:
            if info.is_dir():
                continue
            try:
                normalized = normalize_upload_relative_path(
                    info.filename,
                    strip_prefixes=strip_prefixes,
                )
                target = safe_join(destination_path, normalized)
            except ValueError as exc:
                result.rejected.append(
                    {"relative_path": info.filename, "error": str(exc)}
                )
                continue
            total_size += info.file_size
            if total_size > max_uncompressed_bytes:
                raise ValueError("archive uncompressed size exceeds the limit")
            if info.external_attr >> 16 & 0o170000 == 0o120000:
                result.rejected.append(
                    {
                        "relative_path": info.filename,
                        "error": "symbolic links are not allowed",
                    }
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            result.accepted.append((normalized, target))
    return result
