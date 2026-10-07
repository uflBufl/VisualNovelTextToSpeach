"""Shutdown completion requires released owners, not just attempted cleanup."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from unittest.mock import Mock, patch

from vntts.controller import AppController, create_dialog_read_scheduler
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

    def test_failed_cancellation_attempts_remaining_owners_and_retains_primary(self):
        for primary in (RuntimeError("prime"), SystemExit("prime")):
            with self.subTest(primary=type(primary).__name__):
                controller = AppController(AppSettings())
                controller.tts = controller.speech_backend = backend = Mock()
                controller.live_reader = reader = Mock()
                controller.schedule_dialog_read = schedule = Mock()
                controller.speech_executor = executor = Mock()
                failed, other = Mock(), Mock()
                failed.cancel.side_effect = primary
                controller.voice_prime_futures = {failed, other}
                schedule.cancel.side_effect = OSError("schedule")
                backend.set_live_mode_active.side_effect = OSError("mode")

                with self.assertRaises(type(primary)) as caught:
                    controller.shutdown()

                self.assertIs(caught.exception, primary)
                self.assertTrue(controller.shutdown_complete.wait(1))
                self.assertEqual(
                    primary.__notes__,
                    [
                        "Scheduled read cancellation failed: schedule",
                        "Backend live mode failed: mode",
                    ],
                )
                other.cancel.assert_called_once_with()
                reader.shutdown.assert_called_once_with()
                reader.wait.assert_called_once_with(timeout_seconds=5.0)
                self.assertEqual(executor.shutdown.call_count, 2)
                backend.shutdown.assert_called_once_with()

    def test_uncertain_reader_stop_retains_backend_after_real_worker_drain(self):
        started, release, drained, closed = (Event() for _ in range(4))
        controller = AppController(AppSettings(), error_handler=Mock())
        controller.tts = backend = Mock()
        backend.shutdown.side_effect = closed.set
        controller.live_reader = reader = Mock()
        reader.shutdown_started = False
        reader.stop_event = Event()
        primary = OSError("reader stop before signal")
        reader.shutdown.side_effect = primary
        reader.wait.side_effect = TimeoutError("reader pending")
        worker = ThreadPoolExecutor(max_workers=1)
        controller.speech_executor = worker
        worker.submit(
            lambda: (
                started.set(),
                release.wait(),
                backend.prepare_playback("Ada", "line"),
            )
        )

        def thread_factory(*, target, **options):
            def finish():
                try:
                    target()
                finally:
                    drained.set()

            return Thread(target=finish, **options)

        try:
            self.assertTrue(started.wait(1))
            with patch(
                "vntts.controller_components.Thread", side_effect=thread_factory
            ):
                with self.assertRaises(OSError) as caught:
                    controller.shutdown()
                self.assertIs(caught.exception, primary)
                self.assertFalse(closed.is_set())
                controller.prepare_startup()
                self.assertFalse(controller.start())
                release.set()
                self.assertTrue(drained.wait(1))
            self.assertIs(controller.live_reader, reader)
            self.assertIs(controller.tts, backend)
            self.assertFalse(controller.shutdown_complete.is_set())
            self.assertFalse(closed.is_set())
        finally:
            release.set()
            worker.shutdown(wait=True)

    def test_reader_error_handler_failure_does_not_skip_executor_drain(self):
        primary = KeyboardInterrupt("error reporting")
        controller = AppController(
            AppSettings(), error_handler=Mock(side_effect=primary)
        )
        controller.live_reader = reader = Mock()
        reader.wait.side_effect = TimeoutError("reader pending")
        controller.speech_executor = executor = Mock()
        controller.tts = backend = Mock()

        with self.assertRaises(KeyboardInterrupt) as caught:
            controller.shutdown()

        self.assertIs(caught.exception, primary)
        self.assertTrue(controller.shutdown_complete.wait(1))
        self.assertEqual(executor.shutdown.call_count, 2)
        backend.shutdown.assert_called_once_with()

    def test_shutdown_blocks_late_controller_delivery_and_voice_prime(self):
        controller = AppController(AppSettings())
        controller.speech_backend = backend = Mock()
        controller.speech_executor = executor = Mock()
        controller.history = history = Mock()
        controller._canonical_observed_character = canonical = Mock()
        controller.request_shutdown()

        self.assertFalse(controller._enqueue_dialog("Ada", "line"))
        self.assertFalse(controller._dialog_observed(None, ""))
        self.assertFalse(controller._prime_observed_voice("Narrator"))

        canonical.assert_not_called()
        history.finish_current.assert_not_called()
        executor.submit.assert_not_called()
        backend.prime.assert_not_called()

    def test_scheduler_rejects_late_delivery_and_read_submission_after_shutdown(self):
        requested = Event()
        executor, deliver = Mock(), Mock()
        schedule = create_dialog_read_scheduler(
            executor,
            Mock(),
            ".",
            speech_handler=deliver,
            shutdown_requested=requested,
        )
        self.assertTrue(schedule())
        pending_delivery = executor.submit.call_args.kwargs["speech_handler"]

        requested.set()

        self.assertFalse(pending_delivery("Ada", "line"))
        self.assertFalse(schedule())
        self.assertEqual(executor.submit.call_count, 1)
        deliver.assert_not_called()
