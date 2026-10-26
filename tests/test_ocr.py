import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
from pytesseract import pytesseract as pytesseract_runtime

from vntts.ocr import (
    DialogRegion,
    OCRPreviewCancelled,
    OCRRegions,
    OCRResult,
    UncertainFrameRecorder,
    _ocr_budget,
    calculate_ocr_confidence,
    clean_dialog_lines,
    clean_dialog_lines_from_data,
    crop_dialog_text,
    default_dialog_region,
    load_dialog_region,
    parse_dialog_region,
    parse_recognized_dialog,
    preprocess_dialog_image,
    recognize_dialog_image,
    recognize_dialog_image_result,
    recognize_speaker_from_data,
    save_dialog_region,
)
from vntts.tesseract_config import tesseract_config
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class OCRPreprocessingTest(unittest.TestCase):
    def test_cancelled_budget_has_a_typed_runtime_error_without_changing_message(self):
        remaining = _ocr_budget(None, lambda: True)
        with self.assertRaises(RuntimeError) as caught:
            remaining()
        self.assertIsInstance(caught.exception, OCRPreviewCancelled)
        self.assertEqual(str(caught.exception), "OCR preview cancelled")

    @staticmethod
    def _short_dialogue(text="Hmm?", *, light_background=False):
        background, scenery, foreground = (
            (200, 220, 30) if light_background else (30, 55, 230)
        )
        image = Image.new("L", (1000, 100), background)
        draw = ImageDraw.Draw(image)
        draw.rectangle((400, 0, 800, 99), fill=scenery)
        draw.text((15, 15), text, fill=foreground, font_size=20)
        return image

    def test_sparse_short_text_retains_grayscale_and_normalizes_both_polarities(self):
        for text in ("Hmm?", "I", "?"):
            for light_background in (False, True):
                with self.subTest(text=text, light_background=light_background):
                    image = self._short_dialogue(
                        text, light_background=light_background
                    )
                    processed = preprocess_dialog_image(image)
                    self.assertEqual(processed.mode, "L")
                    background = processed.crop((1700, 20, 1900, 160)).getextrema()
                    self.assertEqual(background[0], background[1])
                    self.assertGreaterEqual(background[0], 128)
                    glyphs = processed.crop((20, 20, 150, 90))
                    self.assertLess(glyphs.getextrema()[0], background[0] - 100)
                    self.assertGreater(len(glyphs.getcolors()), 2)

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_ocr_reads_sparse_interjection_over_bright_scenery(self):
        dialogue = self._short_dialogue()
        image = Image.new("L", (1000, 150))
        image.paste(dialogue, (0, 50))
        ImageDraw.Draw(image).text((15, 10), "Everecho", fill=230, font_size=20)
        result = recognize_dialog_image_result(
            image,
            ocr_regions=OCRRegions(
                DialogRegion(0, 0, 1, 1 / 3),
                DialogRegion(0, 1 / 3, 1, 2 / 3),
            ),
        )
        self.assertEqual((result.character, result.text), ("Everecho", "Hmm?"))
        self.assertTrue(result.is_confident())
        self.assertEqual(result.attempts, 1)


class DialogRegionTest(unittest.TestCase):
    def test_mapping_uses_domain_coordinate_validation(self):
        values = {"left": 0, "top": 0.1, "width": 0.5, "height": 0.5}
        self.assertEqual(
            DialogRegion.from_mapping(MappingProxyType(values)),
            DialogRegion(0, 0.1, 0.5, 0.5),
        )
        for invalid, message in (
            ([], "dialog_region must be an object"),
            ({}, "dialog_region left must be a number"),
            ({**values, "left": False}, "numbers"),
            ({**values, "width": float("nan")}, "finite numbers"),
            ({**values, "width": 2}, "fit inside"),
        ):
            with (
                self.subTest(value=invalid),
                self.assertRaisesRegex(ValueError, message),
            ):
                DialogRegion.from_mapping(invalid)

    def test_deep_region_json_reports_load_error_without_rewriting(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "region.json"
            payload = b"[0]"
            path.write_bytes(payload)
            decoder_error = RecursionError("native parser limit")
            with (
                patch(
                    "vntts.json_types.json.loads", side_effect=decoder_error
                ) as decode,
                self.assertRaisesRegex(
                    ValueError, "Unable to load dialog region"
                ) as caught,
            ):
                load_dialog_region(path)
            decode.assert_called_once_with(payload.decode("utf-8"))
            self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
            self.assertIs(caught.exception.__cause__.__cause__, decoder_error)
            self.assertEqual(path.read_bytes(), payload)

    def test_non_finite_coordinates_are_rejected_before_cropping(self):
        for values in (
            (float("nan"), 0.1, 0.5, 0.5),
            (0.0, float("inf"), 0.5, 0.5),
            (0.0, 0.1, float("-inf"), 0.5),
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, "finite numbers"),
            ):
                DialogRegion(*values)

    def test_boolean_coordinates_are_rejected(self):
        for values in (
            (False, 0.1, 0.5, 0.5),
            (0.0, 0.0, True, 0.5),
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, "numbers"),
            ):
                DialogRegion(*values)

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

    def test_region_uses_the_same_bounded_pixels_for_capture_and_crop(self):
        for size, region in (
            ((1919, 1079), DialogRegion(0.5, 0.5, 0.5, 0.5)),
            ((1, 1), DialogRegion(0.9, 0.9, 0.1, 0.1)),
        ):
            with self.subTest(size=size):
                width, height = size
                box = region.capture_box({"width": width, "height": height})
                cropped = region.crop(Image.new("RGB", size))

                self.assertGreaterEqual(box["left"], 0)
                self.assertGreaterEqual(box["top"], 0)
                self.assertLessEqual(box["left"] + box["width"], width)
                self.assertLessEqual(box["top"] + box["height"], height)
                self.assertEqual(cropped.size, (box["width"], box["height"]))

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

    def test_assigned_voice_does_not_replace_recognized_speaker_identity(self):
        registry = CharacterVoiceRegistry(
            [
                CharacterVoice(
                    "Player candidate Rhiannon 420a10dbcd77",
                    "candidate",
                    aliases=("Rhiannon",),
                    source_character="Rhiannon",
                )
            ]
        )
        registry.set_assignment(
            "Rhiannon", "character:playercandidaterhiannon420a10dbcd77"
        )

        character, text = parse_recognized_dialog(
            "Rhiannon\nThat was close!\n", registry
        )
        speaker = recognize_speaker_from_data(
            {
                "text": ["Rhiannon", "That", "was", "close!"],
                "conf": [96, 96, 96, 96],
                "block_num": [1, 2, 2, 2],
                "par_num": [1, 1, 1, 1],
                "line_num": [1, 1, 1, 1],
                "left": [80, 75, 160, 220],
                "top": [30, 150, 150, 150],
                "width": [180, 70, 50, 90],
                "height": [45, 40, 40, 40],
            },
            registry,
            image_width=1000,
            image_height=300,
        )

        self.assertEqual(character, "Rhiannon")
        self.assertEqual(text, "That was close!")
        self.assertEqual(speaker[0], "Rhiannon")

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

    def test_unknown_nameplate_ocr_alias_is_removed_from_text_fallback(self):
        self.assertEqual(
            parse_recognized_dialog("22\nWho are you?\n", self.registry),
            ("???", "Who are you?"),
        )

    def test_numeral_led_narration_is_not_an_unknown_nameplate(self):
        self.assertEqual(
            parse_recognized_dialog("22 people were there.\n", self.registry),
            ("Narrator", "22 people were there."),
        )

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
            ("6", "No."),
            ("Policeman 1", "No."),
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

    def test_unknown_nameplate_ocr_alias_is_normalized_and_removed_from_dialogue(self):
        merged_data = {
            "text": ["22", "Who", "are", "you?"],
            "conf": [95, 96, 96, 96],
            "block_num": [1, 1, 1, 1],
            "par_num": [1, 1, 1, 1],
            "line_num": [1, 1, 1, 1],
            "left": [80, 75, 180, 250],
            "top": [30, 150, 150, 150],
            "width": [50, 80, 55, 90],
            "height": [45, 40, 40, 40],
        }
        sparse_data = {
            "text": ["22", "Who", "are", "you?"],
            "conf": [95, 96, 96, 96],
            "block_num": [1, 2, 2, 2],
            "par_num": [1, 1, 1, 1],
            "line_num": [1, 1, 1, 1],
            "left": [80, 75, 180, 250],
            "top": [30, 150, 150, 150],
            "width": [50, 80, 55, 90],
            "height": [45, 40, 40, 40],
        }
        dialog_data = {"text": ["Who", "are", "you?"], "conf": [96, 96, 96]}

        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300), "black"),
            recognize_text=Mock(return_value="Who are you?\n"),
            recognize_data=Mock(side_effect=[merged_data, sparse_data, dialog_data]),
        )

        self.assertEqual((result.character, result.text), ("???", "Who are you?"))

    def test_unknown_nameplate_glyph_variants_are_isolated_before_dialogue_ocr(self):
        dialog_data = {"text": ["Who", "are", "you?"], "conf": [96, 96, 96]}
        for nameplate in ("2???", "22?"):
            with self.subTest(nameplate=nameplate):
                frame_data = {
                    "text": [nameplate, "Who", "are", "you?"],
                    "conf": [95, 96, 96, 96],
                    "block_num": [1, 2, 2, 2],
                    "par_num": [1, 1, 1, 1],
                    "line_num": [1, 1, 1, 1],
                    "left": [80, 75, 180, 250],
                    "top": [30, 150, 150, 150],
                    "width": [50, 80, 55, 90],
                    "height": [45, 40, 40, 40],
                }

                result = recognize_dialog_image_result(
                    Image.new("RGB", (1000, 300), "black"),
                    recognize_text=Mock(return_value="Who are you?\n"),
                    recognize_data=Mock(side_effect=[frame_data, dialog_data]),
                )

                self.assertEqual(
                    (result.character, result.text), ("???", "Who are you?")
                )

    def test_short_unknown_interjection_drops_trailing_background_glyphs(self):
        merged_data = {
            "text": ["222", "|", "Aa", "XUN", "Hey!"],
            "conf": [95, 73, 80, 70, 90],
            "block_num": [1, 1, 1, 1, 2],
            "par_num": [1, 1, 1, 1, 1],
            "line_num": [1, 1, 1, 1, 1],
            "left": [80, 180, 300, 450, 64],
            "top": [0, 0, 0, 0, 202],
            "width": [60, 25, 45, 70, 134],
            "height": [62, 62, 62, 62, 63],
        }
        sparse_data = {
            "text": ["\\", "\\ ON", "22?", "at", "————", "Hey!"],
            "conf": [30, 30, 95, 55, 45, 90],
            "block_num": [1, 2, 3, 4, 5, 6],
            "par_num": [1, 1, 1, 1, 1, 1],
            "line_num": [1, 1, 1, 1, 1, 1],
            "left": [1239, 1428, 80, 1052, 2373, 64],
            "top": [0, 0, 70, 138, 164, 202],
            "width": [44, 192, 110, 49, 142, 134],
            "height": [72, 133, 65, 18, 6, 63],
        }
        dialog_data = {
            "text": ["Hey!", "-", "™", "\\", "|", "a", "Se"],
            "conf": [90, 73, 0, 0, 57, 62, 53],
        }

        result = recognize_dialog_image_result(
            Image.new("RGB", (2560, 560), "black"),
            recognize_text=Mock(return_value="Hey! - ™ \\\n| a\nSe\n"),
            recognize_data=Mock(side_effect=[merged_data, sparse_data, dialog_data]),
        )

        self.assertEqual((result.character, result.text), ("???", "Hey!"))
        self.assertEqual(result.confidence, 90)

    def test_unknown_nameplate_normalization_preserves_other_numeric_npc_names(self):
        for nameplate in ("6", "37", "222"):
            with self.subTest(nameplate=nameplate):
                speaker = recognize_speaker_from_data(
                    {
                        "text": [nameplate, "No."],
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

                self.assertEqual(speaker[0], nameplate)

    def test_geometry_keeps_short_dialogue_below_an_unknown_nameplate(self):
        frame_data = {
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
        recognize_text = Mock(return_value="This ...\n")
        recognize_data = Mock(
            side_effect=[frame_data, {"text": ["This", "..."], "conf": [95, 94]}]
        )
        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300), "black"),
            recognize_text=recognize_text,
            recognize_data=recognize_data,
        )

        self.assertEqual((result.character, result.text), ("Hotelier", "This ..."))
        self.assertEqual((result.profile, result.attempts), ("sauvola", 1))
        recognize_text.assert_called_once()
        self.assertEqual(recognize_data.call_count, 2)

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
        )

        self.assertEqual(result.character, "Fatutu")
        self.assertEqual(result.text, "Kamuta ...")
        self.assertIs(result.speaker_image, recognize_data.call_args_list[0].args[0])
        self.assertIs(result.processed_image, recognize_text.call_args.args[0])
        self.assertIs(result.processed_image, recognize_data.call_args.args[0])
        self.assertLess(
            result.processed_image.height,
            recognize_data.call_args_list[0].args[0].height,
        )
        self.assertEqual(
            [call.kwargs["config"] for call in recognize_data.call_args_list],
            [tesseract_config(6), tesseract_config(11), tesseract_config(6)],
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

    def test_invalid_confidence_words_do_not_poison_valid_evidence(self):
        for value in (
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            "nan",
            "inf",
            "-inf",
            "1e999",
            10**400,
            -(10**400),
            100.01,
            1e308,
            "1e308",
            -1,
        ):
            with self.subTest(value=value):
                confidence = calculate_ocr_confidence(
                    {"text": ["noise", "reliable"], "conf": [value, "90"]}
                )
                self.assertEqual(confidence, 90)

        for value in (0, 100, 89.5, "90"):
            with self.subTest(valid=value):
                self.assertEqual(
                    calculate_ocr_confidence({"text": ["word"], "conf": [value]}),
                    float(value),
                )

    def test_invalid_confidence_keeps_single_pass_result_uncertain(self):
        for confidence in (float("nan"), float("inf"), float("-inf"), "invalid"):
            with self.subTest(confidence=confidence):
                recognize_text = Mock(return_value="Bad result")
                recognize_data = Mock(
                    return_value={"text": ["Bad", "result"], "conf": [confidence] * 2}
                )
                result = recognize_dialog_image_result(
                    Image.new("RGB", (320, 120)),
                    recognize_text=recognize_text,
                    recognize_data=recognize_data,
                )
                self.assertEqual(result.text, "Bad result")
                self.assertEqual((result.profile, result.attempts), ("sauvola", 1))
                self.assertEqual(result.confidence, 0)
                self.assertFalse(result.is_confident())
                recognize_text.assert_called_once()
                recognize_data.assert_called_once()

    def test_invalid_timeout_is_rejected_before_preprocessing(self):
        for value in (float("nan"), float("inf"), float("-inf"), 10**400):
            with self.subTest(value=value):
                recognize_text = Mock()
                recognize_data = Mock()
                with (
                    patch("vntts.ocr.preprocess_dialog_image") as preprocess,
                    self.assertRaisesRegex(ValueError, "positive.*finite"),
                ):
                    recognize_dialog_image_result(
                        Image.new("RGB", (32, 16)),
                        recognize_text=recognize_text,
                        recognize_data=recognize_data,
                        timeout=value,
                    )
                preprocess.assert_not_called()
                recognize_text.assert_not_called()
                recognize_data.assert_not_called()

    def test_positive_finite_timeout_reaches_both_recognizers(self):
        recognize_text = Mock(return_value="Reliable dialogue.")
        recognize_data = Mock(return_value={"text": ["Reliable"], "conf": [90]})
        with patch("vntts.ocr.monotonic", return_value=1000):
            result = recognize_dialog_image_result(
                Image.new("RGB", (32, 16)),
                recognize_text=recognize_text,
                recognize_data=recognize_data,
                timeout=2,
            )
        self.assertEqual(result.text, "Reliable dialogue.")
        self.assertEqual(recognize_text.call_args.kwargs["timeout"], 2)
        self.assertEqual(recognize_data.call_args.kwargs["timeout"], 2)

    def test_malformed_group_ids_do_not_hide_later_string_id_lines(self):
        for field in ("block_num", "par_num", "line_num"):
            for invalid in ([], {}, ([1],)):
                with self.subTest(field=field, invalid=invalid):
                    data = {
                        "text": ["bad", "Marcus", "Hello there."],
                        "block_num": ["block"] * 3,
                        "par_num": ["paragraph"] * 3,
                        "line_num": ["noise", "name", "dialog"],
                        "left": [40] * 3,
                        "top": [0, 40, 180],
                        "width": [120] * 3,
                        "height": [45] * 3,
                    }
                    data[field][0] = invalid
                    speaker = recognize_speaker_from_data(
                        data, self.registry, image_width=1000, image_height=400
                    )
                    self.assertIsNotNone(speaker)
                    self.assertEqual(speaker[0], "Marcus")
                    self.assertEqual(speaker[1].text, "Marcus")

    def test_low_confidence_result_keeps_its_single_pass_image(self):
        recognize_text = Mock(return_value="Garbled text")
        recognize_data = Mock(
            return_value={"text": ["Garbled", "text"], "conf": [25, 30]}
        )
        result = recognize_dialog_image_result(
            Image.new("RGB", (320, 120), "black"),
            recognize_text=recognize_text,
            recognize_data=recognize_data,
            minimum_confidence=60,
        )

        self.assertEqual(result.text, "Garbled text")
        self.assertEqual((result.profile, result.attempts), ("sauvola", 1))
        self.assertFalse(result.is_confident())
        self.assertAlmostEqual(result.confidence, (25 * 7 + 30 * 4) / 11)
        self.assertIs(result.processed_image, recognize_text.call_args.args[0])
        self.assertIs(result.processed_image, recognize_data.call_args.args[0])
        self.assertEqual(result.processed_image.mode, "L")
        self.assertEqual(result.processed_image.getextrema(), (255, 255))
        recognize_text.assert_called_once()
        recognize_data.assert_called_once()

    def test_confident_result_uses_a_single_ocr_pass(self):
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
        self.assertEqual((result.profile, result.attempts), ("sauvola", 1))
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
        result = OCRResult("Marcus", "Maybe this text", 41.5, "sauvola", 1)
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
            self.assertEqual(metadata["preprocessing_profile"], "sauvola")
            self.assertIsNone(duplicate)

            recorder.reset()
            self.assertIsNotNone(recorder.record(image, result, 60))

    def test_uncertain_frame_recorder_does_not_publish_image_when_metadata_fails(self):
        result = OCRResult("Marcus", "Maybe this text", 41.5, "sauvola", 1)
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            recorder = UncertainFrameRecorder(directory)

            with self.assertRaises(TypeError):
                recorder.record(Image.new("RGB", (32, 16), "black"), result, object())

            self.assertEqual(list(directory.iterdir()), [])

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_combined_psm6_keeps_real_dialog_result_with_fewer_processes(self):
        samples = Path(__file__).resolve().parents[1] / "samples"
        original = pytesseract_runtime.run_tesseract
        for filename in ("01.jpeg", "02.png"):
            with (
                self.subTest(filename=filename),
                Image.open(samples / filename) as source,
            ):
                image = default_dialog_region.crop(source)
                with patch.object(
                    pytesseract_runtime, "run_tesseract", wraps=original
                ) as run:
                    baseline = recognize_dialog_image_result(
                        image,
                        self.registry,
                        recognize_text=pytesseract_runtime.image_to_string,
                        recognize_data=pytesseract_runtime.image_to_data,
                    )
                    baseline_launches = run.call_count
                    run.reset_mock()
                    combined = recognize_dialog_image_result(image, self.registry)

                self.assertEqual(combined, baseline)
                self.assertLess(run.call_count, baseline_launches)

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
