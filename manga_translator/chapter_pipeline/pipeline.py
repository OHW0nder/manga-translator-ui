from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .artifacts import ArtifactStore, hash_file, make_version_hash
from .config import PipelineSettings
from .inventory import scan_inventory
from .models import (
    ChapterStage,
    InventoryReport,
    JobStatus,
    StageStatus,
    chapter_sort_key,
)
from .stages import (
    DEFAULT_STAGES,
    StageExecution,
    StageResult,
    StageRuntime,
    StageRuntimeDispatcher,
    affected_stages,
    input_hash_for_stage,
    normalize_stages,
    stage_config,
    stage_model_id,
)
from .storage import PipelineStorage, utc_now


logger = logging.getLogger("manga_translator.chapter_pipeline")


class PipelineBusyError(RuntimeError):
    pass


class PipelineStopped(RuntimeError):
    pass


class ChapterPipelineService:
    """Coordinates resumable chapter jobs and persists every stage checkpoint."""

    def __init__(
        self,
        settings: PipelineSettings,
        storage: PipelineStorage,
        runtime: StageRuntime,
        *,
        model_manager: Any | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.runtime = runtime
        self.dispatcher = StageRuntimeDispatcher(runtime)
        self.model_manager = model_manager
        self.artifacts = ArtifactStore(settings.results_dir)
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._runtime_lock = asyncio.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.settings.ensure_writable_directories()
        self.storage.initialize()
        self.storage.recover_interrupted_jobs()
        self._initialized = True

    def scan(
        self,
        root: str | Path | None = None,
        *,
        include_hashes: bool = True,
    ) -> InventoryReport:
        self.initialize()
        scan_root = Path(root or self.settings.raw_dir).resolve()
        strict = scan_root == self.settings.raw_dir.resolve()
        report = scan_inventory(
            scan_root,
            series_name=self.settings.series_name,
            series_slug=self.settings.series_slug,
            languages=self.settings.languages,
            chapter_pattern=self.settings.compiled_chapter_pattern(),
            expected_chapter_count=(
                self.settings.expected_chapter_count if strict else 0
            ),
            expected_page_count=(
                self.settings.expected_page_count if strict else 0
            ),
            include_hashes=include_hashes,
        )
        if report.chapters:
            self.storage.upsert_inventory(report)
        return report

    def create_job(
        self,
        *,
        root: str | Path | None = None,
        chapter_names: Sequence[str] | None = None,
        stages: Sequence[ChapterStage | str] | None = None,
        options: dict[str, Any] | None = None,
        upload_id: str | None = None,
    ) -> str:
        self.initialize()
        report = self.scan(root, include_hashes=True)
        if not report.chapters:
            raise ValueError("no chapter directories were found")
        selected = list(chapter_names or [chapter.name for chapter in report.chapters])
        known = {chapter.name.casefold() for chapter in report.chapters}
        unknown = [name for name in selected if name.casefold() not in known]
        if unknown:
            raise ValueError("unknown chapters: " + ", ".join(unknown))
        options = dict(options or {})
        options.setdefault(
            "max_stage_attempts",
            self.settings.max_stage_attempts,
        )
        enabled = normalize_stages(
            stages,
            enable_embeddings=bool(
                options.get(
                    "enable_embeddings",
                    self.settings.enable_embeddings,
                )
            ),
        )
        series_id = f"series:{self.settings.series_slug}"
        job_id = self.storage.create_job(
            series_id=series_id,
            chapter_names=selected,
            stages=enabled,
            options=options,
            source_root=str(Path(root or self.settings.raw_dir).resolve()),
            upload_id=upload_id,
        )
        return job_id

    def start(self, job_id: str) -> None:
        self.initialize()
        job = self.storage.get_job(job_id)
        if not job:
            raise KeyError(f"job not found: {job_id}")
        task = self._tasks.get(job_id)
        if task and not task.done():
            return
        self._tasks[job_id] = asyncio.create_task(
            self.run_job(job_id),
            name=f"chapter-pipeline:{job_id}",
        )

    def pause(self, job_id: str) -> bool:
        changed = self.storage.request_job_state(job_id, JobStatus.PAUSED)
        return changed

    def cancel(self, job_id: str) -> bool:
        changed = self.storage.request_job_state(job_id, JobStatus.CANCELLED)
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        return changed

    def resume(self, job_id: str) -> None:
        job = self.storage.get_job(job_id)
        if not job:
            raise KeyError(f"job not found: {job_id}")
        if job["status"] == JobStatus.CANCELLED.value:
            raise ValueError("cancelled jobs cannot be resumed; retry the stage")
        if job["status"] in {
            JobStatus.PAUSED.value,
            JobStatus.INTERRUPTED.value,
            JobStatus.FAILED.value,
            JobStatus.QUEUED.value,
        }:
            self.storage.update_job(
                job_id,
                status=JobStatus.QUEUED,
                error=None,
            )
        self.start(job_id)

    def retry_stage(
        self,
        job_id: str,
        stage: ChapterStage | str,
        *,
        chapter_names: Sequence[str] | None = None,
    ) -> None:
        self.initialize()
        job = self.storage.get_job(job_id)
        if not job:
            raise KeyError(f"job not found: {job_id}")
        stage_value = ChapterStage(stage)
        selected = {
            name.casefold()
            for name in (chapter_names or job["requested_chapters"])
        }
        affected = {
            item.value
            for item in affected_stages(stage_value)
        }
        with self.storage.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.name, cs.stage
                FROM chapter_stages cs
                JOIN chapters c ON c.id = cs.chapter_id
                WHERE cs.job_id = ?
                """,
                (job_id,),
            ).fetchall()
            for row in rows:
                if row["name"].casefold() not in selected:
                    continue
                if row["stage"] not in affected:
                    continue
                connection.execute(
                    """
                    UPDATE chapter_stages
                    SET status = ?, error = NULL, finished_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND chapter_id = (
                        SELECT id FROM chapters
                        WHERE series_id = ? AND name = ?
                    ) AND stage = ?
                    """,
                    (
                        StageStatus.PENDING.value,
                        utc_now(),
                        job_id,
                        job["series_id"],
                        row["name"],
                        row["stage"],
                    ),
                )
        self.storage.update_job(
            job_id,
            status=JobStatus.QUEUED,
            error=None,
            finished=False,
        )
        self.start(job_id)

    def retranslate_page(self, job_id: str, page_id: str) -> None:
        self.initialize()
        pages = self.storage.list_pages(page_ids=[page_id])
        if not pages:
            raise KeyError(f"page not found: {page_id}")
        page = pages[0]
        job = self.storage.get_job(job_id)
        if not job:
            raise KeyError(f"job not found: {job_id}")
        self.storage.reset_stage(
            job_id,
            page["chapter_id"],
            ChapterStage.TRANSLATE,
            page_ids=[page_id],
        )
        for stage in (
            ChapterStage.RENDER,
        ):
            self.storage.reset_stage(
                job_id,
                page["chapter_id"],
                stage,
                page_ids=[page_id],
            )
        for stage in (
            ChapterStage.SUMMARIZE,
            ChapterStage.EMBED,
        ):
            self.storage.reset_stage(
                job_id,
                page["chapter_id"],
                stage,
            )
        self.storage.update_job(job_id, status=JobStatus.QUEUED, finished=False)
        self.start(job_id)

    async def run_job(self, job_id: str) -> None:
        # A single GPU hosts OCR, Qwen, LaMa, and the renderer. Serializing
        # complete jobs prevents one job from unloading another job's model.
        async with self._runtime_lock:
            await self._run_job_serialized(job_id)

    async def _run_job_serialized(self, job_id: str) -> None:
        self.initialize()
        lock = self._locks.setdefault(job_id, asyncio.Lock())
        if lock.locked():
            raise PipelineBusyError(f"job is already running: {job_id}")
        async with lock:
            job = self.storage.get_job(job_id)
            if not job:
                return
            if job["status"] == JobStatus.CANCELLED.value:
                return
            self.storage.update_job(
                job_id,
                status=JobStatus.RUNNING,
                started=True,
                error=None,
            )
            try:
                await self._run_job_locked(job)
            except asyncio.CancelledError:
                self.storage.update_job(
                    job_id,
                    status=JobStatus.CANCELLED,
                    finished=True,
                )
                raise
            except PipelineStopped:
                return
            except Exception as exc:
                logger.exception("Chapter pipeline job %s failed", job_id)
                self.storage.update_job(
                    job_id,
                    status=JobStatus.FAILED,
                    error=str(exc),
                    finished=True,
                )
            else:
                current = self.storage.get_job(job_id)
                if not current or current["status"] == JobStatus.RUNNING.value:
                    self.storage.update_job(
                        job_id,
                        status=JobStatus.COMPLETED,
                        finished=True,
                        progress={"completed": 1, "total": 1},
                    )
            finally:
                self._tasks.pop(job_id, None)

    async def _run_job_locked(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        options = job["options"]
        enabled = normalize_stages(
            options.get("stages"),
            enable_embeddings=bool(
                options.get("enable_embeddings", self.settings.enable_embeddings)
            ),
        )
        selected = {
            name.casefold()
            for name in job["requested_chapters"]
        }
        chapters = [
            chapter
            for chapter in self.storage.list_chapters(job["series_id"])
            if chapter["name"].casefold() in selected
        ]
        chapters.sort(
            key=lambda chapter: (
                chapter["sort_key"],
                chapter_sort_key(chapter["name"]),
            )
        )
        total_steps = max(1, len(chapters) * len(enabled))
        completed_steps = 0
        versions_by_chapter: dict[str, dict[str, str]] = {
            chapter["id"]: {} for chapter in chapters
        }
        for stage in enabled:
            pending: list[dict[str, Any]] = []
            for chapter in chapters:
                if await self._should_stop(job_id):
                    return
                pages = self.storage.list_pages(chapter_id=chapter["id"])
                version_hashes = versions_by_chapter[chapter["id"]]
                # One resolution per chapter feeds both the version hash below
                # and StageExecution.options, so per-language parameters can
                # never desync from what the hash was computed over.
                chapter_options = self.settings.chapter_options(chapter, options)
                config = stage_config(
                    stage,
                    settings=self.settings,
                    options=chapter_options,
                )
                input_hash = input_hash_for_stage(
                    stage,
                    pages=pages,
                    version_hashes=version_hashes,
                )
                model_id = stage_model_id(
                    stage,
                    settings=self.settings,
                    options=chapter_options,
                    chapter=chapter,
                )
                version_hash = make_version_hash(
                    stage,
                    model_id=model_id,
                    config=config,
                    input_hash=input_hash,
                )
                version_hashes[stage.value] = version_hash
                state = self.storage.get_stage_state(
                    job_id,
                    chapter["id"],
                    stage,
                )
                manifest = self.artifacts.read_manifest(
                    chapter["name"],
                    stage,
                    version_hash,
                )
                checkpoint = _load_checkpoint(state)
                force_stage_pages = bool(checkpoint.get("page_ids"))
                if (
                    manifest is not None
                    and manifest.get("version_hash") == version_hash
                    and not force_stage_pages
                ):
                    if (
                        not state
                        or state.get("status") != StageStatus.COMPLETED.value
                        or state.get("version_hash") != version_hash
                    ):
                        self.storage.set_stage_state(
                            job_id,
                            chapter["id"],
                            stage,
                            status=StageStatus.COMPLETED,
                            version_hash=version_hash,
                            artifact_dir=str(
                                self.artifacts.stage_dir(
                                    chapter["name"],
                                    stage,
                                    version_hash,
                                )
                            ),
                            checkpoint=manifest.get("checkpoint") or {},
                        )
                        self.artifacts.activate(
                            chapter["name"],
                            stage,
                            version_hash,
                        )
                    completed_steps += 1
                    await self._update_progress(
                        job_id,
                        chapter,
                        stage,
                        completed_steps,
                        total_steps,
                        skipped=True,
                    )
                    continue
                pending.append(
                    {
                        "chapter": chapter,
                        "pages": pages,
                        "config": config,
                        "model_id": model_id,
                        "options": chapter_options,
                        "version_hash": version_hash,
                        "version_hashes": dict(version_hashes),
                    }
                )

            if not pending:
                continue
            can_batch_ocr = (
                stage == ChapterStage.OCR
                and hasattr(self.runtime, "run_ocr_batch")
                and len(pending) > 1
            )
            if can_batch_ocr:
                results = await self._execute_ocr_batch(job, pending)
                for item in pending:
                    result = results[item["chapter"]["id"]]
                    completed_steps += 1
                    await self._update_progress(
                        job_id,
                        item["chapter"],
                        stage,
                        completed_steps,
                        total_steps,
                        skipped=bool(result.checkpoint.get("reused")),
                    )
                continue
            for item in pending:
                await self._execute_stage(
                    job=job,
                    chapter=item["chapter"],
                    pages=item["pages"],
                    stage=stage,
                    model_id=item["model_id"],
                    config=item["config"],
                    options=item["options"],
                    version_hash=item["version_hash"],
                    version_hashes=item["version_hashes"],
                )
                completed_steps += 1
                await self._update_progress(
                    job_id,
                    item["chapter"],
                    stage,
                    completed_steps,
                    total_steps,
                    skipped=False,
                )

    async def _execute_ocr_batch(
        self,
        job: dict[str, Any],
        pending: list[dict[str, Any]],
    ) -> dict[str, StageResult]:
        job_id = job["id"]
        executions: list[StageExecution] = []
        for item in pending:
            chapter = item["chapter"]
            pages = item["pages"]
            state = self.storage.get_stage_state(
                job_id,
                chapter["id"],
                ChapterStage.OCR,
            )
            checkpoint = _load_checkpoint(state)
            page_ids = set(checkpoint.get("page_ids") or [])
            selected_pages = [
                page for page in pages if not page_ids or page["id"] in page_ids
            ]
            self.storage.set_stage_state(
                job_id,
                chapter["id"],
                ChapterStage.OCR,
                status=StageStatus.RUNNING,
                version_hash=item["version_hash"],
                artifact_dir=str(
                    self.artifacts.stage_dir(
                        chapter["name"],
                        ChapterStage.OCR,
                        item["version_hash"],
                    )
                ),
                increment_attempt=True,
            )

            async def report(
                stage_name: str,
                current: int,
                total: int,
                *,
                _chapter=chapter,
            ) -> None:
                await self._update_progress(
                    job_id,
                    _chapter,
                    ChapterStage.OCR,
                    current,
                    max(1, total),
                    skipped=False,
                    detail=stage_name,
                )

            async def cancelled() -> bool:
                return await self._should_stop(job_id)

            executions.append(
                StageExecution(
                    job_id=job_id,
                    series_id=job["series_id"],
                    source_root=job.get("source_root")
                    or str(self.settings.raw_dir),
                    chapter=chapter,
                    pages=selected_pages,
                    options=item["options"],
                    version_hash=item["version_hash"],
                    version_hashes=item["version_hashes"],
                    artifacts=self.artifacts,
                    storage=self.storage,
                    settings=self.settings,
                    report_progress=report,
                    check_cancelled=cancelled,
                    page_ids=page_ids,
                )
            )
        try:
            results = await self.runtime.run_ocr_batch(executions)
        except Exception:
            for item in pending:
                self.storage.set_stage_state(
                    job_id,
                    item["chapter"]["id"],
                    ChapterStage.OCR,
                    status=StageStatus.FAILED,
                    version_hash=item["version_hash"],
                    error="batch OCR stage failed",
                )
            raise
        if await self._should_stop(job_id):
            for item in pending:
                self.storage.set_stage_state(
                    job_id,
                    item["chapter"]["id"],
                    ChapterStage.OCR,
                    status=StageStatus.PENDING,
                    version_hash=item["version_hash"],
                )
            raise PipelineStopped(f"job {job_id} stopped during OCR batch")

        for item, execution in zip(pending, executions):
            chapter = item["chapter"]
            result = results.get(chapter["id"])
            if result is None:
                raise RuntimeError(
                    f"batch OCR did not return Chapter result for {chapter['name']}"
                )
            manifest = {
                "stage": ChapterStage.OCR.value,
                "model_id": item["model_id"],
                "config": item["config"],
                "version_hash": item["version_hash"],
                "input_hash": input_hash_for_stage(
                    ChapterStage.OCR,
                    pages=item["pages"],
                    version_hashes=item["version_hashes"],
                ),
                "output_hash": result.output_hash,
                "checkpoint": result.checkpoint,
                "metrics": result.metrics,
                "created_at": utc_now(),
            }
            self.artifacts.write_manifest(
                chapter["name"],
                ChapterStage.OCR,
                item["version_hash"],
                manifest,
            )
            self.artifacts.activate(
                chapter["name"],
                ChapterStage.OCR,
                item["version_hash"],
            )
            self.storage.set_stage_state(
                job_id,
                chapter["id"],
                ChapterStage.OCR,
                status=StageStatus.COMPLETED,
                version_hash=item["version_hash"],
                artifact_dir=str(
                    self.artifacts.stage_dir(
                        chapter["name"],
                        ChapterStage.OCR,
                        item["version_hash"],
                    )
                ),
                checkpoint=result.checkpoint,
            )
        return results

    async def _execute_stage(
        self,
        *,
        job: dict[str, Any],
        chapter: dict[str, Any],
        pages: list[dict[str, Any]],
        stage: ChapterStage,
        model_id: str,
        config: dict[str, Any],
        options: dict[str, Any],
        version_hash: str,
        version_hashes: dict[str, str],
    ) -> None:
        job_id = job["id"]
        state = self.storage.get_stage_state(job_id, chapter["id"], stage)
        checkpoint = _load_checkpoint(state)
        page_ids = set(checkpoint.get("page_ids") or [])
        selected_pages = [
            page for page in pages if not page_ids or page["id"] in page_ids
        ]
        self.storage.set_stage_state(
            job_id,
            chapter["id"],
            stage,
            status=StageStatus.RUNNING,
            version_hash=version_hash,
            artifact_dir=str(
                self.artifacts.stage_dir(
                    chapter["name"], stage, version_hash
                )
            ),
            increment_attempt=True,
        )

        async def report(stage_name: str, current: int, total: int) -> None:
            await self._update_progress(
                job_id,
                chapter,
                stage,
                current,
                max(1, total),
                skipped=False,
                detail=stage_name,
            )

        async def cancelled() -> bool:
            return await self._should_stop(job_id)

        execution = StageExecution(
            job_id=job_id,
            series_id=job["series_id"],
            source_root=job.get("source_root") or str(self.settings.raw_dir),
            chapter=chapter,
            pages=selected_pages,
            options=options,
            version_hash=version_hash,
            version_hashes=version_hashes,
            artifacts=self.artifacts,
            storage=self.storage,
            settings=self.settings,
            report_progress=report,
            check_cancelled=cancelled,
            page_ids=page_ids,
        )
        started = utc_now()
        started_monotonic = time.monotonic()
        try:
            result = await self.dispatcher.run(stage, execution)
            if await self._should_stop(job_id):
                self.storage.set_stage_state(
                    job_id,
                    chapter["id"],
                    stage,
                    status=StageStatus.PENDING,
                    version_hash=version_hash,
                    checkpoint={"page_ids": sorted(page_ids)} if page_ids else {},
                )
                raise PipelineStopped(
                    f"job {job_id} stopped during {stage.value}"
                )
        except Exception as exc:
            if isinstance(exc, PipelineStopped):
                raise
            self.storage.set_stage_state(
                job_id,
                chapter["id"],
                stage,
                status=StageStatus.FAILED,
                version_hash=version_hash,
                checkpoint={"page_ids": sorted(page_ids)} if page_ids else {},
                error=str(exc),
            )
            raise
        manifest = {
            "stage": stage.value,
            "model_id": model_id,
            "config": config,
            "version_hash": version_hash,
            "input_hash": input_hash_for_stage(
                stage,
                pages=pages,
                version_hashes=version_hashes,
            ),
            "output_hash": result.output_hash,
            "checkpoint": result.checkpoint,
            "metrics": result.metrics,
            "created_at": utc_now(),
        }
        self.artifacts.write_manifest(
            chapter["name"],
            stage,
            version_hash,
            manifest,
        )
        self.artifacts.activate(chapter["name"], stage, version_hash)
        self.storage.set_stage_state(
            job_id,
            chapter["id"],
            stage,
            status=StageStatus.COMPLETED,
            version_hash=version_hash,
            artifact_dir=str(
                self.artifacts.stage_dir(
                    chapter["name"], stage, version_hash
                )
            ),
            checkpoint=result.checkpoint,
        )
        duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        self.storage.record_model_run(
            job_id=job_id,
            chapter_id=chapter["id"],
            page_id=None,
            stage=stage,
            model_id=model_id,
            artifact_version=version_hash,
            config_hash=str(manifest["input_hash"]),
            input_hash=str(manifest["input_hash"]),
            output_hash=result.output_hash,
            status=StageStatus.COMPLETED.value,
            started_at=started,
            finished_at=utc_now(),
            duration_ms=duration_ms,
            peak_memory_mb=result.metrics.get("peak_memory_mb"),
            peak_gpu_memory_mb=result.metrics.get("peak_gpu_memory_mb"),
            metrics=result.metrics,
        )

    async def _should_stop(self, job_id: str) -> bool:
        await asyncio.sleep(0)
        job = self.storage.get_job(job_id)
        return bool(
            not job
            or job["status"]
            in {
                JobStatus.PAUSED.value,
                JobStatus.CANCELLED.value,
            }
        )

    async def _update_progress(
        self,
        job_id: str,
        chapter: dict[str, Any],
        stage: ChapterStage,
        completed: int,
        total: int,
        *,
        skipped: bool,
        detail: str | None = None,
    ) -> None:
        self.storage.update_job(
            job_id,
            progress={
                "completed": completed,
                "total": total,
                "chapter": chapter["name"],
                "stage": stage.value,
                "skipped": skipped,
                "detail": detail or "",
            },
            checkpoint={
                "chapter": chapter["name"],
                "stage": stage.value,
                "updated_at": utc_now(),
            },
        )

    def download(
        self,
        *,
        job_id: str,
        chapters: Sequence[str] | None = None,
    ) -> Path:
        self.initialize()
        job = self.storage.get_job(job_id)
        if not job:
            raise KeyError(f"job not found: {job_id}")
        selected = list(chapters or job["requested_chapters"])
        target = (
            self.settings.upload_dir
            / "_downloads"
            / f"{job_id}-{uuid.uuid4().hex[:8]}.zip"
        )
        return self.artifacts.build_download_zip(target, selected)

    async def close(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


def _load_checkpoint(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {}
    raw = state.get("checkpoint_json")
    if not raw:
        return {}
    import json

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
