from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from manga_translator.chapter_pipeline.models import ChapterStage
from manga_translator.chapter_pipeline.paths import normalize_upload_relative_path
from manga_translator.chapter_pipeline.service import (
    create_upload_id,
    get_pipeline_service,
    get_pipeline_settings,
    model_manager_client,
    public_status,
    upload_relative_path,
    upload_root,
)
from manga_translator.runtime_paths import get_application_dir


router = APIRouter(prefix="/chapters", tags=["chapter-pipeline"])
static_dir = Path(get_application_dir()) / "manga_translator" / "server" / "static"


class ScanRequest(BaseModel):
    upload_id: str | None = None
    include_hashes: bool = True


class CreateJobRequest(BaseModel):
    chapter_names: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    upload_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class RetryStageRequest(BaseModel):
    stage: str
    chapter_names: list[str] = Field(default_factory=list)


class RetranslatePageRequest(BaseModel):
    page_id: str


class ConfirmCharacterRequest(BaseModel):
    canonical_name: str | None = None
    confirmed_by: str = "local-user"


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def chapter_ui():
    page = static_dir / "chapters.html"
    if not page.is_file():
        raise HTTPException(404, detail="chapter UI is not installed")
    return page.read_text(encoding="utf-8")


@router.get("/status")
async def pipeline_status():
    return await asyncio.to_thread(public_status)


@router.get("/models")
async def model_status():
    try:
        return await model_manager_client().status()
    except Exception as exc:
        return {
            "available": False,
            "loaded_model": None,
            "error": str(exc),
        }


@router.get("/inventory")
async def get_inventory(
    upload_id: str | None = None,
    include_hashes: bool = Query(True),
):
    service = get_pipeline_service()
    root = upload_root(upload_id) if upload_id else None
    try:
        report = await asyncio.to_thread(
            service.scan,
            root,
            include_hashes=include_hashes,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return report.to_dict(include_pages=True)


@router.post("/inventory")
async def scan_inventory(request: ScanRequest):
    service = get_pipeline_service()
    root = upload_root(request.upload_id) if request.upload_id else None
    report = await asyncio.to_thread(
        service.scan,
        root,
        include_hashes=request.include_hashes,
    )
    return report.to_dict(include_pages=True)


@router.post("/uploads")
async def upload_chapter_files(
    files: list[UploadFile] = File(...),
    relative_paths: str = Form("[]"),
    upload_id: str | None = Form(None),
):
    if not files:
        raise HTTPException(400, detail="no files were uploaded")
    try:
        paths = json.loads(relative_paths)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, detail="relative_paths must be JSON") from exc
    if not isinstance(paths, list) or len(paths) != len(files):
        raise HTTPException(
            400,
            detail="relative_paths must contain one entry per uploaded file",
        )

    upload_id = upload_id or create_upload_id()
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    destinations: list[tuple[UploadFile, Path, str]] = []
    strip_prefixes = get_pipeline_settings().upload_prefixes()
    for uploaded, raw_relative in zip(files, paths):
        raw_value = str(raw_relative or uploaded.filename or "")
        try:
            relative = normalize_upload_relative_path(
                raw_value,
                strip_prefixes=strip_prefixes,
            )
            destination = upload_relative_path(upload_id, relative)
        except ValueError as exc:
            rejected.append(
                {"relative_path": raw_value, "error": str(exc)}
            )
            continue
        destinations.append((uploaded, destination, relative))

    if rejected:
        raise HTTPException(
            400,
            detail={
                "message": "unsafe or invalid relative paths",
                "rejected": rejected,
            },
        )
    for uploaded, destination, relative in destinations:
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                while chunk := await uploaded.read(1024 * 1024):
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
            await uploaded.close()
        accepted.append(relative)

    service = get_pipeline_service()
    report = await asyncio.to_thread(
        service.scan,
        upload_root(upload_id),
        include_hashes=True,
    )
    return {
        "upload_id": upload_id,
        "accepted": accepted,
        "inventory": report.to_dict(include_pages=True),
    }


@router.post("/jobs")
async def create_job(request: CreateJobRequest):
    service = get_pipeline_service()
    root = upload_root(request.upload_id) if request.upload_id else None
    invalid = [
        stage
        for stage in request.stages
        if stage not in {item.value for item in ChapterStage}
    ]
    if invalid:
        raise HTTPException(
            400,
            detail=f"unknown stages: {', '.join(invalid)}",
        )
    try:
        job_id = await asyncio.to_thread(
            service.create_job,
            root=root,
            chapter_names=request.chapter_names or None,
            stages=request.stages or None,
            options=request.options,
            upload_id=request.upload_id,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    service.start(job_id)
    return service.storage.get_job(job_id)


@router.get("/jobs")
async def list_jobs(limit: int = Query(100, ge=1, le=500)):
    service = get_pipeline_service()
    return await asyncio.to_thread(service.storage.list_jobs, limit)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await asyncio.to_thread(get_pipeline_service().storage.get_job, job_id)
    if not job:
        raise HTTPException(404, detail="job not found")
    return job


@router.post("/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    service = get_pipeline_service()
    if not await asyncio.to_thread(service.pause, job_id):
        raise HTTPException(409, detail="job cannot be paused")
    return service.storage.get_job(job_id)


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    service = get_pipeline_service()
    try:
        service.resume(job_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return service.storage.get_job(job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    service = get_pipeline_service()
    if not await asyncio.to_thread(service.cancel, job_id):
        raise HTTPException(409, detail="job cannot be cancelled")
    return service.storage.get_job(job_id)


@router.post("/jobs/{job_id}/retry")
async def retry_job_stage(job_id: str, request: RetryStageRequest):
    service = get_pipeline_service()
    try:
        service.retry_stage(
            job_id,
            request.stage,
            chapter_names=request.chapter_names or None,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return service.storage.get_job(job_id)


@router.post("/jobs/{job_id}/retranslate-page")
async def retranslate_page(job_id: str, request: RetranslatePageRequest):
    service = get_pipeline_service()
    try:
        service.retranslate_page(job_id, request.page_id)
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    return service.storage.get_job(job_id)


@router.get("/jobs/{job_id}/pages")
async def list_job_pages(job_id: str):
    service = get_pipeline_service()
    job = await asyncio.to_thread(service.storage.get_job, job_id)
    if not job:
        raise HTTPException(404, detail="job not found")
    pages = await asyncio.to_thread(
        service.storage.list_pages,
        None,
        None,
    )
    selected = {
        name.casefold()
        for name in job["requested_chapters"]
    }
    return [
        page
        for page in pages
        if page["chapter_name"].casefold() in selected
    ]


@router.get("/jobs/{job_id}/download")
async def download_job(
    job_id: str,
    chapter: list[str] | None = Query(None),
):
    service = get_pipeline_service()
    try:
        archive = await asyncio.to_thread(
            service.download,
            job_id=job_id,
            chapters=chapter,
        )
    except KeyError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    if not archive.is_file() or archive.stat().st_size == 0:
        raise HTTPException(409, detail="no rendered results are available")
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"love-quest-{job_id[:8]}.zip",
    )


@router.get("/characters")
async def list_characters(review_status: str | None = None):
    service = get_pipeline_service()
    jobs = await asyncio.to_thread(service.storage.list_jobs, 1)
    if not jobs:
        return []
    return await asyncio.to_thread(
        service.storage.list_characters,
        jobs[0]["series_id"],
        review_status=review_status,
    )


@router.post("/characters/{character_id}/confirm")
async def confirm_character(
    character_id: str,
    request: ConfirmCharacterRequest,
):
    service = get_pipeline_service()
    success = await asyncio.to_thread(
        service.storage.confirm_character,
        character_id,
        canonical_name=request.canonical_name,
        confirmed_by=request.confirmed_by,
    )
    if not success:
        raise HTTPException(404, detail="character not found")
    return {"success": True, "character_id": character_id}
