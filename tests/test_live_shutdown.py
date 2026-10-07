"""Real reader workers must wake even when cancellation callbacks fail."""

import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

from vntts.live import LiveDialogReader, SpeechChunk


def create_reader(executor=None, **options):
    arguments = {
        "capture_executor": executor or Mock(),
        "ocr_executor": executor or Mock(),
        "speech_executor": executor or Mock(),
        "playback_executor": executor or Mock(),
        "capture_frame": lambda: object(),
        "recognize_frame": lambda frame: (None, ""),
        "prepare_chunk": lambda chunk: chunk,
        "play_prepared": Mock(),
        "report_error": Mock(),
        "interval_seconds": 0.001,
    }
    arguments.update(options)
    return LiveDialogReader(**arguments)


class LiveShutdownTest(unittest.TestCase):
    def test_emergency_timer_failure_wakes_real_capture_ocr_and_paused_speech(self):
        captured, release_capture, waiting = (Event() for _ in range(3))
        primary = OSError("timer cancellation")
        interrupt, advance = Mock(), Mock()

        def capture():
            captured.set()
            release_capture.wait()
            return object()

        with ThreadPoolExecutor(max_workers=3) as executor:
            reader = create_reader(
                executor,
                capture_frame=capture,
                interrupt_speech=interrupt,
                auto_advance=advance,
            )
            try:
                reader.start()
                self.assertTrue(captured.wait(1))
                with reader.pause_condition:
                    reader.paused = True
                    reader.active_generation = 3
                    reader.current_chunk = chunk = SpeechChunk(3, "Ada", "line")
                    reader.auto_advance_timer = Mock()
                    reader.auto_advance_timer.cancel.side_effect = primary
                    serial = reader._auto_advance_timer_serial

                def paused_playback():
                    with reader.pause_condition:
                        waiting.set()
                        return reader.wait_until_playable(chunk)

                playback = executor.submit(paused_playback)
                self.assertTrue(waiting.wait(1))
                with reader.pause_condition:
                    reader.speech_futures[playback] = chunk
                    playback.add_done_callback(reader._speech_finished)
                with self.assertRaises(OSError) as caught:
                    reader.emergency_stop()
                self.assertIs(caught.exception, primary)
                self.assertFalse(playback.result(timeout=1))
                self.assertFalse(reader.paused)
                self.assertTrue(reader.stop_event.is_set())
                interrupt.assert_called_once_with()
                reader._run_auto_advance(3, serial)
                advance.assert_not_called()
                release_capture.set()
                reader.wait(timeout_seconds=1)
            finally:
                release_capture.set()
                reader.stop_event.set()
                reader.release_waiters()

    def test_queue_cleanup_preserves_timer_error_and_attempts_all_cancellations(self):
        for primary in (OSError("timer cancellation"), KeyboardInterrupt("timer")):
            with self.subTest(primary=type(primary).__name__):
                interrupt = Mock(side_effect=KeyboardInterrupt("speech interruption"))
                reader = create_reader(interrupt_speech=interrupt)
                reader.auto_advance_timer = Mock()
                reader.auto_advance_timer.cancel.side_effect = primary
                first, second = Future(), Future()
                first.cancel = Mock(side_effect=RuntimeError("first future"))
                chunk = SpeechChunk(3, "Ada", "line")
                reader.speech_futures = {first: chunk, second: chunk}

                with self.assertRaises(type(primary)) as caught:
                    reader.emergency_stop()

                self.assertIs(caught.exception, primary)
                self.assertEqual(
                    primary.__notes__,
                    [
                        "Queued speech cancellation failed: first future",
                        "Speech interruption failed: speech interruption",
                    ],
                )
                self.assertTrue(second.cancelled())
                self.assertIsNone(reader.auto_advance_timer)
                self.assertTrue(reader.emergency_stopped)
                self.assertFalse(reader.paused)
                interrupt.assert_called_once_with()

    def test_graceful_timer_failure_still_wakes_real_paused_waiter(self):
        waiting = Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            reader = create_reader()
            chunk = SpeechChunk(0, "Ada", "line")
            reader.paused = True
            reader.auto_advance_timer = Mock()
            reader.auto_advance_timer.cancel.side_effect = OSError("timer cancellation")

            def paused_playback():
                with reader.pause_condition:
                    waiting.set()
                    return reader.wait_until_playable(chunk)

            reader.ocr_future = executor.submit(paused_playback)
            try:
                self.assertTrue(waiting.wait(1))
                with self.assertRaisesRegex(OSError, "timer cancellation"):
                    reader.stop()
                self.assertTrue(reader.ocr_future.result(timeout=1))
                self.assertTrue(reader.stop_event.is_set())
            finally:
                reader.release_waiters()

    def test_terminal_shutdown_cannot_reopen_reader_after_shared_event_is_cleared(self):
        requested = Event()
        reader = create_reader(shutdown_requested=requested)
        chunk = SpeechChunk(0, "Ada", "line")
        reader.current_chunk = chunk
        reader.shutdown()
        requested.clear()

        self.assertTrue(reader.is_shutting_down)
        self.assertFalse(reader.start())
        self.assertFalse(reader.resume_after_emergency())
        self.assertFalse(reader.enqueue("Ada", "new line"))
        self.assertFalse(reader.wait_until_playable(chunk))
        reader._schedule([chunk])
        reader.capture_executor.submit.assert_not_called()
        reader.speech_executor.submit.assert_not_called()

    def test_shutdown_requested_between_start_checks_cannot_submit_workers(self):
        requested = Event()
        reader = create_reader(shutdown_requested=requested)
        reader.clear_queue = Mock(side_effect=requested.set)

        self.assertFalse(reader.start())

        reader.capture_executor.submit.assert_not_called()
        reader.ocr_executor.submit.assert_not_called()

    def test_late_generation_change_cannot_reenable_emergency_playback(self):
        reader = create_reader()
        reader.current_chunk = chunk = SpeechChunk(3, "Ada", "line")
        reader.active_generation = 3
        reader.emergency_stop()

        reader._set_generation(4)

        self.assertFalse(reader.wait_until_playable(chunk))
