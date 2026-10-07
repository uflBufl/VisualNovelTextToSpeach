"""Shutdown completion requires released owners, not just attempted cleanup."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

from vntts.controller import AppController
from vntts.settings import AppSettings


class ControllerShutdownTest(unittest.TestCase):
    def test_executor_failures_preserve_primary_and_remaining_owners(self):
        for primary in (RuntimeError("capture drain"), KeyboardInterrupt("capture")):
            with self.subTest(primary=type(primary).__name__):
                controller = AppController(AppSettings())
                controller.tts = backend = Mock()
                executors = [Mock() for _ in range(4)]
                attributes = (
                    "capture_executor",
                    "ocr_executor",
                    "speech_executor",
                    "playback_executor",
                )
                for attribute, executor in zip(attributes, executors, strict=True):
                    setattr(controller, attribute, executor)
                executors[0].shutdown.side_effect = primary
                executors[2].shutdown.side_effect = OSError("speech drain")

                with self.assertRaises(type(primary)) as caught:
                    controller.shutdown()

                self.assertIs(caught.exception, primary)
                self.assertIn(
                    "speech_executor shutdown failed: speech drain", primary.__notes__
                )
                for executor in executors:
                    executor.shutdown.assert_called_once_with(
                        wait=True, cancel_futures=False
                    )
                self.assertIs(controller.capture_executor, executors[0])
                self.assertIs(controller.speech_executor, executors[2])
                self.assertIsNone(controller.ocr_executor)
                self.assertIsNone(controller.playback_executor)
                backend.shutdown.assert_not_called()
                self.assertFalse(controller.shutdown_complete.is_set())
                controller.prepare_startup()
                self.assertFalse(controller.start())

    def test_background_drain_failure_still_waits_for_other_real_worker(self):
        started, release, reported, backend_closed = (Event() for _ in range(4))
        errors = []

        def report(error):
            errors.append(error)
            if error is primary:
                reported.set()

        controller = AppController(AppSettings(), error_handler=report)
        controller.live_reader = Mock()
        controller.live_reader.wait.side_effect = TimeoutError("reader timeout")
        controller.tts = backend = Mock()
        backend.shutdown.side_effect = backend_closed.set
        failed = Mock()
        primary = RuntimeError("capture drain failed")
        failed.shutdown.side_effect = [None, primary]
        controller.capture_executor = failed
        worker = ThreadPoolExecutor(max_workers=1)
        controller.speech_executor = worker
        worker.submit(
            lambda: (
                started.set(),
                release.wait(),
                backend.prepare_playback("Ada", "line"),
            )
        )
        self.assertTrue(started.wait(1))

        try:
            controller.shutdown()
            self.assertFalse(backend_closed.is_set())
            self.assertIs(controller.speech_executor, worker)
            controller.prepare_startup()
            self.assertFalse(controller.start())
        finally:
            release.set()

        try:
            self.assertTrue(reported.wait(1))
        finally:
            worker.shutdown(wait=True)
        self.assertEqual(errors, [unittest.mock.ANY, primary])
        self.assertIs(controller.capture_executor, failed)
        self.assertIsNone(controller.speech_executor)
        self.assertFalse(backend_closed.is_set())
        self.assertFalse(controller.shutdown_complete.is_set())
        backend.prepare_playback.assert_called_once_with("Ada", "line")

    def test_backend_release_failure_retains_owner_and_blocks_restart(self):
        errors = []
        controller = AppController(AppSettings(), error_handler=errors.append)
        controller.tts = backend = Mock()
        controller.speech_backend = backend
        primary = OSError("backend shutdown")
        backend.shutdown.side_effect = primary

        controller.shutdown()

        self.assertEqual(errors, [primary])
        self.assertIs(controller.tts, backend)
        self.assertIs(controller.speech_backend, backend)
        self.assertFalse(controller.shutdown_complete.is_set())
        controller.prepare_startup()
        self.assertFalse(controller.start())

    def test_backend_stop_failure_does_not_skip_shutdown_or_replace_fatal_error(self):
        controller = AppController(AppSettings())
        controller.tts = backend = Mock()
        primary = KeyboardInterrupt("backend stop")
        backend.stop.side_effect = primary
        backend.shutdown.side_effect = OSError("backend shutdown")

        with self.assertRaises(KeyboardInterrupt) as caught:
            controller.runtime_lifecycle._stop_backend()

        self.assertIs(caught.exception, primary)
        backend.shutdown.assert_called_once_with()
        self.assertEqual(
            primary.__notes__, ["Speech backend shutdown failed: backend shutdown"]
        )
        self.assertIs(controller.tts, backend)

    def test_failed_backend_release_during_startup_cannot_be_replaced(self):
        for fatal in (False, True):
            with self.subTest(fatal=fatal):
                errors = []
                controller = AppController(AppSettings(), error_handler=errors.append)
                controller.tts = backend = Mock()
                primary = (
                    KeyboardInterrupt("shutdown") if fatal else OSError("shutdown")
                )
                backend.shutdown.side_effect = primary
                if fatal:
                    with self.assertRaises(KeyboardInterrupt):
                        controller.runtime_lifecycle._stop_backend()
                else:
                    self.assertFalse(controller.runtime_lifecycle._stop_backend())
                    self.assertEqual(errors, [primary])
                controller.prepare_startup()
                self.assertTrue(controller.shutdown_requested.is_set())
                self.assertFalse(controller.start())
                self.assertIs(controller.tts, backend)
