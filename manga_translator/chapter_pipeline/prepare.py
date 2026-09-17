"""Prepare RAW pages without translating: OCR and (optionally) LaMa erase.

Every series specific parameter comes from the active series configuration
(``config/series/<slug>.yaml``); this CLI only selects chapters, stages and
one-off overrides.

Examples::

    # OCR only, all chapters of the active series
    python -m manga_translator.chapter_pipeline.prepare

    # OCR then LaMa erase for a chapter range, publishing the erased pages
    python -m manga_translator.chapter_pipeline.prepare \\
        --stages ocr inpaint --chapters "Chapter 70" ... "Chapter 90"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence

from .config import PipelineSettings
from .models import ChapterStage, JobStatus
from .pipeline import ChapterPipelineService
from .runtime import DefaultChapterRuntime
from .storage import PipelineStorage


logger = logging.getLogger("manga_translator.chapter_pipeline.prepare")

SUPPORTED_STAGES = (ChapterStage.OCR, ChapterStage.INPAINT)


def build_options(
    settings: PipelineSettings,
    *,
    pages_per_batch: int | None = None,
    mask_dilation: int | None = None,
    inpainting_size: int | None = None,
) -> dict[str, Any]:
    """Series stage defaults with optional CLI overrides applied."""

    options = settings.stage_defaults()
    options["enable_embeddings"] = False
    if pages_per_batch is not None:
        options["ocr_pages_per_batch"] = max(1, pages_per_batch)
    if mask_dilation is not None:
        options["mask_dilation"] = max(0, mask_dilation)
    if inpainting_size is not None:
        stage_config = options.setdefault("stage_config", {})
        inpaint_config = stage_config.setdefault("inpaint", {})
        inpaint_config["inpainting_size"] = max(1, inpainting_size)
    if not ((options.get("stage_config") or {}).get("ocr")):
        logger.warning(
            "series %s declares no OCR stage parameters; the values in "
            "config/config.json will be used as-is",
            settings.series_slug,
        )
    return options


def _normalize_stages(stages: Sequence[str]) -> list[ChapterStage]:
    resolved: list[ChapterStage] = []
    for value in stages:
        try:
            stage = ChapterStage(str(value).casefold())
        except ValueError as exc:
            allowed = ", ".join(stage.value for stage in SUPPORTED_STAGES)
            raise ValueError(
                f"unsupported stage {value!r}; this command supports: {allowed}"
            ) from exc
        if stage not in SUPPORTED_STAGES:
            allowed = ", ".join(item.value for item in SUPPORTED_STAGES)
            raise ValueError(
                f"stage {value!r} is not available here; supported: {allowed}"
            )
        if stage not in resolved:
            resolved.append(stage)
    if not resolved:
        resolved = [ChapterStage.OCR]
    return resolved


def publish_inpaint_pages(
    service: ChapterPipelineService,
    storage: PipelineStorage,
    chapters: Sequence[dict[str, Any]],
) -> tuple[int, list[str]]:
    """Copy erased pages to ``results/RAW/<chapter>/`` for direct use.

    Follows the same convention the render stage uses for finished pages, so a
    later RENDER run simply overwrites them with the translated result.

    ``chapters`` are storage chapter records (needing ``id`` and ``name``).
    Publishing is a convenience step over the authoritative versioned
    artifacts, so per-chapter failures are collected instead of aborting.
    """

    published = 0
    errors: list[str] = []
    for chapter in chapters:
        try:
            version = service.artifacts.active_version(
                chapter["name"],
                ChapterStage.INPAINT,
            )
            if not version:
                errors.append(
                    f"{chapter['name']}: no active inpaint version to publish"
                )
                continue
            for page in storage.list_pages(chapter_id=chapter["id"]):
                source = service.artifacts.artifact_path(
                    chapter["name"],
                    ChapterStage.INPAINT,
                    version,
                    page["relative_path"],
                )
                if not source.is_file():
                    continue
                service.artifacts.publish_image(
                    chapter["name"],
                    ChapterStage.INPAINT,
                    version,
                    page["relative_path"],
                    source,
                )
                published += 1
        except Exception as exc:
            message = f"{chapter['name']}: {type(exc).__name__}: {exc}"
            logger.warning("publishing erased pages failed for %s", message)
            errors.append(message)
    return published, errors


async def run_prepare(
    *,
    settings: PipelineSettings,
    input_root: Path,
    chapter_names: list[str] | None,
    stages: Sequence[ChapterStage],
    options: dict[str, Any],
    report_path: Path,
    publish: bool = True,
) -> dict[str, Any]:
    if not input_root.is_dir():
        raise FileNotFoundError(f"input root does not exist: {input_root}")
    storage = PipelineStorage(settings.database_path)
    runtime = DefaultChapterRuntime(settings, storage)
    service = ChapterPipelineService(
        settings,
        storage,
        runtime,
        model_manager=runtime.model_manager,
    )
    service.initialize()
    report = service.scan(input_root, include_hashes=True)
    if not report.chapters:
        raise ValueError(
            f"no chapters were found under {input_root} "
            f"(errors: {report.errors or 'none'})"
        )
    selected = chapter_names or [chapter.name for chapter in report.chapters]
    job_id = service.create_job(
        root=input_root,
        chapter_names=selected,
        stages=list(stages),
        options=options,
    )
    await service.run_job(job_id)
    job = storage.get_job(job_id)
    if not job or job["status"] != JobStatus.COMPLETED.value:
        raise RuntimeError(
            f"prepare job failed: {job_id} "
            f"status={job.get('status') if job else 'missing'} "
            f"error={job.get('error') if job else ''}"
        )
    await service.close()

    chapter_records = [
        chapter
        for chapter in storage.list_chapters(job["series_id"])
        if chapter["name"] in selected
    ]
    chapter_rows: list[dict[str, Any]] = []
    for chapter in chapter_records:
        regions = 0
        ocr_pages = 0
        for page in storage.list_pages(chapter_id=chapter["id"]):
            artifact = service.artifacts.read_json(
                chapter["name"],
                ChapterStage.OCR,
                page["ocr_artifact"] or "",
                page["relative_path"],
            )
            if artifact is None:
                continue
            ocr_pages += 1
            regions += len(artifact.get("regions") or [])
        row = {
            "chapter": chapter["name"],
            "source_language": chapter["source_language"],
            "ocr_pages": ocr_pages,
            "regions": regions,
            "page_count": len(storage.list_pages(chapter_id=chapter["id"])),
        }
        if ChapterStage.INPAINT in stages:
            row["inpaint_version"] = service.artifacts.active_version(
                chapter["name"],
                ChapterStage.INPAINT,
            )
        chapter_rows.append(row)

    published = 0
    publish_errors: list[str] = []
    if publish and ChapterStage.INPAINT in stages:
        published, publish_errors = publish_inpaint_pages(
            service,
            storage,
            chapter_records,
        )

    result = {
        "job_id": job_id,
        "series": settings.series_name,
        "series_slug": settings.series_slug,
        "input_root": str(input_root),
        "results_dir": str(settings.results_dir),
        "stages": [stage.value for stage in stages],
        "chapters_requested": selected,
        "chapters_completed": chapter_rows,
        "page_count": sum(row["page_count"] for row in chapter_rows),
        "ocr_page_count": sum(row["ocr_pages"] for row in chapter_rows),
        "region_count": sum(row["regions"] for row in chapter_rows),
        "published_inpaint_pages": published,
        "publish_errors": publish_errors,
        "report_path": str(report_path),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run OCR and optional LaMa erase for the active series. "
            "Parameters come from config/series/<slug>.yaml."
        )
    )
    parser.add_argument(
        "--series",
        default=None,
        help="Series slug to activate. Defaults to MT_SERIES / the only series found.",
    )
    parser.add_argument(
        "--input-root",
        type=Path,
        default=None,
        help="RAW directory. Defaults to the series configuration.",
    )
    parser.add_argument(
        "--chapters",
        nargs="+",
        default=None,
        help="Chapter directory names, e.g. \"Chapter 70\". Defaults to every chapter.",
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        default=[ChapterStage.OCR.value],
        help="Stages to run: ocr, inpaint (default: ocr).",
    )
    parser.add_argument("--pages-per-batch", type=int, default=None)
    parser.add_argument("--mask-dilation", type=int, default=None)
    parser.add_argument("--inpainting-size", type=int, default=None)
    parser.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="Defaults to <results>/prepare-report.json.",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Keep erased pages only in .pipeline/inpaint/ instead of "
        "publishing them under results/RAW/<chapter>/.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.series:
        os.environ["MT_SERIES"] = args.series
    settings = PipelineSettings()
    input_root = (args.input_root or settings.raw_dir).resolve()
    report_path = (
        args.report_path or settings.results_dir / "prepare-report.json"
    ).resolve()
    stages = _normalize_stages(args.stages)
    options = build_options(
        settings,
        pages_per_batch=args.pages_per_batch,
        mask_dilation=args.mask_dilation,
        inpainting_size=args.inpainting_size,
    )
    logger.info(
        "series=%s root=%s stages=%s chapters=%s",
        settings.series_slug,
        input_root,
        [stage.value for stage in stages],
        args.chapters or "all",
    )
    asyncio.run(
        run_prepare(
            settings=settings,
            input_root=input_root,
            chapter_names=args.chapters,
            stages=stages,
            options=options,
            report_path=report_path,
            publish=not args.no_publish,
        )
    )


if __name__ == "__main__":
    main()
