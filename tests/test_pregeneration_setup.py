import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from vntts_artifacts import write_story_index_document  # noqa: E402

from vntts.game_content_importer import (  # noqa: E402
    GameContentImportCancelled,
    ImporterAvailability,
)
from vntts.pregeneration_generation import OfflineGenerationCancelled  # noqa: E402
from vntts.pregeneration_queue import PregenerationQueueCancelled  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    PregenerationSetupError,
    discover_game_content,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import PregenerationVoiceCancelled  # noqa: E402
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


class ManualThreadPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


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
            pool = ManualThreadPool()
            generation_result = Mock(generated=2, failed=0)
            generator = Mock()
            generator.generate.return_value = generation_result
            acceptance_result = Mock(generation=generation_result, approved=2)
            acceptance = Mock()
            acceptance.accept.return_value = acceptance_result
            pack_result = Mock()
            publisher = Mock()
            publisher.publish.return_value = pack_result
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
                generator=generator,
                acceptance=acceptance,
                publisher=publisher,
                thread_pool=pool,
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
            self.assertTrue(dialog.planning_voices)
            self.assertFalse(dialog.stories.isEnabled())
            self.assertEqual(dialog.cancel_button.text(), "Cancel voice matching")
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.planning_voices)
            self.assertTrue(dialog.preparing_inputs)
            self.assertEqual(dialog.cancel_button.text(), "Cancel preparation")
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.preparing_inputs)
            self.assertTrue(dialog.generating)
            self.assertEqual(dialog.cancel_button.text(), "Cancel generation")
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(dialog.accepting_audio)
            self.assertEqual(dialog.cancel_button.text(), "Cancel final checks")
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(dialog.publishing_pack)
            self.assertEqual(dialog.cancel_button.text(), "Cancel final save")
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertIsNotNone(dialog.job())
            self.assertIsNotNone(dialog.voice_plan())
            self.assertIsNotNone(dialog.generation_input())
            self.assertIs(dialog.generation_result(), generation_result)
            self.assertFalse(dialog.planning_voices)
            self.assertFalse(dialog.preparing_inputs)
            self.assertFalse(dialog.generating)
            self.assertFalse(dialog.accepting_audio)
            self.assertFalse(dialog.publishing_pack)
            self.assertIs(dialog.pack_result(), pack_result)
            self.assertTrue(store.path_for(dialog.job().job_id).is_file())
            self.assertTrue(
                (
                    store.path_for(dialog.job().job_id).parent / "voice-plan.json"
                ).is_file()
            )
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

    def test_failed_first_pass_runs_automatic_recovery_before_accepting(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            first = Mock(generated=1, failed=2)
            final = Mock(generated=2, failed=1)
            recovery_result = Mock(generation=final, recovered=1)
            generator = Mock()
            generator.generate.return_value = first
            recovery = Mock()
            recovery.recover.return_value = recovery_result
            acceptance_result = Mock(generation=final, approved=2)
            acceptance = Mock()
            acceptance.accept.return_value = acceptance_result
            pack_result = Mock()
            publisher = Mock()
            publisher.publish.return_value = pack_result
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                generator=generator,
                recovery=recovery,
                acceptance=acceptance,
                publisher=publisher,
                thread_pool=pool,
            )

            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            pool.tasks.pop().run()
            self.application.processEvents()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(dialog.recovering)
            self.assertEqual(dialog.cancel_button.text(), "Cancel automatic recovery")
            self.assertIn("2 unfinished lines", dialog.resume_status.text())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.recovering)
            self.assertTrue(dialog.accepting_audio)
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.accepting_audio)
            self.assertTrue(dialog.publishing_pack)
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.publishing_pack)
            self.assertIs(dialog.generation_result(), final)
            self.assertIs(dialog.recovery_result(), recovery_result)
            self.assertIs(dialog.pack_result(), pack_result)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
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

            with patch(
                "vntts.pregeneration_ui.QFileDialog.getExistingDirectory",
                return_value="/selected/game",
            ):
                dialog.game_folder_button.click()
            self.assertTrue(dialog.importing)
            self.assertFalse(dialog.source.isEnabled())
            self.assertEqual(dialog.cancel_button.text(), "Cancel import")
            self.assertEqual(len(pool.tasks), 1)

            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(
                importer.import_installed.call_args.args[1],
                "/selected/game",
            )
            self.assertFalse(dialog.importing)
            self.assertEqual(dialog.source.count(), 1)
            self.assertEqual(dialog.stories.count(), 2)
            self.assertIn("successfully", dialog.source_status.text())
            dialog.deleteLater()

    def test_import_cancel_waits_for_worker_terminal_result(self):
        importer = Mock()
        importer.availability.return_value = ImporterAvailability(True, "Ready")

        def import_installed(cancel_event, installation_root):
            self.assertIsNone(installation_root)
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

    def test_voice_matching_cancel_waits_for_worker_and_closes_cleanly(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            voice_plan_store = Mock()

            def create(_job, _settings, *, cancellation, ignore_decisions=False):
                self.assertFalse(ignore_decisions)
                self.assertTrue(cancellation.is_set())
                raise PregenerationVoiceCancelled("cancelled")

            voice_plan_store.create.side_effect = create
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_plan_store=voice_plan_store,
                thread_pool=pool,
            )

            dialog.continue_button.click()
            dialog.cancel_button.click()

            self.assertTrue(dialog.planning_voices)
            self.assertIn("Cancelling voice", dialog.resume_status.text())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.planning_voices)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            dialog.deleteLater()

    def test_generation_input_cancel_waits_for_its_worker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            voice_plan = Mock()
            voice_plan_store = Mock()
            voice_plan_store.create.return_value = voice_plan
            input_store = Mock()

            def materialize(_job, selected_plan, *, cancellation):
                self.assertIs(selected_plan, voice_plan)
                self.assertTrue(cancellation.is_set())
                raise PregenerationQueueCancelled("cancelled")

            input_store.materialize.side_effect = materialize
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_plan_store=voice_plan_store,
                input_store=input_store,
                thread_pool=pool,
            )

            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(dialog.preparing_inputs)

            dialog.cancel_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.preparing_inputs)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            dialog.deleteLater()

    def test_change_saved_voices_reopens_decisions_during_initial_planning(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            voice_plan = Mock(audition_count=0)
            voice_plan_store = Mock()
            voice_plan_store.create.return_value = voice_plan
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_plan_store=voice_plan_store,
                input_store=Mock(),
                thread_pool=pool,
            )

            dialog.change_voices.setChecked(True)
            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(
                voice_plan_store.create.call_args.kwargs["ignore_decisions"]
            )
            self.assertFalse(dialog.selection_panel.isVisible())
            dialog.deleteLater()

    def test_generation_cancel_terminates_before_dialog_closes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            voice_plan = Mock()
            voice_plan_store = Mock()
            voice_plan_store.create.return_value = voice_plan
            generation_input = Mock(ready_items=2)
            input_store = Mock()
            input_store.materialize.return_value = generation_input
            generator = Mock()

            def generate(selected_input, selected_plan, cancellation):
                self.assertIs(selected_input, generation_input)
                self.assertIs(selected_plan, voice_plan)
                self.assertTrue(cancellation.is_set())
                raise OfflineGenerationCancelled("cancelled")

            generator.generate.side_effect = generate
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_plan_store=voice_plan_store,
                input_store=input_store,
                generator=generator,
                thread_pool=pool,
            )

            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertTrue(dialog.generating)

            dialog.cancel_button.click()
            self.assertIn("Cancelling generation", dialog.resume_status.text())
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertFalse(dialog.generating)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
