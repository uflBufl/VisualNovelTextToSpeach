import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import ModuleType
from unittest.mock import ANY, Mock, call, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox  # noqa: E402

from vntts.app import (  # noqa: E402
    SettingsDialog,
    TrayApplication,
    create_application_icon,
    main,
)
from vntts.cli import CLIReportResult  # noqa: E402
from vntts.controller import LiveSequenceStatus  # noqa: E402
from vntts.diagnostics import DiagnosticSnapshot  # noqa: E402
from vntts.generated_audio import AudioRouteTrace  # noqa: E402
from vntts.ocr import DialogRegion  # noqa: E402
from vntts.profiles import GameProfileStore  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


class TrayApplicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def wait_until(self, predicate, *, timeout_ms=2000):
        for _ in range(max(1, timeout_ms // 5)):
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for an asynchronous UI operation")

    def test_tray_shell_exposes_runtime_controls(self):
        controller = Mock()
        controller_factory = Mock(return_value=controller)

        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=controller_factory,
        )

        self.assertEqual(tray_application.read_action.text(), "Read current dialog")
        self.assertEqual(tray_application.dialog_action.text(), "No dialog detected")
        self.assertEqual(tray_application.live_action.text(), "Start live reading")
        self.assertEqual(tray_application.pause_action.text(), "Pause speech")
        self.assertEqual(
            tray_application.skip_action.text(),
            "Skip current speech",
        )
        self.assertEqual(tray_application.repeat_action.text(), "Repeat last speech")
        self.assertEqual(
            tray_application.clear_queue_action.text(),
            "Clear speech queue",
        )
        self.assertEqual(
            tray_application.emergency_stop_action.text(), "Emergency stop"
        )
        self.assertEqual(
            tray_application.calibrate_action.text(),
            "Calibrate dialog region",
        )
        self.assertEqual(
            tray_application.diagnostics_action.text(),
            "Live diagnostics...",
        )
        self.assertEqual(tray_application.setup_action.text(), "Run setup...")
        self.assertEqual(tray_application.profiles_action.text(), "Game profiles...")
        self.assertEqual(
            tray_application.corrections_action.text(),
            "OCR corrections...",
        )
        self.assertEqual(
            tray_application.ocr_review_action.text(),
            "Review uncertain OCR...",
        )
        self.assertEqual(
            tray_application.pregeneration_action.text(),
            "Prepare offline audio...",
        )
        self.assertEqual(
            tray_application.assets_action.text(),
            "Manage models and voices...",
        )
        self.assertTrue(tray_application.speaker_mapping_action.isVisible())
        self.assertEqual(
            tray_application.voice_preview_action.text(),
            "Choose narrator voice...",
        )
        self.assertEqual(tray_application.history_action.text(), "Dialogue history...")
        self.assertEqual(
            tray_application.support_action.text(),
            "Diagnostics and logs...",
        )
        self.assertEqual(
            tray_application.macos_permissions_action.text(),
            "macOS permissions...",
        )
        self.assertFalse(tray_application.read_action.isEnabled())
        self.assertFalse(tray_application.live_action.isEnabled())
        self.assertFalse(tray_application.sequence_resync_action.isVisible())
        self.assertFalse(tray_application.pause_action.isEnabled())
        tray_application.shutdown()
        controller.shutdown.assert_called_once_with()

    def test_packaged_content_import_worker_runs_without_creating_qt(self):
        package = ModuleType("r1999extractor")
        package.__path__ = []
        bootstrap_module = ModuleType("r1999extractor.bootstrap")
        bootstrap = Mock(return_value=0)
        bootstrap_module.main = bootstrap
        with (
            patch.dict(
                "sys.modules",
                {
                    "r1999extractor": package,
                    "r1999extractor.bootstrap": bootstrap_module,
                },
            ),
            patch("vntts.app.QApplication") as qt_application,
        ):
            result = main(
                [
                    "--game-content-import-worker",
                    "reverse1999",
                    "--data-directory",
                    "/tmp/import-output",
                ]
            )

        self.assertEqual(result, 0)
        bootstrap.assert_called_once_with(["--data-directory", "/tmp/import-output"])
        qt_application.assert_not_called()

    def test_packaged_generation_worker_runs_without_creating_qt(self):
        with (
            patch("vntts.authoring.cli.main", return_value=0) as generation_main,
            patch("vntts.app.QApplication") as qt_application,
        ):
            result = main(
                [
                    "--offline-generation-worker",
                    "generate",
                    "--queue",
                    "/tmp/queue.jsonl",
                ]
            )

        self.assertEqual(result, 0)
        generation_main.assert_called_once_with(
            ["generate", "--queue", "/tmp/queue.jsonl"]
        )
        qt_application.assert_not_called()

    def test_offline_audio_action_opens_guided_selection_and_reports_saved_scope(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        job = Mock()
        job.estimate.selected_lines = 42
        voice_plan = Mock()
        voice_plan.groups = (Mock(), Mock(), Mock())
        voice_plan.narrator_fallback_count = 1
        generation_input = Mock()
        generation_input.ready_items = 39
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.job.return_value = job
        dialog.voice_plan.return_value = voice_plan
        dialog.generation_input.return_value = generation_input

        with patch(
            "vntts.app.OfflineAudioPreparationDialog",
            return_value=dialog,
        ) as create_dialog:
            result = tray_application.open_pregeneration()

        self.assertIs(result, job)
        create_dialog.assert_called_once_with(
            tray_application.settings,
            parent=tray_application.dashboard,
        )
        self.assertIn("42 lines", tray_application.dashboard.status.text())
        self.assertIn("Matched 3 voice groups", tray_application.dashboard.status.text())
        self.assertIn("1 will use narrator", tray_application.dashboard.status.text())
        self.assertIn("39 lines are ready", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_sequence_resync_action_selects_the_visible_canonical_event(self):
        controller = Mock()
        controller.live_sequence_anchor_options.return_value = (
            ("Chapter 1, sequence 1 - Ada: First [event-1]", "event-1"),
            ("Chapter 1, sequence 2 - Bea: Second [event-2]", "event-2"),
        )
        controller.story_cursor.current_event_id = "event-2"
        controller.get_live_sequence_status.return_value = LiveSequenceStatus(
            "audio-manual", "locked", event_id="event-2"
        )
        controller.resync_live_sequence.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.QInputDialog.getItem",
            return_value=(
                "Chapter 1, sequence 1 - Ada: First [event-1]",
                True,
            ),
        ) as choose:
            self.assertTrue(tray_application.choose_sequence_position())

        self.assertTrue(tray_application.sequence_resync_action.isVisible())
        self.assertEqual(choose.call_args.args[4], 1)
        controller.resync_live_sequence.assert_called_once_with("event-1")
        tray_application.shutdown()

    def test_single_expected_sequence_action_uses_current_candidate_without_dialog(
        self,
    ):
        controller = Mock()
        controller.live_sequence_expected_options.return_value = (
            ("Sequence 2 - Ada: Repeated [event-2]", "event-2"),
        )
        controller.select_expected_live_sequence_event.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.QInputDialog.getItem") as choose:
            self.assertTrue(tray_application.choose_expected_sequence_event())

        choose.assert_not_called()
        controller.select_expected_live_sequence_event.assert_called_once_with(
            "event-2"
        )
        tray_application.shutdown()

    def test_multiple_expected_sequence_candidates_use_bounded_chooser(self):
        controller = Mock()
        options = (
            ("Sequence 2 - Ada: Left [left]", "left"),
            ("Sequence 3 - Bea: Right [right]", "right"),
        )
        controller.live_sequence_expected_options.return_value = options
        controller.select_expected_live_sequence_event.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.QInputDialog.getItem",
            return_value=(options[1][0], True),
        ):
            self.assertTrue(tray_application.choose_expected_sequence_event())

        controller.select_expected_live_sequence_event.assert_called_once_with("right")
        tray_application.shutdown()

    def test_compact_expected_sequence_button_uses_fresh_controller_candidate(self):
        controller = Mock()
        controller.live_sequence_expected_options.return_value = (
            ("Sequence 2 - Ada: Repeated [event-2]", "event-2"),
        )
        controller.select_expected_live_sequence_event.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(live_sequence_mode="audio-manual"),
            controller_factory=Mock(return_value=controller),
        )
        tray_application._controller_ready = True
        tray_application.set_sequence_status(
            LiveSequenceStatus(
                "audio-manual",
                "locked",
                expected_candidate_count=1,
            )
        )
        tray_application.compact_controller.set_ready(True)

        tray_application.compact_controller.sequence_expected_button.click()

        controller.live_sequence_expected_options.assert_called_once_with()
        controller.select_expected_live_sequence_event.assert_called_once_with(
            "event-2"
        )
        tray_application.shutdown()

    def test_compact_controls_replace_dashboard_and_persist_preference(self):
        controller = Mock()
        controller.get_capture_geometry.return_value = WindowGeometry(
            100, 200, 1600, 900
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.AppSettings.save") as save:
            tray_application.show_compact_controls()
            self.application.processEvents()

        self.assertFalse(tray_application.dashboard.isVisible())
        self.assertTrue(tray_application.compact_controller.isVisible())
        self.assertTrue(tray_application.settings.compact_controls)
        save.assert_called_once_with()

        with patch("vntts.app.AppSettings.save") as save:
            tray_application.show_dashboard()

        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertFalse(tray_application.compact_controller.isVisible())
        self.assertFalse(tray_application.settings.compact_controls)
        save.assert_called_once_with()
        tray_application.shutdown()

    def test_live_status_is_mirrored_from_tray_to_compact_window(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.set_live(True)

        tray_application.set_status(
            "Auto advance paused: source-audio completion is unavailable"
        )

        self.assertEqual(tray_application.compact_controller.mode.text(), "Live")
        self.assertEqual(
            tray_application.compact_controller.status.text(),
            "Auto advance paused: source-audio completion is unavailable",
        )
        self.assertEqual(
            tray_application.status_action.text(),
            tray_application.compact_controller.status.text(),
        )
        tray_application.shutdown()

    def test_audio_route_trace_goes_to_support_log_without_replacing_status(self):
        controller_factory = Mock(return_value=Mock())
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=controller_factory,
        )
        tray_application.set_status("Live reading active")
        trace_handler = controller_factory.call_args.kwargs["route_trace_handler"]

        trace_handler(
            AudioRouteTrace(
                3,
                "moss-tts:fresh-generation",
                "exact",
                "generated-audio-entry-not-found",
                "voice:rhiannon-v2:reference-1",
                "reverse1999:3",
                "generated-audio-entry-not-found",
            )
        )

        event = tray_application.support_log.snapshot()[-1]
        self.assertEqual(event["level"], "audio-route")
        self.assertEqual(event["generation"], 3)
        self.assertEqual(event["line_id"], "reverse1999:3")
        self.assertEqual(
            tray_application.compact_controller.status.text(),
            "Live reading active",
        )
        tray_application.shutdown()

    def test_unknown_speaker_adds_mapping_action_and_notification(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch.object(tray_application.tray, "showMessage") as notification,
            patch("vntts.app.configure_floating_window") as configure_window,
            patch("vntts.app.sys.platform", "darwin"),
        ):
            tray_application.signals.unknown_speaker.emit("Selone")
            self.application.processEvents()

        self.assertTrue(tray_application.speaker_mapping_action.isVisible())
        self.assertEqual(
            tray_application.speaker_mapping_action.text(), "Manage voice for Selone..."
        )
        self.assertIn("narrator voice", notification.call_args.args[1])
        self.assertIn("No voice is assigned", tray_application.dashboard.status.text())
        self.assertEqual(
            tray_application.compact_controller.status.text(),
            "Voice needed: Selone",
        )
        self.assertIsInstance(
            tray_application.unknown_speaker_prompt,
            QMessageBox,
        )
        self.assertIsNone(tray_application.unknown_speaker_prompt.parent())
        self.assertIn(
            "Selone",
            tray_application.unknown_speaker_prompt.text(),
        )
        self.assertTrue(
            tray_application.unknown_speaker_prompt.testAttribute(
                Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow
            )
        )
        configure_window.assert_called_once_with(
            tray_application.unknown_speaker_prompt
        )
        tray_application.unknown_speaker_continue_button.click()
        self.application.processEvents()
        controller.allow_narrator_fallback.assert_called_once_with("Selone")
        tray_application.shutdown()

    def test_live_preflight_blocks_start_until_named_speakers_are_approved(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = ("Selone", "Hotelier")
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.configure_floating_window"):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()

        controller.toggle_live.assert_not_called()
        self.assertIn(
            "2 named story speaker(s)",
            tray_application.live_voice_preflight_prompt.text(),
        )
        self.assertIn(
            "Selone, Hotelier",
            tray_application.live_voice_preflight_prompt.informativeText(),
        )

        tray_application.live_voice_preflight_narrator_button.click()
        controller.approve_live_narrator_fallbacks.assert_not_called()
        self.wait_until(lambda: controller.toggle_live.called)

        controller.approve_live_narrator_fallbacks.assert_called_once_with(
            ("Selone", "Hotelier")
        )
        self.assertEqual(controller.unresolved_live_speakers.call_count, 2)
        controller.toggle_live.assert_called_once_with()
        tray_application.shutdown()

    def test_live_preflight_defers_close_until_native_button_event_unwinds(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [
            ("Mrs. Owen",),
            ("Mrs. Owen",),
        ]
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.configure_floating_window"):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()

        prompt = tray_application.live_voice_preflight_prompt
        narrator = tray_application.live_voice_preflight_narrator_button
        QTest.mouseClick(narrator, Qt.MouseButton.LeftButton)

        self.assertIsNone(tray_application.live_voice_preflight_prompt)
        self.assertIs(tray_application.live_voice_preflight_action_prompt, prompt)
        self.assertFalse(prompt.isEnabled())
        controller.approve_live_narrator_fallbacks.assert_not_called()
        controller.toggle_live.assert_not_called()

        self.wait_until(lambda: controller.approve_live_narrator_fallbacks.called)

        controller.approve_live_narrator_fallbacks.assert_called_once_with(
            ("Mrs. Owen",)
        )
        controller.toggle_live.assert_called_once_with()
        tray_application.shutdown()

    def test_live_preflight_rejects_stale_narrator_approval_and_refreshes(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [
            ("Selone",),
            ("Hotelier",),
        ]
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.configure_floating_window"):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()
            tray_application.live_voice_preflight_narrator_button.click()
            self.wait_until(
                lambda: tray_application.live_voice_preflight_prompt is not None
            )

        controller.approve_live_narrator_fallbacks.assert_not_called()
        controller.toggle_live.assert_not_called()
        self.assertIn(
            "Hotelier",
            tray_application.live_voice_preflight_prompt.informativeText(),
        )
        tray_application.shutdown()

    def test_live_preflight_does_not_start_when_narrator_scope_becomes_empty(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [("Selone",), ()]
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.configure_floating_window"):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()
            tray_application.live_voice_preflight_narrator_button.click()
            self.wait_until(
                lambda: "changed" in tray_application.dashboard.status.text()
            )

        controller.approve_live_narrator_fallbacks.assert_not_called()
        controller.toggle_live.assert_not_called()
        self.assertIn("changed", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_preflight_identifies_current_scope_silently_then_starts(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [None, ()]
        controller.identify_live_scope.return_value = True
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        runner = Mock(active=False)
        tray_application.live_scope_runner = runner

        self.assertFalse(tray_application.toggle_live())

        runner.start.assert_called_once_with(controller.identify_live_scope)
        self.assertIsNone(tray_application.live_voice_preflight_prompt)
        controller.toggle_live.assert_not_called()

        tray_application._live_scope_finished(True, None)

        controller.toggle_live.assert_called_once_with()
        controller.read_once.assert_not_called()
        tray_application.shutdown()

    def test_live_scope_identification_failure_does_not_start(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._live_scope_finished(False, None)

        controller.toggle_live.assert_not_called()
        self.assertIn("complete dialog line", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_scope_failure_explains_story_match_instead_of_missing_text(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        controller.live_scope_identification_failure = "story-line-no-match"
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._live_scope_finished(False, None)

        self.assertIn("unambiguous line", tray_application.dashboard.status.text())
        self.assertNotIn("not visible", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_scope_failure_explains_empty_capture(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        controller.live_scope_identification_failure = "no-dialog-text"
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._live_scope_finished(False, None)

        self.assertIn("no dialog text", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_scope_identification_still_prompts_for_unresolved_speakers(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [None, ("Hotelier",)]
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        with patch("vntts.app.configure_floating_window"):
            self.assertFalse(tray_application.toggle_live())
            tray_application._live_scope_finished(True, None)
            self.application.processEvents()

        controller.toggle_live.assert_not_called()
        self.assertIn(
            "Hotelier",
            tray_application.live_voice_preflight_prompt.informativeText(),
        )
        tray_application.shutdown()

    def test_repeated_live_start_coalesces_scope_identification(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        runner = Mock(active=False)
        tray_application.live_scope_runner = runner

        self.assertFalse(tray_application.toggle_live())
        self.assertFalse(tray_application.toggle_live())

        runner.start.assert_called_once_with(controller.identify_live_scope)
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_stale_live_scope_identification_cannot_start_live_mode(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.live_scope_runner = Mock(active=False)

        self.assertFalse(tray_application.toggle_live())
        tray_application._lifecycle_generation += 1
        tray_application._live_scope_finished(True, None)

        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_emergency_stop_cancels_pending_live_scope_identification(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = None
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        runner = Mock(active=False)
        tray_application.live_scope_runner = runner

        self.assertFalse(tray_application.toggle_live())
        tray_application.emergency_stop()
        tray_application._live_scope_finished(True, None)

        runner.cancel.assert_called_once_with()
        controller.emergency_stop.assert_called_once_with()
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_closing_live_preflight_never_silently_approves_narrator(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.return_value = ("Selone",)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.configure_floating_window"):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()
            tray_application.live_voice_preflight_prompt.close()
            self.application.processEvents()

        controller.approve_live_narrator_fallbacks.assert_not_called()
        controller.toggle_live.assert_not_called()
        self.assertIn("cancelled", tray_application.dashboard.status.text())
        tray_application.shutdown()

    def test_live_preflight_rechecks_scope_after_voice_assignment(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [("Selone",), ()]
        controller.toggle_live.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch("vntts.app.configure_floating_window"),
            patch.object(tray_application, "open_speaker_mapping") as open_mapping,
        ):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()
            tray_application.live_voice_preflight_assign_button.click()
            self.wait_until(lambda: open_mapping.called)

        open_mapping.assert_called_once_with()
        self.assertEqual(controller.unresolved_live_speakers.call_count, 2)
        controller.toggle_live.assert_called_once_with()
        tray_application.shutdown()

    def test_cancelled_voice_assignment_returns_to_preflight_without_starting(self):
        controller = Mock(is_live_running=False)
        controller.unresolved_live_speakers.side_effect = [
            ("Selone",),
            ("Selone",),
        ]
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch("vntts.app.configure_floating_window"),
            patch.object(tray_application, "open_speaker_mapping") as open_mapping,
        ):
            self.assertFalse(tray_application.toggle_live())
            self.application.processEvents()
            tray_application.live_voice_preflight_assign_button.click()
            self.wait_until(
                lambda: tray_application.live_voice_preflight_prompt is not None
            )

        open_mapping.assert_called_once_with()
        controller.toggle_live.assert_not_called()
        self.assertIn(
            "Selone",
            tray_application.live_voice_preflight_prompt.informativeText(),
        )
        tray_application.shutdown()

    def test_voice_mapping_resumes_live_mode_after_assignment(self):
        controller = Mock()
        controller.is_live_running = True
        controller.unresolved_live_speakers.return_value = ()

        def toggle_live():
            controller.is_live_running = not controller.is_live_running
            return controller.is_live_running

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch.object(
            tray_application,
            "open_speaker_mapping",
            return_value=True,
        ):
            tray_application._open_pending_speaker_mapping("Selone")
            self.wait_until(lambda: controller.is_live_running)

        self.assertTrue(controller.is_live_running)
        self.assertEqual(controller.toggle_live.call_count, 2)
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.live_reader.wait.assert_called_once_with()
        self.assertFalse(tray_application.resume_live_after_unknown_mapping)
        tray_application.shutdown()

    def test_narrator_choice_resumes_live_after_cancelled_voice_mapping(self):
        controller = Mock()
        controller.is_live_running = False
        controller.unresolved_live_speakers.return_value = ()

        def toggle_live():
            controller.is_live_running = True
            return True

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.resume_live_after_unknown_mapping = True

        tray_application._continue_unknown_with_narrator("Selone")

        controller.allow_narrator_fallback.assert_called_once_with("Selone")
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.toggle_live.assert_called_once_with()
        self.assertTrue(controller.is_live_running)
        self.assertFalse(tray_application.resume_live_after_unknown_mapping)
        tray_application.shutdown()

    def test_macos_tray_icon_is_a_distinct_adaptive_mask(self):
        icon = create_application_icon(
            self.application.style(),
            platform="darwin",
        )

        self.assertFalse(icon.isNull())
        self.assertTrue(icon.isMask())
        self.assertFalse(icon.pixmap(64, 64).isNull())

    def test_other_platforms_keep_the_native_speaker_icon(self):
        icon = create_application_icon(
            self.application.style(),
            platform="win32",
        )

        self.assertFalse(icon.isNull())
        self.assertFalse(icon.isMask())

    def test_saved_ocr_corrections_are_reloaded_by_controller(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted

        with patch("vntts.app.OCRCorrectionsDialog", return_value=dialog):
            tray_application.open_corrections()

        controller.refresh_corrections.assert_called_once_with()
        self.assertEqual(tray_application.status_action.text(), "OCR corrections saved")
        tray_application.shutdown()

    def test_ocr_review_uses_active_profile_and_runtime_reload(self):
        with TemporaryDirectory() as temporary_directory:
            profile_store = GameProfileStore(
                Path(temporary_directory) / "profiles.json"
            )
            profile = profile_store.create("Game", AppSettings())
            controller = Mock()
            tray_application = TrayApplication(
                self.application,
                AppSettings(
                    active_profile_id=profile.id,
                    ocr_diagnostics_directory="review",
                ),
                controller_factory=Mock(return_value=controller),
                profile_store=profile_store,
            )
            dialog = Mock()

            with patch("vntts.app.OCRReviewDialog", return_value=dialog) as factory:
                tray_application.open_ocr_review()

        factory.assert_called_once_with(
            "review",
            tray_application.correction_store,
            profile.id,
            "Game",
            controller.refresh_corrections,
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_settings_expose_minimum_ocr_confidence(self):
        dialog = SettingsDialog(
            AppSettings(
                ocr_minimum_confidence=73,
                retain_uncertain_frames=True,
                ocr_diagnostics_directory="custom/ocr-diagnostics",
            )
        )

        self.assertEqual(dialog.ocr_minimum_confidence.value(), 73)
        dialog.ocr_minimum_confidence.setValue(81)

        self.assertEqual(dialog.settings().ocr_minimum_confidence, 81)
        self.assertTrue(dialog.settings().retain_uncertain_frames)
        self.assertEqual(
            dialog.settings().ocr_diagnostics_directory,
            "custom/ocr-diagnostics",
        )
        dialog.deleteLater()

    def test_settings_are_scrollable_and_grouped_into_visual_regions(self):
        dialog = SettingsDialog(AppSettings())

        self.assertTrue(dialog.settings_scroll.widgetResizable())
        self.assertEqual(
            [region.title() for region in dialog.settings_regions],
            [
                "Keyboard shortcuts",
                "Capture and OCR",
                "Speech and voices",
                "Playback and automation",
                "Application behavior",
            ],
        )
        available = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.width(), max(320, available.width() - 64))
        self.assertLessEqual(dialog.height(), max(320, available.height() - 64))
        self.assertTrue(
            all(
                region.layout().fieldGrowthPolicy()
                == region.layout().FieldGrowthPolicy.AllNonFixedFieldsGrow
                for region in dialog.settings_regions
            )
        )
        self.assertEqual(
            [
                dialog.section_navigation.itemText(index)
                for index in range(dialog.section_navigation.count())
            ],
            [region.title() for region in dialog.settings_regions],
        )
        dialog.show()
        dialog.section_navigation.setFocus()
        QTest.keyClick(dialog.section_navigation, Qt.Key.Key_End)
        self.application.processEvents()
        self.assertEqual(dialog.section_navigation.currentIndex(), 4)
        self.assertGreater(dialog.settings_scroll.verticalScrollBar().value(), 0)
        dialog.deleteLater()

    def test_settings_paths_share_browse_and_accessibility_contract(self):
        dialog = SettingsDialog(AppSettings())
        fields_and_buttons = (
            (dialog.screenshot_directory, dialog.screenshot_browse_button),
            (dialog.ocr_diagnostics_directory, dialog.diagnostics_browse_button),
            (dialog.narrator_reference, dialog.narrator_reference_button),
            (dialog.game_pack, dialog.game_pack_button),
            (dialog.voice_manifest, dialog.voice_manifest_button),
            (dialog.story_index, dialog.story_index_button),
            (dialog.live_sequence_plan, dialog.live_sequence_plan_button),
            (dialog.live_speaker_corpus, dialog.live_speaker_corpus_button),
            (
                dialog.generated_audio_manifest,
                dialog.generated_audio_manifest_button,
            ),
        )

        for field, button in fields_and_buttons:
            self.assertTrue(field.accessibleName())
            self.assertTrue(field.accessibleDescription())
            self.assertTrue(button.accessibleName())
            self.assertTrue(button.accessibleDescription())

        with TemporaryDirectory() as temporary_directory:
            selected = Path(temporary_directory) / "story.jsonl"
            selected.touch()
            with patch(
                "vntts.app.QFileDialog.getOpenFileName",
                return_value=(str(selected), ""),
            ):
                dialog.story_index_button.click()
            self.assertEqual(dialog.story_index.text(), str(selected))
        dialog.deleteLater()

    def test_settings_inline_validation_lists_all_errors_and_focuses_first(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            narrator = root / "narrator.wav"
            narrator.touch()
            dialog = SettingsDialog(AppSettings(screenshot_directory=str(root)))
            dialog.screenshot_directory.clear()
            dialog.capture_mode.setCurrentIndex(dialog.capture_mode.findData("window"))
            dialog.game_window.setCurrentText("")
            dialog.speech_backend.setCurrentIndex(
                dialog.speech_backend.findData("moss-tts")
            )
            dialog.narrator_reference.clear()
            dialog.show()
            self.application.processEvents()

            dialog.validate_and_accept()
            self.application.processEvents()

            self.assertNotEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
            self.assertIn("Screenshot directory", dialog.validation_summary.text())
            self.assertIn("Capture source", dialog.validation_summary.text())
            self.assertIn("Narrator reference", dialog.validation_summary.text())
            self.assertEqual(dialog.section_navigation.currentIndex(), 1)
            self.assertTrue(dialog.screenshot_directory.hasFocus())

            dialog.screenshot_directory.setText(str(root))
            dialog.game_window.setCurrentText("Reverse: 1999")
            dialog.narrator_reference.setText(str(narrator))
            self.assertEqual(
                dialog.validation_summary.text(), "All settings are valid."
            )
            dialog.validate_and_accept()
            self.assertEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
            dialog.deleteLater()

    def test_settings_restart_fields_are_marked_per_control(self):
        dialog = SettingsDialog(AppSettings())
        restart_labels = {
            label.text()
            for label in dialog.findChildren(QLabel)
            if label.text().endswith("(restart required)")
        }

        self.assertEqual(
            restart_labels,
            {
                "Speech engine (restart required)",
                "Speech model (restart required)",
                "TTS language (restart required)",
                "Narrator reference (restart required)",
                "Voice manifest (restart required)",
                "Narrator speaker (restart required)",
            },
        )
        for field in (
            dialog.speech_backend,
            dialog.tts_model,
            dialog.tts_language,
            dialog.narrator_reference,
            dialog.voice_manifest,
            dialog.narrator_speaker,
        ):
            self.assertIn("require", field.accessibleDescription().casefold())
        dialog.deleteLater()

    def test_settings_fit_scaled_fonts_with_navigation_and_validation_visible(self):
        base_font = QApplication.font()
        base_size = base_font.pointSizeF() if base_font.pointSizeF() > 0 else 12.0
        for scale in (1.0, 1.5, 2.0):
            with self.subTest(scale=scale):
                font = QFont(base_font)
                font.setPointSizeF(base_size * scale)
                dialog = SettingsDialog(AppSettings())
                dialog.setFont(font)
                dialog.resize(520, 420)
                dialog.show()
                self.application.processEvents()

                self.assertTrue(dialog.section_navigation.isVisibleTo(dialog))
                self.assertTrue(dialog.validation_summary.isVisibleTo(dialog))
                self.assertTrue(dialog.settings_scroll.isVisibleTo(dialog))
                self.assertTrue(dialog.save_button.isVisibleTo(dialog))

                dialog.close()
                dialog.deleteLater()

    def test_settings_expose_output_volume_and_speech_rate(self):
        dialog = SettingsDialog(
            AppSettings(output_volume_percent=75, speech_rate_percent=110)
        )

        self.assertEqual(dialog.output_volume.value(), 75)
        self.assertEqual(dialog.speech_rate.value(), 110)
        dialog.output_volume.setValue(45)
        dialog.speech_rate.setValue(125)

        self.assertEqual(dialog.settings().output_volume_percent, 45)
        self.assertEqual(dialog.settings().speech_rate_percent, 125)
        dialog.deleteLater()

    def test_settings_expose_guarded_auto_advance_controls(self):
        dialog = SettingsDialog(
            AppSettings(
                auto_advance_enabled=True,
                auto_advance_key="enter",
                auto_advance_delay_ms=600,
            )
        )

        self.assertTrue(dialog.auto_advance.isChecked())
        self.assertEqual(dialog.auto_advance_key.currentData(), "enter")
        self.assertEqual(dialog.auto_advance_delay.value(), 600)
        dialog.auto_advance_key.setCurrentIndex(
            dialog.auto_advance_key.findData("right")
        )
        dialog.auto_advance_delay.setValue(250)

        settings = dialog.settings()
        self.assertEqual(settings.auto_advance_key, "right")
        self.assertEqual(settings.auto_advance_delay_ms, 250)
        dialog.deleteLater()

    def test_settings_expose_disabled_by_default_speaker_announcements(self):
        dialog = SettingsDialog(AppSettings(announce_speaker_changes=True))

        self.assertEqual(dialog.speaker_announcement_mode.currentData(), "all-speakers")
        dialog.speaker_announcement_mode.setCurrentIndex(
            dialog.speaker_announcement_mode.findData("narrator-fallback-roles")
        )

        self.assertFalse(dialog.settings().announce_speaker_changes)
        self.assertEqual(
            dialog.settings().speaker_announcement_mode,
            "narrator-fallback-roles",
        )
        dialog.deleteLater()

    def test_settings_control_startup_voice_warmup(self):
        dialog = SettingsDialog(AppSettings(warm_up_voices=True))

        dialog.warm_up_voices.setChecked(False)

        self.assertFalse(dialog.settings().warm_up_voices)
        dialog.deleteLater()

    def test_settings_select_low_latency_speech_backend(self):
        dialog = SettingsDialog(AppSettings(speech_backend="chatterbox-nano"))

        self.assertEqual(dialog.speech_backend.currentData(), "chatterbox-nano")
        self.assertFalse(dialog.tts_model.isEnabled())
        self.assertFalse(dialog.tts_language.isEnabled())
        self.assertFalse(dialog.narrator_speaker.isEnabled())
        self.assertFalse(dialog.tts_profile.isEnabled())
        self.assertFalse(dialog.speech_rate.isEnabled())
        self.assertEqual(dialog.settings().speech_backend, "chatterbox-nano")
        dialog.deleteLater()

    def test_settings_offer_default_streaming_backend(self):
        dialog = SettingsDialog(AppSettings(speech_backend="pocket-tts"))

        self.assertEqual(dialog.speech_backend.currentData(), "pocket-tts")
        self.assertIn("recommended", dialog.speech_backend.currentText().casefold())
        self.assertFalse(dialog.speech_rate.isEnabled())
        self.assertTrue(dialog.pocket_gated_model.isVisibleTo(dialog))
        self.assertFalse(dialog.pocket_gated_model.isChecked())
        dialog.pocket_gated_model.setChecked(True)
        self.assertTrue(dialog.settings().pocket_gated_model_accepted)
        dialog.deleteLater()

    def test_settings_select_explicit_audio_source_policy(self):
        dialog = SettingsDialog(AppSettings(audio_source_policy="prefer-game-audio"))

        self.assertEqual(
            dialog.audio_source_policy.currentData(),
            "prefer-game-audio",
        )
        dialog.audio_source_policy.setCurrentIndex(
            dialog.audio_source_policy.findData("live-tts-only")
        )

        self.assertEqual(dialog.settings().audio_source_policy, "live-tts-only")
        dialog.deleteLater()

    def test_settings_preserve_explicit_live_speaker_corpus(self):
        dialog = SettingsDialog(
            AppSettings(live_speaker_corpus="session-speakers.json")
        )

        self.assertEqual(dialog.live_speaker_corpus.text(), "session-speakers.json")
        self.assertEqual(
            dialog.settings().live_speaker_corpus,
            "session-speakers.json",
        )
        dialog.deleteLater()

    def test_settings_sequence_shadow_requires_plan_and_story_index(self):
        dialog = SettingsDialog(AppSettings(live_sequence_mode="shadow"))

        errors = tuple(
            message for _section, _widget, message in dialog.validation_errors()
        )

        self.assertTrue(any("live sequence plan" in message for message in errors))
        self.assertTrue(any("Story index" in message for message in errors))
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            story = root / "story.jsonl"
            plan = root / "live-sequence.json"
            story.touch()
            plan.touch()
            dialog.story_index.setText(str(story))
            dialog.live_sequence_plan.setText(str(plan))

            self.assertFalse(dialog.validation_errors())
            settings = dialog.settings()

        self.assertEqual(settings.live_sequence_mode, "shadow")
        self.assertEqual(settings.live_sequence_plan, str(plan))
        dialog.deleteLater()

    def test_sequence_audio_manual_disables_auto_advance_controls(self):
        dialog = SettingsDialog(
            AppSettings(
                live_sequence_mode="audio-manual",
                auto_advance_enabled=True,
            )
        )

        self.assertEqual(dialog.live_sequence_mode.currentData(), "audio-manual")
        self.assertFalse(dialog.auto_advance.isEnabled())
        self.assertFalse(dialog.auto_advance_key.isEnabled())
        self.assertFalse(dialog.auto_advance_delay.isEnabled())
        self.assertIn("never sends advance keys", dialog.auto_advance.toolTip())
        dialog.deleteLater()

    def test_sequence_audio_auto_keeps_guarded_auto_advance_opt_in(self):
        dialog = SettingsDialog(
            AppSettings(
                live_sequence_mode="audio-auto",
                auto_advance_enabled=False,
            )
        )

        self.assertEqual(dialog.live_sequence_mode.currentData(), "audio-auto")
        self.assertTrue(dialog.auto_advance.isEnabled())
        self.assertFalse(dialog.auto_advance.isChecked())
        self.assertFalse(dialog.auto_advance_key.isEnabled())
        self.assertIn("at most one key", dialog.auto_advance.toolTip())
        dialog.auto_advance.setChecked(True)
        self.assertTrue(dialog.auto_advance_key.isEnabled())
        self.assertTrue(dialog.auto_advance_delay.isEnabled())
        dialog.deleteLater()

    def test_new_install_settings_recommend_guarded_sequence_auto(self):
        dialog = SettingsDialog(AppSettings())

        self.assertEqual(dialog.live_sequence_mode.currentData(), "audio-auto")
        self.assertIn("recommended", dialog.live_sequence_mode.currentText().casefold())
        self.assertTrue(dialog.auto_advance.isChecked())
        self.assertFalse(dialog.validation_errors())
        dialog.deleteLater()

    def test_settings_offer_moss_with_model_language_and_reference(self):
        dialog = SettingsDialog(
            AppSettings(
                speech_backend="moss-tts",
                tts_speaker_wav="matilda.wav",
            )
        )

        self.assertEqual(dialog.speech_backend.currentData(), "moss-tts")
        self.assertTrue(dialog.tts_model.isEnabled())
        self.assertTrue(dialog.tts_language.isEnabled())
        self.assertTrue(dialog.narrator_reference.isEnabled())
        self.assertTrue(dialog.tts_profile.isEnabled())
        self.assertIn("MOSS-TTS-Local-Transformer", dialog.tts_model.text())
        self.assertFalse(dialog.speech_rate.isEnabled())
        self.assertEqual(dialog.settings().tts_speaker_wav, "matilda.wav")
        dialog.deleteLater()

    def test_settings_switch_default_model_between_xtts_and_moss(self):
        dialog = SettingsDialog(
            AppSettings(
                speech_backend="coqui-xtts",
                tts_model="tts_models/multilingual/multi-dataset/xtts_v2",
            )
        )

        dialog.speech_backend.setCurrentIndex(
            dialog.speech_backend.findData("moss-tts")
        )
        self.assertIn("MOSS-TTS-Local-Transformer", dialog.tts_model.text())
        dialog.speech_backend.setCurrentIndex(
            dialog.speech_backend.findData("coqui-xtts")
        )
        self.assertEqual(
            dialog.tts_model.text(),
            "tts_models/multilingual/multi-dataset/xtts_v2",
        )
        dialog.deleteLater()

    def test_settings_control_macos_launch_at_login(self):
        dialog = SettingsDialog(AppSettings(launch_at_login=True))

        self.assertTrue(dialog.launch_at_login.isChecked())
        dialog.launch_at_login.setChecked(False)

        self.assertFalse(dialog.settings().launch_at_login)
        dialog.deleteLater()

    def test_macos_settings_explain_control_window_only_hotkeys(self):
        with patch("vntts.app.sys.platform", "darwin"):
            dialog = SettingsDialog(AppSettings())

        self.assertFalse(dialog.macos_hotkey_notice.isHidden())
        self.assertIn(
            "Global hotkeys are unavailable", dialog.macos_hotkey_notice.text()
        )
        self.assertIn("macOS controls", dialog.macos_hotkey_notice.text())
        self.assertIn("compact controls", dialog.macos_hotkey_notice.text())
        self.assertTrue(
            all(not recorder.isEnabled() for recorder in dialog.hotkey_recorders)
        )
        dialog.deleteLater()

    def test_composite_settings_fields_have_accessible_labels(self):
        dialog = SettingsDialog(AppSettings())
        expected = {
            "Screenshot directory": dialog.screenshot_directory,
            "Game window": dialog.game_window,
            "Diagnostics directory": dialog.ocr_diagnostics_directory,
            "Narrator reference (restart required)": dialog.narrator_reference,
            "Game pack": dialog.game_pack,
            "Voice manifest (restart required)": dialog.voice_manifest,
            "Story index": dialog.story_index,
            "Live speaker corpus": dialog.live_speaker_corpus,
            "Generated audio manifest": dialog.generated_audio_manifest,
        }
        labels = {
            label.text(): label
            for label in dialog.findChildren(QLabel)
            if label.text() in expected
        }

        for name, field in expected.items():
            self.assertIs(labels[name].buddy(), field)
            self.assertTrue(field.accessibleName())
            self.assertTrue(field.accessibleDescription())
        for button in (
            dialog.screenshot_browse_button,
            dialog.refresh_windows_button,
            dialog.diagnostics_browse_button,
            dialog.narrator_reference_button,
            dialog.game_pack_button,
            dialog.voice_manifest_button,
            dialog.story_index_button,
            dialog.live_speaker_corpus_button,
            dialog.generated_audio_manifest_button,
        ):
            self.assertTrue(button.accessibleName())
            self.assertTrue(button.accessibleDescription())
        dialog.deleteLater()

    def test_settings_change_updates_macos_launch_at_login(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(launch_at_login=False),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        updated = AppSettings(launch_at_login=True)
        dialog.settings.return_value = updated

        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch("vntts.app.configure_macos_launch_at_login") as configure,
            patch.object(tray_application, "start_hotkeys"),
            patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
        ):
            tray_application.open_settings()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        configure.assert_called_once_with(True)
        controller.apply_settings.assert_called_once_with(updated, cancellation=ANY)
        self.assertEqual(tray_application.settings, updated)
        tray_application.shutdown()

    def test_failed_settings_write_rolls_back_launch_at_login_before_publish(self):
        original = AppSettings(launch_at_login=False)
        controller = Mock(settings=original)
        tray_application = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.settings.return_value = original.updated(launch_at_login=True)

        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch("vntts.app.configure_macos_launch_at_login") as configure,
            patch(
                "vntts.app.AppSettings.save",
                side_effect=OSError("disk full"),
            ),
        ):
            tray_application.open_settings()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        self.assertEqual(configure.call_args_list, [call(True), call(False)])
        self.assertIs(tray_application.settings, original)
        controller.apply_settings.assert_not_called()
        self.assertIn("disk full", tray_application.status_action.text())
        tray_application.shutdown()

    def test_backend_setting_reports_restart_and_keeps_effective_identity_visible(self):
        controller = Mock()
        controller.settings = AppSettings(speech_backend="pocket-tts")
        tray_application = TrayApplication(
            self.application,
            controller.settings,
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        updated = controller.settings.updated(speech_backend="moss-tts")
        dialog.settings.return_value = updated

        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch.object(tray_application, "start_hotkeys"),
            patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
        ):
            tray_application.open_settings()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        controller.apply_settings.assert_called_once_with(updated, cancellation=ANY)
        self.assertEqual(tray_application.settings.speech_backend, "moss-tts")
        self.assertIn("restart required", tray_application.status_action.text())
        self.assertIn("still uses pocket-tts", tray_application.status_action.text())
        tray_application.shutdown()

    def test_macos_permission_action_opens_recovery_dialog(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        dialog = Mock()

        with patch("vntts.app.MacOSPermissionsDialog", return_value=dialog):
            tray_application.open_macos_permissions()

        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_voice_preview_dialog_uses_controller_voices_and_handler(self):
        controller = Mock()
        controller.is_live_running = False
        controller.available_voice_characters.return_value = ["Narrator", "Marcus"]
        choices = [Mock(id="preset:alba", label="Alba")]
        controller.available_voice_choices.return_value = choices
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with patch("vntts.app.VoicePreviewDialog", return_value=dialog) as factory:
            tray_application.open_voice_previews()

        factory.assert_called_once_with(
            ["Narrator", "Marcus"],
            choices,
            controller.preview_voice_choice,
            tray_application.assign_voice,
            controller.voice_assignment_for,
            tray_application.clear_voice_assignment,
            force_live_handler=tray_application.set_force_live_narrator,
            current_force_live_handler=ANY,
            preview_stop_handler=controller.stop_voice_preview,
            initial_character="Narrator",
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_narrator_voice_dialog_pauses_live_and_restores_it(self):
        controller = Mock()
        controller.is_live_running = True
        controller.unresolved_live_speakers.return_value = ()
        controller.available_voice_characters.return_value = ["Narrator"]
        controller.available_voice_choices.return_value = []

        def toggle_live():
            controller.is_live_running = not controller.is_live_running
            return controller.is_live_running

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.side_effect = lambda: self.assertFalse(controller.is_live_running)

        with patch("vntts.app.VoicePreviewDialog", return_value=dialog):
            tray_application.open_voice_previews()
            self.wait_until(lambda: controller.is_live_running)

        self.assertTrue(controller.is_live_running)
        self.assertEqual(controller.toggle_live.call_count, 2)
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.live_reader.wait.assert_called_once_with()
        tray_application.shutdown()

    def test_history_dialog_uses_controller_session_and_replay(self):
        controller = Mock()
        controller.is_live_running = False
        controller.inspect_current_dialog.return_value = DiagnosticSnapshot(
            None,
            text="Fresh manual capture",
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with patch("vntts.app.DialogueHistoryDialog", return_value=dialog) as factory:
            tray_application.open_history()

        factory.assert_called_once_with(
            controller.history,
            controller.replay_dialog,
            stop_handler=controller.stop_voice_preview,
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_history_dialog_pauses_live_capture_and_restores_it_after_close(self):
        controller = Mock()
        controller.is_live_running = True
        controller.unresolved_live_speakers.return_value = ()

        def toggle_live():
            controller.is_live_running = not controller.is_live_running
            return controller.is_live_running

        controller.toggle_live.side_effect = toggle_live
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.side_effect = lambda: self.assertFalse(controller.is_live_running)

        with patch("vntts.app.DialogueHistoryDialog", return_value=dialog):
            tray_application.open_history()
            self.wait_until(lambda: controller.is_live_running)

        self.assertTrue(controller.is_live_running)
        self.assertEqual(controller.toggle_live.call_count, 2)
        controller.unresolved_live_speakers.assert_called_once_with()
        controller.live_reader.wait.assert_called_once_with()
        tray_application.shutdown()

    def test_support_bundle_export_runs_with_sanitized_runtime_inputs(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        controller = Mock()
        diagnostic = DiagnosticSnapshot(None, confidence=88)
        controller.get_latest_diagnostic.return_value = diagnostic
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        builder = Mock()
        builder.build.return_value = Path("support.zip")
        tray_application.support_dialog = Mock()

        with (
            patch(
                "vntts.app.QFileDialog.getSaveFileName",
                return_value=("support.zip", "ZIP archives (*.zip)"),
            ),
            patch("vntts.app.SupportBundleBuilder", return_value=builder) as factory,
            patch("vntts.app.Thread", ImmediateThread),
        ):
            tray_application.export_support_bundle()

        factory.assert_called_once_with(
            tray_application.settings,
            tray_application.support_log,
            diagnostic=diagnostic,
            generation_timelines=tray_application.generation_timelines,
        )
        builder.build.assert_called_once_with("support.zip")
        tray_application.support_dialog.set_export_result.assert_called_once_with(
            True,
            "support.zip",
        )
        self.assertIn("Support bundle saved", tray_application.status_action.text())
        tray_application.shutdown()

    def test_cancelled_support_export_restores_dialog_action(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.support_dialog = Mock()

        with patch(
            "vntts.app.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ):
            tray_application.export_support_bundle()

        tray_application.support_dialog.set_export_result.assert_called_once_with(
            None,
            "Support report export cancelled.",
        )
        tray_application.shutdown()

    def test_support_launch_results_return_to_the_support_dialog(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        tray_application.support_dialog = Mock()

        with patch.object(
            tray_application,
            "open_diagnostics",
            side_effect=OSError("capture unavailable"),
        ):
            tray_application.open_support_diagnostics()
        tray_application.support_dialog.set_launcher_result.assert_called_with(
            "diagnostics",
            False,
            "Unable to open live diagnostics: capture unavailable",
        )

        tray_application.support_dialog.reset_mock()
        settings_path = Path("/tmp/vntts-settings")
        with patch.object(
            tray_application,
            "open_settings_folder",
            return_value=settings_path,
        ):
            tray_application.open_support_settings_folder()
        tray_application.support_dialog.set_launcher_result.assert_called_once_with(
            "settings-folder",
            True,
            f"Settings folder opened: {settings_path}",
        )
        tray_application.shutdown()

    def test_settings_reject_duplicate_recorded_hotkeys(self):
        dialog = SettingsDialog(AppSettings())
        dialog.live_hotkey.set_hotkey(dialog.read_hotkey.hotkey())

        dialog.validate_and_accept()

        self.assertIn("duplicates", dialog.validation_summary.text())
        self.assertEqual(dialog.section_navigation.currentIndex(), 0)
        self.assertNotEqual(dialog.result(), SettingsDialog.DialogCode.Accepted)
        dialog.deleteLater()

    def test_recognized_dialog_has_a_dedicated_tray_status(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )

        tray_application.signals.dialog_changed.emit("Marcus", "Ready to continue.")
        tray_application.signals.status_changed.emit("Speech queue cleared")

        self.assertEqual(
            tray_application.dialog_action.text(),
            "Marcus: Ready to continue.",
        )
        self.assertEqual(tray_application.status_action.text(), "Speech queue cleared")
        tray_application.shutdown()

    def test_tray_speech_controls_delegate_to_controller(self):
        controller = Mock()
        controller.toggle_speech_pause.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        tray_application.toggle_speech_pause()
        tray_application.skip_current_speech()
        tray_application.repeat_last_speech()
        tray_application.clear_speech_queue()

        controller.toggle_speech_pause.assert_called_once_with()
        controller.skip_current_speech.assert_called_once_with()
        controller.repeat_last_speech.assert_called_once_with()
        controller.clear_speech_queue.assert_called_once_with()
        self.assertEqual(tray_application.pause_action.text(), "Resume speech")
        tray_application.shutdown()

    def test_tray_can_toggle_auto_advance_without_restarting_live_mode(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(auto_advance_enabled=False),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.AppSettings.save") as save:
            tray_application.auto_advance_action.setChecked(True)

        controller.set_auto_advance_enabled.assert_called_once_with(True)
        self.assertTrue(tray_application.settings.auto_advance_enabled)
        save.assert_called_once_with()
        tray_application.shutdown()

    def test_failed_auto_advance_write_restores_action_without_runtime_change(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(auto_advance_enabled=False),
            controller_factory=Mock(return_value=controller),
        )

        with patch(
            "vntts.app.AppSettings.save",
            side_effect=OSError("read-only directory"),
        ):
            tray_application.auto_advance_action.setChecked(True)

        self.assertFalse(tray_application.auto_advance_action.isChecked())
        self.assertFalse(tray_application.settings.auto_advance_enabled)
        controller.set_auto_advance_enabled.assert_not_called()
        self.assertIn("read-only directory", tray_application.status_action.text())
        tray_application.shutdown()

    def test_failed_profile_asset_and_compact_writes_do_not_publish(self):
        original = AppSettings(compact_controls=False)
        candidate = original.updated(game_window_title="Changed")
        dialog_cases = (
            ("GameProfilesDialog", "open_profiles"),
            ("AssetManagerDialog", "open_assets"),
        )
        for dialog_name, method_name in dialog_cases:
            with self.subTest(method=method_name):
                controller = Mock(settings=original)
                tray_application = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                dialog = Mock()
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                dialog.settings.return_value = candidate
                with (
                    patch(f"vntts.app.{dialog_name}", return_value=dialog),
                    patch(
                        "vntts.app.AppSettings.save",
                        side_effect=OSError("disk full"),
                    ),
                ):
                    getattr(tray_application, method_name)()

                self.assertIs(tray_application.settings, original)
                controller.apply_settings.assert_not_called()
                self.assertFalse(tray_application.profile_restart_runner.active)
                self.assertIn("disk full", tray_application.status_action.text())
                tray_application.shutdown()

        tray_application = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=Mock(settings=original)),
        )
        with patch(
            "vntts.app.AppSettings.save",
            side_effect=OSError("disk full"),
        ):
            tray_application._save_compact_preference(True)
        self.assertIs(tray_application.settings, original)
        self.assertIn("disk full", tray_application.status_action.text())
        tray_application.shutdown()

    def test_failed_voice_writes_do_not_publish_application_settings(self):
        original = AppSettings()
        candidate = original.updated(voice_assignments={"Selone": "preset:alba"})
        operations = (
            (
                "assign_voice",
                ("Selone", "preset:alba"),
                lambda controller: controller.assign_voice,
            ),
            (
                "clear_voice_assignment",
                ("Selone",),
                lambda controller: controller.clear_voice_assignment,
            ),
            (
                "set_force_live_narrator",
                (True,),
                lambda controller: controller.set_force_live_narrator,
            ),
        )
        for method_name, arguments, controller_method in operations:
            with self.subTest(method=method_name):
                controller = Mock(settings=original)

                def invoke_commit(*_args, commit_settings=None):
                    commit_settings(candidate)
                    return candidate

                controller_method(controller).side_effect = invoke_commit
                tray_application = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                with (
                    patch(
                        "vntts.app.AppSettings.save",
                        side_effect=OSError("read-only directory"),
                    ),
                    self.assertRaisesRegex(OSError, "read-only directory"),
                ):
                    getattr(tray_application, method_name)(*arguments)

                self.assertIs(tray_application.settings, original)
                self.assertIn(
                    "read-only directory",
                    tray_application.status_action.text(),
                )
                tray_application.shutdown()

    def test_live_diagnostics_refresh_captures_a_fresh_snapshot(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        stale = DiagnosticSnapshot(None, text="Already captured")
        fresh = DiagnosticSnapshot(None, text="Fresh capture")
        controller = Mock()
        controller.is_live_running = True
        controller.get_latest_diagnostic.return_value = stale
        controller.inspect_current_dialog.return_value = fresh
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        diagnostics_dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = diagnostics_dialog

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot", side_effect=lambda _delay, call: call()
            ),
            patch("vntts.app.Thread", ImmediateThread),
        ):
            tray_application.refresh_diagnostics()

        controller.inspect_current_dialog.assert_called_once_with(notify=False)
        diagnostics_dialog.set_snapshot.assert_called_once_with(fresh)
        diagnostics_dialog.conceal_for_capture.assert_called_once_with()
        tray_application.shutdown()

    def test_manual_diagnostics_hides_window_before_capture(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        controller = Mock()
        controller.is_live_running = False
        controller.inspect_current_dialog.return_value = DiagnosticSnapshot(
            None,
            text="Fresh manual capture",
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        diagnostics_dialog = Mock()
        tray_application.diagnostics_dialog = diagnostics_dialog

        with (
            patch(
                "vntts.app.get_macos_permission_status",
                return_value={"screen_capture": True, "accessibility": True},
            ),
            patch(
                "vntts.app.QTimer.singleShot", side_effect=lambda _delay, call: call()
            ),
            patch("vntts.app.Thread", ImmediateThread),
        ):
            tray_application.refresh_diagnostics()

        diagnostics_dialog.conceal_for_capture.assert_called_once_with()
        controller.inspect_current_dialog.assert_called_once_with(notify=False)
        tray_application.shutdown()

    def test_late_diagnostic_refresh_result_is_ignored(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        diagnostics_dialog = Mock(refresh_in_flight=True)
        tray_application.diagnostics_dialog = diagnostics_dialog
        tray_application.diagnostics_refresh_generation = 2

        tray_application._diagnostics_refresh_finished(
            1,
            DiagnosticSnapshot(None, text="Stale capture"),
        )
        diagnostics_dialog.refresh_in_flight = False
        tray_application._diagnostics_refresh_finished(
            2,
            DiagnosticSnapshot(None, text="Timed-out capture"),
        )

        diagnostics_dialog.set_snapshot.assert_not_called()
        tray_application.shutdown()

    def test_diagnostic_result_restores_concealed_window(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )
        diagnostics_dialog = Mock()
        tray_application.diagnostics_dialog = diagnostics_dialog
        snapshot = DiagnosticSnapshot(None, text="Visible after capture")

        tray_application.update_diagnostics_snapshot(snapshot)

        diagnostics_dialog.set_snapshot.assert_called_once_with(snapshot)
        diagnostics_dialog.restore_after_capture.assert_called_once_with()
        tray_application.shutdown()

    def test_invalid_saved_hotkey_falls_back_without_preventing_startup(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(read_hotkey="not a hotkey"),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.keyboard.GlobalHotKeys") as listener_factory:
            tray_application.start_hotkeys()

        registered_hotkeys = listener_factory.call_args.args[0]
        self.assertIn(AppSettings().read_hotkey, registered_hotkeys)
        self.assertEqual(
            set(registered_hotkeys),
            {
                AppSettings().read_hotkey,
                AppSettings().live_hotkey,
                AppSettings().pause_hotkey,
                AppSettings().skip_hotkey,
                AppSettings().repeat_hotkey,
                AppSettings().clear_queue_hotkey,
                AppSettings().emergency_stop_hotkey,
            },
        )
        listener_factory.return_value.start.assert_called_once_with()
        tray_application.shutdown()

    def test_hotkey_registration_is_deferred_on_the_qt_thread(self):
        controller = Mock()
        controller.start.return_value = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch("vntts.app.QTimer.singleShot") as single_shot:
            generation = tray_application._begin_controller_lifecycle()
            tray_application._initial_start_generation = generation
            ready = tray_application._initialize_controller(generation)
            tray_application._initial_start_finished(ready, None)

        single_shot.assert_called_once_with(
            250,
            tray_application._start_hotkeys_safely,
        )
        tray_application.shutdown()

    def test_quit_during_initial_start_forces_late_controller_cleanup(self):
        started = Event()
        release = Event()
        runtime = {"live": False}
        controller = Mock()

        def start():
            started.set()
            release.wait(2)
            runtime["live"] = True
            return True

        def shutdown():
            runtime["live"] = False

        controller.start.side_effect = start
        controller.shutdown.side_effect = shutdown
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=True),
            controller_factory=Mock(return_value=controller),
        )
        ready_events = []
        hotkey_events = []
        tray_application.signals.ready_changed.connect(ready_events.append)
        tray_application.signals.hotkeys_requested.connect(
            lambda: hotkey_events.append(True)
        )

        with (
            patch.object(tray_application, "show_dashboard"),
            patch.object(tray_application, "show_compact_controls"),
            patch.object(tray_application.tray, "show"),
        ):
            tray_application.start()
            self.assertTrue(started.wait(1))
            tray_application.shutdown()
            release.set()
            self.wait_until(lambda: controller.shutdown.call_count == 1)

        self.assertFalse(runtime["live"])
        self.assertEqual(ready_events, [])
        self.assertEqual(hotkey_events, [])
        self.assertTrue(tray_application._shutting_down)

    def test_macos_skips_unstable_native_hotkey_listener(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=Mock()),
        )

        with (
            patch("vntts.app.sys.platform", "darwin"),
            patch.object(tray_application, "start_hotkeys") as start_hotkeys,
        ):
            tray_application._start_hotkeys_safely()

        start_hotkeys.assert_not_called()
        self.assertIn("macOS hotkeys disabled", tray_application.status_action.text())
        self.assertIn(
            "listener is unstable",
            tray_application.support_log.snapshot()[-2]["message"],
        )
        tray_application.shutdown()

    def test_window_calibration_uses_selected_client_geometry(self):
        controller = Mock()
        geometry = WindowGeometry(100, 200, 1600, 900)
        controller.get_capture_geometry.return_value = geometry
        tray_application = TrayApplication(
            self.application,
            AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
            ),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch("vntts.app.show_calibration_overlay") as show_overlay,
            patch(
                "vntts.app.QTimer.singleShot",
                side_effect=lambda _delay, callback: callback(),
            ),
        ):
            tray_application.calibrate()

        show_overlay.assert_called_once_with(geometry)
        tray_application.shutdown()

    def test_calibration_updates_the_active_game_profile(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Game", AppSettings())
            tray_application = TrayApplication(
                self.application,
                AppSettings(active_profile_id=profile.id),
                controller_factory=Mock(return_value=Mock()),
                profile_store=store,
            )
            region = DialogRegion(0.1, 0.6, 0.8, 0.3)

            tray_application.update_profile_region(region)

            self.assertEqual(store.get(profile.id).dialog_region, region)
            tray_application.shutdown()

    def test_profile_selection_reloads_runtime_with_profile_settings(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create(
                "Reverse: 1999",
                AppSettings(game_window_title="Reverse: 1999"),
            )
            selected_settings = profile.apply(AppSettings())
            controller = Mock()
            controller.start.return_value = True
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch.object(tray_application, "start_hotkeys"),
                patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
            ):
                tray_application.open_profiles()
                self.wait_until(
                    lambda: not tray_application.profile_restart_runner.active
                )

            controller.shutdown.assert_called_once_with()
            controller.apply_settings.assert_called_once_with(selected_settings)
            controller.start.assert_called_once_with()
            self.assertIn("Reverse: 1999", tray_application.status_action.text())
            tray_application.shutdown()

    def test_live_modal_stop_wait_does_not_block_qt_events(self):
        controller = Mock()
        controller.is_live_running = True
        release = Event()

        def toggle_live():
            controller.is_live_running = False
            return False

        controller.toggle_live.side_effect = toggle_live
        controller.live_reader.wait.side_effect = lambda: release.wait(2)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        opened = []
        heartbeat = []

        with patch.object(
            tray_application,
            "_open_history_dialog",
            side_effect=lambda resume: opened.append(resume),
        ):
            tray_application.open_history()
            QTimer.singleShot(0, lambda: heartbeat.append(True))
            self.application.processEvents()
            self.assertEqual(heartbeat, [True])
            self.assertEqual(opened, [])
            self.assertTrue(tray_application.live_stop_runner.active)
            release.set()
            self.wait_until(lambda: opened == [True])

        tray_application.shutdown()

    def test_profile_restart_does_not_block_qt_events(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Reverse: 1999", AppSettings())
            selected_settings = profile.apply(AppSettings())
            release = Event()
            controller = Mock()
            controller.start.side_effect = lambda: release.wait(2) or True
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings
            heartbeat = []

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
            ):
                tray_application.open_profiles()
                QTimer.singleShot(0, lambda: heartbeat.append(True))
                self.application.processEvents()
                self.assertEqual(heartbeat, [True])
                self.assertTrue(tray_application.profile_restart_runner.active)
                self.assertFalse(tray_application.profiles_action.isEnabled())
                release.set()
                self.wait_until(
                    lambda: not tray_application.profile_restart_runner.active
                )

            self.assertTrue(tray_application.profiles_action.isEnabled())
            tray_application.shutdown()

    def test_settings_and_assets_apply_without_blocking_qt_events(self):
        original = AppSettings()
        candidate = original.updated(game_window_title="Changed")
        for dialog_name, method_name in (
            ("SettingsDialog", "open_settings"),
            ("AssetManagerDialog", "open_assets"),
        ):
            with self.subTest(method=method_name):
                started = Event()
                release = Event()
                controller = Mock(settings=original)

                def blocked_apply(_settings, **_options):
                    started.set()
                    release.wait(2)

                controller.apply_settings.side_effect = blocked_apply
                tray_application = TrayApplication(
                    self.application,
                    original,
                    controller_factory=Mock(return_value=controller),
                )
                dialog = Mock()
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                dialog.settings.return_value = candidate
                heartbeat = []
                with (
                    patch(f"vntts.app.{dialog_name}", return_value=dialog),
                    patch(
                        "vntts.app.AppSettings.save",
                        return_value=Path("settings.json"),
                    ),
                ):
                    getattr(tray_application, method_name)()
                    QTimer.singleShot(0, lambda: heartbeat.append(True))
                    self.wait_until(lambda: started.is_set() and bool(heartbeat))
                    self.assertTrue(tray_application.configuration_runner.active)
                    self.assertFalse(tray_application.settings_action.isEnabled())
                    self.assertFalse(tray_application.assets_action.isEnabled())
                    release.set()
                    self.wait_until(
                        lambda: not tray_application.configuration_runner.active
                    )

                controller.apply_settings.assert_called_once_with(
                    candidate,
                    cancellation=ANY,
                )
                self.assertTrue(tray_application.settings_action.isEnabled())
                tray_application.shutdown()

    def test_saved_settings_runtime_apply_can_be_cancelled(self):
        original = AppSettings()
        candidate = original.updated(game_window_title="Changed")
        started = Event()
        controller = Mock(settings=original)

        def blocked_apply(_settings, *, cancellation):
            started.set()
            cancellation.wait(2)
            return False

        def cancel(cancellation):
            cancellation.set()
            return True

        controller.apply_settings.side_effect = blocked_apply
        controller.cancel_settings_apply.side_effect = cancel
        tray_application = TrayApplication(
            self.application,
            original,
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.settings.return_value = candidate
        with (
            patch("vntts.app.SettingsDialog", return_value=dialog),
            patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
        ):
            tray_application.open_settings()
            self.wait_until(started.is_set)
            self.assertTrue(tray_application.cancel_configuration_action.isVisible())
            self.assertTrue(tray_application.cancel_configuration_action.isEnabled())
            tray_application.cancel_configuration_action.trigger()
            self.wait_until(lambda: not tray_application.configuration_runner.active)

        cancellation = controller.apply_settings.call_args.kwargs["cancellation"]
        controller.cancel_settings_apply.assert_called_once_with(cancellation)
        self.assertIs(tray_application.settings, candidate)
        self.assertIn("saved settings", tray_application.status_action.text().lower())
        self.assertFalse(tray_application.cancel_configuration_action.isVisible())
        tray_application.shutdown()

    def test_profile_restart_disables_runtime_and_quit_prevents_restart(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Reverse: 1999", AppSettings())
            selected_settings = profile.apply(AppSettings())
            entered = Event()
            release = Event()
            controller = Mock()

            def blocked_shutdown():
                entered.set()
                release.wait(2)

            controller.shutdown.side_effect = blocked_shutdown
            controller.start.return_value = True
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            tray_application.set_ready(True)
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
            ):
                tray_application.open_profiles()
                self.assertTrue(entered.wait(1))
                self.application.processEvents()
                self.assertFalse(tray_application.read_action.isEnabled())
                self.assertFalse(tray_application.live_action.isEnabled())
                self.assertFalse(tray_application.dashboard.read_button.isEnabled())
                tray_application.shutdown()
                release.set()
                tray_application.profile_restart_runner.thread_pool.waitForDone(2_000)
                self.application.processEvents()

            controller.start.assert_not_called()
            controller.apply_settings.assert_not_called()
            self.assertFalse(tray_application.read_action.isEnabled())
            self.assertFalse(tray_application.history_action.isEnabled())

    def test_quit_during_profile_start_cleans_up_the_late_runtime(self):
        with TemporaryDirectory() as temporary_directory:
            store = GameProfileStore(Path(temporary_directory) / "profiles.json")
            profile = store.create("Reverse: 1999", AppSettings())
            selected_settings = profile.apply(AppSettings())
            entered = Event()
            release = Event()
            controller = Mock()

            def blocked_start():
                entered.set()
                release.wait(2)
                return True

            controller.start.side_effect = blocked_start
            tray_application = TrayApplication(
                self.application,
                AppSettings(),
                controller_factory=Mock(return_value=controller),
                profile_store=store,
            )
            tray_application.set_ready(True)
            dialog = Mock()
            dialog.exec.return_value = SettingsDialog.DialogCode.Accepted
            dialog.settings.return_value = selected_settings

            with (
                patch("vntts.app.GameProfilesDialog", return_value=dialog),
                patch("vntts.app.AppSettings.save", return_value=Path("settings.json")),
            ):
                tray_application.open_profiles()
                self.assertTrue(entered.wait(1))
                tray_application.shutdown()
                release.set()
                tray_application.profile_restart_runner.thread_pool.waitForDone(2_000)
                self.application.processEvents()

            controller.start.assert_called_once_with()
            self.assertEqual(controller.shutdown.call_count, 2)
            self.assertFalse(tray_application.read_action.isEnabled())

    def test_shutdown_cancels_a_pending_live_stop_continuation(self):
        controller = Mock()
        controller.is_live_running = True
        release = Event()

        def toggle_live():
            controller.is_live_running = False
            return False

        controller.toggle_live.side_effect = toggle_live
        controller.live_reader.wait.side_effect = lambda: release.wait(2)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.set_ready(True)

        with patch.object(tray_application, "_open_history_dialog") as opened:
            tray_application.open_history()
            self.assertTrue(tray_application.live_stop_runner.active)
            self.assertFalse(tray_application.history_action.isEnabled())
            tray_application.shutdown()
            release.set()
            tray_application.live_stop_runner.thread_pool.waitForDone(2_000)
            self.application.processEvents()

        opened.assert_not_called()
        self.assertFalse(tray_application.history_action.isEnabled())

    def test_incomplete_setup_opens_wizard_instead_of_loading_model(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=controller),
        )

        with (
            patch.object(tray_application.tray, "show"),
            patch("vntts.app.QTimer.singleShot") as single_shot,
        ):
            tray_application.start()

        controller.start.assert_not_called()
        self.assertEqual(single_shot.call_args.args[0], 0)
        self.assertEqual(single_shot.call_args.args[1], tray_application.run_onboarding)
        tray_application.shutdown()

    def test_onboarding_wizard_runs_without_nested_modal_event_loop(self):
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=Mock()),
        )

        tray_application.run_onboarding()
        wizard = tray_application.onboarding_wizard

        self.assertIsNotNone(wizard)
        self.assertTrue(wizard.isVisible())

        wizard.reject()
        self.application.processEvents()

        self.assertIsNone(tray_application.onboarding_wizard)
        self.assertEqual(tray_application.status_action.text(), "Setup required")
        tray_application.shutdown()

    def test_successful_onboarding_returns_to_focused_start_action_without_playing(
        self,
    ):
        controller = Mock()
        controller.is_ready = True
        tray_application = TrayApplication(
            self.application,
            AppSettings(onboarding_completed=False),
            controller_factory=Mock(return_value=controller),
        )
        tray_application.run_onboarding()
        wizard = tray_application.onboarding_wizard
        wizard.test_page.set_result(True, "Success. Recognized Rhiannon: Hello.")

        with patch(
            "vntts.settings.AppSettings.save",
            return_value=Path("settings.json"),
        ):
            wizard.accept()
            self.application.processEvents()

        self.assertIsNone(tray_application.onboarding_wizard)
        self.assertTrue(tray_application.dashboard.isVisible())
        self.assertIs(
            tray_application.dashboard.focusWidget(),
            tray_application.dashboard.live_button,
        )
        self.assertIn(
            "Next: click Start live reading", tray_application.status_action.text()
        )
        controller.toggle_live.assert_not_called()
        tray_application.shutdown()

    def test_onboarding_test_runs_controller_end_to_end(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        controller = Mock()
        controller.start.return_value = True
        controller.test_current_dialog.return_value = (
            "Marcus",
            "This is a complete test.",
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        with patch("vntts.app.Thread", ImmediateThread):
            tray_application.run_onboarding_test(
                AppSettings(speech_backend="coqui-xtts", tts_model="xtts_v2")
            )

        controller.apply_settings.assert_called_once()
        controller.model_assets.download.assert_called_once()
        controller.test_current_dialog.assert_called_once_with()
        self.assertTrue(results[0][0])
        self.assertIn("Marcus", results[0][1])
        tray_application.shutdown()

    def test_onboarding_test_displays_the_controller_startup_error(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        controller = Mock()
        controller.start.return_value = False
        controller_factory = Mock(return_value=controller)
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=controller_factory,
        )
        report_error = controller_factory.call_args.kwargs["error_handler"]
        controller.start.side_effect = lambda: (
            report_error(RuntimeError("invalid Pioneer reference")),
            False,
        )[1]
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        with patch("vntts.app.Thread", ImmediateThread):
            tray_application.run_onboarding_test(AppSettings())

        self.assertFalse(results[0][0])
        self.assertIn("invalid Pioneer reference", results[0][1])
        tray_application.shutdown()

    def test_pocket_onboarding_cancellation_stops_startup_and_reports_cancelled(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        controller.start.side_effect = lambda: (
            tray_application.cancel_onboarding_download(),
            False,
        )[1]
        results = []
        tray_application.signals.onboarding_test_finished.connect(
            lambda success, message: results.append((success, message))
        )

        with patch("vntts.app.Thread", ImmediateThread):
            tray_application.run_onboarding_test(
                AppSettings(speech_backend="pocket-tts")
            )

        controller.shutdown.assert_called_once_with()
        controller.test_current_dialog.assert_not_called()
        self.assertEqual(results, [(False, "OCR-to-speech test cancelled.")])
        tray_application.shutdown()

    def test_onboarding_cancel_only_signals_the_background_owner(self):
        controller = Mock()
        controller.shutdown.side_effect = AssertionError(
            "Qt cancellation must not shut the controller down"
        )
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        heartbeat = []

        QTimer.singleShot(0, lambda: heartbeat.append(True))
        tray_application.cancel_onboarding_download()
        self.application.processEvents()

        self.assertEqual(heartbeat, [True])
        controller.shutdown.assert_not_called()
        controller.shutdown.side_effect = None
        tray_application.shutdown()

    def test_package_self_test_does_not_start_qt_application(self):
        with (
            patch("vntts.app.configure_bundled_dependencies"),
            patch(
                "vntts.app.run_package_self_test",
                return_value=CLIReportResult(True, Path("report.json")),
            ) as self_test,
            patch("vntts.app.QApplication") as application,
        ):
            result = main(
                [
                    "--package-self-test",
                    "--package-self-test-report",
                    "custom-report.json",
                ]
            )

        self.assertEqual(result, 0)
        self_test.assert_called_once_with("custom-report.json")
        application.assert_not_called()

    def test_release_smoke_test_does_not_start_qt_application(self):
        with (
            patch("vntts.app.configure_bundled_dependencies"),
            patch(
                "vntts.app.run_release_smoke_test",
                return_value=CLIReportResult(True, Path("report.json")),
            ) as smoke_test,
            patch("vntts.app.QApplication") as application,
        ):
            result = main(
                [
                    "--release-smoke-test-image",
                    "dialog.png",
                    "--release-smoke-test-report",
                    "custom-report.json",
                    "--release-smoke-test-expected-speaker",
                    "Marcus",
                ]
            )

        self.assertEqual(result, 0)
        smoke_test.assert_called_once_with(
            image_path="dialog.png",
            window_title=None,
            report_path="custom-report.json",
            model_name="tts_models/en/vctk/vits",
            expected_speaker="Marcus",
        )
        application.assert_not_called()


if __name__ == "__main__":
    unittest.main()
