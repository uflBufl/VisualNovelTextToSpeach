import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from PIL import Image

from vntts.ocr import (
    DialogRegion,
    OCRPreprocessingProfile,
    OCRResult,
    UncertainFrameRecorder,
    calculate_ocr_confidence,
    default_dialog_region,
    load_dialog_region,
    parse_dialog_region,
    parse_recognized_dialog,
    recognize_dialog_image,
    recognize_dialog_image_result,
    save_dialog_region,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class DialogRegionTest(unittest.TestCase):
    def test_region_converts_normalized_values_to_monitor_coordinates(self):
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)

        self.assertEqual(
            region.capture_box(
                {
                    "left": 100,
                    "top": 50,
                    "width": 2000,
                    "height": 1000,
                }
            ),
            {"left": 300, "top": 650, "width": 1600, "height": 300},
        )

    def test_region_can_be_parsed_from_environment_format(self):
        self.assertEqual(
            parse_dialog_region("0.1, 0.6, 0.8, 0.3"),
            DialogRegion(0.1, 0.6, 0.8, 0.3),
        )

    def test_invalid_region_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "fit inside"):
            DialogRegion(0.5, 0.5, 0.6, 0.6)

    def test_region_round_trips_through_json_file(self):
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "nested" / "region.json"

            save_dialog_region(region, path)

            self.assertEqual(load_dialog_region(path), region)
            self.assertEqual(json.loads(path.read_text()), region.to_json())


class RecognizedDialogTest(unittest.TestCase):
    def setUp(self):
        self.registry = CharacterVoiceRegistry(
            [
                CharacterVoice("Marcus", "marcus"),
                CharacterVoice("X", "x"),
            ]
        )

    def test_fuzzy_speaker_name_ignores_ui_noise(self):
        character, text = parse_recognized_dialog(
            "AUTO SKIP\nMareus\nHello from the suitcase.\nv\n",
            self.registry,
        )

        self.assertEqual(character, "Marcus")
        self.assertEqual(text, "Hello from the suitcase.")

    def test_confidence_is_weighted_by_recognized_word_length(self):
        confidence = calculate_ocr_confidence(
            {
                "text": ["A", "reliable", "", "ignored"],
                "conf": [20, 90, -1, "invalid"],
            }
        )

        self.assertAlmostEqual(confidence, (20 + 90 * 8) / 9)

    def test_low_confidence_result_retries_with_alternate_preprocessing(self):
        profiles = (
            OCRPreprocessingProfile("first", 1.0, 170),
            OCRPreprocessingProfile("second", 2.0, 200),
        )
        recognize_text = Mock(
            side_effect=["Garbled text", "Clear recognized dialogue."]
        )
        recognize_data = Mock(
            side_effect=[
                {"text": ["Garbled", "text"], "conf": [25, 30]},
                {
                    "text": ["Clear", "recognized", "dialogue"],
                    "conf": [88, 91, 90],
                },
            ]
        )

        result = recognize_dialog_image_result(
            Image.new("RGB", (320, 120), "black"),
            recognize_text=recognize_text,
            recognize_data=recognize_data,
            minimum_confidence=60,
            profiles=profiles,
        )

        self.assertEqual(result.text, "Clear recognized dialogue.")
        self.assertEqual(result.profile, "second")
        self.assertEqual(result.attempts, 2)
        self.assertGreater(result.confidence, 60)

    def test_confident_first_attempt_does_not_run_extra_ocr(self):
        recognize_text = Mock(return_value="A reliable result.")
        recognize_data = Mock(
            return_value={"text": ["A", "reliable", "result"], "conf": [95, 94, 96]}
        )

        result = recognize_dialog_image_result(
            Image.new("RGB", (320, 120), "black"),
            recognize_text=recognize_text,
            recognize_data=recognize_data,
        )

        self.assertTrue(result.is_confident())
        recognize_text.assert_called_once()
        recognize_data.assert_called_once()

    def test_uncertain_frame_recorder_saves_image_and_metadata_once(self):
        result = OCRResult("Marcus", "Maybe this text", 41.5, "balanced", 3)
        with TemporaryDirectory() as temporary_directory:
            recorder = UncertainFrameRecorder(temporary_directory)
            image = Image.new("RGB", (32, 16), "black")

            image_path = recorder.record(image, result, 60)
            duplicate = recorder.record(image, result, 60)

            self.assertTrue(image_path.is_file())
            metadata_path = image_path.with_suffix(".json")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["confidence"], 41.5)
            self.assertEqual(metadata["minimum_confidence"], 60)
            self.assertEqual(metadata["preprocessing_profile"], "balanced")
            self.assertIsNone(duplicate)

            recorder.reset()
            self.assertIsNotNone(recorder.record(image, result, 60))

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_sample_screenshots_resolve_speaker_and_dialog(self):
        samples = Path(__file__).resolve().parents[1] / "samples"
        expected = {
            "01.jpeg": (
                "Marcus",
                "And me, I just wanted to go home for a visit. "
                "Why would you take away even that?!",
            ),
            "02.png": (
                "X",
                "You were the perfect fulcrum! "
                "I don't know what I would have done without you.",
            ),
        }

        for filename, dialog in expected.items():
            with self.subTest(filename=filename):
                screenshot = Image.open(samples / filename)
                cropped_dialog = default_dialog_region.crop(screenshot)

                self.assertEqual(
                    recognize_dialog_image(cropped_dialog, self.registry),
                    dialog,
                )


if __name__ == "__main__":
    unittest.main()
