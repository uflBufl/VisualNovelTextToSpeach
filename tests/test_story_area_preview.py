import unittest
from dataclasses import asdict, replace
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from vntts.controller import AppController
from vntts.dialog_capture import CapturedDialogFrame
from vntts.model_assets import ModelDownloadCancelled
from vntts.ocr import DialogRegion, OCRPreviewCancelled, OCRRegions
from vntts.settings import AppSettings
from vntts.text_detection import ScreenTextDetection, ScreenTextDetector, TextLine


class StoryAreaPreviewTest(unittest.TestCase):
    def setUp(self):
        self.controller = AppController(AppSettings(discover_text_layouts=False))
        self.image = Image.new("RGB", (400, 300), "navy")
        self.story = DialogRegion(0, 0.2, 1, 0.8)
        self.dialogue = DialogRegion(0, 0.7, 1, 0.3)
        self.cancel = Event()
        self.worker = Mock()
        self.controller.caption_discovery = self.worker

    def line(self, text="Frozen story caption", top=0.3, confidence=95):
        return TextLine(DialogRegion(0.1, top, 0.8, 0.04), text, confidence)

    def preview(self, **kwargs):
        return self.controller.preview_story_area(
            self.image, self.story, self.dialogue, self.cancel, **kwargs
        )

    def test_returns_selected_production_crop_without_touching_live_state(self):
        first = self.line()
        second = self.line("Second caption line", top=0.36)
        self.worker.detect.return_value = ScreenTextDetection((first, second))
        settings = asdict(self.controller.settings)
        profile_state = tuple(self.controller.profile_store.profiles)
        live_layout_state = vars(self.controller.live_dialog_layout).copy()
        with (
            patch.object(self.controller, "_create_capture_target") as capture,
            patch.object(self.controller, "_resolve_story_ocr_result") as resolve,
            patch.object(self.controller, "_story_region") as permission,
        ):
            result = self.preview()
        self.assertEqual(result.text, "Frozen story caption Second caption line")
        self.assertEqual(result.profile, "caption-ppocr")
        expected = DialogRegion(0.1, 0.3, 0.8, 0.1).crop(self.image)
        self.assertEqual(result.processed_image.size, expected.size)
        self.assertEqual(result.processed_image.tobytes(), expected.tobytes())
        self.worker.detect.assert_called_once_with(
            self.image,
            allowed_region=self.story,
            covered_region=self.dialogue,
            dialogue_region=self.dialogue,
            cancel_event=self.cancel,
        )
        self.assertEqual(asdict(self.controller.settings), settings)
        self.assertEqual(tuple(self.controller.profile_store.profiles), profile_state)
        self.assertEqual(vars(self.controller.live_dialog_layout), live_layout_state)
        capture.assert_not_called()
        resolve.assert_not_called()
        permission.assert_not_called()

    def test_production_detector_preserves_body_presence_before_caption_recognition(
        self,
    ):
        detector = ScreenTextDetector()
        detector._load_models = Mock()
        detector._detector = Mock(
            return_value=SimpleNamespace(
                boxes=[
                    [[40, 15], [360, 15], [360, 27], [40, 27]],
                    [[40, 90], [360, 90], [360, 102], [40, 102]],
                    [[40, 240], [360, 240], [360, 252], [40, 252]],
                ]
            )
        )
        detector._recognizer = Mock(
            return_value=SimpleNamespace(txts=("Permitted caption",), scores=(0.95,))
        )
        detector._crop_line = Mock(return_value=np.zeros((12, 320, 3), dtype=np.uint8))
        self.worker.detect.side_effect = lambda image, *, cancel_event, **scope: (
            detector.detect(image, **scope)
        )
        result = self.preview()
        self.assertEqual(result.profile, "dialogue-active")
        self.assertEqual(result.text, "")
        detector._crop_line.assert_not_called()
        detector._recognizer.assert_not_called()

    def test_absent_and_covered_text_differ_from_ambiguous_and_uncertain_text(self):
        for lines, profile in (
            ((), "story-area-empty"),
            ((self.line(top=0.8),), "story-area-empty"),
            ((self.line(), self.line("Other block", top=0.55)), "layout-pending"),
            ((self.line(confidence=20),), "layout-pending"),
        ):
            with self.subTest(lines=lines):
                self.worker.detect.return_value = ScreenTextDetection(lines)
                result = self.preview()
                self.assertEqual(result.profile, profile)
                self.assertEqual(result.text, "")
                self.assertIsNone(result.processed_image)

    def test_nameplate_does_not_hide_caption_when_configured_dialogue_body_is_empty(
        self,
    ):
        regions = OCRRegions(DialogRegion(0, 0, 1, 0.25), DialogRegion(0, 0.4, 1, 0.6))
        detector = ScreenTextDetector()
        detector._load_models = Mock()
        detector._detector = Mock(
            return_value=SimpleNamespace(
                boxes=[
                    [[40, 192], [360, 192], [360, 204], [40, 204]],
                    [[40, 218], [100, 218], [100, 230], [40, 230]],
                ]
            )
        )
        detector._recognizer = Mock(
            return_value=SimpleNamespace(txts=("A central phrase.",), scores=(0.95,))
        )
        detector._crop_line = Mock(return_value=np.zeros((12, 320, 3), dtype=np.uint8))
        self.worker.detect.side_effect = lambda image, *, cancel_event, **scope: (
            detector.detect(image, **scope)
        )
        with (
            patch.object(
                self.controller, "_capture_region", return_value=self.dialogue
            ),
            patch.object(self.controller, "_capture_ocr_regions", return_value=regions),
        ):
            result = self.preview()
        self.assertEqual(result.text, "A central phrase.")
        self.assertEqual(result.profile, "caption-ppocr")
        detector._crop_line.assert_called_once()
        self.assertEqual(
            self.worker.detect.call_args.kwargs["dialogue_region"],
            DialogRegion(0, 0.82, 1, 0.18),
        )

    def test_changed_draft_does_not_reuse_another_crop_body_split(self):
        self.worker.detect.return_value = ScreenTextDetection((), True)
        with (
            patch.object(
                self.controller,
                "_capture_region",
                return_value=DialogRegion(0, 0.5, 1, 0.5),
            ),
            patch.object(self.controller, "_capture_ocr_regions") as regions,
        ):
            result = self.preview()
        self.assertEqual(result.profile, "dialogue-active")
        self.assertEqual(
            self.worker.detect.call_args.kwargs["dialogue_region"], self.dialogue
        )
        regions.assert_not_called()

    def test_draft_confidence_applies_only_to_preview(self):
        self.worker.detect.return_value = ScreenTextDetection(
            (self.line(confidence=70),)
        )
        self.controller.caption_discovery_unavailable = True
        self.assertEqual(self.preview(minimum_confidence=80).profile, "layout-pending")
        self.assertEqual(self.preview().profile, "caption-ppocr")
        self.assertEqual(self.controller.settings.ocr_minimum_confidence, 60)
        self.assertFalse(self.controller.caption_discovery_unavailable)

    def test_successful_preview_recovers_live_worker_but_does_not_grant_permission(
        self,
    ):
        self.controller.caption_discovery_unavailable = True
        self.controller.settings = replace(
            self.controller.settings, discover_text_layouts=True
        )
        self.worker.detect.return_value = ScreenTextDetection((self.line(),))
        self.assertEqual(self.preview().profile, "caption-ppocr")
        self.assertFalse(self.controller.caption_discovery_unavailable)
        frame = CapturedDialogFrame(
            self.dialogue.crop(self.image),
            0,
            canonical_image=self.image,
            capture_region=self.dialogue,
        )
        with patch.object(self.controller, "_story_region", return_value=None):
            self.assertIs(self.controller._select_text_layout(frame), frame)
        self.assertEqual(self.worker.detect.call_count, 1)
        with patch.object(self.controller, "_story_region", return_value=self.story):
            selected = self.controller._select_text_layout(frame)
        self.assertEqual(selected.layout_kind, "caption")
        self.assertIs(self.controller.caption_discovery, self.worker)

    def test_old_preview_cannot_clear_failure_of_replacement_worker(self):
        replacement = Mock()
        self.controller.caption_discovery_unavailable = True

        def detect(*_args, **_kwargs):
            with self.controller.caption_discovery_lock:
                self.controller.caption_discovery = replacement
            return ScreenTextDetection((self.line(),))

        self.worker.detect.side_effect = detect
        self.assertEqual(self.preview().profile, "caption-ppocr")
        self.assertTrue(self.controller.caption_discovery_unavailable)
        self.assertIs(self.controller.caption_discovery, replacement)

    def test_invalid_scope_and_threshold_fail_before_model_preparation(self):
        self.controller.caption_discovery = None
        with patch("vntts.controller.prepare_text_detection_models") as prepare:
            for minimum in (-1, 101, True, float("nan")):
                with self.subTest(minimum=minimum), self.assertRaises(ValueError):
                    self.preview(minimum_confidence=minimum)
            with self.assertRaisesRegex(ValueError, "contain the dialogue"):
                self.controller.preview_story_area(
                    self.image,
                    DialogRegion(0, 0, 1, 0.5),
                    self.dialogue,
                    self.cancel,
                )
            prepare.assert_not_called()

    def test_cancellation_before_and_during_detection_cannot_publish_text(self):
        self.controller.caption_discovery_unavailable = True
        self.cancel.set()
        with self.assertRaises(OCRPreviewCancelled):
            self.preview()
        self.worker.detect.assert_not_called()
        self.cancel.clear()

        def detect(*_args, **_kwargs):
            self.cancel.set()
            return ScreenTextDetection((self.line(),))

        self.worker.detect.side_effect = detect
        with self.assertRaises(OCRPreviewCancelled):
            self.preview()
        self.assertTrue(self.controller.caption_discovery_unavailable)

    def test_preparation_and_detection_errors_propagate_without_disabling_live(self):
        self.controller.caption_discovery = None
        for error in (
            RuntimeError("Run `uv sync` to install screen text models"),
            ModelDownloadCancelled("Preparation cancelled"),
        ):
            with (
                self.subTest(error=error),
                patch(
                    "vntts.controller.prepare_text_detection_models", side_effect=error
                ),
                self.assertRaises(type(error)) as caught,
            ):
                self.preview()
            self.assertIs(caught.exception, error)
            self.assertFalse(self.controller.caption_discovery_unavailable)
        self.controller.caption_discovery = self.worker
        error = TimeoutError("Screen text detection timed out")
        self.worker.detect.side_effect = error
        with self.assertRaises(TimeoutError) as caught:
            self.preview()
        self.assertIs(caught.exception, error)
        self.assertFalse(self.controller.caption_discovery_unavailable)

    def test_failed_preview_preserves_earlier_model_unavailability(self):
        self.controller.caption_discovery_unavailable = True
        self.controller.caption_discovery = None
        with (
            patch(
                "vntts.controller.prepare_text_detection_models",
                side_effect=RuntimeError("Model unavailable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.preview()
        self.assertTrue(self.controller.caption_discovery_unavailable)
        self.controller.caption_discovery = self.worker
        self.worker.detect.side_effect = TimeoutError("Screen text detection timed out")
        with self.assertRaises(TimeoutError):
            self.preview()
        self.assertTrue(self.controller.caption_discovery_unavailable)

    def test_preview_and_live_reuse_one_worker_but_live_still_requires_permission(self):
        self.controller.caption_discovery = None
        entered, release = Event(), Event()
        errors, results = [], []

        def prepare(*_args, **_kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return "detector", "recognizer"

        def preview():
            try:
                results.append(self.preview())
            except Exception as error:
                errors.append(error)

        self.worker.detect.return_value = ScreenTextDetection((self.line(),))
        with (
            patch(
                "vntts.controller.prepare_text_detection_models", side_effect=prepare
            ) as preparation,
            patch(
                "vntts.controller.TextDetectionWorker", return_value=self.worker
            ) as factory,
        ):
            first, second = Thread(target=preview), Thread(target=preview)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(
                [result.profile for result in results], ["caption-ppocr"] * 2
            )
            frame = CapturedDialogFrame(
                self.dialogue.crop(self.image),
                0,
                canonical_image=self.image,
                capture_region=self.dialogue,
            )
            self.assertIs(self.controller._select_text_layout(frame), frame)
            self.controller.settings = replace(
                self.controller.settings, discover_text_layouts=True
            )
            with patch.object(self.controller, "_story_region", return_value=None):
                self.assertIs(self.controller._select_text_layout(frame), frame)
            with patch.object(
                self.controller, "_story_region", return_value=self.story
            ):
                selected = self.controller._select_text_layout(frame)
            self.assertEqual(selected.layout_kind, "caption")
            preparation.assert_called_once()
            factory.assert_called_once()


if __name__ == "__main__":
    unittest.main()
