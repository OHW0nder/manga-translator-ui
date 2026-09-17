from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .models import (
    ChapterStage,
    PageSchemaVersion,
    ReviewStatus,
    SourceLanguage,
    canonical_relative_path,
)
from .paths import safe_join


def hash_json(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hash_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def make_version_hash(
    stage: ChapterStage | str,
    *,
    model_id: str,
    config: dict[str, Any] | None = None,
    input_hash: str = "",
) -> str:
    stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
    return hash_json(
        {
            "stage": stage_value,
            "model_id": model_id,
            "config": config or {},
            "input_hash": input_hash,
        }
    )[:20]


class ArtifactStore:
    """Versioned JSON and image artifacts rooted below ``results/RAW``."""

    def __init__(self, results_root: str | Path):
        self.results_root = Path(results_root).resolve()
        self.raw_root = self.results_root / "RAW"

    def chapter_dir(self, chapter_name: str) -> Path:
        target = safe_join(self.raw_root, f"{chapter_name}/.keep")
        return target.parent

    def stage_dir(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
    ) -> Path:
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        return (
            self.chapter_dir(chapter_name)
            / ".pipeline"
            / stage_value
            / version_hash
        )

    def artifact_path(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
        relative_path: str,
        *,
        suffix: str | None = None,
    ) -> Path:
        relative = canonical_relative_path(relative_path)
        parts = Path(relative).parts
        if parts and parts[0].casefold() == chapter_name.casefold():
            parts = parts[1:]
        target_name = Path(*parts) if parts else Path("artifact")
        if suffix:
            target_name = target_name.with_suffix(suffix)
        stage_dir = self.stage_dir(chapter_name, stage, version_hash)
        return safe_join(stage_dir, target_name.as_posix())

    def write_json(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
        relative_path: str,
        payload: dict[str, Any],
    ) -> Path:
        target = self.artifact_path(
            chapter_name,
            stage,
            version_hash,
            relative_path,
            suffix=".json",
        )
        self._atomic_write_json(target, payload)
        return target

    def read_json(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
        relative_path: str,
    ) -> dict[str, Any] | None:
        target = self.artifact_path(
            chapter_name,
            stage,
            version_hash,
            relative_path,
            suffix=".json",
        )
        if not target.is_file():
            return None
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None

    def write_manifest(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
        payload: dict[str, Any],
    ) -> Path:
        stage_dir = self.stage_dir(chapter_name, stage, version_hash)
        target = stage_dir / "manifest.json"
        self._atomic_write_json(target, payload)
        return target

    def read_manifest(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
    ) -> dict[str, Any] | None:
        target = self.stage_dir(chapter_name, stage, version_hash) / "manifest.json"
        if not target.is_file():
            return None
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def activate(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
    ) -> Path:
        active_path = self.chapter_dir(chapter_name) / ".pipeline" / "active.json"
        state: dict[str, Any] = {}
        if active_path.is_file():
            try:
                state = json.loads(active_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        state[stage_value] = version_hash
        self._atomic_write_json(active_path, state)
        return active_path

    def active_version(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
    ) -> str | None:
        active_path = self.chapter_dir(chapter_name) / ".pipeline" / "active.json"
        if not active_path.is_file():
            return None
        try:
            state = json.loads(active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        value = state.get(stage_value)
        return str(value) if value else None

    def publish_image(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
        relative_path: str,
        source_image: str | Path,
        *,
        publish_active: bool = True,
    ) -> Path:
        relative = canonical_relative_path(relative_path)
        versioned = self.artifact_path(
            chapter_name,
            stage,
            version_hash,
            relative,
        )
        versioned.parent.mkdir(parents=True, exist_ok=True)
        source = Path(source_image)
        if source.resolve() != versioned.resolve():
            self._atomic_copy(source, versioned)
        if publish_active:
            active = safe_join(self.raw_root, relative)
            active.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_copy(versioned, active)
        return versioned

    def iter_versioned_images(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
    ) -> Iterable[Path]:
        stage_dir = self.stage_dir(chapter_name, stage, version_hash)
        if not stage_dir.is_dir():
            return []
        return sorted(path for path in stage_dir.rglob("*") if path.is_file())

    def hash_stage(
        self,
        chapter_name: str,
        stage: ChapterStage | str,
        version_hash: str,
    ) -> str:
        stage_dir = self.stage_dir(chapter_name, stage, version_hash)
        digest = hashlib.sha256()
        if not stage_dir.is_dir():
            return digest.hexdigest()
        for path in sorted(item for item in stage_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(stage_dir).as_posix()
            if relative == "manifest.json" or relative.startswith("_work/"):
                continue
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hash_file(path).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def build_download_zip(
        self,
        destination: str | Path,
        chapters: Iterable[str],
        *,
        image_extensions: Iterable[str] | None = None,
    ) -> Path:
        import zipfile

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        allowed = {
            extension.casefold()
            for extension in (
                image_extensions
                or {
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
        }
        with zipfile.ZipFile(
            destination_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for chapter_name in chapters:
                chapter_dir = self.chapter_dir(chapter_name)
                if not chapter_dir.is_dir():
                    continue
                for image_path in sorted(chapter_dir.iterdir()):
                    if (
                        not image_path.is_file()
                        or image_path.suffix.casefold() not in allowed
                    ):
                        continue
                    archive.write(
                        image_path,
                        arcname=f"{chapter_name}/{image_path.name}",
                    )
        return destination_path

    @staticmethod
    def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_copy(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle, source.open("rb") as source_handle:
                while chunk := source_handle.read(1024 * 1024):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


def upgrade_page_payload(
    payload: dict[str, Any],
    *,
    relative_path: str,
    source_language: SourceLanguage | str,
    artifact_version: str,
    review_status: ReviewStatus | str = ReviewStatus.UNREVIEWED,
) -> dict[str, Any]:
    """Attach optional provenance fields without changing legacy region data."""

    source_value = (
        source_language.value
        if isinstance(source_language, SourceLanguage)
        else str(source_language)
    )
    review_value = (
        review_status.value
        if isinstance(review_status, ReviewStatus)
        else str(review_status)
    )
    result = dict(payload)
    result.update(
        {
            "schema_version": PageSchemaVersion.CHAPTER_PIPELINE.value,
            "relative_path": canonical_relative_path(relative_path),
            "source_language": source_value,
            "artifact_version": artifact_version,
            "review_status": review_value,
            "target_language": SourceLanguage.SIMPLIFIED_CHINESE.value,
        }
    )
    regions = result.get("regions")
    if isinstance(regions, list):
        upgraded_regions = []
        for region in regions:
            if not isinstance(region, dict):
                upgraded_regions.append(region)
                continue
            item = dict(region)
            item.setdefault("schema_version", PageSchemaVersion.CHAPTER_PIPELINE.value)
            item.setdefault("relative_path", result["relative_path"])
            item.setdefault("source_language", source_value)
            item.setdefault("artifact_version", artifact_version)
            item.setdefault("review_status", review_value)
            item.setdefault("speaker_id", None)
            item.setdefault("speaker_confidence", None)
            item.setdefault("speech_type", "unknown")
            upgraded_regions.append(item)
        result["regions"] = upgraded_regions
    return result
