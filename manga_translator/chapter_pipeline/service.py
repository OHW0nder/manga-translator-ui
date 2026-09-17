from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import PipelineSettings
from .model_control import ModelManagerClient
from .paths import safe_join
from .pipeline import ChapterPipelineService
from .runtime import DefaultChapterRuntime
from .storage import PipelineStorage


_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_service: ChapterPipelineService | None = None
_settings: PipelineSettings | None = None
_storage: PipelineStorage | None = None


def get_pipeline_settings() -> PipelineSettings:
    global _settings
    if _settings is None:
        _settings = PipelineSettings()
    return _settings


def get_pipeline_storage() -> PipelineStorage:
    global _storage
    if _storage is None:
        _storage = PipelineStorage(get_pipeline_settings().database_path)
        _storage.initialize()
    return _storage


def get_pipeline_service() -> ChapterPipelineService:
    global _service
    if _service is None:
        settings = get_pipeline_settings()
        storage = get_pipeline_storage()
        runtime = DefaultChapterRuntime(settings, storage)
        _service = ChapterPipelineService(
            settings,
            storage,
            runtime,
            model_manager=runtime.model_manager,
        )
        _service.initialize()
    return _service


def create_upload_id() -> str:
    return uuid4().hex


def upload_root(upload_id: str) -> Path:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise ValueError("invalid upload id")
    root = get_pipeline_settings().upload_dir / upload_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def upload_relative_path(upload_id: str, relative_path: str) -> Path:
    return safe_join(upload_root(upload_id), relative_path)


def remove_upload(upload_id: str) -> None:
    root = upload_root(upload_id).resolve()
    uploads = get_pipeline_settings().upload_dir.resolve()
    try:
        root.relative_to(uploads)
    except ValueError as exc:
        raise ValueError("upload path escapes the configured upload directory") from exc
    if root.exists():
        shutil.rmtree(root)


async def shutdown_pipeline_service() -> None:
    global _service
    if _service is not None:
        await _service.close()
    _service = None


def model_manager_client() -> ModelManagerClient:
    settings = get_pipeline_settings()
    return ModelManagerClient(
        settings.model_manager_url,
        settings.model_manager_token,
    )


def public_status() -> dict[str, Any]:
    settings = get_pipeline_settings()
    service = get_pipeline_service()
    return {
        **settings.as_public_dict(),
        "stats": service.storage.stats(),
    }
