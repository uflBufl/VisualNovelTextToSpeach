import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from vntts.app import (  # noqa: E402
    SettingsDialog,
    TrayApplication,
    create_application_icon,
    main,
)
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
            tray_application.assets_action.text(),
            "Manage models and voices...",
        )
        self.assertTrue(tray_application.speaker_mapping_action.isVisible())
        self.assertEqual(
            tray_application.voice_preview_action.text(), "Choose voices..."
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
        self.assertFalse(tray_application.pause_action.isEnabled())
        tray_application.shutdown()
        controller.shutdown.assert_called_once_with()

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

    def test_unknown_speaker_adds_mapping_action_and_notification(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )

        with patch.object(tray_application.tray, "showMessage") as notification:
            tray_application.signals.unknown_speaker.emit("Selone")
            self.application.processEvents()

        self.assertTrue(tray_application.speaker_mapping_action.isVisible())
        self.assertEqual(
            tray_application.speaker_mapping_action.text(), "Manage voice for Selone..."
        )
        self.assertIn("narrator voice", notification.call_args.args[1])
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
        self.assertIn("default", dialog.speech_backend.currentText().casefold())
        self.assertFalse(dialog.speech_rate.isEnabled())
        dialog.deleteLater()

    def test_settings_control_macos_launch_at_login(self):
        dialog = SettingsDialog(AppSettings(launch_at_login=True))

        self.assertTrue(dialog.launch_at_login.isChecked())
        dialog.launch_at_login.setChecked(False)

        self.assertFalse(dialog.settings().launch_at_login)
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

        configure.assert_called_once_with(True)
        controller.apply_settings.assert_called_once_with(updated)
        self.assertEqual(tray_application.settings, updated)
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
            initial_character="Narrator",
        )
        dialog.exec.assert_called_once_with()
        tray_application.shutdown()

    def test_history_dialog_uses_controller_session_and_replay(self):
        controller = Mock()
        tray_application = TrayApplication(
            self.application,
            AppSettings(),
            controller_factory=Mock(return_value=controller),
        )
        dialog = Mock()

        with patch("vntts.app.DialogueHistoryDialog", return_value=dialog) as factory:
            tray_application.open_history()

        factory.assert_called_once_with(controller.history, controller.replay_dialog)
        dialog.exec.assert_called_once_with()
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
        )
        builder.build.assert_called_once_with("support.zip")
        self.assertIn("Support bundle saved", tray_application.status_action.text())
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

    def test_manual_diagnostics_hides_window_before_capture(self):
        class ImmediateThread:
            def __init__(self, *, target, daemon):
                self.target = target

            def start(self):
                self.target()

        controller = Mock()
        controller.is_live_running = False
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
        controller.inspect_current_dialog.assert_called_once_with()
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
            tray_application._initialize_controller()

        single_shot.assert_called_once_with(
            250,
            tray_application._start_hotkeys_safely,
        )
        tray_application.shutdown()

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
