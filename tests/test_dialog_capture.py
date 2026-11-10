import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from tests.native_capture_fixtures import native_frame
from vntts.dialog_capture import (
    CapturedDialogFrame,
    OCRError,
    ScreenCaptureError,
    analyze_dialog_snapshot,
    capture_dialog,
    capture_live_frame,
    format_runtime_error,
    recognize_screenshot_result,
)
from vntts.native_capture import CaptureError
from vntts.ocr import DialogRegion, OCRPreviewCancelled, OCRRegions, OCRResult
from vntts.window_capture import (
    WindowCaptureError,
    WindowCaptureTarget,
    WindowCaptureUnavailableError,
    WindowGeometry,
    WindowInfo,
    WindowMinimizedError,
    WindowNotFoundError,
)


class ScreenshotRecognitionTest(unittest.TestCase):
    def test_cancellation_keeps_its_type_and_identity(self):
        for regions in (
            None,
            OCRRegions(DialogRegion(0, 0, 1, 0.3), DialogRegion(0, 0.4, 1, 0.6)),
        ):
            with self.subTest(regions=regions):
                cancelled = OCRPreviewCancelled("OCR preview cancelled")
                backend = Mock()
                backend.recognize.side_effect = cancelled
                with self.assertRaises(OCRPreviewCancelled) as caught:
                    recognize_screenshot_result(
                        Image.new("RGB", (20, 20)),
                        ocr_backend=backend,
                        ocr_regions=regions,
                    )
                self.assertIs(caught.exception, cancelled)

    def test_recognition_failures_keep_ocr_stage_and_cause(self):
        for failure in (TimeoutError("OCR timed out"), RuntimeError("worker died")):
            with self.subTest(error=type(failure).__name__):
                backend = Mock()
                backend.recognize.side_effect = failure
                with self.assertRaises(OCRError) as caught:
                    recognize_screenshot_result(
                        Image.new("RGB", (20, 20)), ocr_backend=backend
                    )
                self.assertIs(caught.exception.__cause__, failure)
                self.assertEqual(str(caught.exception), str(failure))


class CaptureProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(100, "Selected game", 42)
        self.backend = Mock()
        self.backend.list_windows.return_value = [self.window]
        self.backend.get_window.return_value = self.window
        self.backend.get_client_geometry.return_value = WindowGeometry(40, 50, 200, 100)
        self.target = WindowCaptureTarget("Selected game", self.backend)
        self.source = Mock(clock_offset=0.0, clock_uncertainty=0.001)
        self.source.capture.return_value = native_frame()
        factory = patch(
            "vntts.window_capture.NativeCaptureSession", return_value=self.source
        )
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        process = patch("vntts.window_capture.psutil.Process")
        process.start()
        self.addCleanup(process.stop)
        fingerprint = patch(
            "vntts.window_capture.process_fingerprint",
            return_value={"scheme": "test", "token": "selected-process"},
        )
        fingerprint.start()
        self.addCleanup(fingerprint.stop)
        self.addCleanup(self.target.close)
        self.region = DialogRegion(0, 0.5, 1, 0.5)

    def test_saved_capture_embeds_and_saves_truthful_scope_and_identity(self):
        records = []
        with TemporaryDirectory() as directory:
            image, output = capture_dialog(
                directory,
                capture_target=self.target,
                region=self.region,
                run_id="reading-run",
                provenance_handler=records.append,
            )
            saved = json.loads(output.with_suffix(".capture.json").read_text())
            with Image.open(output) as png:
                self.assertEqual(json.loads(png.info["capture_provenance"]), saved)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertNotEqual(record.capture_id, record.run_id)
        self.assertEqual(record.run_id, "reading-run")
        self.assertEqual(
            (record.window_id, record.owner_pid, record.window_title),
            (100, 42, "Selected game"),
        )
        self.assertEqual(
            record.window_bounds, {"left": 40, "top": 50, "width": 200, "height": 100}
        )
        self.assertEqual(
            record.capture_bounds, {"left": 0, "top": 50, "width": 200, "height": 50}
        )
        self.assertEqual(record.output_dimensions, (200, 50))
        self.assertEqual(
            (record.scope, record.mechanism),
            ("selected_window", "macos_screencapturekit"),
        )
        self.assertTrue(record.success)
        self.assertGreaterEqual(record.completed_monotonic, record.started_monotonic)
        self.assertEqual(image.info["capture_provenance"], record.as_dict())
        self.backend.list_windows.assert_called_once_with()

    def test_live_capture_has_unique_attempt_ids_and_run_correlation(self):
        records = []
        first = capture_live_frame(
            "unused",
            self.target,
            region=self.region,
            run_id="run",
            provenance_handler=records.append,
        )
        second = capture_live_frame(
            "unused", self.target, region=self.region, run_id="run"
        )
        self.assertIs(first.provenance, records[0])
        self.assertNotEqual(first.provenance.capture_id, second.provenance.capture_id)
        self.assertEqual(first.provenance.run_id, second.provenance.run_id)

    def test_missing_or_minimized_target_records_failure_without_desktop_fallback(self):
        for windows, error_type in (
            ([], "WindowNotFoundError"),
            ([WindowInfo(100, "Selected game", 42, True)], "WindowMinimizedError"),
        ):
            with self.subTest(error_type=error_type):
                self.backend.list_windows.return_value = windows
                target = WindowCaptureTarget("Selected game", self.backend)
                records = []
                with self.assertRaises(ScreenCaptureError) as caught:
                    capture_dialog(
                        "unused",
                        save_screenshot=False,
                        capture_target=target,
                        provenance_handler=records.append,
                    )
                self.assertIs(caught.exception.provenance, records[0])
                self.assertFalse(records[0].success)
                self.assertEqual(records[0].error_type, error_type)
                self.assertEqual(records[0].requested_window_title, "Selected game")
                self.source.capture.assert_not_called()

    def test_handle_reuse_requires_explicit_new_selection(self):
        capture_live_frame("unused", self.target)
        replacement = WindowInfo(100, "Selected game", 84)
        self.backend.get_window.return_value = replacement
        self.backend.list_windows.return_value = [replacement]
        records = []
        with self.assertRaises(ScreenCaptureError):
            capture_live_frame("unused", self.target, provenance_handler=records.append)
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].owner_pid, 42)
        self.source.capture.return_value = native_frame(pid=84)
        selected = WindowCaptureTarget("Selected game", self.backend)
        self.addCleanup(selected.close)
        next_frame = capture_live_frame("unused", selected)
        self.assertTrue(next_frame.provenance.success)
        self.assertEqual(next_frame.provenance.owner_pid, 84)

    def test_sidecar_failure_preserves_embedded_successful_capture(self):
        with TemporaryDirectory() as directory:
            with patch.object(
                Path, "write_text", side_effect=PermissionError("private details")
            ):
                with self.assertWarnsRegex(RuntimeWarning, "PermissionError"):
                    image, output = capture_dialog(directory, region=self.region)
            self.assertTrue(image.info["capture_provenance"]["success"])
            with Image.open(output) as png:
                self.assertTrue(json.loads(png.info["capture_provenance"])["success"])

    def test_snapshot_capture_context_restores_controls_before_ocr_or_capture_error(
        self,
    ):
        for fail in (False, True):
            events = []

            @contextmanager
            def capture_context():
                events.append("hide")
                try:
                    yield
                finally:
                    events.append("restore")

            def pixels(*_args, **_kwargs):
                events.append("pixels")
                if fail:
                    raise RuntimeError("capture failed")
                return Image.new("RGB", (20, 20)), None

            def recognize(*_args, **_kwargs):
                events.append("ocr")
                return OCRResult("Ada", "Full dialogue.", 99, "balanced", 1)

            with (
                self.subTest(fail=fail),
                patch("vntts.dialog_capture.capture_dialog", side_effect=pixels),
                patch(
                    "vntts.dialog_capture.recognize_screenshot_result",
                    side_effect=recognize,
                ),
            ):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "capture failed"):
                        analyze_dialog_snapshot(
                            "unused", capture_context=capture_context
                        )
                else:
                    analyze_dialog_snapshot("unused", capture_context=capture_context)
                self.assertEqual(
                    events, ["hide", "pixels", "restore"] + ([] if fail else ["ocr"])
                )

    def test_diagnostic_snapshot_receives_capture_provenance(self):
        snapshots = []
        with patch(
            "vntts.dialog_capture.recognize_screenshot_result",
            return_value=OCRResult("Name", "Dialogue", 99, "balanced", 1),
        ):
            analyze_dialog_snapshot(
                "unused",
                capture_target=self.target,
                diagnostic_handler=snapshots.append,
            )
        self.assertEqual(snapshots[0].capture_provenance["window_id"], 100)

    def test_telemetry_failure_cannot_change_capture_success_or_primary_error(self):
        broken = Mock(side_effect=RuntimeError("telemetry failed"))
        with self.assertWarnsRegex(RuntimeWarning, "record capture provenance"):
            frame = capture_live_frame("unused", self.target, provenance_handler=broken)
        self.assertTrue(frame.provenance.success)
        self.backend.get_window.return_value = None
        self.backend.list_windows.return_value = []
        with self.assertWarnsRegex(RuntimeWarning, "record capture provenance"):
            with self.assertRaises(ScreenCaptureError) as caught:
                capture_live_frame("unused", self.target, provenance_handler=broken)
        self.assertIsInstance(caught.exception.__cause__, WindowNotFoundError)

    def test_native_layout_failure_records_failed_attempt(self):
        self.source.capture.side_effect = CaptureError(
            "Native layout changed", code="layout_changed"
        )
        records = []
        with self.assertRaises(ScreenCaptureError):
            capture_live_frame("unused", self.target, provenance_handler=records.append)
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].error_type, "CaptureError")
        self.source.close.assert_called_once()

    def test_image_write_failure_is_reported_once_with_attempt_provenance(self):
        records = []
        with TemporaryDirectory() as directory:
            with patch.object(Image.Image, "save", side_effect=OSError("disk full")):
                with self.assertRaises(ScreenCaptureError) as caught:
                    capture_dialog(directory, provenance_handler=records.append)
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].error_type, "OSError")
        self.assertIs(caught.exception.provenance, records[0])

    def test_window_errors_are_expected_but_unrelated_errors_remain_unexpected(self):
        for kind in (
            WindowCaptureError,
            WindowCaptureUnavailableError,
            WindowNotFoundError,
            WindowMinimizedError,
        ):
            self.assertTrue(
                format_runtime_error(kind("closed")).startswith(
                    "Screen capture unavailable:"
                )
            )
        self.assertTrue(
            format_runtime_error(ValueError("bad data")).startswith(
                "Unexpected dialog processing failure:"
            )
        )
        wrapped = ScreenCaptureError("closed")
        wrapped.__cause__ = WindowNotFoundError("closed")
        self.assertTrue(
            format_runtime_error(wrapped).startswith("Screen capture unavailable:")
        )


def glyph_frame(
    text="Ouch", speaker="Name", *, brightness=230, shift=(0, 0), cue=False
):
    """Binary glyph fixtures isolate identity from antialiasing and camera noise."""
    image = Image.new("RGB", (256, 100), (25, 25, 25))
    mask = Image.new("1", image.size)
    draw = ImageDraw.Draw(mask)
    dx, dy = shift
    draw.text((3 + dx, 3 + dy), speaker, fill=1)
    draw.text((3 + dx, 50 + dy), text, fill=1)
    image.paste((brightness,) * 3, mask=mask)
    if cue:
        ImageDraw.Draw(image).polygon(((246, 84), (250, 84), (248, 88)), fill="white")
    return CapturedDialogFrame(image, 0)


def ridge_frame(text="A complete dialogue line.", speaker="Cornerstone"):
    image = Image.new("RGB", (1600, 324), (25,) * 3)
    draw = ImageDraw.Draw(image)
    # Three antialiased pixels produce five contiguous strong ridge rows.
    for y, brightness in ((163, 80), (164, 110), (165, 80)):
        draw.line((55, y, 1494, y), fill=(brightness,) * 3)
    draw.text((60, 111), speaker, fill="white", font_size=30)
    draw.text((60, 190), text, fill="white", font_size=30)
    return CapturedDialogFrame(image, 0)


class DialogFrameRegionsTest(unittest.TestCase):
    def test_five_row_divider_preserves_complete_speaker_and_dialogue(self):
        frame = ridge_frame()
        self.assertIsNotNone(frame.ocr_regions)
        regions = frame.ocr_regions
        self.assertEqual(regions.dialogue._pixel_box(*frame.image.size)[1], 167)
        draw = ImageDraw.Draw(frame.image)
        for region, position, text in (
            (regions.speaker, (60, 111), "Cornerstone"),
            (regions.dialogue, (60, 190), "A complete dialogue line."),
        ):
            with self.subTest(text=text):
                left, top, right, bottom = region._pixel_box(*frame.image.size)
                ink_left, ink_top, ink_right, ink_bottom = draw.textbbox(
                    position, text, font_size=30
                )
                self.assertLessEqual(left, ink_left)
                self.assertLessEqual(top, ink_top)
                self.assertGreaterEqual(right, ink_right)
                self.assertGreaterEqual(bottom, ink_bottom)


if __name__ == "__main__":
    unittest.main()
