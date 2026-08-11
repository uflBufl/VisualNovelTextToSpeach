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
    clean_dialog_lines,
    crop_dialog_text,
    default_dialog_region,
    detect_choice_layout,
    load_dialog_region,
    parse_dialog_region,
    parse_recognized_dialog,
    recognize_dialog_image,
    recognize_dialog_image_result,
    recognize_speaker_from_data,
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

    def test_unknown_speaker_is_detected_from_text_line_structure(self):
        character, text = parse_recognized_dialog(
            "Kamuta\nThese old ones are enough to carry everyone.\n",
            self.registry,
        )

        self.assertEqual(character, "Kamuta")
        self.assertEqual(text, "These old ones are enough to carry everyone.")

    def test_unknown_speaker_is_detected_from_ocr_geometry(self):
        data = {
            "text": ["Kamuta", "These", "old", "ones", "are", "enough"],
            "conf": [96, 95, 95, 94, 96, 95],
            "block_num": [1, 1, 1, 1, 1, 1],
            "par_num": [1, 2, 2, 2, 2, 2],
            "line_num": [1, 1, 1, 1, 1, 1],
            "left": [145, 109, 270, 360, 480, 570],
            "top": [84, 272, 272, 272, 272, 272],
            "width": [384, 140, 70, 100, 70, 140],
            "height": [80, 88, 88, 88, 88, 88],
        }

        speaker = recognize_speaker_from_data(
            data,
            self.registry,
            image_width=2000,
            image_height=600,
        )

        self.assertEqual(speaker[0], "Kamuta")
        self.assertEqual(speaker[1].text, "Kamuta")

    def test_geometry_prefers_known_speaker_over_uppercase_ui_noise(self):
        data = {
            "text": ["AUTO", "SKIP", "Mareus", "Hello", "from", "the", "suitcase"],
            "conf": [90] * 7,
            "block_num": [1] * 7,
            "par_num": [1, 1, 2, 3, 3, 3, 3],
            "line_num": [1, 1, 1, 1, 1, 1, 1],
            "left": [1500, 1640, 120, 100, 260, 380, 470],
            "top": [20, 20, 80, 220, 220, 220, 220],
            "width": [120, 120, 300, 130, 90, 60, 160],
            "height": [50] * 7,
        }

        speaker = recognize_speaker_from_data(
            data,
            self.registry,
            image_width=2000,
            image_height=500,
        )

        self.assertEqual(speaker[0], "Marcus")

    def test_separated_short_rows_are_detected_as_a_choice_menu(self):
        data = {
            "text": ["Ask", "about", "the", "island", "Leave", "quietly"],
            "conf": [95] * 6,
            "block_num": [1] * 6,
            "par_num": [1, 1, 1, 1, 2, 2],
            "line_num": [1] * 6,
            "left": [300, 370, 470, 540, 305, 390],
            "top": [80, 80, 80, 80, 180, 180],
            "width": [60, 90, 50, 90, 70, 100],
            "height": [35] * 6,
        }

        self.assertTrue(detect_choice_layout(data, image_width=1200))

    def test_tightly_wrapped_dialog_is_not_a_choice_menu(self):
        data = {
            "text": ["This", "is", "normal", "wrapped", "dialogue"],
            "conf": [95] * 5,
            "block_num": [1] * 5,
            "par_num": [1, 1, 1, 2, 2],
            "line_num": [1] * 5,
            "left": [40, 120, 160, 40, 180],
            "top": [80, 80, 80, 120, 120],
            "width": [70, 30, 110, 120, 130],
            "height": [35] * 5,
        }

        self.assertFalse(detect_choice_layout(data, image_width=1200))

    def test_sparse_layout_fallback_recovers_nameplate_merged_with_separator(self):
        merged_data = {
            "text": ["Fatutu", "fe", "Oe", ">", "Kamuta", "...", "~"],
            "conf": [96, 25, 18, 10, 94, 80, 12],
            "block_num": [1, 1, 1, 1, 1, 1, 1],
            "par_num": [1, 1, 1, 1, 2, 2, 2],
            "line_num": [1, 1, 1, 1, 1, 1, 1],
            "left": [50, 200, 260, 330, 35, 180, 230],
            "top": [6, 6, 6, 6, 115, 115, 115],
            "width": [140, 40, 50, 30, 130, 35, 20],
            "height": [88, 88, 88, 88, 37, 37, 37],
        }
        sparse_data = {
            "text": ["Fatutu", "Kamuta", "..."],
            "conf": [96, 94, 80],
            "block_num": [1, 2, 2],
            "par_num": [1, 1, 1],
            "line_num": [1, 1, 1],
            "left": [50, 35, 180],
            "top": [41, 124, 124],
            "width": [140, 130, 35],
            "height": [35, 28, 28],
        }
        dialog_data = {
            "text": ["Kamuta", "...", "_"],
            "conf": [94, 80, 12],
        }
        recognize_data = Mock(side_effect=[merged_data, sparse_data, dialog_data])
        recognize_text = Mock(return_value="Kamuta ... _\n")
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Fatutu", "reverse-1999-fatutu")]
        )

        result = recognize_dialog_image_result(
            Image.new("RGB", (1476, 258), "black"),
            registry,
            recognize_text=recognize_text,
            recognize_data=recognize_data,
            profiles=(OCRPreprocessingProfile("balanced", 1.8, 180),),
        )

        self.assertEqual(result.character, "Fatutu")
        self.assertEqual(result.text, "Kamuta ...")
        self.assertEqual(
            [call.kwargs["config"] for call in recognize_data.call_args_list],
            ["--psm 6", "--psm 11", "--psm 6"],
        )

    def test_dialog_cleanup_removes_only_trailing_non_speech_glyphs(self):
        self.assertEqual(clean_dialog_lines("Kamuta ... _\n"), ["Kamuta ..."])
        self.assertEqual(clean_dialog_lines("Wait!\n"), ["Wait!"])

    def test_dialog_crop_preserves_the_right_edge_of_long_text(self):
        image = Image.new("RGB", (1000, 400), "white")
        speaker_line = recognize_speaker_from_data(
            {
                "text": ["Kamuta", "A", "long", "dialogue", "line"],
                "conf": [95] * 5,
                "block_num": [1] * 5,
                "par_num": [1, 2, 2, 2, 2],
                "line_num": [1] * 5,
                "left": [50, 40, 100, 180, 300],
                "top": [40, 180, 180, 180, 180],
                "width": [120, 40, 70, 100, 60],
                "height": [50] * 5,
            },
            image_width=image.width,
            image_height=image.height,
        )[1]

        cropped = crop_dialog_text(image, speaker_line)

        self.assertEqual(cropped.getbbox()[2], cropped.width)
        self.assertEqual(cropped.width, 970)

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

    def test_configured_ocr_language_is_passed_to_tesseract(self):
        recognize_text = Mock(return_value="A reliable result.")
        recognize_data = Mock(
            return_value={"text": ["A", "reliable", "result"], "conf": [95, 94, 96]}
        )

        recognize_dialog_image_result(
            Image.new("RGB", (320, 120), "black"),
            recognize_text=recognize_text,
            recognize_data=recognize_data,
            language="eng+jpn",
        )

        self.assertEqual(recognize_text.call_args.kwargs["lang"], "eng+jpn")
        self.assertEqual(recognize_data.call_args.kwargs["lang"], "eng+jpn")

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
