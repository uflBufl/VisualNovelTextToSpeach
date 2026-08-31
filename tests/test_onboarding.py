import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QSizePolicy  # noqa: E402

from vntts.calibration import DialogRegionOverlay  # noqa: E402
from vntts.onboarding import DiagnosticResult, OnboardingDiagnostics  # noqa: E402
from vntts.onboarding_ui import OnboardingWizard  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


def granted_permissions():
    return {"screen_capture": True, "accessibility": True}


class OnboardingDiagnosticsTest(unittest.TestCase):
    def test_ready_environment_passes_with_valid_voice_pack(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            model_path = root / "model"
            model_path.mkdir()
            reference = root / "marcus.wav"
            reference.touch()
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Marcus",
                                "speaker": "reverse-1999-marcus",
                                "reference": reference.name,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            diagnostics = OnboardingDiagnostics(
                tesseract_probe=lambda: "5.5.0",
                audio_probe=lambda: "Speakers",
                model_path_resolver=lambda _model: model_path,
                permission_status_provider=granted_permissions,
            )
            settings = AppSettings(
                capture_mode="window",
                game_window_title="Reverse: 1999",
                speech_backend="coqui-xtts",
                tts_model="xtts_v2",
                voice_manifest=str(manifest),
            )

            results = diagnostics.run(settings)

        self.assertTrue(all(result.passed for result in results))
        self.assertTrue(all(result.status == "ok" for result in results))

    def test_missing_external_components_are_actionable_errors(self):
        def fail_tesseract():
            raise RuntimeError("Tesseract executable was not found")

        def fail_audio():
            raise RuntimeError("No output device")

        diagnostics = OnboardingDiagnostics(
            tesseract_probe=fail_tesseract,
            audio_probe=fail_audio,
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=granted_permissions,
        )

        results = diagnostics.run(
            AppSettings(speech_backend="coqui-xtts", tts_model="xtts_v2")
        )
        errors = {
            result.name: result.message for result in results if not result.passed
        }

        self.assertIn("Tesseract executable", errors["Tesseract OCR"])
        self.assertIn("No output device", errors["Audio output"])
        model_result = next(
            result for result in results if result.name == "Speech model"
        )
        self.assertEqual(model_result.status, "warning")

    def test_missing_voice_reference_is_an_error(self):
        with TemporaryDirectory() as temporary_directory:
            manifest = Path(temporary_directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Marcus",
                                "speaker": "marcus",
                                "reference": "missing.wav",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            diagnostics = OnboardingDiagnostics(
                tesseract_probe=lambda: "5.5.0",
                audio_probe=lambda: "Speakers",
                model_path_resolver=lambda _model: Path(temporary_directory),
                permission_status_provider=granted_permissions,
            )

            results = diagnostics.run(
                AppSettings(tts_model="xtts_v2", voice_manifest=str(manifest))
            )

        voice_result = next(
            result for result in results if result.name == "Character voices"
        )
        self.assertEqual(voice_result.status, "error")
        self.assertIn("missing.wav", voice_result.message)
        self.assertEqual(voice_result.remediation, "voices")

    def test_xtts_without_voice_pack_requires_narrator(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=granted_permissions,
        )

        results = diagnostics.run(AppSettings(tts_model="xtts_v2"))

        voice_result = next(
            result for result in results if result.name == "Character voices"
        )
        self.assertEqual(voice_result.status, "error")
        self.assertIn("narrator speaker", voice_result.message)
        self.assertEqual(voice_result.remediation, "settings")

    def test_missing_macos_permissions_block_setup_with_actionable_guidance(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=lambda: {
                "screen_capture": False,
                "accessibility": False,
            },
        )

        results = diagnostics.run(
            AppSettings(
                game_window_title="Reverse: 1999",
                tts_model="xtts_v2",
                narrator_speaker="Narrator",
                auto_advance_enabled=True,
            )
        )

        permission = next(
            result for result in results if result.name == "macOS permissions"
        )
        self.assertEqual(permission.status, "error")
        self.assertIn("Screen Recording", permission.message)
        self.assertIn("Accessibility", permission.message)
        self.assertIn("System Settings", permission.message)
        self.assertEqual(permission.remediation, "permissions")

    def test_accessibility_is_optional_without_auto_advance(self):
        diagnostics = OnboardingDiagnostics(
            tesseract_probe=lambda: "5.5.0",
            audio_probe=lambda: "Speakers",
            model_path_resolver=lambda _model: Path("missing-model"),
            permission_status_provider=lambda: {
                "screen_capture": True,
                "accessibility": False,
            },
        )

        results = diagnostics.run(
            AppSettings(
                game_window_title="Reverse: 1999",
                tts_model="xtts_v2",
                narrator_speaker="Narrator",
            )
        )

        permission = next(
            result for result in results if result.name == "macOS permissions"
        )
        self.assertEqual(permission.status, "ok")
        self.assertNotIn("Accessibility", permission.message)


class OnboardingWizardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_new_setup_defaults_to_window_capture_and_pocket_tts(self):
        wizard = OnboardingWizard(AppSettings())

        self.assertEqual(wizard.configuration_page.capture_mode.currentData(), "window")
        self.assertEqual(
            wizard.configuration_page.speech_backend.currentData(), "pocket-tts"
        )
        self.assertIn("xtts", wizard.configuration_page.tts_model.text())
        self.assertEqual(wizard.configuration_page.tts_language.text(), "en")
        self.assertEqual(
            wizard.configuration_page.narrator_speaker.text(),
            "Claribel Dervla",
        )

    def test_macos_setup_explains_control_window_only_hotkeys(self):
        with patch("vntts.onboarding_ui.sys.platform", "darwin"):
            wizard = OnboardingWizard(AppSettings())

        page = wizard.configuration_page
        self.assertFalse(page.macos_hotkey_notice.isHidden())
        self.assertIn("Global hotkeys are unavailable", page.macos_hotkey_notice.text())
        self.assertIn("compact controls", page.macos_hotkey_notice.text())
        self.assertFalse(page.read_hotkey.isEnabled())
        self.assertFalse(page.live_hotkey.isEnabled())
        wizard.deleteLater()

    def test_finish_requires_successful_end_to_end_test(self):
        wizard = OnboardingWizard(AppSettings())

        wizard.accept()
        self.assertFalse(wizard.settings().onboarding_completed)

        wizard.test_page.set_result(True, "Success")
        wizard.accept()

        self.assertTrue(wizard.settings().onboarding_completed)

    def test_running_end_to_end_test_guards_navigation_and_cancels_truthfully(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show_page(len(wizard.pages) - 1)
        cancelled = []
        wizard.cancel_requested.connect(lambda: cancelled.append(True))

        wizard.test_page.run_test()

        self.assertTrue(wizard.test_page.running)
        self.assertEqual(wizard.test_page.cancel_button.text(), "Cancel test")
        self.assertFalse(wizard.back_button.isEnabled())
        self.assertFalse(wizard.cancel_button.isEnabled())
        wizard.reject()
        self.assertEqual(cancelled, [True])
        self.assertTrue(wizard.test_page.running)
        self.assertEqual(wizard.test_page.cancel_button.text(), "Cancelling...")
        self.assertIn("Cancelling", wizard.test_page.status.text())

        wizard.test_page.set_result(False, "OCR-to-speech test cancelled.")
        self.assertFalse(wizard.test_page.running)
        self.assertTrue(wizard.back_button.isEnabled())
        self.assertTrue(wizard.cancel_button.isEnabled())
        wizard.deleteLater()

    def test_configuration_gives_text_fields_room_to_expand(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page

        self.assertTrue(page.configuration_scroll.widgetResizable())
        self.assertEqual(wizard.minimumWidth(), 520)
        self.assertEqual(wizard.minimumHeight(), 420)
        self.assertEqual(page.window_layout.stretch(0), 1)
        self.assertEqual(page.manifest_layout.stretch(0), 1)
        self.assertGreaterEqual(page.refresh_button.minimumWidth(), 120)
        self.assertGreaterEqual(page.browse_manifest_button.minimumWidth(), 120)
        self.assertEqual(
            page.manage_assets_button.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )
        wizard.deleteLater()

    def test_step_progress_and_navigation_are_keyboard_accessible(self):
        wizard = OnboardingWizard(AppSettings())
        wizard.show()
        wizard.next_button.setFocus()

        self.assertEqual(wizard.step_label.text(), "Step 1 of 5")
        self.assertTrue(wizard.step_label.accessibleName())
        QTest.keyClick(wizard.next_button, Qt.Key.Key_Return)

        self.assertEqual(wizard.current_page_index, 1)
        self.assertEqual(wizard.step_label.text(), "Step 2 of 5")
        wizard.deleteLater()

    def test_configuration_validation_lists_all_errors_and_focuses_first(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page
        wizard.show_page(1)
        wizard.show()
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("coqui-xtts"))
        page.tts_model.clear()
        page.ocr_language.clear()
        page.tts_language.clear()
        page.terms.setChecked(False)

        self.assertFalse(page.validatePage())
        self.application.processEvents()

        self.assertIn("Game window", page.validation_summary.text())
        self.assertIn("Speech model", page.validation_summary.text())
        self.assertIn("OCR language", page.validation_summary.text())
        self.assertIn("TTS language", page.validation_summary.text())
        self.assertIn("XTTS license", page.validation_summary.text())
        self.assertTrue(page.game_window.hasFocus())

        page.capture_mode.setCurrentIndex(page.capture_mode.findData("screen"))
        page.speech_backend.setCurrentIndex(page.speech_backend.findData("pocket-tts"))
        page.ocr_language.setText("eng")
        self.assertEqual(page.validation_summary.text(), "Configuration is ready.")
        self.assertTrue(page.validatePage())
        wizard.deleteLater()

    def test_configuration_defaults_pocket_to_public_presets_and_persists_opt_in(self):
        wizard = OnboardingWizard(AppSettings(speech_backend="pocket-tts"))
        page = wizard.configuration_page
        wizard.show_page(1)
        wizard.show()

        self.assertTrue(page.pocket_gated_model.isVisibleTo(wizard))
        self.assertFalse(page.pocket_gated_model.isChecked())
        self.assertFalse(page.terms.isVisibleTo(wizard))

        page.pocket_gated_model.setChecked(True)

        self.assertTrue(page.settings().pocket_gated_model_accepted)
        wizard.deleteLater()

    def test_configuration_scrolls_at_scaled_fonts(self):
        base_font = QApplication.font()
        base_size = base_font.pointSizeF() if base_font.pointSizeF() > 0 else 12.0
        for scale in (1.0, 1.5, 2.0):
            with self.subTest(scale=scale):
                font = QFont(base_font)
                font.setPointSizeF(base_size * scale)
                wizard = OnboardingWizard(AppSettings())
                wizard.setFont(font)
                wizard.resize(520, 420)
                wizard.show_page(1)
                wizard.show()
                self.application.processEvents()

                page = wizard.configuration_page
                self.assertTrue(page.validation_summary.isVisibleTo(wizard))
                self.assertTrue(page.configuration_scroll.isVisibleTo(wizard))
                self.assertGreater(
                    page.configuration_scroll.verticalScrollBar().maximum(), 0
                )
                self.assertTrue(wizard.next_button.isVisibleTo(wizard))

                wizard.close()
                wizard.deleteLater()

    def test_diagnostics_page_is_async_cancellable_and_stale_safe(self):
        class ManualThreadPool:
            def __init__(self):
                self.tasks = []

            def start(self, task):
                self.tasks.append(task)

        diagnostics = Mock()
        diagnostics.run.return_value = (DiagnosticResult("Capture", "ok", "Ready"),)
        wizard = OnboardingWizard(AppSettings(), diagnostics=diagnostics)
        pool = ManualThreadPool()
        wizard.diagnostics_page.runner.thread_pool = pool

        wizard.show_page(2)
        self.assertTrue(wizard.diagnostics_page.runner.active)
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertEqual(wizard.diagnostics_page.results.count(), 0)

        wizard.diagnostics_page.cancel_checks()
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertEqual(wizard.diagnostics_page.results.count(), 0)
        self.assertIn("cancelled", wizard.diagnostics_page.status.text().casefold())

        wizard.diagnostics_page.start_checks()
        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertTrue(wizard.diagnostics_page.complete)
        self.assertTrue(wizard.next_button.isEnabled())
        self.assertEqual(wizard.diagnostics_page.results.count(), 1)
        wizard.deleteLater()

    def test_composite_configuration_fields_have_accessible_labels(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page
        labels = {
            label.text(): label
            for label in page.findChildren(QLabel)
            if label.text() in {"Game window", "Narrator reference", "Voice manifest"}
        }

        self.assertIs(labels["Game window"].buddy(), page.game_window)
        self.assertIs(labels["Narrator reference"].buddy(), page.narrator_reference)
        self.assertIs(labels["Voice manifest"].buddy(), page.voice_manifest)
        for field in (
            page.game_window,
            page.narrator_reference,
            page.voice_manifest,
            page.refresh_button,
            page.narrator_reference_button,
            page.browse_manifest_button,
        ):
            self.assertTrue(field.accessibleName())
            self.assertTrue(field.accessibleDescription())
        wizard.deleteLater()

    def test_calibration_hides_wizard_before_frozen_overlay_capture(self):
        target = Mock()
        target.get_geometry.return_value = WindowGeometry(0, 0, 1000, 600)
        with TemporaryDirectory() as temporary_directory:
            wizard = OnboardingWizard(
                AppSettings(
                    capture_mode="window",
                    game_window_title="Reverse: 1999",
                ),
                capture_target_factory=Mock(return_value=target),
            )
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                platform="darwin",
            )

            def show_overlay(_geometry):
                overlay.show()
                return overlay

            with (
                patch(
                    "vntts.onboarding_ui.show_calibration_overlay",
                    side_effect=show_overlay,
                ),
                patch(
                    "vntts.onboarding_ui.QTimer.singleShot",
                    side_effect=lambda _delay, callback: callback(),
                ),
            ):
                wizard.calibration_page.calibrate()

            self.assertTrue(overlay.isVisible())
            self.assertFalse(wizard.isVisible())

            overlay.close()
            self.application.processEvents()

            self.assertTrue(wizard.isVisible())
            wizard.close()
            wizard.deleteLater()


if __name__ == "__main__":
    unittest.main()
