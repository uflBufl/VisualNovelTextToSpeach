import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from vntts.asset_integrity import ModelIntegrityError
from vntts.model_assets import ModelDownloadCancelled
from vntts.ocr import DialogRegion
from vntts.text_detection import (
    ScreenTextDetector,
    TextBlock,
    TextLine,
    group_text_lines,
    prepare_text_detection_models,
)


def line(left=0.2, top=0.3, width=0.6, height=0.03, text="A line"):
    return TextLine(DialogRegion(left, top, width, height), text, 95)


class TextGroupingTest(unittest.TestCase):
    def test_groups_neighboring_lines_away_from_center_without_joining_columns(self):
        first = line(0.03, 0.1, 0.35)
        second = line(0.03, 0.15, 0.29, text="Another line")
        other = line(0.65, 0.11, 0.3)
        blocks = group_text_lines((second, other, first))
        self.assertEqual(blocks[0].lines, (first, second))
        self.assertEqual(blocks[0].text, "A line Another line")
        self.assertEqual(blocks[1].lines, (other,))
        self.assertAlmostEqual(blocks[0].box.height, 0.08)

    def test_separates_distant_text_and_different_font_sizes(self):
        lines = (line(top=0.1), line(top=0.5), line(top=0.55, height=0.09))
        self.assertEqual(len(group_text_lines(lines)), 3)
        self.assertEqual(group_text_lines(()), ())

    def test_records_are_immutable_and_reject_invalid_confidence(self):
        with self.assertRaises(FrozenInstanceError):
            line().text = "changed"
        for confidence in (float("nan"), float("inf"), True, -1, 101):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                TextLine(line().box, "text", confidence)
        with self.assertRaises(ValueError):
            TextBlock(())


class TextModelPreparationTest(unittest.TestCase):
    def test_prepares_pinned_detector_explicitly_and_checks_publisher_checksum(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "models").mkdir()
            recognizer = root / "models" / "PP-OCRv6_rec_small.onnx"
            recognizer.write_bytes(b"recognizer")
            manager = Mock()
            manager.download.return_value = root
            progress = Mock()
            cancelled = Event()
            with (
                patch(
                    "vntts.text_detection.import_module",
                    return_value=SimpleNamespace(__file__=str(root / "__init__.py")),
                ),
                patch(
                    "vntts.text_detection.file_sha256",
                    return_value="d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9",
                ) as checksum,
            ):
                paths = prepare_text_detection_models(manager, progress, cancelled)
                self.assertEqual(
                    paths, (root / "ch_PP-OCRv4_det_mobile.onnx", recognizer)
                )
                options = manager.download.call_args.kwargs
                self.assertIs(options["progress"], progress)
                self.assertIs(options["cancel_event"], cancelled)
                self.assertIn("/v3.9.2/", options["asset"].urls[0])
                checksum.assert_called_once_with(
                    paths[0], error_type=ModelIntegrityError
                )
                checksum.return_value = "corrupt"
                with self.assertRaisesRegex(ModelIntegrityError, "publisher catalog"):
                    prepare_text_detection_models(manager)

    def test_missing_recognizer_and_cancellation_do_not_download(self):
        manager = Mock()
        with (
            TemporaryDirectory() as temporary,
            patch(
                "vntts.text_detection.import_module",
                return_value=SimpleNamespace(
                    __file__=str(Path(temporary) / "__init__.py")
                ),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "recognition model is missing"):
                prepare_text_detection_models(manager)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(ModelDownloadCancelled):
            prepare_text_detection_models(manager, cancel_event=cancelled)
        manager.download.assert_not_called()


class ScreenTextDetectorTest(unittest.TestCase):
    def detector(self, boxes, texts=("Text",), scores=(0.9,)):
        detector = ScreenTextDetector()
        detector._load_models = Mock()
        detector._detector = Mock(return_value=SimpleNamespace(boxes=boxes))
        detector._recognizer = Mock(
            return_value=SimpleNamespace(txts=texts, scores=scores)
        )
        detector._crop_line = Mock(return_value=np.zeros((20, 100, 3), dtype=np.uint8))
        return detector

    def test_downscales_only_detection_and_reads_original_bgr_line_crops(self):
        detector = self.detector([[[64, 32], [320, 32], [320, 64], [64, 64]]])
        image = Image.new("RGB", (1280, 640), (10, 20, 30))
        result = detector.detect(image)
        self.assertEqual(
            result, (TextLine(DialogRegion(0.1, 0.1, 0.4, 0.1), "Text", 90),)
        )
        detected_pixels = detector._detector.call_args.args[0]
        self.assertEqual(detected_pixels.shape, (320, 640, 3))
        np.testing.assert_array_equal(detected_pixels[0, 0], [30, 20, 10])
        original_pixels, crop_box = detector._crop_line.call_args.args
        self.assertEqual(original_pixels.shape, (640, 1280, 3))
        np.testing.assert_allclose(crop_box[0], [128, 64])

    def test_empty_frame_detections_skip_recognition(self):
        for boxes in (None, []):
            detector = self.detector(boxes)
            self.assertEqual(detector.detect(Image.new("RGB", (10, 10))), ())
            detector._recognizer.assert_not_called()

    def test_only_fully_contained_lines_are_cropped_and_recognized(self):
        detector = self.detector(
            [
                [[20, 30], [40, 30], [40, 40], [20, 40]],
                [[0, 0], [10, 0], [10, 10], [0, 10]],
                [[10, 30], [40, 30], [40, 40], [10, 40]],
                [[60, 30], [90, 30], [90, 40], [60, 40]],
                [[30, 10], [40, 10], [40, 40], [30, 40]],
                [[30, 60], [40, 60], [40, 90], [30, 90]],
            ],
            texts=("Hi",),
        )
        lines = detector.detect(
            Image.new("RGB", (100, 100)),
            allowed_region=DialogRegion(0.2, 0.2, 0.6, 0.6),
        )
        self.assertEqual([line.text for line in lines], ["Hi"])
        self.assertAlmostEqual(lines[0].box.left, 0.2)
        self.assertAlmostEqual(lines[0].box.top, 0.3)
        self.assertAlmostEqual(lines[0].box.width, 0.2)
        self.assertAlmostEqual(lines[0].box.height, 0.1)
        self.assertEqual(detector._crop_line.call_count, 1)
        self.assertEqual(len(detector._recognizer.call_args.args[0]), 1)

    def test_all_excluded_lines_skip_crops_and_recognition(self):
        detector = self.detector([[[0, 0], [10, 0], [10, 10], [0, 10]]])
        self.assertEqual(
            detector.detect(
                Image.new("RGB", (100, 100)),
                allowed_region=DialogRegion(0.2, 0.2, 0.6, 0.6),
            ),
            (),
        )
        detector._crop_line.assert_not_called()
        detector._recognizer.assert_not_called()

    def test_exact_edge_line_is_recognized_but_one_pixel_overrun_is_excluded(self):
        detector = self.detector(
            [
                [[10, 20], [30, 20], [30, 30], [10, 30]],
                [[10, 40], [31, 40], [31, 50], [10, 50]],
            ]
        )
        lines = detector.detect(
            Image.new("RGB", (100, 100)),
            allowed_region=DialogRegion(0, 0, 0.3, 1),
        )
        self.assertEqual([line.text for line in lines], ["Text"])
        self.assertEqual(detector._crop_line.call_count, 1)
        self.assertEqual(len(detector._recognizer.call_args.args[0]), 1)

    def test_sorts_lines_and_preserves_low_confidence_candidates(self):
        detector = self.detector(
            [
                [[10, 70], [90, 70], [90, 80], [10, 80]],
                [[10, 10], [90, 10], [90, 20], [10, 20]],
            ],
            texts=(" lower ", "upper"),
            scores=(0.1, 0.99),
        )
        lines = detector.detect(Image.new("RGB", (100, 100)))
        self.assertEqual([item.text for item in lines], ["upper", "lower"])
        self.assertEqual(lines[1].confidence, 10)

    def test_rejects_invalid_geometry_and_mismatched_recognition(self):
        for boxes in (
            [[[0, 0]]],
            [[[0, 0], [0, 0], [0, 0], [0, 0]]],
            [[[0, 0], [float("nan"), 0], [1, 1], [0, 1]]],
        ):
            detector = self.detector(boxes)
            with self.subTest(boxes=boxes), self.assertRaises(ValueError):
                detector.detect(Image.new("RGB", (100, 100)))
        detector = self.detector([[[10, 10], [90, 10], [90, 20], [10, 20]]], texts=())
        with self.assertRaises(ValueError):
            detector.detect(Image.new("RGB", (100, 100)))

    def test_initialization_is_lazy_and_missing_dependency_is_actionable(self):
        with patch.dict(sys.modules, {"rapidocr": None}):
            detector = ScreenTextDetector()
            with self.assertRaisesRegex(RuntimeError, "uv sync"):
                detector.detect(Image.new("RGB", (100, 100)))


if __name__ == "__main__":
    unittest.main()
