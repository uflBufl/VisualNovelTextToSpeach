import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from vntts.dialog_capture import (
    CapturedDialogFrame,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
    fingerprint_dialog_render_activity,
    recognize_live_frame,
)
from vntts.ocr import (
    DialogRegion,
    OCRPreprocessingProfile,
    OCRRegions,
    OCRResult,
    combine_ocr_regions,
    default_dialog_region,
    load_dialog_region,
    load_ocr_regions,
    recognize_dialog_image_result,
    save_dialog_region,
)
from vntts.ocr_corrections import OCRCorrectionDictionary
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class SplitOCRTest(unittest.TestCase):
    def setUp(self):
        self.regions = OCRRegions(
            DialogRegion(0.1, 0.1, 0.3, 0.2),
            DialogRegion(0.1, 0.4, 0.7, 0.4),
        )

    def test_regions_validate_bounds_types_and_nonoverlap(self):
        self.assertEqual(OCRRegions.from_mapping(self.regions.to_json()), self.regions)
        with self.assertRaisesRegex(ValueError, "overlap"):
            OCRRegions(self.regions.speaker, self.regions.speaker)
        for invalid in ({}, {"speaker": False}, {"speaker": {"left": True}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OCRRegions.from_mapping(invalid)

    def test_combine_retains_full_capture_for_completion_indicator(self):
        capture = DialogRegion(0, 0.6, 1, 0.4)
        speaker = DialogRegion(0.1, 0.65, 0.2, 0.08)
        dialogue = DialogRegion(0.1, 0.75, 0.7, 0.12)
        actual, regions = combine_ocr_regions(speaker, dialogue, capture)
        self.assertEqual(actual, capture)
        image = Image.new("RGB", (1000, 1000))
        self.assertEqual(regions.speaker.crop(capture.crop(image)).size, (200, 80))
        self.assertEqual(regions.dialogue.crop(capture.crop(image)).size, (700, 120))

    def test_global_configuration_roundtrips_and_legacy_save_clears_layout(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dialog-region.json"
            save_dialog_region(
                DialogRegion(0, 0.6, 1, 0.4), path, ocr_regions=self.regions
            )
            self.assertEqual(load_ocr_regions(path), self.regions)
            self.assertEqual(load_dialog_region(path), DialogRegion(0, 0.6, 1, 0.4))
            save_dialog_region(DialogRegion(0, 0.5, 1, 0.5), path)
            self.assertIsNone(load_ocr_regions(path))

    def test_short_dialogue_never_parses_name_or_needs_nameplate_geometry(self):
        registry = CharacterVoiceRegistry([CharacterVoice("Marcus", "marcus")])
        for text in ("I", "No.", "Hey!", "?", "...", "2"):
            with self.subTest(text=text):
                data = Mock(
                    side_effect=[
                        {"text": ["Mareus"], "conf": [96]},
                        {"text": [text], "conf": [94]},
                    ]
                )
                read = Mock(return_value=text)
                result = recognize_dialog_image_result(
                    Image.new("RGB", (1000, 300)),
                    registry,
                    recognize_text=read,
                    recognize_data=data,
                    ocr_regions=self.regions,
                )
                self.assertEqual((result.character, result.text), ("Marcus", text))
                self.assertEqual(result.speaker_confidence, 96)
                self.assertEqual(result.dialogue_confidence, 94)
                self.assertIs(result.speaker_image, data.call_args_list[0].args[0])
                self.assertIs(result.processed_image, read.call_args.args[0])
                self.assertEqual(result.speaker_image.size, (620, 140))
                self.assertEqual(result.processed_image.size, (1400, 240))
                self.assertEqual(data.call_args_list[0].kwargs["config"], "--psm 7")
                self.assertEqual(read.call_args.kwargs["config"], "--psm 6")
                corrected = OCRCorrectionDictionary(
                    {text: "Corrected line."}
                ).correct_result(result)
                self.assertEqual(corrected.text, "Corrected line.")
                self.assertIs(corrected.speaker_image, result.speaker_image)
                self.assertIs(corrected.processed_image, result.processed_image)

    def test_parts_choose_profiles_independently_and_reject_uncertain_name(self):
        data = Mock(
            side_effect=[
                {"text": ["Marcus"], "conf": [20]},
                {"text": ["Marcus"], "conf": [45]},
                {"text": ["Hey!"], "conf": [96]},
            ]
        )
        read = Mock(return_value="Hey!")
        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300)),
            recognize_text=read,
            recognize_data=data,
            profiles=(
                OCRPreprocessingProfile("first", 1, 170),
                OCRPreprocessingProfile("second", 2, 200),
            ),
            ocr_regions=self.regions,
        )
        self.assertFalse(result.is_confident())
        self.assertEqual(result.confidence, 45)
        self.assertEqual(result.speaker_confidence, 45)
        self.assertEqual(result.dialogue_confidence, 96)
        self.assertIs(result.speaker_image, data.call_args_list[1].args[0])
        self.assertIs(result.processed_image, read.call_args.args[0])

    def test_blank_name_is_narration_without_stealing_first_dialogue_word(self):
        data = Mock(
            side_effect=[{"text": [], "conf": []}, {"text": ["Hey!"], "conf": [96]}]
        )
        result = recognize_dialog_image_result(
            Image.new("RGB", (1000, 300)),
            recognize_text=Mock(return_value="Hey!"),
            recognize_data=data,
            profiles=(OCRPreprocessingProfile("first", 1, 170),),
            ocr_regions=self.regions,
        )
        self.assertEqual((result.character, result.text), ("Narrator", "Hey!"))
        self.assertTrue(result.is_confident())

    def test_stability_ignores_ui_outside_configured_ocr_regions(self):
        image = Image.new("RGB", (1000, 300))
        draw = ImageDraw.Draw(image)
        draw.text((120, 35), "Marcus", fill="white")
        draw.text((120, 145), "Hey!", fill="white")
        changed_ui = image.copy()
        ImageDraw.Draw(changed_ui).rectangle((850, 40, 990, 290), fill="white")
        first = CapturedDialogFrame(image, 0, self.regions)
        second = CapturedDialogFrame(changed_ui, 0, self.regions)
        self.assertTrue(dialog_glyphs_visible(first))
        self.assertEqual(
            fingerprint_dialog_frame(first), fingerprint_dialog_frame(second)
        )
        self.assertEqual(
            fingerprint_dialog_render_activity(first),
            fingerprint_dialog_render_activity(second),
        )
        changed_dialogue = image.copy()
        ImageDraw.Draw(changed_dialogue).text((155, 145), "Wait!", fill="white")
        third = CapturedDialogFrame(changed_dialogue, 0, self.regions)
        self.assertNotEqual(
            fingerprint_dialog_frame(first), fingerprint_dialog_frame(third)
        )
        self.assertNotEqual(
            fingerprint_dialog_render_activity(first),
            fingerprint_dialog_render_activity(third),
        )

    def test_live_capture_uses_frozen_layout_and_reports_both_inputs(self):
        image = Image.new("RGB", (1000, 300))
        result = OCRResult(
            "Marcus",
            "Hey!",
            95,
            "balanced",
            1,
            processed_image=Image.new("L", (100, 40)),
            speaker_image=Image.new("L", (40, 20)),
            speaker_confidence=95,
            dialogue_confidence=96,
        )
        snapshots = []
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=result
        ) as recognizer:
            recognized = recognize_live_frame(
                CapturedDialogFrame(image, 0, self.regions),
                diagnostic_handler=snapshots.append,
            )
        self.assertEqual(recognized, ("Marcus", "Hey!"))
        self.assertEqual(recognizer.call_args.kwargs["ocr_regions"], self.regions)
        self.assertIs(snapshots[0].speaker_image, result.speaker_image)
        self.assertIs(snapshots[0].processed_image, result.processed_image)
        self.assertEqual(snapshots[0].speaker_confidence, 95)
        self.assertEqual(snapshots[0].dialogue_confidence, 96)

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_tight_x_nameplate_is_read_without_merging_dialogue(self):
        sample = Path(__file__).resolve().parents[1] / "samples" / "02.png"
        with Image.open(sample) as screenshot:
            image = default_dialog_region.crop(screenshot)
        regions = OCRRegions(
            DialogRegion(
                737 / 3332 - 0.005, 190 / 600 - 0.02, 49 / 3332 + 0.01, 54 / 600 + 0.04
            ),
            DialogRegion(0.19, 0.45, 0.71, 0.35),
        )
        result = recognize_dialog_image_result(image, ocr_regions=regions)
        self.assertEqual(result.character, "X")
        self.assertEqual(
            result.text,
            "You were the perfect fulcrum! I don't know what I would have done without you.",
        )
        self.assertTrue(result.is_confident())


if __name__ == "__main__":
    unittest.main()
