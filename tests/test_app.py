import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from vntts.app import SettingsDialog, TrayApplication, main  # noqa: E402
from vntts.diagnostics import DiagnosticSnapshot  # noqa: E402
from vntts.ocr import DialogRegion  # noqa: E402
from vntts.profiles import GameProfileStore  # noqa: E402
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
            tray_application.assets_action.text(),
            "Manage models and voices...",
        )
        self.assertEqual(
            tray_application.voice_preview_action.text(), "Preview voices..."
        )
        self.assertFalse(tray_application.read_action.isEnabled())
        self.assertFalse(tray_application.live_action.isEnabled())
        self.assertFalse(tray_application.pause_action.isEnabled())
        tray_application.shutdown()
        controller.shutdown.assert_called_once_with()

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

    def test_voice_preview_dialog_uses_controller_voices_and_handler(self):
        controller = Mock()
        controller.available_voice_characters.return_value = ["Narrator", "Marcus"]
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
            controller.preview_voice,
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_settings_reject_duplicate_recorded_hotkeys(self):
        dialog = SettingsDialog(AppSettings())
        dialog.live_hotkey.set_hotkey(dialog.read_hotkey.hotkey())

        with patch("vntts.app.QMessageBox.warning") as warning:
            dialog.validate_and_accept()

        self.assertIn("duplicates", warning.call_args.args[2])
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

    def test_live_diagnostics_reuses_the_live_pipeline_snapshot(self):
        snapshot = DiagnosticSnapshot(None, text="Already captured")
        controller = Mock()
        controller.is_live_running = True
        controller.get_latest_diagnostic.return_value = snapshot
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        observed = []
        tray_application.signals.diagnostics_changed.connect(observed.append)

        with patch("vntts.app.Thread") as thread:
            tray_application.refresh_diagnostics()

        self.assertEqual(observed, [snapshot])
        thread.assert_not_called()
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

            controller.shutdown.assert_called_once_with()
            controller.apply_settings.assert_called_once_with(selected_settings)
            controller.start.assert_called_once_with()
            self.assertIn("Reverse: 1999", tray_application.status_action.text())
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
