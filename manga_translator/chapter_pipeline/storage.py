from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .models import (
    ChapterStage,
    InventoryReport,
    JobStatus,
    RegionRecord,
    ReviewStatus,
    StageStatus,
    canonical_relative_path,
    source_language_value,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class PipelineStorage:
    """Small SQLite repository for indexes, versions, and job checkpoints."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._schema_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._schema_lock:
            if self._initialized:
                return
            with self.connect() as connection:
                connection.executescript(_SCHEMA)
                self._ensure_fts(connection)
            self._initialized = True

    def _ensure_fts(self, connection: sqlite3.Connection) -> None:
        try:
            connection.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS translation_memory_fts
                USING fts5(
                    source_text,
                    target_text,
                    speaker_id,
                    content='translation_memory',
                    content_rowid='rowid',
                    tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS translation_memory_ai
                AFTER INSERT ON translation_memory BEGIN
                    INSERT INTO translation_memory_fts(
                        rowid, source_text, target_text, speaker_id
                    ) VALUES (
                        new.rowid, new.source_text, new.target_text,
                        coalesce(new.speaker_id, '')
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS translation_memory_ad
                AFTER DELETE ON translation_memory BEGIN
                    INSERT INTO translation_memory_fts(
                        translation_memory_fts, rowid, source_text,
                        target_text, speaker_id
                    ) VALUES (
                        'delete', old.rowid, old.source_text,
                        old.target_text, coalesce(old.speaker_id, '')
                    );
                END;
                CREATE TRIGGER IF NOT EXISTS translation_memory_au
                AFTER UPDATE ON translation_memory BEGIN
                    INSERT INTO translation_memory_fts(
                        translation_memory_fts, rowid, source_text,
                        target_text, speaker_id
                    ) VALUES (
                        'delete', old.rowid, old.source_text,
                        old.target_text, coalesce(old.speaker_id, '')
                    );
                    INSERT INTO translation_memory_fts(
                        rowid, source_text, target_text, speaker_id
                    ) VALUES (
                        new.rowid, new.source_text, new.target_text,
                        coalesce(new.speaker_id, '')
                    );
                END;
                """
            )
        except sqlite3.OperationalError:
            # FTS5 is optional; exact and LIKE retrieval remain available.
            pass

    def upsert_inventory(self, report: InventoryReport) -> str:
        self.initialize()
        series_id = f"series:{report.series_slug or _slug(report.series_name)}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO series(id, name, root_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    root_path = excluded.root_path,
                    updated_at = excluded.updated_at
                """,
                (series_id, report.series_name, report.root, now, now),
            )
            for chapter in report.chapters:
                chapter_id = _chapter_id(series_id, chapter.name)
                connection.execute(
                    """
                    INSERT INTO chapters(
                        id, series_id, name, sort_key, source_language,
                        page_count, raw_relative_path, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        sort_key = excluded.sort_key,
                        source_language = excluded.source_language,
                        page_count = excluded.page_count,
                        raw_relative_path = excluded.raw_relative_path,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chapter_id,
                        series_id,
                        chapter.name,
                        chapter.number,
                        source_language_value(chapter.source_language),
                        len(chapter.pages),
                        chapter.name,
                        now,
                        now,
                    ),
                )
                for page in chapter.pages:
                    page_id = page.page_id
                    connection.execute(
                        """
                        INSERT INTO pages(
                            id, chapter_id, series_id, relative_path, filename,
                            sha256, size_bytes, source_language, width, height,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            chapter_id = excluded.chapter_id,
                            series_id = excluded.series_id,
                            filename = excluded.filename,
                            sha256 = excluded.sha256,
                            size_bytes = excluded.size_bytes,
                            source_language = excluded.source_language,
                            width = excluded.width,
                            height = excluded.height,
                            updated_at = excluded.updated_at
                        """,
                        (
                            page_id,
                            chapter_id,
                            series_id,
                            canonical_relative_path(page.relative_path),
                            page.filename,
                            page.sha256,
                            page.size_bytes,
                            source_language_value(page.source_language),
                            page.width,
                            page.height,
                            now,
                        ),
                    )
            connection.execute("COMMIT")
        return series_id

    def list_chapters(self, series_id: str | None = None) -> list[dict[str, Any]]:
        self.initialize()
        query = """
            SELECT c.*, s.name AS series_name
            FROM chapters c
            JOIN series s ON s.id = c.series_id
        """
        params: tuple[Any, ...] = ()
        if series_id:
            query += " WHERE c.series_id = ?"
            params = (series_id,)
        query += " ORDER BY c.sort_key ASC, c.name ASC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def list_pages(
        self,
        chapter_id: str | None = None,
        page_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if chapter_id:
            clauses.append("p.chapter_id = ?")
            params.append(chapter_id)
        if page_ids:
            placeholders = ",".join("?" for _ in page_ids)
            clauses.append(f"p.id IN ({placeholders})")
            params.extend(page_ids)
        query = """
            SELECT p.*, c.name AS chapter_name, c.sort_key AS chapter_sort_key
            FROM pages p
            JOIN chapters c ON c.id = p.chapter_id
        """
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY c.sort_key ASC, p.relative_path ASC"
        with self.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(query, tuple(params))
            ]

    def get_series(self, series_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM series WHERE id = ?", (series_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_job(
        self,
        *,
        series_id: str,
        chapter_names: Sequence[str],
        stages: Sequence[ChapterStage | str],
        options: dict[str, Any] | None = None,
        source_root: str | None = None,
        upload_id: str | None = None,
        job_id: str | None = None,
    ) -> str:
        self.initialize()
        job_id = job_id or str(uuid.uuid4())
        now = utc_now()
        stage_values = [
            stage.value if isinstance(stage, ChapterStage) else str(stage)
            for stage in stages
        ]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs(
                    id, series_id, kind, status, requested_chapters_json,
                    options_json, progress_json, checkpoint_json, source_root,
                    upload_id, created_at, updated_at
                ) VALUES (?, ?, 'chapter_pipeline', ?, ?, ?, '{}', '{}', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    series_id,
                    JobStatus.QUEUED.value,
                    _json(list(chapter_names)),
                    _json({"stages": stage_values, **(options or {})}),
                    source_root,
                    upload_id,
                    now,
                    now,
                ),
            )
            chapter_rows = connection.execute(
                """
                SELECT id, name FROM chapters
                WHERE series_id = ?
                ORDER BY sort_key ASC, name ASC
                """,
                (series_id,),
            ).fetchall()
            selected = {name.casefold() for name in chapter_names}
            for chapter in chapter_rows:
                if selected and chapter["name"].casefold() not in selected:
                    continue
                for stage in stage_values:
                    connection.execute(
                        """
                        INSERT INTO chapter_stages(
                            id, job_id, chapter_id, stage, status, attempt,
                            max_attempts, checkpoint_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, '{}', ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            job_id,
                            chapter["id"],
                            stage,
                            StageStatus.PENDING.value,
                            int((options or {}).get("max_stage_attempts", 2)),
                            now,
                            now,
                        ),
                    )
            connection.execute("COMMIT")
        return job_id

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT j.*, s.name AS series_name
                FROM jobs j
                JOIN series s ON s.id = j.series_id
                ORDER BY j.created_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._decode_job(dict(row)) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT j.*, s.name AS series_name
                FROM jobs j
                JOIN series s ON s.id = j.series_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return None
            stages = connection.execute(
                """
                SELECT cs.*, c.name AS chapter_name, c.sort_key AS chapter_sort_key
                FROM chapter_stages cs
                JOIN chapters c ON c.id = cs.chapter_id
                WHERE cs.job_id = ?
                ORDER BY c.sort_key ASC, cs.stage ASC
                """,
                (job_id,),
            ).fetchall()
        job = self._decode_job(dict(row))
        job["stages"] = [
            {
                **dict(stage),
                "checkpoint": _load_json(stage["checkpoint_json"], {}),
            }
            for stage in stages
        ]
        return job

    def update_job(
        self,
        job_id: str,
        *,
        status: JobStatus | str | None = None,
        progress: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> None:
        self.initialize()
        fields = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if status is not None:
            status_value = status.value if isinstance(status, JobStatus) else str(status)
            fields.append("status = ?")
            params.append(status_value)
        if progress is not None:
            fields.append("progress_json = ?")
            params.append(_json(progress))
        if checkpoint is not None:
            fields.append("checkpoint_json = ?")
            params.append(_json(checkpoint))
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        elif status is not None:
            fields.append("error = NULL")
        if started:
            fields.append("started_at = coalesce(started_at, ?)")
            params.append(utc_now())
        if finished:
            fields.append("finished_at = ?")
            params.append(utc_now())
        params.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )

    def request_job_state(self, job_id: str, status: JobStatus) -> bool:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (status.value, utc_now(), job_id),
            )
        return cursor.rowcount > 0

    def recover_interrupted_jobs(self) -> int:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.INTERRUPTED.value,
                    utc_now(),
                    JobStatus.RUNNING.value,
                ),
            )
            connection.execute(
                """
                UPDATE chapter_stages
                SET status = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    StageStatus.PENDING.value,
                    utc_now(),
                    StageStatus.RUNNING.value,
                ),
            )
        return cursor.rowcount

    def set_stage_state(
        self,
        job_id: str,
        chapter_id: str,
        stage: ChapterStage | str,
        *,
        status: StageStatus,
        version_hash: str | None = None,
        artifact_dir: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        self.initialize()
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        now = utc_now()
        checkpoint_json = (
            _json(checkpoint) if checkpoint is not None else None
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chapter_stages(
                    id, job_id, chapter_id, stage, status, attempt,
                    max_attempts, version_hash, artifact_dir, checkpoint_json,
                    error, started_at, finished_at, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 2, ?, ?, coalesce(?, '{}'), ?, ?, ?, ?, ?
                )
                ON CONFLICT(job_id, chapter_id, stage) DO UPDATE SET
                    status = excluded.status,
                    attempt = chapter_stages.attempt + ?,
                    version_hash = coalesce(excluded.version_hash, chapter_stages.version_hash),
                    artifact_dir = coalesce(excluded.artifact_dir, chapter_stages.artifact_dir),
                    checkpoint_json = coalesce(?, chapter_stages.checkpoint_json),
                    error = excluded.error,
                    started_at = CASE
                        WHEN excluded.status = 'running' THEN excluded.started_at
                        ELSE chapter_stages.started_at
                    END,
                    finished_at = CASE
                        WHEN excluded.status IN ('completed', 'failed', 'skipped') THEN excluded.finished_at
                        ELSE chapter_stages.finished_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    chapter_id,
                    stage_value,
                    status.value,
                    1 if increment_attempt else 0,
                    version_hash,
                    artifact_dir,
                    checkpoint_json,
                    error,
                    now if status == StageStatus.RUNNING else None,
                    now if status in {
                        StageStatus.COMPLETED,
                        StageStatus.FAILED,
                        StageStatus.SKIPPED,
                    } else None,
                    now,
                    now,
                    1 if increment_attempt else 0,
                    checkpoint_json,
                ),
            )

    def get_stage_state(
        self,
        job_id: str,
        chapter_id: str,
        stage: ChapterStage | str,
    ) -> dict[str, Any] | None:
        self.initialize()
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM chapter_stages
                WHERE job_id = ? AND chapter_id = ? AND stage = ?
                """,
                (job_id, chapter_id, stage_value),
            ).fetchone()
        return dict(row) if row else None

    def reset_stage(
        self,
        job_id: str,
        chapter_id: str,
        stage: ChapterStage | str,
        *,
        page_ids: Sequence[str] | None = None,
    ) -> None:
        checkpoint = {"page_ids": list(page_ids)} if page_ids else {}
        self.set_stage_state(
            job_id,
            chapter_id,
            stage,
            status=StageStatus.PENDING,
            checkpoint=checkpoint,
        )

    def record_model_run(
        self,
        *,
        job_id: str | None,
        chapter_id: str | None,
        page_id: str | None,
        stage: ChapterStage | str,
        model_id: str,
        artifact_version: str,
        config_hash: str,
        input_hash: str,
        output_hash: str | None,
        status: str,
        started_at: str,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        peak_memory_mb: float | None = None,
        peak_gpu_memory_mb: float | None = None,
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        self.initialize()
        run_id = str(uuid.uuid4())
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_runs(
                    id, job_id, chapter_id, page_id, stage, model_id,
                    artifact_version, config_hash, input_hash, output_hash,
                    status, started_at, finished_at, duration_ms,
                    peak_memory_mb, peak_gpu_memory_mb, metrics_json, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    chapter_id,
                    page_id,
                    stage_value,
                    model_id,
                    artifact_version,
                    config_hash,
                    input_hash,
                    output_hash,
                    status,
                    started_at,
                    finished_at,
                    duration_ms,
                    peak_memory_mb,
                    peak_gpu_memory_mb,
                    _json(metrics or {}),
                    error,
                ),
            )
        return run_id

    def record_artifact(
        self,
        *,
        job_id: str | None,
        chapter_id: str | None,
        page_id: str | None,
        stage: ChapterStage | str,
        version_hash: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
    ) -> str:
        self.initialize()
        artifact_id = str(uuid.uuid4())
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, job_id, chapter_id, page_id, stage, version_hash,
                    relative_path, sha256, size_bytes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    job_id,
                    chapter_id,
                    page_id,
                    stage_value,
                    version_hash,
                    canonical_relative_path(relative_path),
                    sha256,
                    size_bytes,
                    utc_now(),
                ),
            )
        return artifact_id

    def upsert_regions(
        self,
        page_id: str,
        regions: Iterable[RegionRecord],
    ) -> None:
        self.initialize()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM regions WHERE page_id = ?", (page_id,))
            for region in regions:
                bbox = region.bbox or [None, None, None, None]
                connection.execute(
                    """
                    INSERT INTO regions(
                        id, page_id, region_index, x1, y1, x2, y2,
                        source_text, translated_text, source_language,
                        ocr_confidence, speaker_id, speaker_name,
                        speaker_confidence, speech_type, review_status,
                        artifact_version, metadata_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{page_id}:{region.region_index}",
                        page_id,
                        region.region_index,
                        bbox[0],
                        bbox[1],
                        bbox[2],
                        bbox[3],
                        region.source_text,
                        region.translated_text,
                        region.source_language,
                        region.ocr_confidence,
                        region.speaker_id,
                        region.speaker_name,
                        region.speaker_confidence,
                        region.speech_type,
                        region.review_status,
                        region.artifact_version,
                        _json(region.metadata),
                        now,
                    ),
                )
            connection.execute("COMMIT")

    def update_page_artifact(
        self,
        page_id: str,
        *,
        stage: ChapterStage | str,
        artifact_version: str,
        review_status: str | None = None,
    ) -> None:
        self.initialize()
        stage_value = stage.value if isinstance(stage, ChapterStage) else str(stage)
        artifact_column = {
            ChapterStage.OCR.value: "ocr_artifact",
            ChapterStage.VISION.value: "vision_artifact",
            ChapterStage.TRANSLATE.value: "translation_artifact",
            ChapterStage.RENDER.value: "render_artifact",
        }.get(stage_value)
        fields = ["updated_at = ?"]
        params: list[Any] = [utc_now()]
        if artifact_column:
            fields.append(f"{artifact_column} = ?")
            params.append(artifact_version)
        if review_status:
            fields.append("review_status = ?")
            params.append(review_status)
        params.append(page_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE pages SET {', '.join(fields)} WHERE id = ?",
                tuple(params),
            )

    def list_characters(
        self,
        series_id: str,
        *,
        review_status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT * FROM characters WHERE series_id = ?"
        params: list[Any] = [series_id]
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status)
        query += " ORDER BY confirmed_name IS NULL, confirmed_name, canonical_name"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
            characters = []
            for row in rows:
                item = dict(row)
                aliases = connection.execute(
                    """
                    SELECT language, alias, confidence, source_ref_json, review_status
                    FROM aliases WHERE character_id = ?
                    ORDER BY confidence DESC, alias
                    """,
                    (row["id"],),
                ).fetchall()
                item["aliases"] = [dict(alias) for alias in aliases]
                characters.append(item)
        return characters

    def upsert_character(
        self,
        *,
        series_id: str,
        character_id: str,
        canonical_name: str,
        description: str = "",
        confidence: float | None = None,
        review_status: ReviewStatus | str = ReviewStatus.NEEDS_REVIEW,
        source_ref: dict[str, Any] | None = None,
    ) -> str:
        self.initialize()
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO characters(
                    id, series_id, canonical_name, description, confidence,
                    review_status, source_ref_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    canonical_name = excluded.canonical_name,
                    description = CASE
                        WHEN excluded.description <> '' THEN excluded.description
                        ELSE characters.description
                    END,
                    confidence = max(
                        coalesce(characters.confidence, 0),
                        coalesce(excluded.confidence, 0)
                    ),
                    review_status = CASE
                        WHEN characters.review_status = 'confirmed'
                            THEN characters.review_status
                        ELSE excluded.review_status
                    END,
                    source_ref_json = coalesce(excluded.source_ref_json, characters.source_ref_json),
                    updated_at = excluded.updated_at
                """,
                (
                    character_id,
                    series_id,
                    canonical_name,
                    description,
                    confidence,
                    status,
                    _json(source_ref or {}),
                    now,
                    now,
                ),
            )
        return character_id

    def confirm_character(
        self,
        character_id: str,
        *,
        canonical_name: str | None = None,
        confirmed_by: str = "local-user",
    ) -> bool:
        self.initialize()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE characters
                SET canonical_name = coalesce(?, canonical_name),
                    confirmed_name = coalesce(?, canonical_name),
                    review_status = 'confirmed',
                    confirmed_by = ?,
                    confirmed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    canonical_name,
                    canonical_name,
                    confirmed_by,
                    utc_now(),
                    utc_now(),
                    character_id,
                ),
            )
        return cursor.rowcount > 0

    def upsert_alias(
        self,
        *,
        character_id: str,
        alias: str,
        language: str,
        confidence: float | None,
        review_status: ReviewStatus | str = ReviewStatus.NEEDS_REVIEW,
        source_ref: dict[str, Any] | None = None,
    ) -> None:
        self.initialize()
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        alias_id = f"{character_id}:{language}:{alias.casefold()}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO aliases(
                    id, character_id, language, alias, confidence,
                    review_status, source_ref_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    confidence = max(coalesce(aliases.confidence, 0), coalesce(excluded.confidence, 0)),
                    review_status = CASE
                        WHEN aliases.review_status = 'confirmed' THEN aliases.review_status
                        ELSE excluded.review_status
                    END,
                    source_ref_json = coalesce(excluded.source_ref_json, aliases.source_ref_json)
                """,
                (
                    alias_id,
                    character_id,
                    language,
                    alias,
                    confidence,
                    status,
                    _json(source_ref or {}),
                    utc_now(),
                ),
            )

    def upsert_voice_profile(
        self,
        *,
        character_id: str,
        language: str,
        profile: dict[str, Any],
        confidence: float | None,
        review_status: ReviewStatus | str = ReviewStatus.NEEDS_REVIEW,
        source_ref: dict[str, Any] | None = None,
    ) -> str:
        self.initialize()
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        profile_json = _json(profile)
        profile_hash = hashlib.sha256(
            profile_json.encode("utf-8")
        ).hexdigest()[:16]
        profile_id = f"{character_id}:{language}:{profile_hash}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_profiles(
                    id, character_id, language, profile_json, confidence,
                    review_status, source_ref_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    profile_json = CASE
                        WHEN voice_profiles.review_status = 'confirmed'
                            THEN voice_profiles.profile_json
                        ELSE excluded.profile_json
                    END,
                    confidence = max(
                        coalesce(voice_profiles.confidence, 0),
                        coalesce(excluded.confidence, 0)
                    ),
                    review_status = CASE
                        WHEN voice_profiles.review_status = 'confirmed'
                            THEN voice_profiles.review_status
                        ELSE excluded.review_status
                    END,
                    source_ref_json = coalesce(excluded.source_ref_json, voice_profiles.source_ref_json),
                    updated_at = excluded.updated_at
                """,
                (
                    profile_id,
                    character_id,
                    language,
                    profile_json,
                    confidence,
                    status,
                    _json(source_ref or {}),
                    utc_now(),
                ),
            )
        return profile_id

    def upsert_term(
        self,
        *,
        series_id: str,
        source: str,
        target: str,
        category: str = "term",
        condition: str = "",
        confidence: float | None = None,
        review_status: ReviewStatus | str = ReviewStatus.NEEDS_REVIEW,
        source_ref: dict[str, Any] | None = None,
    ) -> str:
        self.initialize()
        term_id = f"{series_id}:{category}:{source.casefold()}"
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO terms(
                    id, series_id, source, target, category, condition,
                    confidence, review_status, source_ref_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    target = CASE
                        WHEN terms.review_status = 'confirmed' THEN terms.target
                        ELSE excluded.target
                    END,
                    condition = excluded.condition,
                    confidence = max(coalesce(terms.confidence, 0), coalesce(excluded.confidence, 0)),
                    review_status = CASE
                        WHEN terms.review_status = 'confirmed' THEN terms.review_status
                        ELSE excluded.review_status
                    END,
                    source_ref_json = coalesce(excluded.source_ref_json, terms.source_ref_json),
                    updated_at = excluded.updated_at
                """,
                (
                    term_id,
                    series_id,
                    source,
                    target,
                    category,
                    condition,
                    confidence,
                    status,
                    _json(source_ref or {}),
                    now,
                    now,
                ),
            )
        return term_id

    def list_forced_terms(self, series_id: str) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM terms
                WHERE series_id = ?
                  AND (
                      review_status = 'confirmed'
                      OR (review_status = 'auto_accepted' AND confidence >= 0.95)
                  )
                ORDER BY category, source
                """,
                (series_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_translation_memory(
        self,
        *,
        series_id: str,
        source_language: str,
        source_text: str,
        target_text: str,
        context_hash: str,
        speaker_id: str | None = None,
        quality: float | None = None,
        review_status: ReviewStatus | str = ReviewStatus.NEEDS_REVIEW,
        source_ref: dict[str, Any] | None = None,
    ) -> str:
        self.initialize()
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        memory_id = f"{series_id}:{source_language}:{context_hash}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_memory(
                    id, series_id, source_language, source_text, target_text,
                    context_hash, speaker_id, quality, review_status,
                    source_ref_json, usage_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    target_text = CASE
                        WHEN translation_memory.review_status = 'confirmed'
                            THEN translation_memory.target_text
                        ELSE excluded.target_text
                    END,
                    speaker_id = excluded.speaker_id,
                    quality = max(coalesce(translation_memory.quality, 0), coalesce(excluded.quality, 0)),
                    review_status = CASE
                        WHEN translation_memory.review_status = 'confirmed'
                            THEN translation_memory.review_status
                        ELSE excluded.review_status
                    END,
                    source_ref_json = coalesce(excluded.source_ref_json, translation_memory.source_ref_json),
                    updated_at = excluded.updated_at
                """,
                (
                    memory_id,
                    series_id,
                    source_language,
                    source_text,
                    target_text,
                    context_hash,
                    speaker_id,
                    quality,
                    status,
                    _json(source_ref or {}),
                    now,
                    now,
                ),
            )
        return memory_id

    def search_translation_memory(
        self,
        *,
        series_id: str,
        source_language: str,
        query: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT tm.*, bm25(translation_memory_fts) AS rank
                    FROM translation_memory_fts
                    JOIN translation_memory tm
                      ON tm.rowid = translation_memory_fts.rowid
                    WHERE translation_memory_fts MATCH ?
                      AND tm.series_id = ?
                      AND tm.source_language = ?
                      AND tm.review_status IN ('confirmed', 'auto_accepted')
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (_fts_query(query), series_id, source_language, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = connection.execute(
                    """
                    SELECT *, 0.0 AS rank
                    FROM translation_memory
                    WHERE series_id = ?
                      AND source_language = ?
                      AND source_text LIKE ?
                      AND review_status IN ('confirmed', 'auto_accepted')
                    ORDER BY quality DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (series_id, source_language, f"%{query}%", limit),
                ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE translation_memory SET usage_count = usage_count + 1 WHERE id = ?",
                    (row["id"],),
                )
        return [dict(row) for row in rows]

    def upsert_summary(
        self,
        *,
        series_id: str,
        kind: str,
        content: str,
        chapter_id: str | None = None,
        page_id: str | None = None,
        confidence: float | None = None,
        source_ref: dict[str, Any] | None = None,
        review_status: ReviewStatus | str = ReviewStatus.UNREVIEWED,
    ) -> str:
        self.initialize()
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        summary_id = str(uuid.uuid4())
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT id, review_status
                FROM summaries
                WHERE series_id = ?
                  AND kind = ?
                  AND chapter_id IS ?
                  AND page_id IS ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (series_id, kind, chapter_id, page_id),
            ).fetchone()
            if existing and existing["review_status"] != "confirmed":
                connection.execute(
                    """
                    UPDATE summaries
                    SET content = ?, confidence = ?, review_status = ?,
                        source_ref_json = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        content,
                        confidence,
                        status,
                        _json(source_ref or {}),
                        utc_now(),
                        existing["id"],
                    ),
                )
                return existing["id"]
            connection.execute(
                """
                INSERT INTO summaries(
                    id, series_id, chapter_id, page_id, kind, content,
                    confidence, review_status, source_ref_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    series_id,
                    chapter_id,
                    page_id,
                    kind,
                    content,
                    confidence,
                    status,
                    _json(source_ref or {}),
                    utc_now(),
                ),
            )
        return summary_id

    def upsert_event(
        self,
        *,
        series_id: str,
        chapter_id: str,
        page_id: str | None,
        description: str,
        confidence: float | None,
        source_ref: dict[str, Any] | None = None,
        review_status: ReviewStatus | str = ReviewStatus.UNREVIEWED,
    ) -> str:
        self.initialize()
        event_id = str(uuid.uuid4())
        status = (
            review_status.value
            if isinstance(review_status, ReviewStatus)
            else str(review_status)
        )
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM events
                WHERE series_id = ?
                  AND chapter_id = ?
                  AND description = ?
                  AND review_status != 'confirmed'
                """,
                (series_id, chapter_id, description),
            )
            connection.execute(
                """
                INSERT INTO events(
                    id, series_id, chapter_id, page_id, description,
                    confidence, review_status, source_ref_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    series_id,
                    chapter_id,
                    page_id,
                    description,
                    confidence,
                    status,
                    _json(source_ref or {}),
                    utc_now(),
                ),
            )
        return event_id

    def upsert_embedding(
        self,
        *,
        entity_type: str,
        entity_id: str,
        model_id: str,
        vector: Sequence[float],
        content_hash: str,
    ) -> str:
        self.initialize()
        embedding_id = f"{entity_type}:{entity_id}:{model_id}"
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO embeddings(
                    id, entity_type, entity_id, model_id, dimensions,
                    vector_json, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    dimensions = excluded.dimensions,
                    vector_json = excluded.vector_json,
                    content_hash = excluded.content_hash,
                    created_at = excluded.created_at
                """,
                (
                    embedding_id,
                    entity_type,
                    entity_id,
                    model_id,
                    len(vector),
                    _json(list(vector)),
                    content_hash,
                    utc_now(),
                ),
            )
        return embedding_id

    def stats(self) -> dict[str, Any]:
        self.initialize()
        tables = [
            "series",
            "chapters",
            "pages",
            "regions",
            "characters",
            "aliases",
            "voice_profiles",
            "terms",
            "summaries",
            "events",
            "translation_memory",
            "model_runs",
            "embeddings",
            "jobs",
            "chapter_stages",
            "artifacts",
        ]
        with self.connect() as connection:
            counts = {
                table: connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in tables
            }
        return {
            "database_path": str(self.database_path),
            "size_bytes": self.database_path.stat().st_size
            if self.database_path.exists()
            else 0,
            "counts": counts,
        }

    @staticmethod
    def _decode_job(row: dict[str, Any]) -> dict[str, Any]:
        row["requested_chapters"] = _load_json(
            row.pop("requested_chapters_json", None), []
        )
        row["options"] = _load_json(row.pop("options_json", None), {})
        row["progress"] = _load_json(row.pop("progress_json", None), {})
        row["checkpoint"] = _load_json(row.pop("checkpoint_json", None), {})
        return row


def _chapter_id(series_id: str, chapter_name: str) -> str:
    return f"{series_id}:{chapter_name}"


def _slug(value: str) -> str:
    return "-".join(
        part
        for part in "".join(
            char.casefold() if char.isalnum() else "-"
            for char in value
        ).split("-")
        if part
    )


def _fts_query(value: str) -> str:
    tokens = [
        token
        for token in "".join(
            char if char.isalnum() else " " for char in value
        ).split()
        if token
    ]
    return " OR ".join(f'"{token}"' for token in tokens) or '""'


_SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort_key REAL NOT NULL,
    source_language TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    raw_relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, name)
);

CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    source_language TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    ocr_artifact TEXT,
    vision_artifact TEXT,
    translation_artifact TEXT,
    render_artifact TEXT,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_pages_chapter ON pages(chapter_id);

CREATE TABLE IF NOT EXISTS regions (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    region_index INTEGER NOT NULL,
    x1 REAL,
    y1 REAL,
    x2 REAL,
    y2 REAL,
    source_text TEXT NOT NULL DEFAULT '',
    translated_text TEXT NOT NULL DEFAULT '',
    source_language TEXT NOT NULL DEFAULT 'unknown',
    ocr_confidence REAL,
    speaker_id TEXT,
    speaker_name TEXT,
    speaker_confidence REAL,
    speech_type TEXT NOT NULL DEFAULT 'unknown',
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    artifact_version TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    UNIQUE(page_id, region_index)
);

CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    canonical_name TEXT NOT NULL,
    confirmed_name TEXT,
    description TEXT NOT NULL DEFAULT '',
    first_chapter_id TEXT REFERENCES chapters(id),
    confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'needs_review',
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, canonical_name)
);

CREATE TABLE IF NOT EXISTS aliases (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    alias TEXT NOT NULL,
    confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'needs_review',
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(character_id, language, alias)
);

CREATE TABLE IF NOT EXISTS voice_profiles (
    id TEXT PRIMARY KEY,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    profile_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'needs_review',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS terms (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'term',
    condition TEXT NOT NULL DEFAULT '',
    confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'needs_review',
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, category, source)
);

CREATE TABLE IF NOT EXISTS summaries (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    chapter_id TEXT REFERENCES chapters(id) ON DELETE CASCADE,
    page_id TEXT REFERENCES pages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    confidence REAL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS translation_memory (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    source_language TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_text TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    speaker_id TEXT,
    quality REAL,
    review_status TEXT NOT NULL DEFAULT 'needs_review',
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    usage_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(series_id, source_language, context_hash)
);

CREATE TABLE IF NOT EXISTS model_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    chapter_id TEXT REFERENCES chapters(id) ON DELETE SET NULL,
    page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
    stage TEXT NOT NULL,
    model_id TEXT NOT NULL,
    artifact_version TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    peak_memory_mb REAL,
    peak_gpu_memory_mb REAL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_runs_stage ON model_runs(stage, artifact_version);

CREATE TABLE IF NOT EXISTS embeddings (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(entity_type, entity_id, model_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_chapters_json TEXT NOT NULL DEFAULT '[]',
    options_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    source_root TEXT,
    upload_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS chapter_stages (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    version_hash TEXT,
    artifact_dir TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, chapter_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_stage_status ON chapter_stages(status, stage);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    chapter_id TEXT REFERENCES chapters(id) ON DELETE SET NULL,
    page_id TEXT REFERENCES pages(id) ON DELETE SET NULL,
    stage TEXT NOT NULL,
    version_hash TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_page ON artifacts(page_id, stage);
"""
