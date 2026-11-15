"""Reading owns its selected progress source after preparation is closed."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from tests.error_logging_fixtures import isolated_application_error_log  # noqa: E402
from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from tests.story_fixtures import write_synthetic_game_pack  # noqa: E402
from vntts.app import TrayApplication  # noqa: E402
from vntts.game_pack import import_game_pack  # noqa: E402
from vntts.settings import AppSettings, load_app_settings  # noqa: E402


class PreparationPanel(QDialog):
    readingLineObserved = Signal(str, str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.current_settings = settings
        self.current_job = SimpleNamespace(identity=settings.generated_audio_manifest)
        self.generating = True
        self.recovering = False
        self.publishing_pack = False
        self.pending_work = True
        self.requests = []
        self.observations = []
        self.publisher = SimpleNamespace(base_pack=None)
        self.reading_status = QLabel(self)
        self.readingLineObserved.connect(
            lambda line_id, text_hash: self.observations.append((line_id, text_hash))
        )

    def runtime_playback_settings(self):
        self.isEnabled()
        return self.current_settings

    def has_pending_work(self):
        return self.pending_work

    def set_reading_status(self, message):
        self.reading_status.setText(message)

    def apply_playback_settings(self, settings):
        self.playback_settings = settings

    def apply_narrator_settings(self, settings):
        self.settings = settings

    def job(self):
        return self.current_job

    def prioritize_line(self, line_id, text_hash):
        self.isEnabled()
        self.requests.append((line_id, text_hash))
        return True


class PreparationReadingLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(isolated_application_error_log())
        self.root = Path(directory.name)
        self.enterContext(
            patch.dict(
                os.environ,
                {"VNTTS_SETTINGS_FILE": str(self.root / "settings.json")},
            )
        )
        self.enterContext(patch("vntts.app.check_audio_runtime"))
        self.trays = []
        self.panels = []
        self.addCleanup(self._cleanup_qt)

    def _cleanup_qt(self):
        for tray in self.trays:
            tray.shutdown()
        self.assertTrue(QThreadPool.globalInstance().waitForDone(3000))
        self.application.processEvents()
        objects = [*self.panels]
        for tray in self.trays:
            objects.extend(
                value for value in vars(tray).values() if isinstance(value, QWidget)
            )
        objects.extend(self.trays)
        for obj in objects:
            if isValid(obj):
                obj.deleteLater()
                QCoreApplication.sendPostedEvents(obj, QEvent.Type.DeferredDelete)

    def _progress_settings(self, name, saved):
        pack, *_ = write_synthetic_game_pack(self.root / name)
        return (
            import_game_pack(pack)
            .apply_to(saved)
            .updated(
                game_pack=None,
                audio_source_policy="prefer-game-audio",
                force_live_narrator=False,
            )
        )

    def _tray(self):
        saved = AppSettings(onboarding_completed=True, last_main_section="reading")
        controller = Mock(
            settings=saved,
            is_ready=True,
            is_live_running=False,
            is_one_shot_read_running=False,
        )
        controller.runtime_control_snapshot.return_value = {}

        def apply(settings, **_kwargs):
            controller.settings = settings
            return True

        controller.apply_settings.side_effect = apply
        tray = TrayApplication(
            self.application, saved, controller_factory=Mock(return_value=controller)
        )
        self.trays.append(tray)
        tray.speech_runtime_timer.stop()
        panel = self._panel(tray, self._progress_settings("first", saved))
        return tray, controller, panel, saved

    def _panel(self, tray, settings):
        panel = PreparationPanel(settings)
        self.panels.append(panel)
        tray.pregeneration_dialog = panel
        tray.dashboard.embed_preparation(panel, show=False)
        return panel

    def _read(self, tray, controller):
        pool = ManualThreadPool()
        tray.initial_start_runner.thread_pool = pool
        with (
            patch.object(tray, "toggle_live") as start,
            patch.object(tray, "schedule_hotkeys"),
        ):
            tray._read_prepared_story(True)
            self.assertLessEqual(len(pool.tasks), 1)
            if pool.tasks:
                pool.run_next()
                self.application.processEvents()
        self.assertEqual(pool.tasks, [])
        start.assert_called_once_with()
        controller.is_live_running = True

    def _close(self, tray, panel):
        self.assertIsNone(tray._pregeneration_finished(QDialog.DialogCode.Rejected))
        QCoreApplication.sendPostedEvents(panel, QEvent.Type.DeferredDelete)
        self.application.processEvents()
        self.assertFalse(isValid(panel))
        self.assertIsNone(tray.pregeneration_dialog)

    def _setup_during_generation(self):
        tray, controller, panel, saved = self._tray()
        saved = saved.updated(
            onboarding_completed=False,
            speech_rate_percent=125,
            force_live_narrator=True,
            audio_source_policy="prefer-generated",
        )
        tray.settings = controller.settings = saved
        tray._controller_ready = controller.is_ready = False
        controller.unresolved_live_speakers.return_value = ()
        controller.start.side_effect = lambda: (
            setattr(controller, "is_ready", True) or True
        )
        controller.test_current_dialog.return_value = ("Narrator", "Ready dialogue.")
        controller.last_audio_source_description = "prepared"
        tray.dashboard.show_stories()
        tray._read_prepared_story(True)
        wizard = tray.onboarding_wizard
        self.assertIsNotNone(wizard)
        self.assertTrue(wizard.isVisible())
        self.assertIn("setup", panel.reading_status.text())
        self.assertTrue(panel.generating)
        pool = ManualThreadPool()
        tray.onboarding_test_runner.thread_pool = pool
        tray.configuration_runner.thread_pool = pool
        wizard.show_page(len(wizard.pages) - 1)
        wizard.test_page.run_test()
        pool.run_next()
        self.application.processEvents()
        self.assertTrue(wizard.test_page.successful, wizard.test_page.status.text())
        self.assertEqual(
            controller.settings.generated_audio_manifest,
            panel.current_settings.generated_audio_manifest,
        )
        self.assertEqual(controller.settings.speech_rate_percent, 100)
        controller.start.assert_not_called()
        return tray, controller, panel, saved, wizard, pool

    def test_setup_preserves_progress_source_without_saving_playback_overrides(self):
        tray, controller, panel, saved, wizard, pool = self._setup_during_generation()
        tray.initial_start_runner.thread_pool = pool
        with patch.object(tray, "schedule_hotkeys"):
            wizard.accept()
            self.application.processEvents()
            pool.run_next()
            self.application.processEvents()
        self.assertIsNone(tray.onboarding_wizard)
        self.assertTrue(panel.generating)
        self.assertTrue(panel.has_pending_work())
        self.assertEqual(panel.settings.speech_rate_percent, 100)
        self.assertTrue(tray.settings.onboarding_completed)
        persisted = load_app_settings(self.root / "settings.json", environment={})
        for name in ("speech_rate_percent", "force_live_narrator", "audio_source_policy"):
            self.assertEqual(getattr(persisted, name), getattr(saved, name))
        self.assertIsNone(persisted.generated_audio_manifest)
        self.assertEqual(
            tray._runtime_settings().generated_audio_manifest,
            panel.current_settings.generated_audio_manifest,
        )
        self.assertIn("Select Read", panel.reading_status.text())
        controller.toggle_live.assert_not_called()
        with (
            patch.object(tray, "schedule_hotkeys"),
            patch.object(tray, "_start_live_with_available_scope", return_value=True) as start,
        ):
            tray.toggle_live()
        start.assert_called_once_with()
        self.assertEqual(
            controller.settings.generated_audio_manifest,
            panel.current_settings.generated_audio_manifest,
        )
        self.assertTrue(panel.generating)

    def test_cancelled_or_unsaved_setup_restores_runtime_and_leaves_generation_running(self):
        for outcome in ("cancel", "save-error"):
            with self.subTest(outcome=outcome):
                tray, controller, panel, saved, wizard, pool = self._setup_during_generation()
                with patch.object(
                    tray, "_save_settings_candidate", side_effect=OSError("disk full")
                ):
                    if outcome == "cancel":
                        wizard.reject()
                    else:
                        wizard.accept()
                    self.application.processEvents()
                self.assertIsNone(tray._onboarding_reading_settings)
                self.assertFalse(tray.settings.onboarding_completed)
                self.assertIn("Select Read", panel.reading_status.text())
                pool.run_next()
                self.application.processEvents()
                self.assertEqual(controller.settings, saved)
                self.assertTrue(panel.generating)
                controller.toggle_live.assert_not_called()
                tray.shutdown()

    def test_setup_tracks_finished_story_order_and_keeps_the_same_generation_source(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                tray, controller, panel, _saved, wizard, pool = self._setup_during_generation()
                sequence = self.root / "finished-sequence.json"
                sequence.write_text("{}")
                panel.current_settings = panel.current_settings.updated(
                    live_sequence_plan=str(sequence), live_sequence_mode="audio-auto"
                )
                panel.generating = panel.pending_work = not completed
                tray.initial_start_runner.thread_pool = pool
                with patch.object(tray, "schedule_hotkeys"):
                    wizard.accept()
                    self.application.processEvents()
                    pool.run_next()
                    self.application.processEvents()
                self.assertEqual(controller.settings.live_sequence_plan, str(sequence))
                self.assertEqual(tray._runtime_settings().live_sequence_plan, str(sequence))
                self.assertEqual(panel.has_pending_work(), not completed)
                controller.speech_backend.progress_line_observed("line", "hash")
                self.application.processEvents()
                self.assertIn(("line", "hash"), panel.observations)
                tray.shutdown()

    def test_setup_does_not_switch_to_another_preparation_opened_before_save(self):
        tray, controller, panel, saved, wizard, pool = self._setup_during_generation()
        selected = controller.settings.generated_audio_manifest
        panel.current_settings = self._progress_settings("another", saved)
        tray.initial_start_runner.thread_pool = pool
        with patch.object(tray, "schedule_hotkeys"):
            wizard.accept()
            self.application.processEvents()
            pool.run_next()
            self.application.processEvents()
        self.assertEqual(controller.settings.generated_audio_manifest, selected)
        self.assertEqual(tray._runtime_settings().generated_audio_manifest, selected)
        controller.speech_backend.progress_line_observed("old", "hash")
        self.application.processEvents()
        self.assertEqual(panel.observations, [])

    def test_setup_from_reading_is_available_during_generation_and_defers_activation(self):
        tray, _controller, panel, _saved = self._tray()
        tray.settings = tray.settings.updated(onboarding_completed=False, game_window_title="Game")
        tray.dashboard.set_configuration(tray.settings)
        tray.set_ready(False)
        tray.dashboard.prepare_reading_button.click()
        self.assertIsNotNone(tray.onboarding_wizard)
        panel.defer_activation = Mock()
        tray._activate_ready_preparation()
        panel.defer_activation.assert_called_once()
        self.assertTrue(panel.generating)
        tray.onboarding_wizard.reject()

    def test_stories_read_explains_when_another_setup_is_already_open(self):
        tray, _controller, panel, _saved = self._tray()
        tray.settings = tray.settings.updated(onboarding_completed=False)
        tray.run_onboarding()
        wizard = tray.onboarding_wizard
        tray._read_prepared_story(True)
        self.assertIs(tray.onboarding_wizard, wizard)
        self.assertIsNone(tray._onboarding_reading_settings)
        self.assertIn("Finish or cancel", panel.reading_status.text())
        wizard.reject()

    def test_stories_read_explains_runtime_and_source_blockers_locally(self):
        for blocker, reason in (
            ("loading", "updating"),
            ("audio", "audio missing"),
            ("source", "not ready"),
        ):
            with self.subTest(blocker=blocker):
                tray, controller, panel, _saved = self._tray()
                if blocker == "loading":
                    tray._controller_busy = True
                elif blocker == "source":
                    panel.current_settings = None
                with patch(
                    "vntts.app.check_audio_runtime",
                    side_effect=RuntimeError("audio missing") if blocker == "audio" else None,
                ):
                    tray._read_prepared_story(True)
                self.assertIn(reason, panel.reading_status.text())
                controller.toggle_live.assert_not_called()
                self.assertTrue(panel.generating)
                tray._controller_busy = False
                tray.shutdown()

    def test_failed_reading_initialization_is_reported_beside_stories_read(self):
        tray, controller, panel, _saved = self._tray()
        tray._controller_ready = controller.is_ready = False
        controller.start.return_value = False
        pool = ManualThreadPool()
        tray.initial_start_runner.thread_pool = pool
        tray._read_prepared_story(True)
        pool.run_next()
        self.application.processEvents()
        self.assertIn("Unable to initialize reading", panel.reading_status.text())
        self.assertTrue(panel.generating)
        controller.toggle_live.assert_not_called()

    def test_close_during_speech_pause_or_audio_wait_keeps_reading_source(self):
        for snapshot in ({"speaking": True}, {"paused": True}, {"queued": True}):
            with self.subTest(snapshot=snapshot):
                tray, controller, panel, saved = self._tray()
                self._read(tray, controller)
                selected = controller.settings
                controller.runtime_control_snapshot.return_value = snapshot
                before = tray._runtime_control_state()
                controller.apply_settings.reset_mock()
                controller.start.reset_mock()
                with (
                    patch.object(tray, "_start_configuration_apply") as reconfigure,
                    patch.object(tray, "_save_settings_candidate") as save,
                    patch.object(tray, "_save_prepared_playback_state") as persist,
                ):
                    self._close(tray, panel)
                reconfigure.assert_not_called()
                save.assert_not_called()
                persist.assert_not_called()
                controller.apply_settings.assert_not_called()
                controller.start.assert_not_called()
                controller.stop_live.assert_not_called()
                controller.emergency_stop.assert_not_called()
                self.assertEqual(controller.settings, selected)
                self.assertEqual(tray._runtime_settings(), selected)
                self.assertEqual(tray._runtime_control_state(), before)
                self.assertEqual(tray.settings, saved)

    def test_progress_callbacks_are_safe_after_real_qt_deletion(self):
        tray, controller, panel, _saved = self._tray()
        self._read(tray, controller)
        wait = controller.speech_backend.progress_wait_request
        observed = controller.speech_backend.progress_line_observed
        description = controller.speech_backend.progress_wait_description
        self.assertIsNone(description())
        wait("line", "hash")
        observed("line", "hash")
        self.application.processEvents()
        self.assertEqual(panel.requests, [("line", "hash")])
        self.assertEqual(panel.observations, [("line", "hash")])
        wait("closing", "hash")
        observed("closing", "hash")
        self._close(tray, panel)
        wait("late", "hash")
        observed("late", "hash")
        self.application.processEvents()
        self.assertEqual(panel.requests, [("line", "hash")])
        self.assertEqual(panel.observations, [("line", "hash")])
        self.assertIn("Preparation is stopped", description())

    def test_stop_then_start_reuses_selected_source_after_close(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        self._close(tray, panel)

        def stop():
            controller.is_live_running = False
            controller.runtime_control_snapshot.return_value = {}

        controller.emergency_stop.side_effect = stop
        with patch.object(tray.live_stop_runner, "start") as run:
            self.assertFalse(tray.toggle_live())
            worker, *arguments = run.call_args.args
            self.assertTrue(worker(*arguments))
            tray._live_stop_finished(True, None)
        controller.apply_settings.reset_mock()
        candidate, available = tray._reading_source_choice()
        self.assertTrue(available)
        self.assertEqual(candidate, saved)
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertTrue(tray._runtime_control_state().ready)
        with patch.object(
            tray, "_start_live_with_available_scope", return_value=True
        ) as start:
            self.assertTrue(tray.toggle_live())
        start.assert_called_once_with()
        controller.apply_settings.assert_not_called()
        self.assertEqual(controller.settings, selected)

    def test_opening_different_preparation_does_not_hijack_selected_source(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        self._close(tray, panel)
        new_panel = self._panel(tray, self._progress_settings("second", saved))
        controller.apply_settings.reset_mock()
        candidate, available = tray._reading_source_choice()
        self.assertTrue(available)
        self.assertEqual(candidate, saved)
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertTrue(tray._runtime_control_state().ready)
        controller.apply_settings.assert_not_called()
        controller.is_live_running = False
        self._read(tray, controller)
        self.assertEqual(controller.settings, new_panel.current_settings)
        self.assertEqual(tray._runtime_settings(), new_panel.current_settings)
        self.assertEqual(tray.settings, saved)

    def test_replaced_job_in_same_panel_does_not_receive_old_callbacks(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        wait = controller.speech_backend.progress_wait_request
        observed = controller.speech_backend.progress_line_observed
        panel.current_settings = self._progress_settings("second", saved)
        panel.current_job = SimpleNamespace(
            identity=panel.current_settings.generated_audio_manifest
        )
        tray._preparation_activity_changed(True)
        self.assertIn(
            "Preparation is stopped",
            controller.speech_backend.progress_wait_description(),
        )
        wait("old-line", "hash")
        observed("old-line", "hash")
        self.application.processEvents()
        self.assertEqual(panel.requests, [])
        self.assertEqual(panel.observations, [])
        candidate, available = tray._reading_source_choice()
        self.assertTrue(available)
        self.assertEqual(candidate, saved)
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertTrue(tray._runtime_control_state().ready)

    def test_reopening_selected_job_reconnects_callbacks_without_handoff(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        wait = controller.speech_backend.progress_wait_request
        observed = controller.speech_backend.progress_line_observed
        self._close(tray, panel)
        reopened = self._panel(tray, selected)
        tray._preparation_activity_changed(True)
        controller.apply_settings.reset_mock()
        wait("resumed", "hash")
        observed("resumed", "hash")
        self.application.processEvents()
        self.assertEqual(reopened.requests, [("resumed", "hash")])
        self.assertEqual(reopened.observations, [("resumed", "hash")])
        self.assertIsNone(controller.speech_backend.progress_wait_description())
        controller.apply_settings.assert_not_called()
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertEqual(tray.settings, saved)

    def test_playback_preference_changes_keep_transient_source(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        self._close(tray, panel)
        for update in (
            {"output_volume_percent": 37},
            {"audio_source_policy": "live-tts-only"},
        ):
            with self.subTest(update=update):
                tray.settings = saved.updated(**update)
                self.assertEqual(tray._runtime_settings(), selected.updated(**update))
                self.assertTrue(tray._runtime_control_state().ready)

    def test_failed_handoff_keeps_previous_selected_source(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        self._close(tray, panel)
        self._panel(tray, self._progress_settings("second", saved))
        controller.apply_settings.side_effect = lambda *_args, **_kwargs: False
        controller.is_live_running = False
        pool = ManualThreadPool()
        tray.initial_start_runner.thread_pool = pool
        with (
            patch.object(tray, "toggle_live") as start,
            patch.object(tray, "show_error") as error,
        ):
            tray._read_prepared_story(True)
            self.assertEqual(len(pool.tasks), 1)
            pool.run_next()
            self.application.processEvents()
        start.assert_not_called()
        error.assert_called_once()
        self.assertEqual(controller.settings, selected)
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertFalse(tray._runtime_control_state().ready)
        self.assertIn(
            "Unable to restore previous speech settings", tray.last_controller_error
        )
        self.assertEqual(tray.settings, saved)

    def test_terminal_preparation_activity_keeps_source_and_reports_stopped(self):
        tray, controller, panel, _saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        panel.generating = False
        panel.pending_work = False
        tray._preparation_activity_changed(False)
        self.assertIn(
            "Preparation is stopped",
            controller.speech_backend.progress_wait_description(),
        )
        controller.speech_backend.progress_wait_request("missing", "hash")
        self.application.processEvents()
        self.assertEqual(panel.requests, [])
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertTrue(tray._runtime_control_state().ready)

    def test_source_or_profile_change_releases_transient_selection(self):
        for update in (
            {"active_profile_id": "other-profile"},
            {"story_index": "other-story.jsonl"},
        ):
            with self.subTest(update=update):
                tray, controller, panel, saved = self._tray()
                self._read(tray, controller)
                self._close(tray, panel)
                changed = saved.updated(**update)
                tray.settings = changed
                self.assertEqual(tray._runtime_settings(), changed)
                self.assertFalse(tray._runtime_control_state().ready)


if __name__ == "__main__":
    unittest.main()
