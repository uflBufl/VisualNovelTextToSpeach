import unittest
from concurrent.futures import Future
from threading import Event
from unittest.mock import Mock

from vntts.live import (
    AdaptiveCapturePolicy,
    IncrementalDialogTracker,
    LiveDialogReader,
    SpeechChunk,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ImmediateExecutor:
    def submit(self, function, *arguments):
        future = Future()
        try:
            future.set_result(function(*arguments))
        except Exception as error:
            future.set_exception(error)
        return future


class IncrementalDialogTrackerTest(unittest.TestCase):
    def create_tracker(self, **options):
        self.clock = FakeClock()
        return IncrementalDialogTracker(clock=self.clock, **options)

    def test_typewriter_text_is_spoken_after_sentence_becomes_stable(self):
        tracker = self.create_tracker()

        self.assertEqual(tracker.observe("Alice", "Hello"), [])
        self.assertEqual(tracker.observe("Alice", "Hello world."), [])
        self.assertEqual(
            tracker.observe("Alice", "Hello world. How"),
            [SpeechChunk(1, "Alice", "Hello world.")],
        )

    def test_ocr_correction_in_unspoken_text_is_not_repeated(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "The qu1ck brown")
        tracker.observe("Alice", "The quick brown fox")
        self.assertEqual(tracker.observe("Alice", "The quick brown fox."), [])
        self.assertEqual(
            tracker.observe("Alice", "The quick brown fox."),
            [SpeechChunk(1, "Alice", "The quick brown fox.")],
        )

    def test_idle_text_is_flushed_without_sentence_punctuation(self):
        tracker = self.create_tracker(idle_flush_seconds=0.7)

        tracker.observe("Narrator", "A quiet morning")
        tracker.observe("Narrator", "A quiet morning")
        self.clock.advance(0.8)

        self.assertEqual(
            tracker.observe("Narrator", "A quiet morning"),
            [SpeechChunk(1, "Narrator", "A quiet morning")],
        )

    def test_short_clause_waits_but_long_clause_is_emitted(self):
        tracker = self.create_tracker(min_chunk_characters=10)

        tracker.observe("Alice", "Wait,")
        self.assertEqual(tracker.observe("Alice", "Wait,"), [])
        tracker.observe("Alice", "Wait, this is long enough,")

        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough, next"),
            [SpeechChunk(1, "Alice", "Wait, this is long enough,")],
        )

    def test_new_speaker_starts_new_generation(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "Hello.")
        tracker.observe("Alice", "Hello.")
        first_generation = tracker.generation
        self.assertEqual(tracker.observe("Bob", "Goodbye."), [])

        self.assertGreater(tracker.generation, first_generation)
        self.assertEqual(
            tracker.observe("Bob", "Goodbye."),
            [SpeechChunk(tracker.generation, "Bob", "Goodbye.")],
        )

    def test_unrelated_text_from_same_speaker_starts_new_generation(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "The first dialogue is complete.")
        tracker.observe("Alice", "The first dialogue is complete.")
        first_generation = tracker.generation
        tracker.observe("Alice", "Something entirely different appears.")

        self.assertGreater(tracker.generation, first_generation)

    def test_empty_dialog_invalidates_previous_generation(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "Hello")
        first_generation = tracker.generation
        self.assertEqual(tracker.observe("Narrator", ""), [])

        self.assertGreater(tracker.generation, first_generation)
        self.assertEqual(tracker.latest_text, "")

    def test_flush_speaks_remaining_partial_text_once(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "One sentence. Partial ending")
        self.assertEqual(
            tracker.observe("Alice", "One sentence. Partial ending"),
            [SpeechChunk(1, "Alice", "One sentence.")],
        )
        self.assertEqual(
            tracker.flush(),
            [SpeechChunk(1, "Alice", "Partial ending")],
        )
        self.assertEqual(tracker.flush(), [])


class AdaptiveCapturePolicyTest(unittest.TestCase):
    def test_capture_accelerates_while_text_changes_and_slows_when_stable(self):
        policy = AdaptiveCapturePolicy(
            base_interval=0.2,
            fast_interval=0.08,
            idle_interval=0.7,
            unchanged_frames=2,
        )

        self.assertEqual(policy.observe("Alice", "H"), 0.08)
        self.assertEqual(policy.observe("Alice", "Hello"), 0.08)
        self.assertEqual(policy.observe("Alice", "Hello"), 0.2)
        self.assertEqual(policy.observe("Alice", "Hello"), 0.7)

    def test_unfocused_game_uses_slowest_interval(self):
        policy = AdaptiveCapturePolicy(
            base_interval=0.2,
            unfocused_interval=1.8,
        )

        self.assertEqual(policy.observe(None, None, focused=False), 1.8)
        self.assertEqual(policy.observe("Alice", "Welcome", focused=True), 0.1)


class LiveDialogReaderTest(unittest.TestCase):
    def create_reader(self, **overrides):
        options = {
            "capture_executor": Mock(),
            "speech_executor": ImmediateExecutor(),
            "read_snapshot": Mock(return_value=("Alice", "Hello.")),
            "speak_chunk": Mock(),
            "report_error": Mock(),
            "interval_seconds": 0.001,
        }
        options.update(overrides)
        return LiveDialogReader(**options)

    def test_capture_loop_speaks_stable_text(self):
        stop_event = Event()
        snapshots = iter([("Alice", "Hello."), ("Alice", "Hello.")])

        def read_snapshot():
            snapshot = next(snapshots)
            if snapshot == ("Alice", "Hello.") and read_snapshot.calls == 1:
                stop_event.set()
            read_snapshot.calls += 1
            return snapshot

        read_snapshot.calls = 0
        speak_chunk = Mock()
        reader = self.create_reader(
            read_snapshot=read_snapshot,
            speak_chunk=speak_chunk,
        )

        reader._run(stop_event)

        speak_chunk.assert_called_once_with(SpeechChunk(1, "Alice", "Hello."))

    def test_stale_generation_is_not_spoken(self):
        speak_chunk = Mock()
        reader = self.create_reader(speak_chunk=speak_chunk)
        reader.active_generation = 2

        reader._speak_if_current(SpeechChunk(1, "Alice", "Old text."))

        speak_chunk.assert_not_called()

    def test_speech_failure_is_reported_without_escaping_worker(self):
        report_error = Mock()
        reader = self.create_reader(
            speak_chunk=Mock(side_effect=RuntimeError("audio failed")),
            report_error=report_error,
        )
        reader.active_generation = 1

        reader._speak_if_current(SpeechChunk(1, "Alice", "Hello."))

        report_error.assert_called_once()
        self.assertEqual(str(report_error.call_args.args[0]), "audio failed")

    def test_new_dialog_interrupts_speech_from_previous_generation(self):
        interrupt_speech = Mock(return_value=True)
        reader = self.create_reader(interrupt_speech=interrupt_speech)
        reader.active_generation = 1
        reader.current_chunk = SpeechChunk(1, "Alice", "Old text.")

        reader._set_generation(2)

        interrupt_speech.assert_called_once_with()
        self.assertEqual(reader.active_generation, 2)

    def test_pause_toggle_blocks_and_releases_the_queue(self):
        reader = self.create_reader()

        self.assertTrue(reader.toggle_pause())
        self.assertTrue(reader.paused)
        self.assertFalse(reader.toggle_pause())
        self.assertFalse(reader.paused)

    def test_pausing_current_speech_interrupts_and_replays_it_on_resume(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        interrupt_speech = Mock()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=interrupt_speech,
        )
        chunk = SpeechChunk(1, "Alice", "Continue after pause.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.toggle_pause())
        self.assertFalse(reader.toggle_pause())

        interrupt_speech.assert_called_once_with()
        speech_executor.submit.assert_called_once_with(reader._speak_if_current, chunk)

    def test_pausing_during_synthesis_resumes_without_duplicate_replay(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=Mock(return_value=False),
        )
        chunk = SpeechChunk(1, "Alice", "Still synthesizing.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.toggle_pause())
        self.assertFalse(reader.toggle_pause())

        speech_executor.submit.assert_not_called()

    def test_skip_interrupts_only_when_speech_is_active(self):
        interrupt_speech = Mock()
        reader = self.create_reader(interrupt_speech=interrupt_speech)

        self.assertFalse(reader.skip_current())
        reader.current_chunk = SpeechChunk(1, "Alice", "Current text.")
        self.assertTrue(reader.skip_current())

        interrupt_speech.assert_called_once_with()

    def test_skip_marks_synthesizing_chunk_as_unplayable(self):
        reader = self.create_reader(interrupt_speech=Mock(return_value=False))
        chunk = SpeechChunk(1, "Alice", "Do not play this.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.skip_current())

        self.assertFalse(reader.wait_until_playable(chunk))

    def test_repeat_queues_last_spoken_chunk_for_current_dialog(self):
        speak_chunk = Mock()
        reader = self.create_reader(speak_chunk=speak_chunk)
        reader.active_generation = 3
        reader.last_spoken_chunk = SpeechChunk(1, "Alice", "Repeat me.")

        self.assertTrue(reader.repeat_last())

        speak_chunk.assert_called_once_with(SpeechChunk(3, "Alice", "Repeat me."))

    def test_clear_cancels_pending_speech_and_suppresses_current_dialog(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        interrupt_speech = Mock()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=interrupt_speech,
        )
        pending = Future()
        reader.active_generation = 4
        reader.current_chunk = SpeechChunk(4, "Alice", "Current text.")
        reader.speech_futures[pending] = SpeechChunk(4, "Alice", "Pending text.")

        self.assertTrue(reader.clear_queue())
        reader._schedule([SpeechChunk(4, "Alice", "Queued text.")])

        self.assertTrue(pending.cancelled())
        interrupt_speech.assert_called_once_with()
        speech_executor.submit.assert_not_called()

    def test_observed_dialog_is_reported_only_when_it_changes(self):
        dialog_observed = Mock()
        reader = self.create_reader(dialog_observed=dialog_observed)

        reader._report_observation("Alice", "Hello   world")
        reader._report_observation("Alice", "Hello world")
        reader._report_observation("Bob", "Goodbye")

        self.assertEqual(
            dialog_observed.call_args_list,
            [
                unittest.mock.call("Alice", "Hello world"),
                unittest.mock.call("Bob", "Goodbye"),
            ],
        )

    def test_unfocused_game_skips_capture_and_reports_adaptive_interval(self):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        capture_state_changed = Mock()
        reader = self.create_reader(
            focus_probe=Mock(return_value=False),
            capture_state_changed=capture_state_changed,
        )

        reader._run(stop_event)

        reader.read_snapshot.assert_not_called()
        capture_state_changed.assert_called_once_with(False, 0.008)
        stop_event.wait.assert_called_once_with(0.008)


if __name__ == "__main__":
    unittest.main()
