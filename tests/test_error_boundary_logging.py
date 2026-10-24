"""Caught storage and discovery failures remain available to Support export."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.story_fixtures import write_story_index
from vntts.controller import AppController
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    PregenerationSetupError,
    _load_story_catalog,
    _save_story_catalog,
    discover_game_content,
    inspect_story_index,
)
from vntts.settings import AppSettings


class ErrorBoundaryLoggingTest(unittest.TestCase):
    def test_discovery_logs_rejected_sources_and_keeps_other_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            valid = write_story_index(root / "valid")
            invalid = root / "invalid.jsonl"
            invalid.write_text("invalid JSON", encoding="utf-8")
            with (
                patch(
                    "vntts.pregeneration_setup.get_local_data_directory",
                    return_value=root,
                ),
                self.assertLogs("vntts.pregeneration_setup", level="ERROR") as logged,
            ):
                result = discover_game_content(
                    AppSettings(story_index=str(invalid)),
                    environment={"R1999_EXTRACTOR_DATA": str(root / "extractor")},
                    extra_paths=(valid,),
                )
            self.assertEqual(
                tuple(value.story_index for value in result.content), (valid,)
            )
            self.assertEqual(len(result.errors), 1)
            self.assertIn(str(invalid), logged.output[0])
            self.assertIsNotNone(logged.records[0].exc_info)
            self.assertEqual(len(logged.records), 1)

    def test_inspection_logs_unselectable_content_before_propagating_failure(self):
        with TemporaryDirectory() as directory:
            path = write_story_index(Path(directory).resolve())
            with (
                patch("vntts.pregeneration_setup._story_selections", return_value=()),
                self.assertLogs("vntts.pregeneration_setup", level="ERROR") as logged,
                self.assertRaisesRegex(
                    PregenerationSetupError, "no selectable dialogue"
                ),
            ):
                inspect_story_index(path)
            self.assertIn(str(path), logged.output[0])
            self.assertIsInstance(
                logged.records[0].exc_info[1], PregenerationSetupError
            )

    def test_optional_missing_sources_are_quiet_but_configured_missing_is_logged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            environment = {"R1999_EXTRACTOR_DATA": str(root / "extractor")}
            with patch(
                "vntts.pregeneration_setup.get_local_data_directory", return_value=root
            ):
                with self.assertNoLogs("vntts.pregeneration_setup"):
                    self.assertEqual(
                        discover_game_content(
                            AppSettings(), environment=environment
                        ).content,
                        (),
                    )
                with self.assertLogs(
                    "vntts.pregeneration_setup", level="ERROR"
                ) as logged:
                    result = discover_game_content(
                        AppSettings(story_index=str(root / "missing.jsonl")),
                        environment=environment,
                    )
            self.assertEqual(result.content, ())
            self.assertIn("missing.jsonl", logged.output[0])

    def test_cache_miss_is_quiet_and_cache_damage_and_write_failure_are_logged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            content = inspect_story_index(write_story_index(root / "content"))
            catalog = root / "catalog.sqlite3"
            arguments = (
                content.story_index,
                content.story_index_sha256,
                content.provider_id,
                "a" * 64,
            )
            with patch(
                "vntts.pregeneration_setup._story_catalog_path", return_value=catalog
            ):
                with self.assertNoLogs("vntts.pregeneration_setup"):
                    self.assertIsNone(_load_story_catalog(*arguments))
                catalog.write_bytes(b"damaged sqlite database")
                with self.assertLogs(
                    "vntts.pregeneration_setup", level="WARNING"
                ) as logged:
                    self.assertIsNone(_load_story_catalog(*arguments))
                self.assertIn(str(catalog), logged.output[0])
                self.assertIsNotNone(logged.records[0].exc_info)
                with (
                    patch(
                        "vntts.pregeneration_setup.StoryCatalog.save",
                        side_effect=PermissionError("read only"),
                    ),
                    self.assertLogs(
                        "vntts.pregeneration_setup", level="WARNING"
                    ) as logged,
                ):
                    _save_story_catalog(content, "a" * 64)
                self.assertIn(str(content.story_index), logged.output[0])
                self.assertIsInstance(logged.records[0].exc_info[1], PermissionError)
            self.assertEqual(catalog.read_bytes(), b"damaged sqlite database")

    def test_legacy_catalog_failure_is_logged_without_migration_or_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            original = root / "legacy-job" / "job.json"
            original.parent.mkdir()
            original.write_bytes(b"legacy preparation")
            store = PregenerationJobStore(root)
            with (
                self.assertLogs("vntts.pregeneration_setup", level="ERROR") as logged,
                self.assertRaises(PregenerationSetupError),
            ):
                store.source_story_indexes()
            self.assertIn(str(store.catalog.path), logged.output[0])
            self.assertEqual(original.read_bytes(), b"legacy preparation")
            self.assertFalse(store.catalog.path.exists())

    def test_controller_corpus_failures_are_logged_without_losing_readiness_error(self):
        controller = AppController.__new__(AppController)
        controller.settings = AppSettings()
        with self.assertNoLogs("vntts.controller"):
            controller._load_live_speaker_corpus()
        controller.settings = AppSettings(
            live_speaker_corpus="/unavailable/corpus.json"
        )
        failure = OSError("corpus is unreadable")
        with (
            patch("vntts.controller.LiveSpeakerCorpus.load", side_effect=failure),
            self.assertLogs("vntts.controller", level="ERROR") as logged,
        ):
            controller._load_live_speaker_corpus()
        self.assertEqual(controller.live_speaker_corpus_error, str(failure))
        self.assertIs(logged.records[0].exc_info[1], failure)
        controller.live_speaker_corpus = Mock()
        controller.live_speaker_corpus.revalidate.side_effect = failure
        with self.assertLogs("vntts.controller", level="ERROR") as logged:
            self.assertFalse(controller._revalidate_live_speaker_corpus())
        self.assertIs(logged.records[0].exc_info[1], failure)
