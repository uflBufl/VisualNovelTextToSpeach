import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from vntts_artifacts import write_story_index_document  # noqa: E402

from vntts.game_content_importer import (  # noqa: E402
    GameContentImportCancelled,
    ImporterAvailability,
)
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    PregenerationSetupError,
    discover_game_content,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


def write_story_index(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "game_version": "3.7",
            "language": "en",
            "collections": [
                {
                    "collection_id": "main-1",
                    "title": "Main Story 1",
                    "kind": "main-story",
                    "order": 1,
                },
                {
                    "collection_id": "rhiannon",
                    "title": "Rhiannon",
                    "kind": "character-story",
                    "order": 2,
                },
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": "reverse1999:1",
                "chapter": "1",
                "sequence": 1,
                "speaker": "Centurion",
                "voice_character": "Centurion",
                "text": "Original game voice.",
                "kind": "dialogue",
                "collection_id": "main-1",
                "source_audio_status": "available",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:2",
                "chapter": "1",
                "sequence": 2,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "Generate me.",
                "kind": "dialogue",
                "collection_id": "main-1",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:3",
                "chapter": "2",
                "sequence": 1,
                "speaker": "Aderyn",
                "voice_character": "Rhiannon child",
                "text": "A child line.",
                "kind": "dialogue",
                "collection_id": "rhiannon",
                "source_audio_status": "absent",
                "speakable": True,
            },
        ],
    )
    return path


class PregenerationSetupTest(unittest.TestCase):
    def test_story_content_reports_player_level_collection_coverage(self):
        with TemporaryDirectory() as temporary_directory:
            path = write_story_index(Path(temporary_directory))

            content = inspect_story_index(path, provider_id="reverse1999")

        self.assertEqual(content.display_name, "Reverse: 1999 3.7")
        self.assertEqual(len(content.selections), 2)
        main = content.selections[0]
        self.assertEqual(main.title, "Main Story 1")
        self.assertEqual(main.line_count, 2)
        self.assertEqual(main.original_audio_lines, 1)
        self.assertEqual(main.generation_lines, 1)
        self.assertEqual(main.speakers, ("Rhiannon",))

    def test_discovery_uses_bounded_configured_and_extractor_locations(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            configured = write_story_index(root / "configured")
            extractor = root / "extractor"
            extractor_story = extractor / "reverse1999" / "story-index.jsonl"
            extractor_story.parent.mkdir(parents=True)
            extractor_story.write_bytes(configured.read_bytes())

            discovery = discover_game_content(
                AppSettings(story_index=str(configured)),
                environment={"R1999_EXTRACTOR_DATA": str(extractor)},
            )

        self.assertEqual(len(discovery.content), 2)
        self.assertEqual(discovery.errors, ())
        self.assertEqual(
            {value.provider_id for value in discovery.content},
            {"configured-story-index", "reverse1999"},
        )

    def test_preparation_estimate_and_job_are_checksum_bound_and_resumable(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(
                root / "jobs",
                clock=lambda: datetime(2026, 8, 31, tzinfo=timezone.utc),
            )

            estimate = estimate_preparation(content, ("main-1", "rhiannon"))
            first = store.create_or_resume(content, ("rhiannon", "main-1"))
            second = store.create_or_resume(content, ("main-1", "rhiannon"))

            self.assertEqual(estimate.selected_lines, 3)
            self.assertEqual(estimate.original_audio_lines, 1)
            self.assertEqual(estimate.generation_lines, 2)
            self.assertEqual(estimate.speaker_count, 2)
            self.assertEqual(first, second)
            self.assertEqual(first.selected_story_ids, ("main-1", "rhiannon"))
            self.assertEqual(
                first.selected_line_ids,
                ("reverse1999:1", "reverse1999:2", "reverse1999:3"),
            )
            self.assertEqual(store.latest_for_content(content), first)
            self.assertTrue(store.path_for(first.job_id).is_file())

    def test_empty_and_unknown_selections_fail_before_writing(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")

            with self.assertRaisesRegex(PregenerationSetupError, "at least one"):
                store.create_or_resume(content, ())
            with self.assertRaisesRegex(PregenerationSetupError, "Unknown story"):
                store.create_or_resume(content, ("missing",))

            self.assertFalse((root / "jobs").exists())


class OfflineAudioPreparationDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_default_path_selects_content_and_saves_resumable_player_choice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
            )
            dialog.show()
            self.application.processEvents()

            self.assertEqual(dialog.stories.count(), 2)
            self.assertTrue(
                all(
                    dialog.stories.item(row).checkState() == Qt.CheckState.Checked
                    for row in range(dialog.stories.count())
                )
            )
            self.assertIn("3 dialogue lines selected", dialog.summary.text())
            self.assertNotIn("manifest", dialog.summary.text().casefold())
            self.assertNotIn("queue", dialog.summary.text().casefold())

            dialog.continue_button.click()

            self.assertIsNotNone(dialog.job())
            self.assertTrue(store.path_for(dialog.job().job_id).is_file())
            dialog.deleteLater()

    def test_reopening_restores_the_last_story_selection(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            store.create_or_resume(content, ("rhiannon",))

            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
            )

            self.assertEqual(dialog.selected_story_ids(), ("rhiannon",))
            self.assertIn("Previous selection restored", dialog.resume_status.text())
            dialog.deleteLater()

    def test_missing_content_has_one_plain_recovery_action(self):
        dialog = OfflineAudioPreparationDialog(
            AppSettings(),
            discovery=lambda: ContentDiscovery((), ("Importer is not installed",)),
            job_store=PregenerationJobStore(Path("unused")),
        )

        self.assertIn("No extracted game content", dialog.source_status.text())
        self.assertIn("Importer is not installed", dialog.source_status.text())
        self.assertFalse(dialog.continue_button.isEnabled())
        self.assertTrue(dialog.browse_button.isEnabled())
        dialog.deleteLater()

    def test_installed_game_import_runs_off_ui_thread_and_adds_content(self):
        class ManualThreadPool:
            def __init__(self):
                self.tasks = []

            def start(self, task):
                self.tasks.append(task)

        with TemporaryDirectory() as temporary_directory:
            content = inspect_story_index(
                write_story_index(Path(temporary_directory) / "content")
            )
            importer = Mock()
            importer.availability.return_value = ImporterAvailability(True, "Ready")
            importer.import_installed.return_value = content
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery(()),
                job_store=PregenerationJobStore(Path(temporary_directory) / "jobs"),
                importer=importer,
                thread_pool=pool,
            )

            dialog.import_button.click()
            self.assertTrue(dialog.importing)
            self.assertFalse(dialog.source.isEnabled())
            self.assertEqual(dialog.cancel_button.text(), "Cancel import")
            self.assertEqual(len(pool.tasks), 1)

            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.importing)
            self.assertEqual(dialog.source.count(), 1)
            self.assertEqual(dialog.stories.count(), 2)
            self.assertIn("successfully", dialog.source_status.text())
            dialog.deleteLater()

    def test_import_cancel_waits_for_worker_terminal_result(self):
        class ManualThreadPool:
            def __init__(self):
                self.tasks = []

            def start(self, task):
                self.tasks.append(task)

        importer = Mock()
        importer.availability.return_value = ImporterAvailability(True, "Ready")

        def import_installed(cancel_event):
            if cancel_event.is_set():
                raise GameContentImportCancelled("cancelled")
            raise AssertionError("cancel event was not delivered")

        importer.import_installed.side_effect = import_installed
        pool = ManualThreadPool()
        dialog = OfflineAudioPreparationDialog(
            AppSettings(),
            discovery=lambda: ContentDiscovery(()),
            importer=importer,
            thread_pool=pool,
        )

        dialog.import_button.click()
        dialog.cancel_button.click()

        self.assertTrue(dialog.importing)
        self.assertIn("Cancelling", dialog.source_status.text())
        pool.tasks.pop().run()
        self.application.processEvents()

        self.assertFalse(dialog.importing)
        self.assertEqual(dialog.source_status.text(), "Game import cancelled.")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
