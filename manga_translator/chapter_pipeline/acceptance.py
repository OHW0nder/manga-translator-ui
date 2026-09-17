"""End-to-end acceptance run for the active series.

Runs OCR -> TRANSLATE -> INPAINT -> RENDER over a small, single-language set of
chapters and verifies the exported ZIP structure. Everything series specific
(which chapters, which language, which models) comes from
``config/series/<slug>.yaml``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import zipfile
from pathlib import Path

from .config import PipelineSettings
from .models import ChapterStage, JobStatus
from .pipeline import ChapterPipelineService
from .runtime import DefaultChapterRuntime
from .stages import normalize_stages
from .storage import PipelineStorage


DEFAULT_ACCEPTANCE_CHAPTERS = 3


def _korean_webtoon_options(settings: PipelineSettings) -> dict:
    """Stage options for the acceptance run, on top of the series defaults."""

    options = settings.stage_defaults()
    options.update(
        {
            "translation_provider": "local",
            "translation_mode": "hq",
            "hq_batch_size": 2,
            "hq_max_regions_per_request": 60,
            "enable_embeddings": False,
            "ocr_correction_threshold": settings.ocr_correction_threshold,
            "speaker_confidence_threshold": settings.speaker_confidence_threshold,
            "translation_confidence_threshold": (
                settings.translation_confidence_threshold
            ),
            "korean_webtoon": True,
            "direction": "horizontal",
            "alignment": "center",
            "layout_mode": "smart_scaling",
            "font_family": "Microsoft YaHei UI",
        }
    )
    models = options.setdefault("models", {})
    models.setdefault("translation", settings.translation_model)
    stage_config = options.setdefault("stage_config", {})
    ocr_config = stage_config.setdefault("ocr", {})
    ocr_config.setdefault("limit_mask_dilation_to_bubble_mask", True)
    return options


async def run_acceptance(
    *,
    input_root: Path,
    chapters: list[str],
    output_zip: Path,
    settings: PipelineSettings | None = None,
) -> dict:
    settings = settings or PipelineSettings()
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
    indexed = {chapter.name: chapter for chapter in report.chapters}
    missing = [chapter for chapter in chapters if chapter not in indexed]
    if missing:
        raise ValueError("missing chapters: " + ", ".join(missing))

    languages = {
        str(indexed[chapter_name].source_language) for chapter_name in chapters
    }
    if len(languages) != 1:
        raise ValueError(
            "acceptance run must cover a single source language, got: "
            + ", ".join(sorted(languages))
        )
    source_language = languages.pop()
    ocr_model = settings.ocr_model_for(indexed[chapters[0]])

    stages = normalize_stages(
        [
            ChapterStage.OCR,
            ChapterStage.TRANSLATE,
            ChapterStage.INPAINT,
            ChapterStage.RENDER,
        ]
    )
    job_id = service.create_job(
        root=input_root,
        chapter_names=chapters,
        stages=stages,
        options=_korean_webtoon_options(settings),
    )
    await service.run_job(job_id)
    job = storage.get_job(job_id)
    if not job or job["status"] != JobStatus.COMPLETED.value:
        raise RuntimeError(
            f"acceptance job failed: {job_id} "
            f"status={job['status'] if job else 'missing'} "
            f"error={job.get('error') if job else ''}"
        )

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    archive = service.artifacts.build_download_zip(output_zip, chapters)
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
    expected_chapters = set(chapters)
    observed_chapters = {name.split("/", 1)[0] for name in names}
    if observed_chapters != expected_chapters:
        raise RuntimeError(
            "ZIP chapter structure mismatch: "
            f"expected={sorted(expected_chapters)} observed={sorted(observed_chapters)}"
        )
    await service.close()
    summary = {
        "job_id": job_id,
        "series": settings.series_name,
        "input_root": str(input_root),
        "chapters": chapters,
        "page_count": sum(len(indexed[name].pages) for name in chapters),
        "zip_path": str(archive),
        "zip_files": names,
        "source_language": source_language,
        "ocr_model": ocr_model,
        "target_language": settings.target_language,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the OCR -> TRANSLATE -> INPAINT -> RENDER acceptance "
            "pipeline for the active series."
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
        help="Chapter names. Defaults to the first chapters of the series.",
    )
    parser.add_argument("--output-zip", type=Path, default=None)
    args = parser.parse_args()

    if args.series:
        os.environ["MT_SERIES"] = args.series
    settings = PipelineSettings()
    input_root = (args.input_root or settings.raw_dir).resolve()
    chapters = args.chapters
    if not chapters:
        report = ChapterPipelineService(
            settings,
            PipelineStorage(settings.database_path),
            DefaultChapterRuntime(settings, PipelineStorage(settings.database_path)),
        ).scan(input_root, include_hashes=False)
        chapters = [
            chapter.name
            for chapter in report.chapters[:DEFAULT_ACCEPTANCE_CHAPTERS]
        ]
        if not chapters:
            raise ValueError(f"no chapters were found under {input_root}")
    output_zip = (
        args.output_zip
        or settings.results_dir
        / f"{'-'.join(chapters).replace(' ', '_')}-acceptance.zip"
    ).resolve()
    asyncio.run(
        run_acceptance(
            input_root=input_root,
            chapters=list(chapters),
            output_zip=output_zip,
            settings=settings,
        )
    )


if __name__ == "__main__":
    main()
