from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable


class UnsafeRelativePath(ValueError):
    pass


_DRIVE_RE = re.compile(r"^[A-Za-z]:")

# Leading directory names dropped from browser upload paths when the caller
# does not provide a series specific set. Series pass their own prefixes
# (series name/slug plus the RAW directory name) so folder uploads keep
# working regardless of how a series lays out its source tree.
DEFAULT_UPLOAD_PREFIXES = ("raw",)


def normalize_upload_relative_path(
    raw_path: str,
    *,
    strip_pipeline_roots: bool = True,
    strip_prefixes: Iterable[str] | None = None,
) -> str:
    """Validate and normalize a browser relative path.

    ``webkitRelativePath`` uses ``/`` and must never escape the upload root.
    Absolute paths, drive-qualified paths, NUL bytes, and parent traversal are
    rejected instead of being silently rewritten.
    """

    if not raw_path or "\x00" in raw_path:
        raise UnsafeRelativePath("empty or invalid relative path")

    value = raw_path.replace("\\", "/").strip()
    if not value:
        raise UnsafeRelativePath("empty relative path")
    if value.startswith("/") or value.startswith("//") or _DRIVE_RE.match(value):
        raise UnsafeRelativePath(f"absolute paths are not allowed: {raw_path}")

    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise UnsafeRelativePath(f"path traversal is not allowed: {raw_path}")

    if strip_pipeline_roots:
        prefixes = {
            str(prefix).strip().casefold()
            for prefix in (
                DEFAULT_UPLOAD_PREFIXES
                if strip_prefixes is None
                else strip_prefixes
            )
            if str(prefix).strip()
        }
        while parts and parts[0].casefold() in prefixes:
            parts.pop(0)
        if not parts:
            raise UnsafeRelativePath("relative path does not contain a file")

    normalized = PurePosixPath(*parts).as_posix()
    if normalized in {"", "."}:
        raise UnsafeRelativePath("empty relative path")
    return normalized


def safe_join(root: str | Path, relative_path: str) -> Path:
    normalized = normalize_upload_relative_path(
        relative_path,
        strip_pipeline_roots=False,
    )
    root_path = Path(root).resolve()
    target = (root_path / Path(*PurePosixPath(normalized).parts)).resolve()
    try:
        target.relative_to(root_path)
    except ValueError as exc:
        raise UnsafeRelativePath(
            f"path escapes configured root: {relative_path}"
        ) from exc
    return target


def is_relative_to(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def portable_relative_path(path: str | Path, root: str | Path) -> str:
    relative = os.path.relpath(Path(path), Path(root))
    return PurePosixPath(*Path(relative).parts).as_posix()
