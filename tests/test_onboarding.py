import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.onboarding import OnboardingDiagnostics  # noqa: E402
from vntts.onboarding_ui import OnboardingWizard  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


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
        )

        results = diagnostics.run(AppSettings(tts_model="xtts_v2"))
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
        )

        results = diagnostics.run(AppSettings(tts_model="xtts_v2"))

        voice_result = next(
            result for result in results if result.name == "Character voices"
        )
        self.assertEqual(voice_result.status, "error")
        self.assertIn("narrator speaker", voice_result.message)


class OnboardingWizardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_new_setup_defaults_to_window_capture_and_xtts(self):
        wizard = OnboardingWizard(AppSettings())

        self.assertEqual(wizard.configuration_page.capture_mode.currentData(), "window")
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


if __name__ == "__main__":
    unittest.main()
