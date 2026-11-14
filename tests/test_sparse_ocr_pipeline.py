import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from vntts.ocr import (
    DialogRegion,
    OCRLine,
    OCRPreviewCancelled,
    OCRRegions,
    _recognize_preprocessed_dialog,
    recognize_dialog_image_result,
)


class SparseOCRPipelineTest(unittest.TestCase):
    def image(self):
        image = Image.new("L", (400, 100), "white")
        ImageDraw.Draw(image).rectangle((70, 60, 74, 85), fill="black")
        return image

    def recognize(self, sparse, *, text="", language="eng", cancelled=None):
        return recognize_dialog_image_result(
            self.image(),
            recognize_text=Mock(return_value=text),
            recognize_data=Mock(return_value={"text": [], "conf": []}),
            recognize_sparse=sparse,
            ocr_regions=OCRRegions(
                DialogRegion(0, 0, 1, 0.3),
                DialogRegion(0, 0.4, 1, 0.6),
            ),
            language=language,
            cancelled=cancelled,
        )

    def test_real_sparse_frames_reach_shared_recovery_with_measured_confidence(self):
        fixtures = Path(__file__).parent / "fixtures" / "ocr-sparse-dialogue"
        for name, text in (
            ("dash-exclamation", "—!"),
            ("ellipsis-exclamation", "...!"),
            ("one-letter-ellipsis", "I ..."),
        ):
            with (
                self.subTest(name=name),
                Image.open(fixtures / (name + ".png")) as image,
            ):
                sparse = Mock(return_value=(text, 73.5))
                result = recognize_dialog_image_result(
                    image,
                    recognize_text=Mock(return_value=""),
                    recognize_data=Mock(return_value={"text": [], "conf": []}),
                    recognize_sparse=sparse,
                )
                self.assertEqual(result.text, text)
                self.assertEqual(result.dialogue_confidence, 73.5)
                self.assertTrue(result.is_readable())
                self.assertFalse(result.source_text_confirmed)
                self.assertEqual(result.profile, "ppocrv5-sparse")
                self.assertEqual(result.attempts, 2)
                sparse.assert_called_once()
                crop, remaining = sparse.call_args.args
                self.assertLess(crop.width, image.width / 4)
                self.assertEqual(crop.mode, "RGB")
                remaining()

    def test_successful_text_and_other_languages_do_not_invoke_english_rescue(self):
        for text, language in (("Hello world", "eng"), ("", "rus"), ("", "eng+rus")):
            with self.subTest(text=text, language=language):
                sparse = Mock(return_value=("wrong replacement", 99))
                result = self.recognize(sparse, text=text, language=language)
                self.assertEqual(result.text, text)
                sparse.assert_not_called()

    def test_empty_and_uncertain_rescue_never_gain_source_authority(self):
        for text in ("", "?"):
            with self.subTest(text=text):
                result = self.recognize(Mock(return_value=(text, 42)))
                self.assertEqual(result.text, text)
                self.assertEqual(result.confidence, 42)
                self.assertFalse(result.is_readable())
                self.assertFalse(result.source_text_confirmed)
                self.assertEqual(result.attempts, 2)

    def test_cancellation_is_checked_after_sparse_inference(self):
        cancelled = Event()

        def recover(*_args):
            cancelled.set()
            return "I ...", 98

        with self.assertRaises(OCRPreviewCancelled):
            self.recognize(recover, cancelled=cancelled.is_set)

    def test_derived_dialogue_preserves_successful_whole_frame_ocr_before_rescue(self):
        sparse = Mock(return_value=("?", 42))
        with patch(
            "vntts.ocr.recognize_speaker_from_data",
            return_value=(
                "Marcus",
                OCRLine("Marcus", 10, 5, 100, 25),
            ),
        ):
            result = _recognize_preprocessed_dialog(
                self.image(),
                None,
                Mock(),
                Mock(),
                "eng",
                Mock(
                    side_effect=[
                        ("Marcus\nHello there.", {"text": ["Hello"], "conf": [95]}),
                        ("", {"text": [], "conf": []}),
                    ]
                ),
                lambda: None,
                sparse,
            )
        self.assertEqual(result.text, "Hello there.")
        self.assertEqual(result.confidence, 95)
        sparse.assert_not_called()

    def test_derived_dialogue_uses_same_recovery_after_all_primary_results_are_empty(
        self,
    ):
        for text, confidence in (("I ...", 91), ("", 0)):
            with (
                self.subTest(text=text),
                patch(
                    "vntts.ocr.recognize_speaker_from_data",
                    return_value=("Marcus", OCRLine("Marcus", 10, 5, 100, 25)),
                ),
            ):
                sparse = Mock(return_value=(text, confidence))
                result = _recognize_preprocessed_dialog(
                    self.image(),
                    None,
                    Mock(),
                    Mock(),
                    "eng",
                    Mock(return_value=("", {"text": [], "conf": []})),
                    lambda: None,
                    sparse,
                )
                self.assertEqual((result.character, result.text), ("Marcus", text))
                self.assertEqual(result.dialogue_confidence, confidence)
                self.assertEqual(result.attempts, 2)
                sparse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
