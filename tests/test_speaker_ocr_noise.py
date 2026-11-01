import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image, ImageDraw

from vntts.ocr import (
    DialogRegion,
    OCRRegions,
    is_speaker_ocr_noise,
    recognize_dialog_image_result,
)
from vntts.ocr_backend import RapidOCRBackend


class SpeakerOCRNoiseTest(unittest.TestCase):
    def test_plausible_names_and_unknown_aliases_keep_their_confidence_guard(self):
        for name in (
            "X",
            "I",
            "6",
            "37",
            "222",
            "???",
            "22",
            "22?",
            "2???",
            "Unlisted NPC",
            "lowercase",
            "Mrs. Owen",
            "An-an Lee",
            "D’Arcy",
            "Li Po",
            "李白",
            "A B",
            "Jose\u0301",
            "",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_speaker_ocr_noise(name, 27))

    def test_nonempty_noise_needs_lexical_evidence_and_name_confidence(self):
        for name in ("f ; > | |", "al . :", "= SC”", "— ~- es . | ."):
            with self.subTest(name=name):
                self.assertTrue(is_speaker_ocr_noise(name, 59.99))
                self.assertFalse(is_speaker_ocr_noise(name, 60))
                self.assertFalse(is_speaker_ocr_noise(name, 45, 40))
                self.assertTrue(is_speaker_ocr_noise(name, 35, 40))
        self.assertTrue(is_speaker_ocr_noise("\\", 93))

    @staticmethod
    def recognize(backend, name, confidence, dialogue_confidence):
        image = Image.new("RGB", (400, 200), "black")
        ImageDraw.Draw(image).text((45, 25), "X", fill="white")
        regions = OCRRegions(
            DialogRegion(0.1, 0.1, 0.3, 0.2), DialogRegion(0.1, 0.4, 0.7, 0.4)
        )
        if backend == "tesseract":
            return recognize_dialog_image_result(
                image,
                ocr_regions=regions,
                recognize_text=Mock(return_value="Hey!"),
                recognize_data=Mock(
                    side_effect=[
                        {"text": [name], "conf": [confidence]},
                        {"text": ["Hey!"], "conf": [dialogue_confidence]},
                    ]
                ),
            )
        output = iter(
            SimpleNamespace(
                boxes=[[[0, 0], [20, 0], [20, 20], [0, 20]]],
                txts=[text],
                scores=[score / 100],
            )
            for text, score in ((name, confidence), ("Hey!", dialogue_confidence))
        )
        return RapidOCRBackend(lambda *_a, **_kw: next(output)).recognize(
            image, ocr_regions=regions
        )

    def test_both_backends_recover_narration_and_retain_raw_noise_diagnostics(self):
        for backend in ("tesseract", "rapidocr"):
            for name, confidence in (("al . :", 36), ("\\", 93)):
                for dialogue_confidence in (95, 25):
                    with self.subTest(
                        backend=backend, name=name, dialogue=dialogue_confidence
                    ):
                        result = self.recognize(
                            backend, name, confidence, dialogue_confidence
                        )
                        self.assertEqual(
                            (result.character, result.text), ("Narrator", "Hey!")
                        )
                        self.assertEqual(result.confidence, dialogue_confidence)
                        self.assertEqual(
                            result.is_confident(), dialogue_confidence >= 60
                        )
                        self.assertAlmostEqual(result.speaker_confidence, confidence)
                        self.assertEqual(
                            result.dialogue_confidence, dialogue_confidence
                        )
                        self.assertIn(name, result.speaker_note)
                        self.assertIsNotNone(result.speaker_image)

    def test_plausible_uncertain_names_remain_uncertain_in_both_backends(self):
        for backend in ("tesseract", "rapidocr"):
            for name in ("X", "37", "???", "Unlisted NPC", "Mrs. Owen"):
                with self.subTest(backend=backend, name=name):
                    result = self.recognize(backend, name, 27, 95)
                    self.assertEqual(result.character, name)
                    self.assertEqual(result.confidence, 27)
                    self.assertFalse(result.is_confident())
                    self.assertEqual(result.speaker_note, "")

    def test_empty_name_uses_narrator_and_dialogue_confidence_in_both_backends(self):
        for backend in ("tesseract", "rapidocr"):
            for dialogue_confidence in (25, 95):
                with self.subTest(backend=backend, dialogue=dialogue_confidence):
                    result = self.recognize(backend, "", 0, dialogue_confidence)
                    self.assertEqual(
                        (result.character, result.text), ("Narrator", "Hey!")
                    )
                    self.assertEqual(result.confidence, dialogue_confidence)
                    self.assertIsNone(result.speaker_confidence)
                    self.assertEqual(result.is_confident(), dialogue_confidence >= 60)
                    self.assertEqual(result.speaker_note, "")
