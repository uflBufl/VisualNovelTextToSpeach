import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image
from pytesseract import pytesseract as pytesseract_runtime

from vntts.ocr import (
    DialogRegion,
    OCRPreprocessingProfile,
    OCRResult,
    UncertainFrameRecorder,
    calculate_ocr_confidence,
    clean_dialog_lines,
    clean_dialog_lines_from_data,
    crop_dialog_text,
    default_dialog_region,
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
    def test_tesseract_thread_limit_does_not_leak_into_application_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            arguments = pytesseract_runtime.subprocess_args()

            self.assertEqual(arguments["env"]["OMP_THREAD_LIMIT"], "1")
            self.assertNotIn("OMP_THREAD_LIMIT", os.environ)

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

    def test_unknown_multiword_speaker_is_detected_from_text_line_structure(self):
        character, text = parse_recognized_dialog(
            "Captain Osborn\n"
            "I don't believe you two are acquainted with our Storm Reaction "
            "Protocols, are you?\n",
            self.registry,
        )

        self.assertEqual(character, "Captain Osborn")
        self.assertEqual(
            text,
            "I don't believe you two are acquainted with our Storm Reaction "
            "Protocols, are you?",
        )

    def test_punctuation_only_ellipsis_is_preserved_as_silent_dialogue(self):
        character, text = parse_recognized_dialog(
            "Rhiannon\n...\n",
            self.registry,
        )

        self.assertEqual(character, "Rhiannon")
        self.assertEqual(text, "...")

    def test_numbered_npc_is_detected_from_text_line_structure(self):
        character, text = parse_recognized_dialog(
            "Policeman 2\nYou need to leave this area now.\n",
            self.registry,
        )

        self.assertEqual(character, "Policeman 2")
        self.assertEqual(text, "You need to leave this area now.")

    def test_unknown_multiword_speaker_is_detected_from_ocr_geometry(self):
        data = {
            "text": [
                "Captain",
                "Osborn",
                "I",
                "don't",
                "believe",
                "you",
                "two",
                "are",
                "acquainted",
                "you?",
            ],
            "conf": [96] * 10,
            "block_num": [1] * 10,
            "par_num": [1] * 10,
            "line_num": [1, 1, 2, 2, 2, 2, 2, 2, 2, 3],
            "left": [49, 287, 28, 60, 196, 376, 477, 578, 666, 26],
            "top": [49, 48, 160, 157, 157, 170, 164, 170, 157, 226],
            "width": [218, 203, 14, 120, 164, 84, 83, 70, 258, 109],
            "height": [61, 49, 38, 41, 41, 37, 33, 27, 50, 47],
        }

        speaker = recognize_speaker_from_data(
            data,
            self.registry,
            image_width=1965,
            image_height=340,
        )

        self.assertEqual(speaker[0], "Captain Osborn")
        self.assertEqual(speaker[1].text, "Captain Osborn")

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

    def test_unknown_speaker_geometry_accepts_complete_short_dialogue(self):
        for speaker_name, dialogue in (
            ("Hotelier", "This ..."),
            ("Policeman 2", "No."),
            ("37", "..."),
        ):
            with self.subTest(speaker=speaker_name, dialogue=dialogue):
                speaker = recognize_speaker_from_data(
                    {
                        "text": [speaker_name, dialogue],
                        "conf": [96, 95],
                        "block_num": [1, 2],
                        "par_num": [1, 1],
                        "line_num": [1, 1],
                        "left": [80, 75],
                        "top": [30, 150],
                        "width": [180, 100],
                        "height": [45, 40],
                    },
                    image_width=1000,
                    image_height=300,
                )

                self.assertEqual(speaker[0], speaker_name)

    def test_confident_orphaned_nameplate_does_not_hide_short_dialogue(self):
        orphaned_nameplate = {
            "text": [":", "Hotelier"],
            "conf": [92, 96],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 1],
            "left": [40, 80],
            "top": [30, 30],
            "width": [20, 180],
            "height": [45, 45],
        }
        complete_frame = {
            "text": ["Hotelier", "This", "..."],
            "conf": [96, 95, 94],
            "block_num": [1, 2, 2],
            "par_num": [1, 1, 1],
            "line_num": [1, 1, 1],
            "left": [80, 75, 170],
            "top": [30, 150, 150],
            "width": [180, 80, 35],
            "height": [45, 40, 40],
        }
        false_speaker_frame = {
            "text": ["TTS", "&", "Hotelier"],
            "conf": [90, 70, 96],
            "block_num": [1, 2, 2],
            "par_num": [1, 1, 1],
            "line_num": [1, 1, 1],
            "left": [40, 75, 100],
            "top": [5, 150, 150],
            "width": [100, 20, 180],
            "height": [35, 40, 40],
        }
        false_dialog_data = {"text": ["e", "Hotelier"], "conf": [60, 96]}
        dialog_data = {"text": ["This", "..."], "conf": [95, 94]}

        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300), "black"),
            recognize_text=Mock(
                side_effect=[": Hotelier\n", "e Hotelier\n", "This ...\n"]
            ),
            recognize_data=Mock(
                side_effect=[
                    orphaned_nameplate,
                    orphaned_nameplate,
                    false_speaker_frame,
                    false_dialog_data,
                    complete_frame,
                    dialog_data,
                ]
            ),
            profiles=(
                OCRPreprocessingProfile("balanced", 1.8, 180),
                OCRPreprocessingProfile("dark-background", 2.2, 155),
                OCRPreprocessingProfile("light-background", 1.5, 205),
            ),
            minimum_confidence=60,
        )

        self.assertEqual(result.character, "Hotelier")
        self.assertEqual(result.text, "This ...")
        self.assertEqual(result.profile, "light-background")

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
        self.assertEqual(clean_dialog_lines("...\n…\n"), ["...", "…"])
        self.assertEqual(clean_dialog_lines("___\n"), [])

    def test_dialog_cleanup_removes_low_confidence_background_suffix(self):
        data = {
            "text": ["Alright,", "that", "makes", "five.", "ae"],
            "conf": [96, 95, 96, 96, 42],
        }

        self.assertEqual(
            clean_dialog_lines_from_data(
                data,
                ["Alright, that makes five. ae"],
            ),
            ["Alright, that makes five."],
        )

    def test_dialog_cleanup_preserves_confident_sentence_after_sentence(self):
        data = {
            "text": ["Hello.", "How", "are", "you?"],
            "conf": [96, 92, 94, 95],
        }

        self.assertEqual(
            clean_dialog_lines_from_data(data, ["Hello. How are you?"]),
            ["Hello. How are you?"],
        )

    def test_recognition_keeps_ellipsis_below_a_known_nameplate(self):
        frame_data = {
            "text": ["Marcus", "..."],
            "conf": [96, 94],
            "block_num": [1, 1],
            "par_num": [1, 2],
            "line_num": [1, 1],
            "left": [50, 50],
            "top": [30, 150],
            "width": [160, 60],
            "height": [45, 40],
        }
        dialog_data = {"text": ["..."], "conf": [94]}

        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300), "black"),
            self.registry,
            recognize_text=Mock(return_value="...\n"),
            recognize_data=Mock(side_effect=[frame_data, dialog_data]),
            profiles=(OCRPreprocessingProfile("balanced", 1.8, 180),),
        )

        self.assertEqual(result.character, "Marcus")
        self.assertEqual(result.text, "...")
        self.assertGreaterEqual(result.confidence, 90)

    def test_recognition_removes_background_suffix_from_dialog_result(self):
        frame_data = {
            "text": ["Rhiannon", "Alright,", "that", "makes", "five.", "ae"],
            "conf": [96, 96, 95, 96, 96, 42],
            "block_num": [1] * 6,
            "par_num": [1, 2, 2, 2, 2, 2],
            "line_num": [1] * 6,
            "left": [50, 40, 190, 280, 400, 900],
            "top": [30, 150, 150, 150, 150, 150],
            "width": [160, 130, 70, 100, 80, 30],
            "height": [45, 40, 40, 40, 40, 20],
        }
        dialog_data = {
            "text": ["Alright,", "that", "makes", "five.", "ae"],
            "conf": [96, 95, 96, 96, 42],
        }

        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300), "black"),
            recognize_text=Mock(return_value="Alright, that makes five. ae\n"),
            recognize_data=Mock(side_effect=[frame_data, dialog_data]),
            profiles=(OCRPreprocessingProfile("balanced", 1.8, 180),),
            minimum_confidence=0,
        )

        self.assertEqual(result.character, "Rhiannon")
        self.assertEqual(result.text, "Alright, that makes five.")

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

    def test_uncertain_frame_recorder_does_not_publish_image_when_metadata_fails(self):
        result = OCRResult("Marcus", "Maybe this text", 41.5, "balanced", 3)
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            recorder = UncertainFrameRecorder(directory)

            with self.assertRaises(TypeError):
                recorder.record(Image.new("RGB", (32, 16), "black"), result, object())

            self.assertEqual(list(directory.iterdir()), [])

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
