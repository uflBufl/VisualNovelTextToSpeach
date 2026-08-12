import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from vntts.calibration import DialogRegionOverlay  # noqa: E402
from vntts.onboarding import OnboardingDiagnostics  # noqa: E402
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
            )
        )

        permission = next(
            result for result in results if result.name == "macOS permissions"
        )
        self.assertEqual(permission.status, "error")
        self.assertIn("Screen Recording", permission.message)
        self.assertIn("Accessibility", permission.message)
        self.assertIn("System Settings", permission.message)


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

    def test_finish_requires_successful_end_to_end_test(self):
        wizard = OnboardingWizard(AppSettings())

        wizard.accept()
        self.assertFalse(wizard.settings().onboarding_completed)

        wizard.test_page.set_result(True, "Success")
        wizard.accept()

        self.assertTrue(wizard.settings().onboarding_completed)

    def test_configuration_gives_text_fields_room_to_expand(self):
        wizard = OnboardingWizard(AppSettings())
        page = wizard.configuration_page

        self.assertGreaterEqual(wizard.width(), 920)
        self.assertGreaterEqual(wizard.height(), 680)
        self.assertEqual(page.window_layout.stretch(0), 1)
        self.assertEqual(page.manifest_layout.stretch(0), 1)
        self.assertGreaterEqual(page.refresh_button.minimumWidth(), 120)
        self.assertGreaterEqual(page.browse_manifest_button.minimumWidth(), 120)
        self.assertEqual(
            page.manage_assets_button.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Expanding,
        )
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
