"""Reading owns its selected progress source after preparation is closed."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from tests.story_fixtures import write_synthetic_game_pack  # noqa: E402
from vntts.app import TrayApplication  # noqa: E402
from vntts.game_pack import import_game_pack  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


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
        self.readingLineObserved.connect(
            lambda line_id, text_hash: self.observations.append((line_id, text_hash))
        )

    def runtime_playback_settings(self):
        self.isEnabled()
        return self.current_settings

    def has_pending_work(self):
        return self.pending_work

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
        with patch.object(tray, "toggle_live") as start:
            tray._read_prepared_story()
        start.assert_called_once_with()
        controller.is_live_running = True

    def _close(self, tray, panel):
        self.assertIsNone(tray._pregeneration_finished(QDialog.DialogCode.Rejected))
        QCoreApplication.sendPostedEvents(panel, QEvent.Type.DeferredDelete)
        self.application.processEvents()
        self.assertFalse(isValid(panel))
        self.assertIsNone(tray.pregeneration_dialog)

    def test_close_during_speech_pause_or_audio_wait_keeps_reading_source(self):
        for snapshot in ({"speaking": True}, {"paused": True}, {"queued": True}):
            with self.subTest(snapshot=snapshot):
                tray, controller, panel, saved = self._tray()
                self._read(tray, controller)
                selected = controller.settings
                controller.runtime_control_snapshot.return_value = snapshot
                before = tray._runtime_control_state()
                controller.apply_settings.reset_mock()
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
        tray, controller, panel, _saved = self._tray()
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
        self.assertEqual(candidate or tray._runtime_settings(), selected)
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
        self.assertEqual(candidate or tray._runtime_settings(), selected)
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
        self.assertEqual(candidate or tray._runtime_settings(), selected)
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

    def test_volume_change_keeps_transient_source(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        self._close(tray, panel)
        tray.settings = saved.updated(output_volume_percent=37)
        self.assertEqual(
            tray._runtime_settings(), selected.updated(output_volume_percent=37)
        )
        self.assertTrue(tray._runtime_control_state().ready)

    def test_failed_handoff_keeps_previous_selected_source(self):
        tray, controller, panel, saved = self._tray()
        self._read(tray, controller)
        selected = controller.settings
        self._close(tray, panel)
        self._panel(tray, self._progress_settings("second", saved))
        controller.apply_settings.side_effect = lambda *_args, **_kwargs: False
        controller.is_live_running = False
        with (
            patch.object(tray, "toggle_live") as start,
            patch.object(tray, "show_error") as error,
        ):
            tray._read_prepared_story()
        start.assert_not_called()
        error.assert_called_once()
        self.assertEqual(controller.settings, selected)
        self.assertEqual(tray._runtime_settings(), selected)
        self.assertTrue(tray._runtime_control_state().ready)
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
            {"audio_source_policy": "prefer-generated"},
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
