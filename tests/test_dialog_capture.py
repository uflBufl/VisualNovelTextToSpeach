import json
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from vntts.dialog_capture import (
    CapturedDialogFrame,
    ScreenCaptureError,
    analyze_dialog_snapshot,
    capture_dialog,
    capture_live_frame,
    dialog_frames_equivalent,
    dialog_render_frames_equivalent,
    format_runtime_error,
)
from vntts.ocr import DialogRegion, OCRRegions, OCRResult
from vntts.window_capture import (
    WindowCaptureError,
    WindowCaptureTarget,
    WindowCaptureUnavailableError,
    WindowGeometry,
    WindowInfo,
    WindowMinimizedError,
    WindowNotFoundError,
)


class CaptureProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.window = WindowInfo(100, "Selected game", 42)
        self.backend = Mock()
        self.backend.list_windows.return_value = [self.window]
        self.backend.get_window.return_value = self.window
        self.backend.get_client_geometry.return_value = WindowGeometry(40, 50, 200, 100)
        self.target = WindowCaptureTarget("Selected game", self.backend)
        self.screen = Mock()
        self.screen.monitors = [
            None,
            {"left": 0, "top": 0, "width": 200, "height": 100},
        ]
        self.screen.grab.return_value = Mock(size=(2, 1), bgra=b"\0\0\0\xff" * 2)
        self.factory = patch("vntts.dialog_capture.mss.mss")
        self.factory.start().return_value.__enter__.return_value = self.screen
        self.addCleanup(self.factory.stop)
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
            record.capture_bounds, {"left": 40, "top": 100, "width": 200, "height": 50}
        )
        self.assertEqual(record.output_dimensions, (2, 1))
        self.assertEqual((record.scope, record.mechanism), ("desktop_rectangle", "mss"))
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
                self.screen.grab.assert_not_called()

    def test_handle_reuse_during_capture_fails_and_next_attempt_records_new_owner(self):
        self.backend.get_window.return_value = WindowInfo(100, "Selected game", 84)
        records = []
        with self.assertRaises(ScreenCaptureError):
            capture_live_frame("unused", self.target, provenance_handler=records.append)
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].owner_pid, 42)
        next_frame = capture_live_frame("unused", self.target)
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

    def test_window_move_during_capture_records_failed_attempt(self):
        self.backend.get_client_geometry.side_effect = [
            WindowGeometry(40, 50, 200, 100),
            WindowGeometry(42, 50, 200, 100),
        ]
        records = []
        with self.assertRaises(ScreenCaptureError):
            capture_live_frame("unused", self.target, provenance_handler=records.append)
        self.assertFalse(records[0].success)
        self.assertEqual(records[0].error_type, "WindowCaptureError")

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


class DialogFrameEquivalenceTest(unittest.TestCase):
    def test_short_left_edge_text_survives_brightness_translation_and_cue(self):
        original = glyph_frame()
        for brightness, shift in ((210, (1, -2)), (250, (-2, 2)), (230, (0, 0))):
            with self.subTest(brightness=brightness, shift=shift):
                self.assertTrue(
                    dialog_frames_equivalent(
                        original,
                        glyph_frame(brightness=brightness, shift=shift, cue=True),
                    )
                )

    def test_small_roi_translation_and_size_jitter_keeps_all_text_evidence(self):
        original, shifted = (
            glyph_frame("Ouch!", ""),
            glyph_frame("Ouch!", "", brightness=210, shift=(1, 1)),
        )
        original = CapturedDialogFrame(
            original.image,
            0,
            OCRRegions(DialogRegion(0, 0, 1, 0.3), DialogRegion(0, 0.4, 0.9, 0.5)),
        )
        shifted = CapturedDialogFrame(
            shifted.image,
            0,
            OCRRegions(DialogRegion(0, 0, 0.99, 0.3), DialogRegion(0, 0.41, 0.91, 0.5)),
        )
        self.assertTrue(dialog_frames_equivalent(original, shifted))
        changed = glyph_frame("Ouch!x", "", shift=(1, 1))
        changed = CapturedDialogFrame(changed.image, 0, shifted.ocr_regions)
        self.assertFalse(dialog_frames_equivalent(original, changed))

    def test_typewriter_additions_punctuation_new_name_and_empty_text_invalidate(self):
        original = glyph_frame()
        for changed in (
            glyph_frame("Ouch!"),
            glyph_frame("Ouch."),
            glyph_frame("Ouc"),
            glyph_frame("Ouch", "Other"),
            glyph_frame(""),
            glyph_frame(shift=(3, 0)),
        ):
            self.assertFalse(dialog_frames_equivalent(original, changed))

    def test_small_addition_to_long_line_is_not_hidden_by_global_similarity(self):
        original = glyph_frame("A very long line with many words")
        self.assertFalse(
            dialog_frames_equivalent(
                original, glyph_frame("A very long line with many words.")
            )
        )

    def test_dim_single_pixel_punctuation_is_not_hidden_by_bright_core_threshold(self):
        original = glyph_frame()
        changed = glyph_frame()
        changed.image.putpixel((42, 58), (180, 180, 180))
        self.assertFalse(dialog_frames_equivalent(original, changed))

    def test_real_bottom_right_punctuation_is_not_removed_as_continue_cue(self):
        original = glyph_frame()
        changed = glyph_frame()
        changed.image.putpixel((244, 72), (230, 230, 230))
        self.assertFalse(dialog_frames_equivalent(original, changed))

    def antialiased_frame(self, *, shifted_regions=False):
        image = Image.new("RGB", (800, 162), (25, 25, 25))
        draw = ImageDraw.Draw(image)
        draw.rectangle((28, 48, 36, 53), fill=(182,) * 3)
        draw.rectangle((38, 48, 44, 53), fill=(180,) * 3)
        draw.rectangle((25, 100, 34, 106), fill=(238,) * 3)
        image.putpixel((27, 49), (158,) * 3)
        image.putpixel((37, 50), (159,) * 3)
        image.putpixel((24, 102), (217,) * 3)
        left, width = (16, 735) if shifted_regions else (15, 737)
        regions = OCRRegions(
            DialogRegion(27 / 800, 46 / 162, 143 / 800, 29 / 162),
            DialogRegion(left / 800, 84 / 162, width / 800, 78 / 162),
        )
        return CapturedDialogFrame(image, 0, regions)

    def test_crop_jitter_and_supported_antialiasing_crossings_keep_fixed_anchor(self):
        original = self.antialiased_frame()
        changed = self.antialiased_frame(shifted_regions=True)
        changed.image.putpixel((27, 49), (160,) * 3)
        changed.image.putpixel((37, 50), (162,) * 3)
        changed.image.putpixel((24, 102), (218,) * 3)
        for compare in (dialog_frames_equivalent, dialog_render_frames_equivalent):
            with self.subTest(comparator=compare.__name__):
                self.assertTrue(compare(original, changed))
                self.assertTrue(compare(changed, original))

    def test_crop_union_keeps_new_glyph_at_region_edge(self):
        original = self.antialiased_frame()
        changed = self.antialiased_frame(shifted_regions=True)
        changed.image.putpixel((15, 102), (180,) * 3)
        for compare in (dialog_frames_equivalent, dialog_render_frames_equivalent):
            self.assertFalse(compare(original, changed))

    def test_new_dim_punctuation_without_stable_core_support_invalidates(self):
        original = self.antialiased_frame()
        changed = self.antialiased_frame()
        original.image.putpixel((70, 102), (158,) * 3)
        changed.image.putpixel((70, 102), (160,) * 3)
        for compare in (dialog_frames_equivalent, dialog_render_frames_equivalent):
            self.assertFalse(compare(original, changed))

    def test_new_pixel_beside_glyph_is_not_antialiasing_noise(self):
        original = self.antialiased_frame()
        changed = self.antialiased_frame()
        changed.image.putpixel((27, 49), (180,) * 3)
        for compare in (dialog_frames_equivalent, dialog_render_frames_equivalent):
            self.assertFalse(compare(original, changed))

    def test_cumulative_brightness_changes_are_bounded_by_original_anchor(self):
        original = self.antialiased_frame()
        first = self.antialiased_frame()
        second = self.antialiased_frame()
        first.image.putpixel((27, 49), (162,) * 3)
        second.image.putpixel((27, 49), (166,) * 3)
        for compare in (dialog_frames_equivalent, dialog_render_frames_equivalent):
            self.assertTrue(compare(original, first))
            self.assertTrue(compare(first, second))
            self.assertFalse(compare(original, second))

    def test_independent_speaker_and_dialogue_translations_invalidate(self):
        original = glyph_frame()
        changed = glyph_frame(shift=(1, 0))
        changed.image.paste(glyph_frame(shift=(-1, 0)).image.crop((0, 0, 256, 30)))
        self.assertFalse(dialog_frames_equivalent(original, changed))

    def test_render_comparison_preserves_pixel_position_and_small_text_additions(self):
        original = glyph_frame()
        self.assertFalse(
            dialog_render_frames_equivalent(original, glyph_frame(shift=(1, 0)))
        )
        overlay = glyph_frame()
        ImageDraw.Draw(overlay.image).rectangle((110, 54, 150, 66), fill="white")
        for changed in (
            glyph_frame("Ouch!"),
            glyph_frame("Ouc"),
            glyph_frame("Ouch", "Other"),
            glyph_frame(""),
            overlay,
        ):
            self.assertFalse(dialog_render_frames_equivalent(original, changed))

    def test_continue_cue_alone_does_not_prove_visible_dialogue(self):
        empty = glyph_frame("", "", cue=True)
        self.assertFalse(dialog_frames_equivalent(empty, empty))

    def test_identical_empty_frames_are_not_reading_evidence(self):
        empty = glyph_frame("", "")
        self.assertFalse(dialog_frames_equivalent(empty, empty))


if __name__ == "__main__":
    unittest.main()
