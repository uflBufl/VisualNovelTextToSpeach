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
    ScreenTextDetection,
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

    def test_wide_bridge_merges_existing_groups_in_reading_order(self):
        left = line(0, 0.1, 0.3, text="Left")
        right = line(0.5, 0.1, 0.3, text="Right")
        bridge = line(0, 0.15, 0.8, text="Bridge")
        self.assertEqual(
            group_text_lines((bridge, right, left)), (TextBlock((left, right, bridge)),)
        )

    def test_records_are_immutable_and_reject_invalid_confidence(self):
        with self.assertRaises(FrozenInstanceError):
            line().text = "changed"
        for confidence in (float("nan"), float("inf"), True, -1, 101):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                TextLine(line().box, "text", confidence)
        with self.assertRaises(ValueError):
            TextBlock(())
        detection = ScreenTextDetection((line(),))
        with self.assertRaises(FrozenInstanceError):
            detection.dialogue_text_present = True
        for invalid in (None, 0, 1, "false"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ScreenTextDetection((), invalid)


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
            result,
            ScreenTextDetection(
                (TextLine(DialogRegion(0.1, 0.1, 0.4, 0.1), "Text", 90),)
            ),
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
            self.assertEqual(
                detector.detect(Image.new("RGB", (10, 10))), ScreenTextDetection(())
            )
            detector._recognizer.assert_not_called()

    def test_covered_dialogue_lines_skip_crops_and_recognition(self):
        detector = self.detector(
            [
                [[20, 70], [80, 70], [80, 76], [20, 76]],
                [[20, 82], [80, 82], [80, 88], [20, 88]],
            ]
        )
        self.assertEqual(
            detector.detect(
                Image.new("RGB", (100, 100)),
                covered_region=DialogRegion(0, 0.65, 1, 0.35),
            ),
            ScreenTextDetection(()),
        )
        detector._crop_line.assert_not_called()
        detector._recognizer.assert_not_called()

    def test_body_detection_precedes_covered_filter_and_line_recognition(self):
        outside = [[10, 20], [90, 20], [90, 28], [10, 28]]
        body = [[10, 80], [90, 80], [90, 88], [10, 88]]
        for text, confidence in (("", 0.99), ("unreadable", 0.01)):
            with self.subTest(text=text, confidence=confidence):
                detector = self.detector(
                    (outside, body), texts=(text,), scores=(confidence,)
                )
                detection = detector.detect(
                    Image.new("RGB", (100, 100)),
                    allowed_region=DialogRegion(0, 0, 1, 1),
                    covered_region=DialogRegion(0, 0.7, 1, 0.3),
                    dialogue_region=DialogRegion(0, 0.78, 1, 0.22),
                )
                self.assertEqual(detection, ScreenTextDetection((), True))
                detector._crop_line.assert_not_called()
                detector._recognizer.assert_not_called()

    def test_body_line_in_mixed_group_still_prevents_all_outside_recognition(self):
        detector = self.detector(
            (
                [[10, 55], [90, 55], [90, 61], [10, 61]],
                [[10, 68], [90, 68], [90, 74], [10, 74]],
            )
        )
        self.assertEqual(
            detector.detect(
                Image.new("RGB", (100, 100)),
                covered_region=DialogRegion(0, 0.65, 1, 0.35),
                dialogue_region=DialogRegion(0, 0.7, 1, 0.3),
            ),
            ScreenTextDetection((), True),
        )
        detector._recognizer.assert_not_called()

    def test_name_only_capture_does_not_claim_body_or_hide_outside_caption(self):
        name = [[10, 70], [60, 70], [60, 76], [10, 76]]
        caption = [[10, 30], [90, 30], [90, 36], [10, 36]]
        for boxes, expected_text in (((name,), ()), ((name, caption), ("Text",))):
            with self.subTest(boxes=boxes):
                detector = self.detector(boxes)
                detection = detector.detect(
                    Image.new("RGB", (100, 100)),
                    covered_region=DialogRegion(0, 0.65, 1, 0.35),
                    dialogue_region=DialogRegion(0, 0.8, 1, 0.2),
                )
                self.assertFalse(detection.dialogue_text_present)
                self.assertEqual(
                    tuple(line.text for line in detection.lines), expected_text
                )
                self.assertEqual(detector._crop_line.call_count, len(expected_text))

    def test_adjacent_nameplate_is_never_cropped_or_joined_to_outside_caption(self):
        name = [[10, 70], [90, 70], [90, 76], [10, 76]]
        caption = [[10, 58], [90, 58], [90, 64], [10, 64]]
        for confidence in (0.95, 0.4):
            with self.subTest(confidence=confidence):
                detector = self.detector(
                    (name, caption), texts=("Caption",), scores=(confidence,)
                )
                detection = detector.detect(
                    Image.new("RGB", (100, 100)),
                    covered_region=DialogRegion(0, 0.65, 1, 0.35),
                    dialogue_region=DialogRegion(0, 0.8, 1, 0.2),
                )
                self.assertFalse(detection.dialogue_text_present)
                self.assertEqual(
                    tuple(block.text for block in group_text_lines(detection.lines)),
                    ("Caption",),
                )
                self.assertEqual(detection.lines[0].confidence, confidence * 100)
                detector._crop_line.assert_called_once()
                self.assertEqual(float(detector._crop_line.call_args.args[1][0, 1]), 58)

    def test_body_membership_uses_centers_including_exact_boundary(self):
        region = DialogRegion(0.25, 0.25, 0.5, 0.5)
        for left, top, right, bottom, present in (
            (8, 48, 56, 56, True),
            (7, 48, 55, 56, False),
            (72, 48, 120, 56, True),
            (73, 48, 121, 56, False),
            (48, 8, 56, 56, True),
            (48, 7, 56, 55, False),
            (48, 72, 56, 120, True),
            (48, 73, 56, 121, False),
        ):
            with self.subTest(box=(left, top, right, bottom)):
                detector = self.detector(
                    [[[left, top], [right, top], [right, bottom], [left, bottom]]]
                )
                detection = detector.detect(
                    Image.new("RGB", (128, 128)), dialogue_region=region
                )
                self.assertEqual(detection.dialogue_text_present, present)
                self.assertEqual(detector._crop_line.call_count, int(not present))

    def test_unpermitted_body_box_cannot_claim_dialogue_presence(self):
        detector = self.detector(
            [
                [[10, 70], [90, 70], [90, 80], [10, 80]],
                [[10, 20], [90, 20], [90, 30], [10, 30]],
            ]
        )
        detection = detector.detect(
            Image.new("RGB", (100, 100)),
            allowed_region=DialogRegion(0, 0, 1, 0.75),
            dialogue_region=DialogRegion(0, 0.7, 1, 0.3),
        )
        self.assertFalse(detection.dialogue_text_present)
        self.assertEqual(tuple(line.text for line in detection.lines), ("Text",))
        self.assertEqual(detector._crop_line.call_count, 1)

    def test_covered_bridge_does_not_join_outside_groups_or_change_confidence(self):
        image = Image.new("RGB", (100, 100))
        covered = DialogRegion(0.25, 0.25, 0.5, 0.5)
        boxes = [
            [[0, 30], [40, 30], [40, 40], [0, 40]],
            [[20, 45], [60, 45], [60, 55], [20, 55]],
            [[0, 60], [40, 60], [40, 70], [0, 70]],
        ]
        for confidence in (0.95, 0.4):
            with self.subTest(confidence=confidence):
                detector = self.detector(
                    boxes,
                    texts=("First", "Last"),
                    scores=(0.95, confidence),
                )
                lines = detector.detect(image, covered_region=covered).lines
                self.assertEqual(detector._crop_line.call_count, 2)
                self.assertEqual(
                    [line.text for line in lines],
                    ["First", "Last"],
                )
                self.assertEqual(lines[-1].confidence, confidence * 100)
                self.assertEqual(
                    tuple(block.text for block in group_text_lines(lines)),
                    ("First", "Last"),
                )

    def test_covered_filter_keeps_detection_order_for_recognition(self):
        boxes = [
            [[10, 35], [80, 35], [80, 40], [10, 40]],
            [[10, 80], [80, 80], [80, 85], [10, 85]],
            [[10, 20], [80, 20], [80, 25], [10, 25]],
        ]
        detector = self.detector(boxes, texts=("Later", "Earlier"), scores=(0.9, 0.8))
        detection = detector.detect(
            Image.new("RGB", (100, 100)), covered_region=DialogRegion(0, 0.7, 1, 0.3)
        )
        self.assertEqual([line.text for line in detection.lines], ["Earlier", "Later"])
        self.assertEqual(
            [float(call.args[1][0, 1]) for call in detector._crop_line.call_args_list],
            [35, 20],
        )

    def test_covered_membership_uses_centers_including_exact_boundary(self):
        covered = DialogRegion(0.25, 0.25, 0.5, 0.5)
        for left, top, right, bottom, retained in (
            (8, 48, 56, 56, False),
            (7, 48, 55, 56, True),
            (72, 48, 120, 56, False),
            (73, 48, 121, 56, True),
            (48, 8, 56, 56, False),
            (48, 7, 56, 55, True),
            (48, 72, 56, 120, False),
            (48, 73, 56, 121, True),
        ):
            with self.subTest(box=(left, top, right, bottom)):
                detector = self.detector(
                    [[[left, top], [right, top], [right, bottom], [left, bottom]]]
                )
                detection = detector.detect(
                    Image.new("RGB", (128, 128)), covered_region=covered
                )
                self.assertEqual(bool(detection.lines), retained)
                self.assertEqual(detector._crop_line.call_count, int(retained))

    def test_story_permission_filters_before_covered_lines(self):
        detector = self.detector(
            [
                [[0, 30], [40, 30], [40, 40], [0, 40]],
                [[20, 45], [60, 45], [60, 55], [20, 55]],
            ]
        )
        covered = DialogRegion(0.25, 0.25, 0.5, 0.5)
        result = detector.detect(
            Image.new("RGB", (100, 100)),
            allowed_region=DialogRegion(0.1, 0.4, 0.8, 0.4),
            covered_region=covered,
        )
        self.assertEqual(result, ScreenTextDetection(()))
        detector._crop_line.assert_not_called()
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
        detection = detector.detect(
            Image.new("RGB", (100, 100)),
            allowed_region=DialogRegion(0.2, 0.2, 0.6, 0.6),
        )
        self.assertEqual([line.text for line in detection.lines], ["Hi"])
        self.assertAlmostEqual(detection.lines[0].box.left, 0.2)
        self.assertAlmostEqual(detection.lines[0].box.top, 0.3)
        self.assertAlmostEqual(detection.lines[0].box.width, 0.2)
        self.assertAlmostEqual(detection.lines[0].box.height, 0.1)
        self.assertEqual(detector._crop_line.call_count, 1)
        self.assertEqual(len(detector._recognizer.call_args.args[0]), 1)

    def test_all_excluded_lines_skip_crops_and_recognition(self):
        detector = self.detector([[[0, 0], [10, 0], [10, 10], [0, 10]]])
        self.assertEqual(
            detector.detect(
                Image.new("RGB", (100, 100)),
                allowed_region=DialogRegion(0.2, 0.2, 0.6, 0.6),
            ),
            ScreenTextDetection(()),
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
        detection = detector.detect(
            Image.new("RGB", (100, 100)),
            allowed_region=DialogRegion(0, 0, 0.3, 1),
        )
        self.assertEqual([line.text for line in detection.lines], ["Text"])
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
        detection = detector.detect(Image.new("RGB", (100, 100)))
        self.assertEqual([item.text for item in detection.lines], ["upper", "lower"])
        self.assertEqual(detection.lines[1].confidence, 10)

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
        for missing in ("rapidocr", "onnxruntime"):
            with (
                self.subTest(missing=missing),
                patch.dict(
                    sys.modules,
                    {"rapidocr": Mock(), "onnxruntime": Mock(), missing: None},
                ),
            ):
                detector = ScreenTextDetector()
                with self.assertRaisesRegex(RuntimeError, "uv sync"):
                    detector.detect(Image.new("RGB", (100, 100)))


if __name__ == "__main__":
    unittest.main()
