import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

from tests.native_capture_fixtures import native_frame
from vntts.native_capture import CaptureError
from vntts.window_capture import (
    DisplayCaptureTarget,
    WindowCaptureTarget,
    WindowInfo,
    WindowNotFoundError,
)


class NativeSourceOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.worker = Mock(clock_offset=10.0, clock_uncertainty=0.1)
        self.worker.capture.return_value = native_frame(window_id=None, pid=None)
        self.factory = patch(
            "vntts.window_capture.NativeCaptureSession", return_value=self.worker
        )
        self.create = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.target = DisplayCaptureTarget()
        self.addCleanup(self.target.close)

    def test_static_frames_share_source_until_stop_then_reopen(self):
        first = self.target.capture_native()
        self.assertIs(self.target.capture_native(), first)
        self.create.assert_called_once()
        self.assertEqual(self.target.clock_offset, 10)
        self.target.close()
        self.worker.close.assert_called_once()
        self.target.capture_native()
        self.assertEqual(self.create.call_count, 2)

    def test_close_interrupts_inflight_request_and_does_not_wait_for_capture_lock(self):
        entered = Event()

        def capture(*, cancel):
            entered.set()
            self.assertTrue(stopped.wait(1))
            self.assertTrue(cancel())
            raise CaptureError("cancelled", code="cancelled")

        stopped = Event()
        self.worker.capture.side_effect = capture
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.target.capture_native)
            self.assertTrue(entered.wait(1))
            self.target.close()
            stopped.set()
            with self.assertRaises(CaptureError):
                future.result(timeout=1)
        self.worker.close.assert_called_once()

    def test_close_during_startup_cancels_unpublished_worker(self):
        entered, release = Event(), Event()

        def create(_target, *, cancel):
            entered.set()
            self.assertTrue(release.wait(1))
            self.assertTrue(cancel())
            return self.worker

        self.create.side_effect = create
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.target.capture_native)
            self.assertTrue(entered.wait(1))
            self.target.close()
            release.set()
            with self.assertRaises(CaptureError):
                future.result(timeout=1)
        self.worker.close.assert_called_once()
        self.worker.capture.assert_not_called()

    def test_error_discards_source_and_does_not_return_last_frame(self):
        self.target.capture_native()
        self.worker.capture.side_effect = CaptureError(
            "minimized", code="target_minimized"
        )
        with self.assertRaises(CaptureError):
            self.target.capture_native()
        self.worker.close.assert_called_once()
        self.worker.capture.side_effect = None
        self.target.capture_native()
        self.assertEqual(self.create.call_count, 2)

    def test_external_cancel_does_not_open_source(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(CaptureError):
            self.target.capture_native(cancel=cancelled)
        self.create.assert_not_called()

    def test_rebind_requires_same_process_and_unambiguous_window(self):
        backend = Mock()
        backend.list_windows.return_value = [WindowInfo(100, "Game", 42)]
        target = WindowCaptureTarget("Game", backend)
        self.addCleanup(target.close)
        with patch("vntts.window_capture.psutil.Process") as process:
            process.return_value.create_time.return_value = 1.0
            target.capture_native()
            backend.get_window.return_value = None
            backend.list_windows.return_value = [WindowInfo(101, "Game", 42)]
            target.capture_native()
            self.assertEqual(self.create.call_args.args[0]["window_id"], 101)
            self.worker.close.assert_called_once()
            backend.get_window.return_value = None
            backend.list_windows.return_value = [
                WindowInfo(102, "Game", 42),
                WindowInfo(103, "Game", 42),
            ]
            with self.assertRaises(WindowNotFoundError):
                target.capture_native()
            backend.list_windows.return_value = [WindowInfo(104, "Game", 84)]
            with self.assertRaises(WindowNotFoundError):
                target.capture_native()

    def test_pid_reuse_requires_explicit_reselection(self):
        window = WindowInfo(100, "Game", 42)
        backend = Mock()
        backend.list_windows.return_value = [window]
        backend.get_window.return_value = window
        target = WindowCaptureTarget("Game", backend)
        self.addCleanup(target.close)
        with patch("vntts.window_capture.psutil.Process") as process:
            process.return_value.create_time.return_value = 1.0
            target.capture_native()
            process.return_value.create_time.return_value = 2.0
            with self.assertRaisesRegex(WindowNotFoundError, "process changed"):
                target.capture_native()
        self.create.assert_called_once()


class CalibrationPlacementTest(unittest.TestCase):
    def test_frozen_image_fits_negative_origin_and_scaled_displays(self):
        from PySide6.QtCore import QRect

        from vntts.calibration import fit_calibration_preview

        for available in (QRect(0, 0, 800, 600), QRect(-1280, -100, 1280, 900)):
            for desired in (QRect(790, 590, 1200, 800), QRect(-5000, 100, 1920, 1080)):
                result = fit_calibration_preview(desired, available, (2100, 1600))
                self.assertTrue(available.contains(result))
                self.assertAlmostEqual(
                    result.width() / result.height(), 2100 / 1600, delta=0.003
                )
