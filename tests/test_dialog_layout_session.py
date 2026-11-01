import gc
import unittest
import weakref
from dataclasses import replace
from unittest.mock import patch

from PIL import Image, ImageDraw

from tests import test_adaptive_ocr
from vntts.capture_provenance import CaptureProvenance
from vntts.dialog_capture import (
    CapturedDialogFrame,
    DialogLayoutSession,
    capture_live_frame,
    dialog_glyphs_visible,
    fingerprint_dialog_frame,
)
from vntts.ocr import DialogRegion, OCRRegions


class DialogLayoutSessionTest(unittest.TestCase):
    @staticmethod
    def image(*, name="X", text="?", lines=1, divider=True):
        image, y = test_adaptive_ocr.AdaptiveOCRTest.frame(lines, name, text)
        if not divider:
            ImageDraw.Draw(image).line((10, y, 405, y), fill="black")
        return image

    @staticmethod
    def provenance():
        return CaptureProvenance(
            capture_id="first",
            run_id="run",
            started_monotonic=1,
            completed_monotonic=2,
            requested_window_title="Game",
            window_id=10,
            window_title="Game",
            owner_pid=20,
            window_bounds=None,
            capture_bounds=None,
            output_dimensions=(420, 180),
            success=True,
            session_id="capture-session",
            frame_sequence=1,
            layout_epoch=1,
        )

    def test_positive_blank_positive_reuses_only_until_fresh_divider_moves(self):
        session = DialogLayoutSession()
        first = CapturedDialogFrame(self.image(), 1)
        self.assertIs(session.apply(first), first)
        blank = CapturedDialogFrame(Image.new("RGB", first.image.size), 2)
        self.assertIsNone(blank.ocr_regions)

        held = session.apply(blank)

        self.assertIs(held.ocr_regions, first.ocr_regions)
        self.assertIs(held.image, blank.image)
        self.assertEqual(held.capture_ms, 2)
        moved = CapturedDialogFrame(self.image(lines=4), 3)
        self.assertIs(session.apply(moved), moved)
        self.assertLess(moved.ocr_regions.dialogue.top, held.ocr_regions.dialogue.top)
        next_blank = CapturedDialogFrame(Image.new("RGB", first.image.size), 4)
        self.assertIs(session.apply(next_blank).ocr_regions, moved.ocr_regions)

    def test_initial_dividerless_frames_keep_legacy_and_explicit_configuration(self):
        configured = OCRRegions(
            DialogRegion(0.1, 0.1, 0.3, 0.2),
            DialogRegion(0.1, 0.4, 0.7, 0.5),
        )
        for regions in (None, configured):
            with self.subTest(configured=regions is not None):
                session = DialogLayoutSession()
                frame = CapturedDialogFrame(self.image(divider=False), 0, regions)
                self.assertIs(session.apply(frame), frame)
                self.assertIs(frame.ocr_regions, regions)
                blank = CapturedDialogFrame(Image.new("RGB", frame.image.size), 0)
                self.assertIsNone(session.apply(blank).ocr_regions)

    def test_native_source_changes_invalidate_learned_geometry(self):
        provenance = self.provenance()
        changes = (
            {"run_id": "next-run"},
            {"window_id": 11},
            {"owner_pid": 21},
            {"session_id": "next-session"},
            {"layout_epoch": 2},
        )
        for change in changes:
            with self.subTest(change=change):
                session = DialogLayoutSession()
                first = CapturedDialogFrame(self.image(), 0, provenance=provenance)
                session.apply(first)
                blank = CapturedDialogFrame(
                    Image.new("RGB", first.image.size),
                    0,
                    provenance=replace(provenance, **change),
                )
                self.assertIsNone(session.apply(blank).ocr_regions)

    def test_provenance_availability_dimensions_and_context_invalidate_geometry(self):
        provenance = self.provenance()
        for before, after, size, context in (
            (None, provenance, (420, 180), "same"),
            (provenance, None, (420, 180), "same"),
            (provenance, provenance, (840, 180), "same"),
            (provenance, provenance, (420, 180), "changed"),
        ):
            with self.subTest(before=before, after=after, size=size, context=context):
                session = DialogLayoutSession()
                session.apply(
                    CapturedDialogFrame(self.image(), 0, provenance=before),
                    context="same",
                )
                frame = CapturedDialogFrame(Image.new("RGB", size), 0, provenance=after)
                self.assertIsNone(session.apply(frame, context=context).ocr_regions)

    def test_new_capture_metadata_does_not_discard_geometry_or_replace_pixels(self):
        session = DialogLayoutSession()
        provenance = self.provenance()
        first = CapturedDialogFrame(self.image(), 1, provenance=provenance)
        session.apply(first)
        current_provenance = replace(
            provenance,
            capture_id="second",
            frame_sequence=2,
            started_monotonic=3,
            completed_monotonic=4,
            window_title="Game - Chapter 2",
        )
        current = CapturedDialogFrame(
            Image.new("RGB", first.image.size), 7, provenance=current_provenance
        )

        held = session.apply(current)

        self.assertIs(held.ocr_regions, first.ocr_regions)
        self.assertIs(held.image, current.image)
        self.assertIs(held.provenance, current_provenance)
        self.assertEqual(held.capture_ms, 7)
        self.assertIsNone(current.ocr_regions)
        self.assertEqual(held.image.getpixel((0, 0)), (0, 0, 0))

    def test_session_does_not_retain_frames_or_images(self):
        session = DialogLayoutSession()
        frame = CapturedDialogFrame(self.image(), 0)
        image_ref = weakref.ref(frame.image)
        frame_ref = weakref.ref(frame)
        session.apply(frame)
        del frame
        gc.collect()

        self.assertIsNone(frame_ref())
        self.assertIsNone(image_ref())
        self.assertIsNotNone(
            session.apply(
                CapturedDialogFrame(Image.new("RGB", (420, 180)), 0)
            ).ocr_regions
        )

    def test_retained_geometry_keeps_short_punctuation_and_typewriter_glyphs(self):
        session = DialogLayoutSession()
        seed = session.apply(CapturedDialogFrame(self.image(name="???"), 0))
        fingerprints = []
        for text in ("I", "No.", "?", "?!", "...", "H", "He", "Hey!"):
            with self.subTest(text=text):
                image = self.image(name="???", text=text, divider=False)
                frame = session.apply(CapturedDialogFrame(image, 0))
                self.assertIs(frame.ocr_regions, seed.ocr_regions)
                self.assertTrue(dialog_glyphs_visible(frame))
                # Every ink pixel below the divider must remain in the body crop.
                expected_ink = image.crop((0, 123, 420, 180)).convert("L").getbbox()
                self.assertIsNotNone(expected_ink)
                left, top, right, bottom = frame.ocr_regions.dialogue._pixel_box(
                    *image.size
                )
                x1, y1, x2, y2 = expected_ink
                self.assertLessEqual(left, x1)
                self.assertLessEqual(top, 123 + y1)
                self.assertGreaterEqual(right, x2)
                self.assertGreaterEqual(bottom, 123 + y2)
                fingerprints.append(fingerprint_dialog_frame(frame))
        self.assertEqual(len(set(fingerprints)), len(fingerprints))

    def test_live_factory_uses_original_calibration_after_a_short_name_and_blank(self):
        configured = OCRRegions(
            DialogRegion(0.02, 0.1, 0.94, 0.2),
            DialogRegion(0.02, 0.72, 0.95, 0.28),
        )
        images = [
            self.image(name="X"),
            Image.new("RGB", (420, 180)),
            self.image(name="Adar Llwch Gwin", lines=4),
        ]
        session = DialogLayoutSession()
        with patch(
            "vntts.dialog_capture.capture_dialog",
            side_effect=[(image, None) for image in images],
        ):
            frames = [
                capture_live_frame(
                    "unused", ocr_regions=configured, layout_session=session
                )
                for _ in images
            ]

        self.assertIs(frames[1].ocr_regions, frames[0].ocr_regions)
        long_box = frames[2].ocr_regions.speaker._pixel_box(*images[2].size)
        short_box = frames[0].ocr_regions.speaker._pixel_box(*images[0].size)
        self.assertGreater(long_box[2], short_box[2])
        self.assertGreater(long_box[2], 150)
        self.assertLess(
            frames[2].ocr_regions.dialogue.top, frames[0].ocr_regions.dialogue.top
        )
        self.assertFalse(configured._resolved)
        for frame, image in zip(frames, images, strict=True):
            self.assertIs(frame.image, image)

    def test_live_factory_invalidates_on_every_capture_configuration_change(self):
        configured = OCRRegions(
            DialogRegion(0.1, 0.1, 0.3, 0.2),
            DialogRegion(0.1, 0.4, 0.7, 0.5),
        )
        original = {
            "capture_target": object(),
            "region": DialogRegion(0, 0.6, 1, 0.4),
            "run_id": "first",
            "layout_context": "profile",
        }
        for change in (
            {"capture_target": object()},
            {"region": DialogRegion(0, 0.5, 1, 0.5)},
            {"run_id": "second"},
            {"layout_context": "other-profile"},
            {"ocr_regions": configured},
        ):
            with self.subTest(change=change):
                session = DialogLayoutSession()
                with patch(
                    "vntts.dialog_capture.capture_dialog",
                    side_effect=[
                        (self.image(), None),
                        (Image.new("RGB", (420, 180)), None),
                    ],
                ):
                    capture_live_frame("unused", layout_session=session, **original)
                    current = capture_live_frame(
                        "unused", layout_session=session, **(original | change)
                    )
                self.assertIs(current.ocr_regions, change.get("ocr_regions"))


if __name__ == "__main__":
    unittest.main()
