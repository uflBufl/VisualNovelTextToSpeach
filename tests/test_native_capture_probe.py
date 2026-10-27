import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.native_capture_probe import capture_report, main
from vntts.native_capture import CaptureError, NativeFrame


class CaptureProbeTest(unittest.TestCase):
    def test_target_requires_explicit_authorization_before_worker_launch(self):
        with patch("scripts.native_capture_probe.NativeCaptureSession") as worker:
            with (
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                main(["--window-id", "1", "--pid", "2", "--output", "unused"])
            worker.assert_not_called()

    def test_invalid_arguments_never_launch_worker_or_fixture(self):
        for arguments in (
            ["--window-id", "1", "--allow-live", "--output", "unused"],
            ["--window-id", "-1", "--pid", "2", "--allow-live", "--output", "unused"],
            ["--display-id", "0", "--pid", "2", "--allow-live", "--output", "unused"],
            [
                "--display-id",
                "0",
                "--allow-live",
                "--output",
                "unused",
                "--frames",
                "101",
            ],
            ["--fixture", "--allow-live"],
        ):
            with (
                self.subTest(arguments=arguments),
                patch("scripts.native_capture_probe.NativeCaptureSession") as worker,
                patch("scripts.native_capture_probe.fixture") as fixture,
            ):
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    main(arguments)
                worker.assert_not_called()
                fixture.assert_not_called()

    def options(self, output, *, save_frames=False):
        return argparse.Namespace(
            window_id=17,
            pid=29,
            display_id=None,
            output=output,
            frames=2,
            timeout_ms=100,
            save_frames=save_frames,
        )

    def owner(self):
        owner = Mock()
        owner.backend = "macos_screencapturekit"
        owner.worker_clock_at_open = 500.0
        owner.capture.return_value = NativeFrame(
            backend=owner.backend,
            scope="selected_window",
            session_id="fixture",
            window_id=17,
            pid=29,
            width=1,
            height=1,
            row_stride=4,
            pixel_format="BGRA8",
            content_rect=(0, 0, 1, 1),
            desktop_bounds=(0, 0, 1, 1),
            frame_sequence=1,
            source_time=1.0,
            acquired_at=4.0,
            layout_epoch=1,
            pixels=bytes([0, 0, 255, 255]),
        )
        return owner

    def test_report_omits_pixels_by_default_and_preserves_clock_domains(self):
        owner = self.owner()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.native_capture_probe.NativeCaptureSession") as session,
        ):
            session.return_value.__enter__.return_value = owner
            output = Path(directory) / "report"
            capture_report(self.options(output))
            session.assert_called_once_with(
                {"kind": "window", "window_id": 17, "pid": 29}
            )
            result = json.loads((output / "report.json").read_text())
            self.assertEqual(list(output.iterdir()), [output / "report.json"])
            self.assertEqual(result["worker_clock_at_open"], 500.0)
            self.assertEqual(len(result["frames"]), 2)
            self.assertNotIn("pixels", result["frames"][0])
            self.assertEqual(result["frames"][0]["source_time"], 1.0)
            self.assertIn("python_response_received", result["frames"][0])
            session.return_value.__exit__.assert_called_once()

    def test_save_requires_separate_flag_and_never_overwrites_output(self):
        owner = self.owner()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.native_capture_probe.NativeCaptureSession") as session,
        ):
            session.return_value.__enter__.return_value = owner
            output = Path(directory) / "report"
            capture_report(self.options(output, save_frames=True))
            self.assertEqual(len(list(output.glob("*.png"))), 2)
            with self.assertRaises(FileExistsError):
                capture_report(self.options(output, save_frames=True))
            self.assertEqual(session.call_count, 1)

    def test_failure_report_keeps_worker_code(self):
        owner = self.owner()
        owner.capture.side_effect = CaptureError("Target closed", code="target_closed")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.native_capture_probe.NativeCaptureSession") as session,
        ):
            session.return_value.__enter__.return_value = owner
            output = Path(directory) / "report"
            with self.assertRaises(CaptureError):
                capture_report(self.options(output))
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(
                report["error"], {"code": "target_closed", "message": "Target closed"}
            )
            self.assertEqual(report["frames"], [])
