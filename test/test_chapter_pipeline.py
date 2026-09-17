from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path

from manga_translator.chapter_pipeline.artifacts import ArtifactStore, make_version_hash
from manga_translator.chapter_pipeline.config import PipelineSettings
from manga_translator.chapter_pipeline.inventory import (
    extract_upload_zip,
    scan_inventory,
)
from manga_translator.chapter_pipeline.models import (
    ChapterStage,
    JobStatus,
    ReviewStatus,
    SourceLanguage,
    StageStatus,
    source_language_value,
)
from manga_translator.chapter_pipeline.paths import (
    UnsafeRelativePath,
    normalize_upload_relative_path,
)
from manga_translator.chapter_pipeline.pipeline import ChapterPipelineService
from manga_translator.chapter_pipeline.series_config import (
    ChapterSelector,
    LanguageRule,
    LanguageRuleSet,
    SeriesConfig,
    SeriesConfigError,
    SeriesPaths,
    expand_env,
    slugify,
)
from manga_translator.chapter_pipeline.stages import (
    DEFAULT_STAGES,
    StageResult,
    affected_stages,
    normalize_stages,
    stage_config,
    stage_model_id,
)
from manga_translator.chapter_pipeline.storage import PipelineStorage


def _write_inventory(root: Path, chapters: dict[str, int]) -> None:
    for chapter_name, count in chapters.items():
        chapter = root / chapter_name
        chapter.mkdir(parents=True, exist_ok=True)
        for index in range(1, count + 1):
            (chapter / f"{index:03d}.png").write_bytes(
                f"{chapter_name}:{index}".encode("utf-8")
            )


def _series(
    name: str,
    root: Path,
    *,
    korean_max: float = 14,
    expected_chapters: int = 0,
    expected_pages: int = 0,
    spanish_overrides: dict | None = None,
) -> SeriesConfig:
    """A series whose chapters 1..korean_max are Korean and the rest Spanish."""

    return SeriesConfig(
        name=name,
        slug=slugify(name),
        paths=SeriesPaths(
            raw=root / "RAW",
            results=root / "results",
            database=root / "pipeline.db",
            uploads=root / "uploads",
        ),
        languages=LanguageRuleSet(
            (
                LanguageRule(
                    chapters=ChapterSelector.parse(f"1-{korean_max:g}"),
                    source="ko",
                    ocr_model="paddleocr_korean",
                    ocr_hint="Korean",
                    translator_code="KOR",
                ),
                LanguageRule(
                    chapters=ChapterSelector.parse("*"),
                    source="es",
                    ocr_model="paddleocr_latin",
                    ocr_hint="Spanish",
                    translator_code="ESP",
                    options=spanish_overrides or {},
                ),
            )
        ),
        defaults={
            "ocr_pages_per_batch": 8,
            "mask_dilation": 20,
            "models": {"inpaint": "lama_large"},
            "stage_config": {
                "ocr": {
                    "detector": "default",
                    "detection_size": 2048,
                    "text_threshold": 0.45,
                    "box_threshold": 0.65,
                    "ignore_bubble": 0.3,
                    "merge_gamma": 0.9,
                },
                "inpaint": {"inpainting_size": 2048},
            },
        },
        expected_chapter_count=expected_chapters,
        expected_page_count=expected_pages,
    )


class FakeRuntime:
    def __init__(self):
        self.calls: Counter[str] = Counter()
        self.ocr_batch_calls = 0
        self._write_lock = asyncio.Lock()

    async def run_ocr(self, execution):
        return await self._finish(execution, ChapterStage.OCR)

    async def run_ocr_batch(self, executions):
        self.ocr_batch_calls += 1
        return {
            execution.chapter["id"]: await self._finish(
                execution,
                ChapterStage.OCR,
            )
            for execution in executions
        }

    async def run_vision(self, execution):
        return await self._finish(execution, ChapterStage.VISION)

    async def run_knowledge(self, execution):
        return await self._finish(execution, ChapterStage.KNOWLEDGE)

    async def run_translate(self, execution):
        return await self._finish(execution, ChapterStage.TRANSLATE)

    async def run_inpaint(self, execution):
        return await self._finish(execution, ChapterStage.INPAINT)

    async def run_render(self, execution):
        return await self._finish(execution, ChapterStage.RENDER)

    async def run_summarize(self, execution):
        return await self._finish(execution, ChapterStage.SUMMARIZE)

    async def run_embed(self, execution):
        return await self._finish(execution, ChapterStage.EMBED)

    async def _finish(self, execution, stage):
        self.calls[stage.value] += 1
        async with self._write_lock:
            for page in execution.pages:
                payload = {
                    "regions": [
                        {
                            "lines": [
                                [[0, 0], [10, 0], [10, 10], [0, 10]]
                            ],
                            "text": "source",
                            "translation": "译文",
                            "review_status": ReviewStatus.AUTO_ACCEPTED.value,
                        }
                    ],
                    "source_language": source_language_value(
                        execution.chapter["source_language"]
                    ),
                }
                execution.artifacts.write_json(
                    execution.chapter["name"],
                    stage,
                    execution.version_hash,
                    page["relative_path"],
                    payload,
                )
            execution.artifacts.write_json(
                execution.chapter["name"],
                stage,
                execution.version_hash,
                "_chapter.json",
                {"stage": stage.value},
            )
        return StageResult(
            output_hash=execution.artifacts.hash_stage(
                execution.chapter["name"],
                stage,
                execution.version_hash,
            ),
            checkpoint={"pages": len(execution.pages)},
        )


class SeriesConfigTests(unittest.TestCase):
    def test_chapter_selector_syntax(self):
        everything = ChapterSelector.parse("*")
        self.assertTrue(everything.matches(1))
        self.assertTrue(everything.matches(900))

        bounded = ChapterSelector.parse("1-90")
        self.assertTrue(bounded.matches(1))
        self.assertTrue(bounded.matches(90))
        self.assertFalse(bounded.matches(90.5))
        self.assertFalse(bounded.matches(91))

        open_ended = ChapterSelector.parse("91-")
        self.assertFalse(open_ended.matches(90))
        self.assertTrue(open_ended.matches(91))
        self.assertTrue(open_ended.matches(500))

        up_to = ChapterSelector.parse("-14")
        self.assertTrue(up_to.matches(14))
        self.assertFalse(up_to.matches(15))

        exact = ChapterSelector.parse("114.5")
        self.assertTrue(exact.matches(114.5))
        self.assertFalse(exact.matches(114))

        mixed = ChapterSelector.parse("1-14,24.5")
        self.assertTrue(mixed.matches(3))
        self.assertTrue(mixed.matches(24.5))
        self.assertFalse(mixed.matches(20))

        with self.assertRaises(SeriesConfigError):
            ChapterSelector.parse("not-a-number")
        with self.assertRaises(SeriesConfigError):
            ChapterSelector.parse("-")

    def test_language_rules_are_configured_not_hardcoded(self):
        """Both supported layouts must come from configuration alone."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            love_quest = _series("Love Quest", root, korean_max=14)
            self.assertEqual(love_quest.language_for(1).source, "ko")
            self.assertEqual(love_quest.language_for(14).source, "ko")
            self.assertEqual(love_quest.language_for(15).source, "es")
            self.assertEqual(love_quest.language_for(24.5).source, "es")
            self.assertEqual(
                love_quest.language_for(15).ocr_model,
                "paddleocr_latin",
            )

            onahole = _series("Wireless Onahole", root, korean_max=90)
            self.assertEqual(onahole.language_for(70).source, "ko")
            self.assertEqual(onahole.language_for(90).source, "ko")
            self.assertEqual(onahole.language_for(91).source, "es")
            self.assertEqual(onahole.language_for(114.5).source, "es")
            self.assertEqual(
                onahole.language_for(70).ocr_model,
                "paddleocr_korean",
            )

    def test_uncovered_chapter_raises_instead_of_guessing(self):
        rules = LanguageRuleSet(
            (LanguageRule(chapters=ChapterSelector.parse("1-10"), source="ko"),)
        )
        with self.assertRaises(SeriesConfigError):
            rules.resolve(11)

    def test_slug_and_env_expansion(self):
        self.assertEqual(slugify("Wireless Onahole"), "wireless-onahole")
        self.assertEqual(slugify("Love Quest"), "love-quest")
        self.assertEqual(expand_env("no-vars"), "no-vars")
        # Undefined variables stay visible instead of collapsing to an empty path.
        self.assertEqual(expand_env("${MT_DEFINITELY_UNSET}/x"), "${MT_DEFINITELY_UNSET}/x")

    def test_upload_prefixes_come_from_the_series(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            series = _series("Wireless Onahole", root)
            self.assertIn("wireless onahole", series.upload_prefixes)
            self.assertIn("wireless-onahole", series.upload_prefixes)
            self.assertIn("raw", series.upload_prefixes)


class SettingsTests(unittest.TestCase):
    def test_settings_resolve_paths_from_the_series(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PipelineSettings(series=_series("Test Quest", root))
            self.assertEqual(settings.series_name, "Test Quest")
            self.assertEqual(settings.series_slug, "test-quest")
            self.assertEqual(settings.raw_dir, root / "RAW")
            self.assertEqual(settings.results_dir, root / "results")
            self.assertEqual(settings.database_path, root / "pipeline.db")
            self.assertEqual(settings.upload_dir, root / "uploads")
            self.assertEqual(settings.target_code, "CHS")
            self.assertEqual(settings.target_language, "zh-Hans")

    def test_ocr_model_and_translator_code_follow_the_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PipelineSettings(
                series=_series("Wireless Onahole", root, korean_max=90)
            )
            korean = {"sort_key": 70.0, "source_language": "ko", "name": "Chapter 70"}
            spanish = {"sort_key": 91.0, "source_language": "es", "name": "Chapter 91"}
            self.assertEqual(settings.ocr_model_for(korean), "paddleocr_korean")
            self.assertEqual(settings.ocr_model_for(spanish), "paddleocr_latin")
            self.assertEqual(settings.translator_code_for_chapter(korean), "KOR")
            self.assertEqual(settings.translator_code_for_chapter(spanish), "ESP")

    def test_job_options_override_the_series_ocr_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PipelineSettings(series=_series("Test Quest", root))
            chapter = {"sort_key": 3.0, "source_language": "ko", "name": "Chapter 3"}
            self.assertEqual(
                settings.ocr_model_for(chapter, options={"models": {"ocr": "48px"}}),
                "48px",
            )
            self.assertEqual(
                stage_model_id(
                    ChapterStage.OCR,
                    settings=settings,
                    options={"models": {"ocr": "48px"}},
                    chapter=chapter,
                ),
                "48px",
            )

    def test_per_language_parameter_overrides(self):
        """A language rule can tune parameters for its own chapter range."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PipelineSettings(
                series=_series(
                    "Wireless Onahole",
                    root,
                    korean_max=90,
                    spanish_overrides={
                        "mask_dilation": 15,
                        "stage_config": {
                            "ocr": {"text_threshold": 0.35, "ignore_bubble": 0.5}
                        },
                    },
                )
            )
            korean = {"sort_key": 70.0, "source_language": "ko", "name": "Chapter 70"}
            spanish = {"sort_key": 91.0, "source_language": "es", "name": "Chapter 91"}

            korean_options = settings.chapter_options(korean)
            spanish_options = settings.chapter_options(spanish)

            # Spanish overrides apply only to the Spanish range.
            self.assertEqual(korean_options["stage_config"]["ocr"]["text_threshold"], 0.45)
            self.assertEqual(spanish_options["stage_config"]["ocr"]["text_threshold"], 0.35)
            self.assertEqual(korean_options["stage_config"]["ocr"]["ignore_bubble"], 0.3)
            self.assertEqual(spanish_options["stage_config"]["ocr"]["ignore_bubble"], 0.5)
            self.assertEqual(korean_options["mask_dilation"], 20)
            self.assertEqual(spanish_options["mask_dilation"], 15)

            # Untouched baseline keys survive the deep merge.
            for options in (korean_options, spanish_options):
                self.assertEqual(options["stage_config"]["ocr"]["detector"], "default")
                self.assertEqual(options["stage_config"]["ocr"]["detection_size"], 2048)
                self.assertEqual(
                    options["stage_config"]["inpaint"]["inpainting_size"], 2048
                )
                self.assertEqual(options["models"]["inpaint"], "lama_large")

            # The rule's ocr_model reaches models.ocr so model resolution has
            # a single source of truth per rule.
            self.assertEqual(korean_options["models"]["ocr"], "paddleocr_korean")
            self.assertEqual(spanish_options["models"]["ocr"], "paddleocr_latin")

            # A job-level override still beats the language rule.
            forced = settings.chapter_options(
                spanish,
                {"stage_config": {"ocr": {"text_threshold": 0.2}}},
            )
            self.assertEqual(forced["stage_config"]["ocr"]["text_threshold"], 0.2)
            self.assertEqual(forced["stage_config"]["ocr"]["ignore_bubble"], 0.5)

            # Different parameters must produce different version hashes.
            def ocr_hash(chapter):
                resolved = settings.chapter_options(chapter)
                return make_version_hash(
                    ChapterStage.OCR,
                    model_id=settings.ocr_model_for(chapter, options=resolved),
                    config=stage_config(
                        ChapterStage.OCR,
                        settings=settings,
                        options=resolved,
                    ),
                    input_hash="",
                )

            self.assertNotEqual(ocr_hash(korean), ocr_hash(spanish))

    def test_stage_config_reports_the_series_target_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PipelineSettings(series=_series("Test Quest", root))
            config = stage_config(
                ChapterStage.TRANSLATE,
                settings=settings,
                options={},
            )
            self.assertEqual(config["target_language"], "zh-Hans")


class InventoryTests(unittest.TestCase):
    def test_scan_ignores_work_dirs_and_assigns_languages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_inventory(
                root,
                {
                    "Chapter 14": 2,
                    "Chapter 15": 1,
                    "Chapter 24.5": 1,
                },
            )
            (root / "Chapter 14" / "manga_translator_work").mkdir()
            (root / "Chapter 14" / "manga_translator_work" / "nested.png").write_bytes(
                b"nested"
            )
            series = _series("Love Quest", root)
            report = scan_inventory(
                root,
                series_name=series.name,
                series_slug=series.slug,
                languages=series.languages,
                expected_chapter_count=3,
                expected_page_count=4,
            )
            self.assertTrue(report.valid)
            self.assertEqual(report.series_slug, "love-quest")
            self.assertEqual(
                [chapter.name for chapter in report.chapters],
                ["Chapter 14", "Chapter 15", "Chapter 24.5"],
            )
            self.assertEqual(
                report.chapters[0].source_language,
                SourceLanguage.KOREAN,
            )
            self.assertEqual(
                report.chapters[1].source_language,
                SourceLanguage.SPANISH,
            )
            self.assertEqual(
                report.chapters[2].source_language,
                SourceLanguage.SPANISH,
            )

    def test_chapters_outside_the_rules_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_inventory(root, {"Chapter 1": 1, "Chapter 11": 1})
            series = _series("Narrow", root, korean_max=10)
            report = scan_inventory(
                root,
                series_name=series.name,
                languages=LanguageRuleSet(
                    (
                        LanguageRule(
                            chapters=ChapterSelector.parse("1-10"),
                            source="ko",
                            ocr_model="paddleocr_korean",
                        ),
                    )
                ),
            )
            self.assertEqual([chapter.name for chapter in report.chapters], ["Chapter 1"])
            self.assertFalse(report.valid)
            self.assertTrue(
                any("Chapter 11" in error for error in report.errors),
                report.errors,
            )

    def test_relative_path_validation(self):
        self.assertEqual(
            normalize_upload_relative_path(
                r"Wireless Onahole\RAW\Chapter 1\001.png",
                strip_prefixes=("wireless-onahole", "wireless onahole", "raw"),
            ),
            "Chapter 1/001.png",
        )
        # A bare RAW prefix is always dropped.
        self.assertEqual(
            normalize_upload_relative_path("RAW/Chapter 1/001.png"),
            "Chapter 1/001.png",
        )
        for value in (
            "../escape.png",
            "/absolute.png",
            r"C:\absolute.png",
            "Chapter 1/../../escape.png",
        ):
            with self.subTest(value=value):
                with self.assertRaises(UnsafeRelativePath):
                    normalize_upload_relative_path(value)

    def test_zip_slip_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "upload.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.png", b"bad")
                output.writestr("Chapter 1/001.png", b"good")
            result = extract_upload_zip(archive, root / "staging")
            self.assertEqual(len(result.accepted), 1)
            self.assertEqual(result.rejected[0]["relative_path"], "../escape.png")
            self.assertFalse((root / "escape.png").exists())


class StorageTests(unittest.TestCase):
    def test_inventory_jobs_and_stage_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "RAW"
            _write_inventory(raw, {"Chapter 1": 2})
            series = _series("Test Quest", root)
            report = scan_inventory(
                raw,
                series_name=series.name,
                series_slug=series.slug,
                languages=series.languages,
                expected_chapter_count=1,
                expected_page_count=2,
            )
            storage = PipelineStorage(root / "pipeline.db")
            series_id = storage.upsert_inventory(report)
            self.assertEqual(series_id, "series:test-quest")
            chapter = storage.list_chapters(series_id)[0]
            job_id = storage.create_job(
                series_id=series_id,
                chapter_names=["Chapter 1"],
                stages=DEFAULT_STAGES,
            )
            storage.set_stage_state(
                job_id,
                chapter["id"],
                ChapterStage.OCR,
                status=StageStatus.RUNNING,
                increment_attempt=True,
            )
            storage.set_stage_state(
                job_id,
                chapter["id"],
                ChapterStage.OCR,
                status=StageStatus.COMPLETED,
                version_hash="abc",
                checkpoint={"pages": 2},
            )
            state = storage.get_stage_state(
                job_id, chapter["id"], ChapterStage.OCR
            )
            self.assertEqual(state["status"], StageStatus.COMPLETED.value)
            self.assertEqual(state["attempt"], 1)
            self.assertEqual(
                json.loads(state["checkpoint_json"])["pages"],
                2,
            )
            storage.update_job(job_id, status=JobStatus.RUNNING)
            storage.set_stage_state(
                job_id,
                chapter["id"],
                ChapterStage.VISION,
                status=StageStatus.RUNNING,
            )
            storage.recover_interrupted_jobs()
            self.assertEqual(
                storage.get_job(job_id)["status"],
                JobStatus.INTERRUPTED.value,
            )
            self.assertEqual(
                storage.get_stage_state(
                    job_id,
                    chapter["id"],
                    ChapterStage.VISION,
                )["status"],
                StageStatus.PENDING.value,
            )
            storage.upsert_translation_memory(
                series_id=series_id,
                source_language="ko",
                source_text="안녕 세상",
                target_text="你好，世界",
                context_hash="context-1",
                quality=0.99,
                review_status=ReviewStatus.CONFIRMED,
            )
            matches = storage.search_translation_memory(
                series_id=series_id,
                source_language="ko",
                query="안녕",
            )
            self.assertEqual(matches[0]["target_text"], "你好，世界")


class PipelineTests(unittest.TestCase):
    def _settings(self, root: Path) -> PipelineSettings:
        return PipelineSettings(
            series=_series(
                "Test Quest",
                root,
                expected_chapters=1,
                expected_pages=2,
            ),
            enable_embeddings=False,
        )

    def test_resume_skips_completed_stages_and_invalidates_translation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "RAW"
            _write_inventory(raw, {"Chapter 1": 2})
            settings = self._settings(root)
            runtime = FakeRuntime()
            storage = PipelineStorage(settings.database_path)
            service = ChapterPipelineService(settings, storage, runtime)
            job_id = service.create_job(
                root=raw,
                chapter_names=["Chapter 1"],
                stages=[
                    ChapterStage.OCR,
                    ChapterStage.VISION,
                    ChapterStage.KNOWLEDGE,
                    ChapterStage.TRANSLATE,
                    ChapterStage.INPAINT,
                    ChapterStage.RENDER,
                    ChapterStage.SUMMARIZE,
                ],
            )
            asyncio.run(service.run_job(job_id))
            self.assertEqual(
                storage.get_job(job_id)["status"],
                JobStatus.COMPLETED.value,
            )
            first_calls = runtime.calls.copy()
            self.assertTrue(all(first_calls[stage.value] == 1 for stage in DEFAULT_STAGES[:-1]))

            resumed_runtime = FakeRuntime()
            resumed_service = ChapterPipelineService(
                settings,
                PipelineStorage(settings.database_path),
                resumed_runtime,
            )
            asyncio.run(resumed_service.run_job(job_id))
            self.assertEqual(sum(resumed_runtime.calls.values()), 0)

            changed_runtime = FakeRuntime()
            changed_service = ChapterPipelineService(
                settings,
                PipelineStorage(settings.database_path),
                changed_runtime,
            )
            changed_job = changed_service.create_job(
                root=raw,
                chapter_names=["Chapter 1"],
                stages=[
                    ChapterStage.OCR,
                    ChapterStage.VISION,
                    ChapterStage.KNOWLEDGE,
                    ChapterStage.TRANSLATE,
                    ChapterStage.INPAINT,
                    ChapterStage.RENDER,
                    ChapterStage.SUMMARIZE,
                ],
                options={
                    "translation_provider": "online",
                    "models": {"translation": "online-model-v2"},
                },
            )
            asyncio.run(changed_service.run_job(changed_job))
            self.assertEqual(changed_runtime.calls[ChapterStage.OCR.value], 0)
            self.assertEqual(changed_runtime.calls[ChapterStage.VISION.value], 0)
            self.assertEqual(changed_runtime.calls[ChapterStage.KNOWLEDGE.value], 0)
            self.assertEqual(changed_runtime.calls[ChapterStage.TRANSLATE.value], 1)
            self.assertEqual(changed_runtime.calls[ChapterStage.INPAINT.value], 0)
            self.assertEqual(changed_runtime.calls[ChapterStage.RENDER.value], 1)

    def test_artifacts_are_versioned_and_manual_download_uses_original_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results"
            store = ArtifactStore(results)
            payload = {"regions": []}
            first = store.write_json(
                "Chapter 1",
                ChapterStage.OCR,
                "version-a",
                "Chapter 1/001.png",
                payload,
            )
            second = store.write_json(
                "Chapter 1",
                ChapterStage.OCR,
                "version-b",
                "Chapter 1/001.png",
                payload,
            )
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file() and second.is_file())

            rendered = results / "RAW" / "Chapter 1" / "001.png"
            rendered.parent.mkdir(parents=True, exist_ok=True)
            rendered.write_bytes(b"image")
            archive = store.build_download_zip(
                root / "download.zip",
                ["Chapter 1"],
            )
            with zipfile.ZipFile(archive) as output:
                self.assertEqual(output.namelist(), ["Chapter 1/001.png"])

    def test_ocr_runs_as_one_multi_chapter_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "RAW"
            _write_inventory(
                raw,
                {
                    "Chapter 1": 2,
                    "Chapter 2": 2,
                },
            )
            settings = PipelineSettings(
                series=_series(
                    "Batch Test",
                    root,
                    expected_chapters=2,
                    expected_pages=4,
                )
            )
            runtime = FakeRuntime()
            service = ChapterPipelineService(
                settings,
                PipelineStorage(settings.database_path),
                runtime,
            )
            job_id = service.create_job(
                root=raw,
                chapter_names=["Chapter 1", "Chapter 2"],
                stages=[ChapterStage.OCR],
            )
            asyncio.run(service.run_job(job_id))
            self.assertEqual(runtime.ocr_batch_calls, 1)

    def test_stage_invalidation_matches_model_change_scope(self):
        self.assertEqual(
            affected_stages(ChapterStage.TRANSLATE),
            (
                ChapterStage.TRANSLATE,
                ChapterStage.RENDER,
                ChapterStage.SUMMARIZE,
                ChapterStage.EMBED,
            ),
        )
        self.assertEqual(
            affected_stages(ChapterStage.INPAINT),
            (ChapterStage.INPAINT, ChapterStage.RENDER),
        )
        self.assertEqual(
            normalize_stages([ChapterStage.RENDER]),
            (
                ChapterStage.OCR,
                ChapterStage.TRANSLATE,
                ChapterStage.INPAINT,
                ChapterStage.RENDER,
            ),
        )


if __name__ == "__main__":
    unittest.main()
