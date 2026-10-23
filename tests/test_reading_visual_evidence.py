import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from vntts.capture_provenance import CaptureProvenance
from vntts.dialog_capture import CapturedDialogFrame
from vntts.reading_diagnostics import (
    ReadingDiagnosticSession,
    retain_visual_wait_frames,
)


class ReadingVisualEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = object.__new__(ReadingDiagnosticSession)
        self.session.directory = Path(self.temporary.name)
        self.session.recording = SimpleNamespace(target={"pid": 2, "window_id": 1})
        self.session.phase = "recording"
        self.session.visual_wait_lock = Lock()
        self.session.visual_wait_frames = {}
        self.session.visual_wait_pixels = self.session.visual_wait_dropped = 0
        self.provenance = CaptureProvenance(
            "capture-id",
            "run-id",
            1,
            2,
            "Game",
            1,
            "Game",
            2,
            None,
            None,
            (100, 80),
            True,
        )
        self.frame = CapturedDialogFrame(
            Image.new("RGB", (100, 80)), 1, provenance=self.provenance
        )
        self.active = patch("vntts.reading_diagnostics._active", self.session)
        self.active.start()
        self.addCleanup(self.active.stop)

    def retain(self, generation=3, latest=None):
        retain_visual_wait_frames(
            self.frame,
            latest or self.frame,
            run_id="run-id",
            generation=generation,
            frame_version=4,
        )

    def test_original_pixels_and_geometry_survive_until_export(self):
        self.retain()
        self.frame.image.putpixel((0, 0), (255, 255, 255))
        self.session.phase = "finalizing"
        self.session._retain_visual_wait_frames()
        folder = self.session.directory / "visual-waits"
        with Image.open(folder / "00-routed.png") as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))
        metadata = json.loads((folder / "frames.json").read_text())["entries"][0]
        self.assertEqual(metadata["generation"], 3)
        self.assertEqual(metadata["frames"][0]["capture"]["window_id"], 1)
        self.assertIn("ocr_regions", metadata["frames"][0])

    def test_journal_only_foreign_and_failed_captures_never_retain_pixels(self):
        recording = self.session.recording
        self.session.recording = None
        self.retain()
        self.session.recording = recording
        for provenance in (
            replace(self.provenance, owner_pid=3),
            replace(self.provenance, run_id="other-run"),
            replace(self.provenance, window_id=4),
            replace(self.provenance, success=False),
            None,
        ):
            self.retain(latest=replace(self.frame, provenance=provenance))
        self.assertFalse(self.session.visual_wait_frames)

    def test_retention_is_bounded_and_closed_during_finalization(self):
        for generation in range(20):
            self.retain(generation)
            self.retain(generation)
        self.assertEqual(len(self.session.visual_wait_frames), 12)
        self.assertGreater(self.session.visual_wait_dropped, 0)
        self.session.visual_wait_frames.clear()
        self.session.visual_wait_pixels = 8_000_000
        self.retain()
        self.assertFalse(self.session.visual_wait_frames)
        self.session.visual_wait_pixels = 0
        self.session.phase = "finalizing"
        self.retain()
        self.assertFalse(self.session.visual_wait_frames)
