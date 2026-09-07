import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402
from vntts_artifacts import write_story_index_document  # noqa: E402

from vntts.game_content_importer import (  # noqa: E402
    GameContentImportCancelled,
    ImporterAvailability,
)
from vntts.pregeneration_generation import (  # noqa: E402
    OfflineGenerationCancelled,
    OfflineGenerationProgress,
)
from vntts.pregeneration_pack import (  # noqa: E402
    OfflinePackPublisher,
    OfflinePreparationChanges,
    StoryAudioCoverage,
)
from vntts.pregeneration_queue import PregenerationQueueCancelled  # noqa: E402
from vntts.pregeneration_setup import (  # noqa: E402
    ContentDiscovery,
    PregenerationJobStore,
    PregenerationSetupError,
    _story_selections,
    discover_game_content,
    estimate_preparation,
    inspect_story_index,
)
from vntts.pregeneration_ui import OfflineAudioPreparationDialog  # noqa: E402
from vntts.pregeneration_voices import PregenerationVoiceCancelled  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.versioned_json import write_versioned_json  # noqa: E402


def write_story_index(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "game_version": "3.7",
            "language": "en",
            "source_audio_completion": "duration-seconds",
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
                "source_audio_duration_seconds": 1.0,
                "source_audio_completeness": "full",
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
    def test_story_records_are_grouped_in_one_pass(self):
        class CountingRecords(tuple):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        class Record(SimpleNamespace):
            def __init__(self, line_id, collection_id, status):
                super().__init__(
                    collection_id=collection_id,
                    line_id=line_id,
                    speakable=True,
                    source_audio_status=status,
                    voice_character=line_id,
                    speaker=line_id,
                )

            def __eq__(self, _other):
                raise AssertionError("story partitioning must not search by equality")

        collections = tuple(
            SimpleNamespace(
                collection_id=value,
                title=value.title(),
                kind="character-story",
                order=index,
            )
            for index, value in enumerate(("first", "second"), start=1)
        )
        records = CountingRecords(
            (
                Record("original", "first", "available"),
                Record("generated", "first", "absent"),
                Record("other", "second", "absent"),
            )
        )

        selections = _story_selections(
            SimpleNamespace(collections=collections, records=records)
        )

        self.assertEqual(records.iterations, 1)
        self.assertEqual(len(selections), 2)
        self.assertEqual(selections[0].original_audio_lines, 1)
        self.assertEqual(selections[0].generation_lines, 1)

    def test_legacy_chapter_records_are_grouped_in_one_pass(self):
        class CountingRecords(tuple):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        records = CountingRecords(
            (
                SimpleNamespace(
                    chapter="2",
                    speakable=True,
                    source_audio_status="absent",
                    voice_character="Rhiannon",
                    speaker="Rhiannon",
                    line_id="second",
                ),
                SimpleNamespace(
                    chapter="1",
                    speakable=True,
                    source_audio_status="available",
                    voice_character="Centurion",
                    speaker="Centurion",
                    line_id="first",
                ),
            )
        )

        selections = _story_selections(SimpleNamespace(collections=(), records=records))

        self.assertEqual(records.iterations, 1)
        self.assertEqual(
            [(selection.selection_id, selection.line_ids) for selection in selections],
            [("chapter:1", ("first",)), ("chapter:2", ("second",))],
        )

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

    def test_discovery_uses_configured_app_import_and_extractor_locations(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            configured = write_story_index(root / "configured")
            app_data = root / "LocalAppData" / "VisualNovelTextToSpeech"
            app_story = (
                app_data
                / "game-content"
                / "reverse1999"
                / "reverse1999"
                / "story-index.jsonl"
            )
            app_story.parent.mkdir(parents=True)
            app_story.write_bytes(configured.read_bytes())
            extractor = root / "extractor"
            extractor_story = extractor / "reverse1999" / "story-index.jsonl"
            extractor_story.parent.mkdir(parents=True)
            extractor_story.write_bytes(configured.read_bytes())

            with patch(
                "vntts.pregeneration_setup.get_local_data_directory",
                return_value=app_data,
            ):
                discovery = discover_game_content(
                    AppSettings(story_index=str(configured)),
                    environment={"R1999_EXTRACTOR_DATA": str(extractor)},
                )

        self.assertEqual(len(discovery.content), 3)
        self.assertEqual(discovery.errors, ())
        self.assertEqual(
            [value.provider_id for value in discovery.content],
            ["configured-story-index", "reverse1999", "reverse1999"],
        )

    def test_discovery_rejects_outdated_reverse1999_index_before_full_parse(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "story-index.jsonl"
            write_story_index_document(
                path,
                {
                    "game": "Reverse: 1999",
                    "language": "en",
                },
                [
                    {
                        "record_type": "line",
                        "line_id": "legacy:1",
                        "chapter": "1001",
                        "sequence": 1,
                        "speaker": "Rhiannon",
                        "text": "Legacy line.",
                        "kind": "dialogue",
                    }
                ],
            )
            with patch("vntts.pregeneration_setup.inspect_story_index") as inspect:
                discovery = discover_game_content(
                    AppSettings(story_index=str(path)),
                    environment={"R1999_EXTRACTOR_DATA": str(path.parent / "unused")},
                )

        self.assertEqual(discovery.content, ())
        self.assertIn("Outdated Reverse: 1999", discovery.errors[0])
        inspect.assert_not_called()

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

    def test_prepared_story_coverage_accumulates_across_jobs(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            main = store.create_or_resume(content, ("main-1",))
            rhiannon = store.create_or_resume(content, ("rhiannon",))

            self.assertEqual(
                store.story_statuses(content),
                {"main-1": "in_progress", "rhiannon": "in_progress"},
            )

            store.mark_prepared(main)
            self.assertEqual(store.prepared_story_ids(content), {"main-1"})
            self.assertEqual(
                store.story_statuses(content),
                {"main-1": "ready", "rhiannon": "in_progress"},
            )

            store.mark_prepared(rhiannon)
            self.assertEqual(store.prepared_story_ids(content), {"main-1", "rhiannon"})


class OfflineAudioPreparationDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_default_discovery_runs_after_the_window_opens(self):
        with TemporaryDirectory() as temporary_directory:
            content = inspect_story_index(
                write_story_index(Path(temporary_directory) / "content")
            )
            pool = ManualThreadPool()
            with patch(
                "vntts.pregeneration_ui.discover_game_content",
                return_value=ContentDiscovery((content,)),
            ) as discovery:
                dialog = OfflineAudioPreparationDialog(
                    AppSettings(),
                    job_store=PregenerationJobStore(Path(temporary_directory) / "jobs"),
                    thread_pool=pool,
                )

                self.assertFalse(discovery.called)
                dialog.show()
                self.application.processEvents()
                self.assertEqual(len(pool.tasks), 1)
                self.assertEqual(dialog.stories.count(), 0)
                self.assertTrue(dialog.discovery_panel.isVisible())
                self.assertFalse(dialog.selection_panel.isVisible())
                self.assertFalse(dialog.continue_button.isEnabled())
                self.assertTrue(dialog.cancel_button.isEnabled())
                self.assertEqual(dialog.discovery_progress.minimum(), 0)
                self.assertEqual(dialog.discovery_progress.maximum(), 0)

                pool.tasks.pop().run()
                self.application.processEvents()

                self.assertFalse(dialog.discovery_panel.isVisible())
                self.assertTrue(dialog.selection_panel.isVisible())
                self.assertTrue(dialog.continue_button.isEnabled())
                dialog.refresh_button.click()
                self.assertEqual(len(pool.tasks), 1)
                self.assertTrue(dialog.discovery_panel.isVisible())
                self.assertFalse(dialog.selection_panel.isVisible())
                self.assertFalse(dialog.continue_button.isEnabled())
                self.assertTrue(dialog.cancel_button.isEnabled())

                pool.tasks.pop().run()
                self.application.processEvents()

            self.assertEqual(dialog.stories.count(), 2)
            self.assertFalse(dialog.discovery_panel.isVisible())
            self.assertTrue(dialog.selection_panel.isVisible())
            self.assertTrue(dialog.refresh_button.isEnabled())
            dialog.close()
            dialog.deleteLater()

    def test_story_filters_preserve_selection_across_refresh_and_sources(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            other = replace(content, story_index_sha256="b" * 64, game="Other game")
            store = PregenerationJobStore(root / "jobs")
            store.mark_prepared(store.create_or_resume(content, ("rhiannon",)))
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content, other)),
                job_store=store,
            )
            self.addCleanup(dialog.deleteLater)
            dialog.story_search.setText("MAIN STORY")
            self.assertFalse(dialog.stories.item(0).isHidden())
            self.assertTrue(dialog.stories.item(1).isHidden())
            self.assertIn("1 hidden by filters", dialog.story_filter_status.text())
            dialog.select_all_button.click()
            self.assertEqual(dialog.selected_story_ids(), ("main-1", "rhiannon"))
            dialog.select_none_button.click()
            self.assertEqual(dialog.selected_story_ids(), ("rhiannon",))
            dialog.story_search.clear()
            dialog.story_filter.setCurrentIndex(
                dialog.story_filter.findData("in_progress")
            )
            self.assertTrue(dialog.stories.item(0).isHidden())
            self.assertFalse(dialog.stories.item(1).isHidden())
            dialog.select_none_button.click()
            self.assertEqual(dialog.selected_story_ids(), ())
            dialog.refresh_button.click()
            self.assertEqual(dialog.selected_story_ids(), ())
            self.assertFalse(dialog.continue_button.isEnabled())
            self.assertEqual(dialog.story_context.text(), "No stories selected.")
            dialog.source.setCurrentIndex(1)
            self.assertEqual(dialog.selected_story_ids(), ("main-1", "rhiannon"))
            self.assertIn("0 of 2 stories shown", dialog.story_filter_status.text())
            self.assertIn("2 hidden by filters", dialog.story_filter_status.text())
            dialog.source.setCurrentIndex(0)
            self.assertEqual(dialog.selected_story_ids(), ())
            dialog.story_filter.setCurrentIndex(
                dialog.story_filter.findData("not_started")
            )
            self.assertFalse(dialog.stories.item(0).isHidden())
            self.assertTrue(dialog.stories.item(1).isHidden())
            dialog.select_all_button.click()
            dialog.refresh_button.click()
            self.assertEqual(dialog.selected_story_ids(), ("main-1",))
            self.assertIn(
                "1 of 2 stories shown; 1 selected", dialog.story_filter_status.text()
            )
            dialog.story_search.setText("missing story")
            self.assertIn("Clear filters", dialog.story_filter_status.text())
            self.assertEqual(dialog.selected_story_ids(), ("main-1",))

    def test_unstarted_story_selection_survives_close_without_creating_jobs(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            other = replace(content, story_index_sha256="b" * 64, game="Other game")

            def reopen():
                dialog = OfflineAudioPreparationDialog(
                    AppSettings(),
                    discovery=lambda: ContentDiscovery((content, other)),
                    job_store=PregenerationJobStore(root / "jobs"),
                )
                self.addCleanup(dialog.deleteLater)
                dialog.show()
                return dialog

            first = reopen()
            first.select_none_button.click()
            first.source.setCurrentIndex(1)
            first.stories.item(1).setCheckState(Qt.CheckState.Unchecked)
            first.close()
            second = reopen()
            self.assertEqual(second.selected_story_ids(), ())
            self.assertFalse(second.continue_button.isEnabled())
            second.source.setCurrentIndex(1)
            self.assertEqual(second.selected_story_ids(), ("main-1",))
            self.assertEqual(second.job_store.jobs_for_content(content), ())
            self.assertEqual(second.job_store.story_statuses(other), {})
            second.close()

    def test_selection_save_failure_keeps_previous_draft_and_allows_retry(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            store.save_selection(content, ("rhiannon",))
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
            )
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            dialog.select_none_button.click()
            with patch.object(
                store, "save_selection", side_effect=PermissionError("read only")
            ):
                dialog._continue_requested()
                self.assertIsNone(dialog.job())
                self.assertIn("Unable to save", dialog.selection_status.text())
                with patch(
                    "vntts.pregeneration_ui.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Cancel,
                ):
                    dialog.close()
                self.assertTrue(dialog.isVisible())
                self.assertFalse(dialog.voice_panel._shutdown_requested)
                self.assertEqual(store.selection_for_content(content), ("rhiannon",))
            dialog.close()
            self.assertFalse(dialog.isVisible())
            self.assertEqual(store.selection_for_content(content), ())
            dialog.show()
            dialog.select_all_button.click()
            with (
                patch.object(
                    store, "save_selection", side_effect=PermissionError("read only")
                ),
                patch(
                    "vntts.pregeneration_ui.QMessageBox.question",
                    return_value=QMessageBox.StandardButton.Discard,
                ) as prompt,
            ):
                dialog.close()
            prompt.assert_called_once()
            self.assertFalse(dialog.isVisible())
            self.assertEqual(store.selection_for_content(content), ())

    def test_damaged_story_selection_is_visible_and_does_not_select_every_story(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            store.save_selection(content, ())
            path = store._selection_path(content)
            write_versioned_json(path, 2, {"selected_story_ids": ["rhiannon"]})
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
            )
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.selected_story_ids(), ())
            self.assertIn("Unable to restore", dialog.selection_status.text())
            dialog.close()
            with self.assertRaises(ValueError):
                store.selection_for_content(content)

    def test_story_audio_check_runs_in_background_and_discards_old_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                thread_pool=pool,
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stories.setCurrentRow(0)
            dialog.check_story_audio.click()
            self.assertEqual(len(pool.tasks), 1)
            self.assertIn("background", dialog.story_audio_status.text())
            dialog.stories.setCurrentRow(1)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertNotIn("Original game audio", dialog.story_audio_status.text())
            dialog.stories.setCurrentRow(0)
            dialog.check_story_audio.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("Main Story 1", dialog.story_audio_status.text())
            self.assertIn(
                "Original game audio (indexed): 1", dialog.story_audio_status.text()
            )
            self.assertIn("not prepared: 1", dialog.story_audio_status.text())
            self.assertIn("No saved preparation pack", dialog.story_audio_status.text())
            self.assertEqual(dialog.selected_story_ids(), ("main-1", "rhiannon"))

    def test_checked_story_readiness_updates_list_and_primary_without_preparing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            jobs.mark_prepared(jobs.create_or_resume(content, ("main-1",)))
            pool = ManualThreadPool()
            active_path = root / "active-game-pack.json"
            dialog = OfflineAudioPreparationDialog(
                AppSettings(
                    game_pack=str(active_path), audio_source_policy="prefer-game-audio"
                ),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                thread_pool=pool,
            )
            self.addCleanup(dialog.deleteLater)
            self.assertIn("Partially prepared", dialog.stories.item(0).text())
            self.assertEqual(dialog.continue_button.text(), "Continue preparation")
            dialog.stories.setCurrentRow(0)
            ready = StoryAudioCoverage(
                "Main Story 1", active_path, original=1, generated=1
            )
            with patch(
                "vntts.pregeneration_ui.inspect_story_audio", return_value=ready
            ) as inspect:
                self.assertEqual(
                    dialog._inspect_story_audio(content, "main-1", str(active_path)),
                    (ready, ready, ("Centurion", "Rhiannon")),
                )
                inspect.assert_called_once_with(
                    content, "main-1", jobs, manifest=str(active_path)
                )
            live = replace(ready, generated=0, live=1)
            opened = Mock()
            dialog.readingRequested.connect(opened)
            with patch.object(
                dialog,
                "_inspect_story_audio",
                return_value=(ready, live, ("Centurion", "Rhiannon")),
            ):
                dialog.check_story_audio.click()
                pool.tasks.pop().run()
                self.application.processEvents()
            self.assertIn("Partially prepared", dialog.stories.item(0).text())
            self.assertIn("1 live speech", dialog.stories.item(0).text())
            self.assertIn("active in Reading", dialog.stories.item(0).text())
            self.assertEqual(dialog.continue_button.text(), "Continue preparation")
            with (
                patch.object(
                    dialog,
                    "_inspect_story_audio",
                    return_value=(ready, ready, ("Centurion", "Rhiannon")),
                ),
                patch.object(
                    dialog, "_generation_engine_available", return_value=False
                ),
            ):
                dialog.check_story_audio.click()
                pool.tasks.pop().run()
                self.application.processEvents()
                self.assertEqual(
                    dialog.stories.item(0).data(Qt.ItemDataRole.UserRole + 2), "ready"
                )
                self.assertEqual(dialog.continue_button.text(), "Start reading")
                self.assertTrue(dialog.continue_button.isEnabled())
                dialog.change_voices.setChecked(True)
                self.assertEqual(dialog.continue_button.text(), "Continue preparation")
                dialog.change_voices.setChecked(False)
                self.assertEqual(dialog.continue_button.text(), "Start reading")
                dialog.continue_button.click()
                opened.assert_called_once_with()
                self.assertEqual(pool.tasks, [])
            with patch.object(
                dialog,
                "_inspect_story_audio",
                side_effect=ValueError("Saved audio damaged. Prepare again."),
            ):
                dialog.check_story_audio.click()
                pool.tasks.pop().run()
                self.application.processEvents()
            self.assertIn("Needs attention", dialog.stories.item(0).text())
            self.assertIn("Saved audio damaged", dialog.story_audio_status.text())
            self.assertEqual(dialog.continue_button.text(), "Continue preparation")
            dialog.refresh()
            self.assertIn("check readiness", dialog.stories.item(0).text())

    def test_active_preparation_and_shared_progress_report_saved_counts_and_failure(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                thread_pool=pool,
            )
            self.addCleanup(dialog.deleteLater)
            self.assertEqual(dialog.continue_button.text(), "Prepare")
            updates = []
            dialog.phaseChanged.connect(updates.append)
            dialog.continue_button.click()
            self.assertTrue(
                all("Preparing" in dialog.stories.item(row).text() for row in range(2))
            )
            dialog._generation_input = SimpleNamespace(ready_items=3)
            dialog._render_generation_progress(
                OfflineGenerationProgress(generated=1, failed=1)
            )
            self.assertIn("2 of 3 lines processed and saved", updates[-1])
            dialog._voice_plan_finished(
                None, ValueError("No character references. Import the game again.")
            )
            self.application.processEvents()
            self.assertIn("Needs attention", dialog.stories.item(0).text())
            self.assertIn("No character references", updates[-1])
            self.assertEqual(dialog.continue_button.text(), "Continue preparation")
            dialog.voice_runner.cancel()

    def test_scoped_regeneration_keeps_story_scope_and_reuses_voice_decisions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            jobs.mark_prepared(jobs.create_or_resume(content, ("main-1",)))
            jobs.mark_prepared(jobs.create_or_resume(content, ("rhiannon",)))
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(
                    game_pack=str(root / "active.json"),
                    audio_source_policy="prefer-game-audio",
                ),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                thread_pool=pool,
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stories.item(0).setCheckState(Qt.CheckState.Checked)
            dialog.stories.item(1).setCheckState(Qt.CheckState.Unchecked)
            ready = StoryAudioCoverage(
                "Main Story 1", root / "active.json", original=1, generated=1
            )
            dialog._story_audio_checks[dialog._story_audio_key("main-1")] = (
                ready,
                ready,
                None,
            )
            dialog._refresh_story_statuses()
            dialog._selection_changed()
            self.assertEqual(dialog.continue_button.text(), "Start reading")
            self.assertTrue(dialog.prepare_again.isVisibleTo(dialog))
            self.assertIn("recorded voice and model", dialog.summary.text())
            self.assertEqual(
                dialog.prepare_again.toolTip(), "Prepare again: Main Story 1"
            )
            reading = Mock()
            dialog.readingRequested.connect(reading)
            with patch.object(
                dialog,
                "_create_voice_plan",
                side_effect=PregenerationVoiceCancelled("Paused after scope check"),
            ) as prepare:
                dialog.prepare_again.click()
                pool.tasks.pop().run()
                self.application.processEvents()
                self.assertEqual(
                    prepare.call_args.args[0].selected_story_ids, ("main-1",)
                )
                self.assertFalse(prepare.call_args.args[1])
            reading.assert_not_called()
            self.assertIn("rhiannon", jobs.prepared_story_ids(content))

    def test_checked_pack_keeps_artifact_evidence_but_respects_live_reading_overrides(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            settings = AppSettings(
                game_pack=str(root / "active.json"),
                audio_source_policy="prefer-game-audio",
            )
            dialog = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            self.addCleanup(dialog.deleteLater)
            dialog.stories.item(1).setCheckState(Qt.CheckState.Unchecked)
            dialog.stories.setCurrentRow(0)
            ready = StoryAudioCoverage(
                "Main Story 1", root / "active.json", original=1, generated=1
            )
            key = dialog._story_audio_key("main-1")
            dialog._story_audio_checks[key] = (ready, ready, None)
            dialog._story_playback_speakers[key] = ("Centurion", "Narrator")
            for changed, blocked in (
                ({"audio_source_policy": "live-tts-only"}, True),
                ({"audio_source_policy": "prefer-generated"}, True),
                ({"speech_rate_percent": 120}, True),
                ({"voice_assignments": {"Centurion": "preset:alba"}}, True),
                (
                    {
                        "voice_assignments": {"Narrator": "preset:alba"},
                        "force_live_narrator": True,
                    },
                    True,
                ),
                (
                    {
                        "voice_assignments": {"Narrator": "preset:alba"},
                        "force_live_narrator": False,
                    },
                    False,
                ),
                ({"voice_assignments": {"Unrelated": "preset:alba"}}, False),
                ({"character_voice_defaults": {"Centurion": "preset:alba"}}, False),
            ):
                with self.subTest(changed=changed):
                    dialog.settings = settings.updated(**changed)
                    dialog._refresh_story_statuses()
                    dialog._selection_changed()
                    dialog._story_audio_changed()
                    self.assertEqual(dialog._can_start_reading(), not blocked)
                    self.assertEqual(
                        dialog.stories.item(0).data(Qt.ItemDataRole.UserRole + 2),
                        "attention" if blocked else "ready",
                    )
                    if blocked:
                        self.assertNotIn(
                            "No live speech needed", dialog.story_audio_status.text()
                        )
                        self.assertIn(
                            "Reading needs attention", dialog.story_audio_status.text()
                        )
                    self.assertIs(dialog._story_audio_checks[key][1], ready)

    def test_changed_character_default_prepares_new_inputs_only_for_selected_story(
        self,
    ):
        from tests.test_pregeneration_voices import write_manifest

        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            jobs.mark_prepared(jobs.create_or_resume(content, ("main-1",)))
            settings = AppSettings(
                voice_manifest=str(write_manifest(root / "voices")),
                character_voice_defaults={"Rhiannon": "preset:alba"},
            )
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=jobs,
                thread_pool=pool,
            )
            self.addCleanup(dialog.deleteLater)
            dialog.prepare_again.click()
            for _ in range(2):
                pool.tasks.pop(0).run()
                self.application.processEvents()
            self.assertTrue(dialog._awaiting_voice_confirmation)
            first = dialog.generation_input()
            self.assertEqual(dialog.voice_plan().groups[0].source_id, "preset:alba")
            other = jobs.create_or_resume(content, ("rhiannon",))
            other_plan = dialog.voice_plan_store.create(other, settings)
            other_input = dialog.input_store.materialize(other, other_plan)
            other_bytes = other_input.voice_manifest.read_bytes()
            dialog.apply_narrator_settings(
                settings.updated(character_voice_defaults={"Rhiannon": "preset:jean"})
            )
            dialog.prepare_again.click()
            for _ in range(2):
                pool.tasks.pop(0).run()
                self.application.processEvents()
            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertEqual(dialog.selected_story_ids(), ("main-1",))
            self.assertEqual(dialog.voice_plan().audition_count, 0)
            self.assertEqual(dialog.voice_plan().groups[0].source_id, "preset:jean")
            self.assertNotEqual(dialog.generation_input().identity, first.identity)
            self.assertTrue(first.directory.is_dir())
            self.assertEqual(other_input.voice_manifest.read_bytes(), other_bytes)

    def test_saving_unchanged_voice_settings_keeps_current_confirmation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            settings = AppSettings(last_main_section="stories")
            dialog = OfflineAudioPreparationDialog(
                settings,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            self.addCleanup(dialog.deleteLater)
            plan = SimpleNamespace(
                synthesis_backend="pocket-tts", synthesis_model="pocket-tts"
            )
            prepared_input = object()
            dialog._voice_plan = plan
            dialog._generation_input = prepared_input
            dialog._awaiting_voice_confirmation = True
            dialog.voice_confirmation.show()
            dialog.selection_panel.hide()
            dialog.apply_narrator_settings(settings.updated(last_main_section="voices"))
            self.assertIs(dialog.voice_plan(), plan)
            self.assertIs(dialog.generation_input(), prepared_input)
            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertFalse(dialog.voice_confirmation.isHidden())
            self.assertTrue(dialog.selection_panel.isHidden())
            self.assertEqual(dialog.selected_story_ids(), ("main-1", "rhiannon"))

    def test_saved_settings_refresh_story_readiness_without_changing_active_job_snapshot(
        self,
    ):
        from vntts.app import TrayApplication

        with TemporaryDirectory() as directory:
            root = Path(directory)
            content = inspect_story_index(write_story_index(root / "content"))
            original = AppSettings(
                game_pack=str(root / "active.json"),
                audio_source_policy="prefer-game-audio",
                last_main_section="stories",
            )
            preparation = OfflineAudioPreparationDialog(
                original,
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
            )
            self.addCleanup(preparation.deleteLater)
            preparation.stories.item(1).setCheckState(Qt.CheckState.Unchecked)
            preparation.stories.setCurrentRow(0)
            ready = StoryAudioCoverage(
                "Main Story 1", root / "active.json", original=1, generated=1
            )
            preparation._story_audio_checks[preparation._story_audio_key("main-1")] = (
                ready,
                ready,
                None,
            )
            preparation._refresh_story_statuses()
            preparation._selection_changed()
            self.assertTrue(preparation._can_start_reading())
            controller = Mock(settings=original, is_ready=False, is_live_running=False)
            tray = TrayApplication(
                self.application,
                original,
                controller_factory=Mock(return_value=controller),
            )
            tray.pregeneration_dialog = preparation
            pool = ManualThreadPool()
            tray.configuration_runner.thread_pool = pool
            candidate = original.updated(audio_source_policy="live-tts-only")
            settings_dialog = Mock()
            settings_dialog.exec.return_value = QDialog.DialogCode.Accepted
            settings_dialog.settings.return_value = candidate
            with (
                patch.dict(
                    os.environ, {"VNTTS_SETTINGS_FILE": str(root / "settings.json")}
                ),
                patch("vntts.app.SettingsDialog", return_value=settings_dialog),
                patch(
                    "vntts.configuration_apply.apply_game_pack", return_value=candidate
                ),
                patch.object(tray, "start_hotkeys"),
            ):
                tray.open_settings()
                self.assertIn(
                    '"audio_source_policy": "live-tts-only"',
                    (root / "settings.json").read_text(),
                )
                self.assertEqual(
                    preparation.settings.audio_source_policy, "live-tts-only"
                )
                self.assertFalse(preparation._can_start_reading())
                preparation._story_audio_changed()
                self.assertIn(
                    "Reading uses live speech only",
                    preparation.story_audio_status.text(),
                )
                pool.tasks.pop().run()
                self.application.processEvents()
                plan = SimpleNamespace(
                    synthesis_backend="pocket-tts", synthesis_model=None
                )
                preparation._voice_plan = plan
                preparation._awaiting_voice_confirmation = True
                tray.settings = preparation.settings.updated(last_main_section="voices")
                tray._refresh_preparation_settings()
                self.assertIs(preparation.voice_plan(), plan)
                self.assertTrue(preparation._awaiting_voice_confirmation)
                snapshot = preparation.settings
                preparation.generating = True
                tray.settings = tray.settings.updated(speech_rate_percent=120)
                tray._refresh_preparation_settings()
                self.assertIs(preparation.settings, snapshot)
                preparation.generating = False
                self.assertIs(tray.open_pregeneration(), preparation)
                self.assertEqual(preparation.settings.speech_rate_percent, 120)
                tray.pregeneration_dialog = None
                tray.shutdown()

    def test_reopen_prefers_saved_full_source_over_active_one_story_pack(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_path = write_story_index(root / "full-source")
            source = inspect_story_index(source_path)
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(source, ("rhiannon",))
            published = (
                jobs.path_for(job.job_id).parent
                / "game-packs"
                / f"pack-{'1' * 24}"
                / "game-pack.json"
            )
            published.parent.mkdir(parents=True)
            published.write_text("{}", encoding="utf-8")
            pack_path = root / "active-pack" / "story-index.jsonl"
            write_story_index_document(
                pack_path,
                {
                    "game": "Reverse: 1999",
                    "language": "en",
                    "collections": [
                        {
                            "collection_id": "rhiannon",
                            "title": "Rhiannon",
                            "kind": "character-story",
                            "order": 2,
                        }
                    ],
                },
                [
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
                    }
                ],
            )
            pool = ManualThreadPool()
            app_data = root / "app-data"
            with (
                patch.dict(
                    os.environ,
                    {"R1999_EXTRACTOR_DATA": str(root / "unused-extractor")},
                ),
                patch(
                    "vntts.pregeneration_setup.get_local_data_directory",
                    return_value=app_data,
                ),
            ):
                dialog = OfflineAudioPreparationDialog(
                    AppSettings(story_index=str(pack_path)),
                    job_store=jobs,
                    thread_pool=pool,
                )
                dialog.show()
                self.application.processEvents()
                pool.tasks.pop().run()
                self.application.processEvents()

            self.assertEqual(dialog.source.count(), 2)
            self.assertEqual(
                dialog.current_content().story_index, source_path.resolve()
            )
            self.assertIn("2 stories", dialog.source.currentText())
            self.assertEqual(dialog.stories.count(), 2)
            self.assertIn("Not prepared", dialog.stories.item(0).text())
            self.assertIn(
                "Partially prepared - saved audio; check readiness",
                dialog.stories.item(1).text(),
            )
            dialog.close()
            dialog.deleteLater()

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
            publisher = Mock(wraps=OfflinePackPublisher())
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
            self.assertTrue(dialog._awaiting_voice_confirmation)
            self.assertIn("new lines", dialog.change_summary.text())
            generator.generate.assert_not_called()
            dialog.continue_button.click()
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

    def test_app_import_and_story_selection_restore_after_restart(self):
        with TemporaryDirectory() as temporary_directory:
            app_data = (
                Path(temporary_directory) / "LocalAppData" / "VisualNovelTextToSpeech"
            )
            write_story_index(app_data / "game-content" / "reverse1999" / "reverse1999")
            jobs = PregenerationJobStore(app_data / "pregeneration" / "jobs")
            with patch(
                "vntts.pregeneration_setup.get_local_data_directory",
                return_value=app_data,
            ):
                environment = {"R1999_EXTRACTOR_DATA": str(app_data / "unused")}
                content = discover_game_content(
                    AppSettings(), environment=environment
                ).content[0]
                jobs.create_or_resume(content, ("rhiannon",))
                dialog = OfflineAudioPreparationDialog(
                    AppSettings(),
                    discovery=lambda: discover_game_content(
                        AppSettings(), environment=environment
                    ),
                    job_store=jobs,
                )

            self.assertEqual(dialog.source.count(), 1)
            self.assertEqual(dialog.selected_story_ids(), ("rhiannon",))
            self.assertIn("Partially prepared", dialog.stories.item(1).text())
            dialog.deleteLater()

    def test_progress_card_polls_durable_counts_during_slow_generation(self):
        with TemporaryDirectory() as temporary_directory:
            content = inspect_story_index(
                write_story_index(Path(temporary_directory) / "content")
            )
            generator = Mock()
            generator.inspect_progress.side_effect = (
                OfflineGenerationProgress(
                    generated=1,
                    active_phase="generating",
                    runtime_status="GPU: RTX 2070 SUPER; auxiliary: CPU",
                ),
                OfflineGenerationProgress(
                    generated=2,
                    failed=1,
                    active_phase="validating",
                ),
            )
            pool = ManualThreadPool()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                generator=generator,
                thread_pool=pool,
            )
            dialog._generation_input = Mock(ready_items=4)
            dialog.generating = True
            dialog.show()

            dialog._start_generation_progress()
            generator.inspect_progress.assert_not_called()
            dialog._poll_generation_progress()
            self.assertEqual(len(pool.tasks), 1)
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(dialog.progress_bar.value(), 1)
            self.assertIn("1 of 4", dialog.progress_counts.text())
            self.assertIn("saved on disk", dialog.progress_guarantee.text())
            self.assertIn("RTX 2070 SUPER", dialog.progress_runtime.text())
            self.assertTrue(dialog.progress_runtime.isVisible())

            dialog._poll_generation_progress()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertEqual(dialog.progress_bar.value(), 3)
            self.assertEqual(dialog.progress_phase.text(), "Checking generated audio")
            self.assertNotIn("RTX 2070 SUPER", dialog.progress_runtime.text())
            self.assertIn("confirm CPU/GPU", dialog.progress_runtime.text())
            self.assertIn("2 prepared, 1 failed", dialog.progress_counts.text())
            self.assertIn("automatic recovery", dialog.progress_failures.text())
            self.assertIn(
                "only unfinished lines", dialog.progress_cancel_consequence.text()
            )
            dialog.generating = False
            dialog.progress_timer.stop()
            dialog.close()
            dialog.deleteLater()

    def test_progress_eta_errors_and_stale_results_do_not_mislead(self):
        with (
            TemporaryDirectory() as directory,
            patch("vntts.pregeneration_ui.monotonic") as clock,
        ):
            clock.return_value = 0
            content = inspect_story_index(
                write_story_index(Path(directory) / "content")
            )
            pool = ManualThreadPool()
            generator = Mock()
            generator.inspect_progress.side_effect = (
                OfflineGenerationProgress(generated=50),
                OfflineGenerationProgress(generated=52),
                OSError("cannot read progress"),
                OfflineGenerationProgress(available=False),
                OfflineGenerationProgress(generated=60),
            )
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                generator=generator,
                thread_pool=pool,
            )
            dialog._generation_input = Mock(ready_items=60)
            dialog.generating = True
            dialog._start_generation_progress()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("Estimating", dialog.progress_timing.text())
            clock.return_value = 60
            dialog._poll_generation_progress()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("4 min", dialog.progress_timing.text())
            dialog._poll_generation_progress()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("Progress unavailable", dialog.progress_timing.text())
            self.assertEqual(dialog.progress_bar.value(), 52)
            dialog._poll_generation_progress()
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertIn("Waiting for progress", dialog.progress_timing.text())
            self.assertEqual(dialog.progress_bar.value(), 52)
            dialog._poll_generation_progress()
            dialog._stop_generation_progress()
            dialog.generating = False
            pool.tasks.pop().run()
            self.application.processEvents()
            self.assertEqual(dialog.progress_bar.value(), 52)
            self.assertEqual(dialog.progress_timing.text(), "")
            dialog.deleteLater()

    def test_reopening_restores_the_last_story_selection(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            store = PregenerationJobStore(root / "jobs")
            store.create_or_resume(content, ("main-1",))
            store.mark_prepared(store.create_or_resume(content, ("rhiannon",)))

            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=store,
            )

            self.assertEqual(dialog.selected_story_ids(), ("rhiannon",))
            self.assertIn(
                "2 partially prepared, 0 need attention, 0 not prepared",
                dialog.coverage_summary.text(),
            )
            self.assertIn("Saved offline audio found", dialog.resume_status.text())
            self.assertIn(
                "Partially prepared - saved audio; check readiness",
                dialog.stories.item(1).text(),
            )
            self.assertIn("Partially prepared", dialog.stories.item(0).text())
            dialog.deleteLater()

    def test_failed_first_pass_runs_automatic_recovery_before_accepting(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            pool = ManualThreadPool()
            first = Mock(generated=1, failed=2, other_terminal=0)
            final = Mock(generated=2, failed=0, other_terminal=1)
            recovery_result = Mock(
                generation=final,
                recovered=1,
                live_fallbacks=1,
                remaining_failed=0,
            )
            generator = Mock()
            generator.generate.return_value = first
            recovery = Mock()
            recovery.recover.return_value = recovery_result
            acceptance_result = Mock(generation=final, approved=2)
            acceptance = Mock()
            acceptance.accept.return_value = acceptance_result
            pack_result = Mock(
                approved=2,
                live_fallbacks=1,
                story_lines=3,
                omissions=0,
            )
            publisher = Mock(wraps=OfflinePackPublisher())
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
            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()

            self.assertTrue(dialog.recovering)
            self.assertEqual(dialog.cancel_button.text(), "Cancel automatic recovery")
            self.assertIn("2 unfinished lines", dialog.resume_status.text())
            self.assertEqual(dialog.progress_phase.text(), "Recovering failed lines")
            self.assertIn("2 failed items", dialog.progress_failures.text())
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
            self.assertEqual(dialog.job().status, "prepared")
            self.assertTrue(
                all(
                    "Partially prepared - saved audio; check readiness"
                    in dialog.stories.item(row).text()
                    for row in range(dialog.stories.count())
                )
            )
            self.assertIn(
                "2 partially prepared, 0 need attention, 0 not prepared",
                dialog.coverage_summary.text(),
            )
            self.assertTrue(dialog.selection_panel.isHidden())
            self.assertIn("Step 4", dialog.step.text())
            self.assertEqual(
                dialog.progress_phase.text(),
                "Ready with live speech for remaining lines",
            )
            self.assertIn("1 original-game-audio", dialog.progress_coverage.text())
            self.assertIn("2 prepared lines", dialog.progress_coverage.text())
            self.assertIn("1 live fallback", dialog.progress_coverage.text())
            self.assertEqual(dialog.continue_button.text(), "Use prepared audio")
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            dialog.continue_button.click()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            dialog.deleteLater()

    def test_remaining_safe_repair_keeps_preparation_incomplete(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_story_index(root / "content"))
            acceptance = Mock()
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                acceptance=acceptance,
            )
            dialog._generation_input = Mock(ready_items=3)
            dialog.recovering = True
            generation = Mock(generated=2, failed=1, other_terminal=0)
            result = Mock(
                generation=generation,
                recovered=1,
                live_fallbacks=0,
                remaining_failed=1,
            )

            dialog._recovery_finished(result, None)

            self.assertEqual(dialog.progress_phase.text(), "Automatic recovery paused")
            self.assertIn("not complete", dialog.resume_status.text())
            self.assertEqual(dialog.progress_bar.value(), 2)
            self.assertFalse(dialog.accepting_audio)
            acceptance.accept.assert_not_called()
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

    def test_voice_matching_uses_candidates_prepared_by_game_importer(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(
                write_story_index(root / "content"),
                provider_id="reverse1999",
            )
            job = PregenerationJobStore(root / "jobs").create_or_resume(
                content,
                tuple(selection.selection_id for selection in content.selections),
            )
            manifest = root / "candidate-manifest.json"
            importer = Mock()
            importer.availability.return_value = ImporterAvailability(True, "Ready")
            importer.prepare_voice_candidates.return_value = manifest
            voice_plan = Mock()
            voice_plan_store = Mock()
            voice_plan_store.create.return_value = voice_plan
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                importer=importer,
                job_store=PregenerationJobStore(root / "other-jobs"),
                voice_plan_store=voice_plan_store,
            )

            effective_settings = AppSettings(
                speech_backend="pocket-tts", tts_profile="default"
            )
            with (
                patch(
                    "vntts.pregeneration_ui.find_default_voice_manifest",
                    return_value=None,
                ),
                patch(
                    "vntts.pregeneration_ui.resolve_pregeneration_settings",
                    return_value=effective_settings,
                ),
            ):
                result = dialog._create_voice_plan(job)

            self.assertIs(result, voice_plan)
            importer.prepare_voice_candidates.assert_called_once_with(
                job,
                dialog.voice_cancel_event,
                progress=dialog.decoderProgress.emit,
            )
            self.assertEqual(
                voice_plan_store.create.call_args.kwargs["manifest_path"],
                manifest,
            )
            self.assertIs(voice_plan_store.create.call_args.args[1], effective_settings)
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
            dialog.continue_button.click()
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
            publisher = Mock()
            publisher.inspect_changes.return_value = OfflinePreparationChanges(
                0, 2, 0, 0, 0, 0, 0, 0
            )
            dialog = OfflineAudioPreparationDialog(
                AppSettings(),
                discovery=lambda: ContentDiscovery((content,)),
                job_store=PregenerationJobStore(root / "jobs"),
                voice_plan_store=voice_plan_store,
                input_store=input_store,
                generator=generator,
                publisher=publisher,
                thread_pool=pool,
            )

            dialog.continue_button.click()
            pool.tasks.pop().run()
            self.application.processEvents()
            pool.tasks.pop().run()
            self.application.processEvents()
            dialog.continue_button.click()
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
