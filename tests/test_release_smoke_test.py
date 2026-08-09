import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from PIL import Image

from vntts.ocr import OCRResult
from vntts.release_smoke_test import run_release_smoke_test
from vntts.window_capture import WindowCaptureTarget


class ReleaseSmokeTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_sample_reaches_speech_stage(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "01.jpeg"
        engine = Mock()
        with TemporaryDirectory() as temporary_directory:
            successful, report_path = run_release_smoke_test(
                image_path=sample,
                report_path=Path(temporary_directory) / "report.json",
                expected_speaker="Marcus",
                engine_factory=Mock(return_value=engine),
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertTrue(successful)
        self.assertEqual(report["speaker"], "Marcus")
        self.assertIn("wanted to go home", report["text"])
        engine.speak.assert_called_once_with(report["text"])

    def test_static_image_runs_ocr_synthesis_and_audio_playback(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            image_path = root / "dialog.png"
            report_path = root / "report.json"
            Image.new("RGB", (1000, 800), "white").save(image_path)
            recognize = Mock(
                return_value=OCRResult(
                    "Marcus",
                    "This is a release smoke test.",
                    94.0,
                    "balanced",
                    1,
                )
            )
            engine = Mock()
            engine_factory = Mock(return_value=engine)

            successful, written_path = run_release_smoke_test(
                image_path=image_path,
                report_path=report_path,
                expected_speaker="Marcus",
                recognize=recognize,
                engine_factory=engine_factory,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(successful)
            self.assertEqual(written_path, report_path)
            self.assertTrue(report["success"])
            self.assertEqual(report["speaker"], "Marcus")
            self.assertEqual(report["confidence"], 94.0)
            engine_factory.assert_called_once_with(model_name="tts_models/en/vctk/vits")
            engine.speak.assert_called_once_with("This is a release smoke test.")

    def test_selected_window_uses_production_capture_target(self):
        image = Image.new("RGB", (400, 120), "white")
        capture = Mock(return_value=(image, None))
        recognize = Mock(
            return_value=OCRResult(
                "X",
                "Window capture works.",
                90.0,
                "balanced",
                1,
            )
        )
        engine = Mock()
        with TemporaryDirectory() as temporary_directory:
            successful, _ = run_release_smoke_test(
                window_title="Reverse: 1999",
                report_path=Path(temporary_directory) / "report.json",
                capture=capture,
                recognize=recognize,
                engine_factory=Mock(return_value=engine),
            )

        self.assertTrue(successful)
        capture_target = capture.call_args.kwargs["capture_target"]
        self.assertIsInstance(capture_target, WindowCaptureTarget)
        self.assertEqual(capture_target.window_title, "Reverse: 1999")
        self.assertFalse(capture.call_args.kwargs["save_screenshot"])
        engine.speak.assert_called_once_with("Window capture works.")

    def test_uncertain_ocr_fails_without_loading_speech_model(self):
        recognize = Mock(
            return_value=OCRResult(
                "Marcus",
                "Uncertain text",
                25.0,
                "balanced",
                3,
            )
        )
        engine_factory = Mock()
        with TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "dialog.png"
            report_path = Path(temporary_directory) / "report.json"
            Image.new("RGB", (1000, 800), "white").save(image_path)

            successful, _ = run_release_smoke_test(
                image_path=image_path,
                report_path=report_path,
                recognize=recognize,
                engine_factory=engine_factory,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertFalse(successful)
        self.assertFalse(report["success"])
        self.assertIn("below 60%", report["checks"][-1]["message"])
        engine_factory.assert_not_called()

    def test_requires_exactly_one_capture_source(self):
        with TemporaryDirectory() as temporary_directory:
            successful, report_path = run_release_smoke_test(
                report_path=Path(temporary_directory) / "report.json",
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertFalse(successful)
        self.assertIn("exactly one", report["checks"][0]["message"])


if __name__ == "__main__":
    unittest.main()
