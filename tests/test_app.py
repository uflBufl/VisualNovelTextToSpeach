import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.app import SettingsDialog, TrayApplication, main  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


class TrayApplicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

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
            tray_application.calibrate_action.text(),
            "Calibrate dialog region",
        )
        self.assertEqual(tray_application.setup_action.text(), "Run setup...")
        self.assertEqual(
            tray_application.assets_action.text(),
            "Manage models and voices...",
        )
        self.assertFalse(tray_application.read_action.isEnabled())
        self.assertFalse(tray_application.live_action.isEnabled())
        self.assertFalse(tray_application.pause_action.isEnabled())
        tray_application.shutdown()
        controller.shutdown.assert_called_once_with()

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
            },
        )
        listener_factory.return_value.start.assert_called_once_with()
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

        with patch("vntts.app.show_calibration_overlay") as show_overlay:
            tray_application.calibrate()

        show_overlay.assert_called_once_with(geometry)
        tray_application.shutdown()

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
            tray_application.run_onboarding_test(AppSettings())

        controller.apply_settings.assert_called_once()
        controller.model_assets.download.assert_called_once()
        controller.test_current_dialog.assert_called_once_with()
        self.assertTrue(results[0][0])
        self.assertIn("Marcus", results[0][1])
        tray_application.shutdown()

    def test_package_self_test_does_not_start_qt_application(self):
        with (
            patch("vntts.app.configure_bundled_dependencies"),
            patch(
                "vntts.app.run_package_self_test",
                return_value=(True, "report.json"),
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
                return_value=(True, "report.json"),
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
