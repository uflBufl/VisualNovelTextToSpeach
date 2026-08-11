import unittest
from concurrent.futures import Future
from threading import Event, Thread
from unittest.mock import Mock

from vntts.live import (
    AdaptiveCapturePolicy,
    AdaptiveSpeechBackpressure,
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


class AdaptiveSpeechBackpressureTest(unittest.TestCase):
    def test_underflow_serializes_until_a_clean_cooldown_has_elapsed(self):
        clock = FakeClock()
        policy = AdaptiveSpeechBackpressure(
            normal_jobs=2,
            cooldown_seconds=10,
            clock=clock,
        )

        self.assertEqual(
            policy.observe_playback(underflowed=True),
            (1, True),
        )
        clock.advance(9)
        self.assertEqual(
            policy.observe_playback(underflowed=False),
            (1, False),
        )
        clock.advance(1)
        self.assertEqual(
            policy.observe_playback(underflowed=False),
            (2, True),
        )

    def test_another_underflow_restarts_the_cooldown(self):
        clock = FakeClock()
        policy = AdaptiveSpeechBackpressure(cooldown_seconds=10, clock=clock)

        policy.observe_playback(underflowed=True)
        clock.advance(9)
        policy.observe_playback(underflowed=True)
        clock.advance(2)

        self.assertEqual(
            policy.observe_playback(underflowed=False),
            (1, False),
        )

    def test_serial_backend_never_reports_a_backpressure_transition(self):
        policy = AdaptiveSpeechBackpressure(normal_jobs=1)

        self.assertEqual(
            policy.observe_playback(underflowed=True),
            (1, False),
        )


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
        tracker = self.create_tracker(
            min_chunk_characters=10,
            complete_sentences_only=False,
        )

        tracker.observe("Alice", "Wait,")
        self.assertEqual(tracker.observe("Alice", "Wait,"), [])
        tracker.observe("Alice", "Wait, this is long enough,")

        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough, next"),
            [SpeechChunk(1, "Alice", "Wait, this is long enough,")],
        )

    def test_live_quality_mode_waits_instead_of_speaking_clause_fragments(self):
        tracker = self.create_tracker(
            min_chunk_characters=10,
            idle_flush_seconds=0.7,
        )

        tracker.observe("Alice", "Wait, this is long enough,")
        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough,"),
            [],
        )
        self.clock.advance(0.8)

        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough,"),
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

    def test_visible_sentences_are_prepared_as_separate_chunks(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "First sentence. Second sentence.")

        self.assertEqual(
            tracker.observe("Alice", "First sentence. Second sentence."),
            [
                SpeechChunk(1, "Alice", "First sentence."),
                SpeechChunk(1, "Alice", "Second sentence."),
            ],
        )

    def test_noise_only_text_is_committed_without_being_spoken(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "...")
        tracker.observe("Alice", "...")

        self.assertEqual(tracker.flush(), [])
        self.assertEqual(tracker.committed_position, 3)

    def test_idle_complete_requires_all_visible_text_to_be_committed(self):
        tracker = self.create_tracker(idle_flush_seconds=0.7)
        tracker.observe("Alice", "First sentence. Second")
        tracker.observe("Alice", "First sentence. Second")
        self.assertFalse(tracker.is_idle_complete())

        self.clock.advance(0.8)
        tracker.observe("Alice", "First sentence. Second")

        self.assertTrue(tracker.is_idle_complete())


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

    def test_prepared_speech_prefetches_then_plays_on_separate_executor(self):
        prepare_chunk = Mock(return_value="prepared audio")
        play_prepared = Mock()
        reader = self.create_reader(
            playback_executor=ImmediateExecutor(),
            prepare_chunk=prepare_chunk,
            play_prepared=play_prepared,
        )
        chunk = SpeechChunk(1, "Alice", "Two-stage speech.")
        reader.active_generation = 1

        reader._schedule([chunk])

        prepare_chunk.assert_called_once_with(chunk)
        play_prepared.assert_called_once_with(chunk, "prepared audio")
        self.assertEqual(reader.last_spoken_chunk, chunk)
        metrics = reader.get_pipeline_metrics()
        self.assertIsNotNone(metrics.last_generation_started_at)
        self.assertIsNotNone(metrics.last_first_pcm_at)
        self.assertIsNotNone(metrics.last_playback_started_at)
        self.assertIsNotNone(metrics.last_playback_completed_at)

    def test_streaming_backend_records_first_pcm_from_backend_callback(self):
        reader = self.create_reader(first_pcm_on_prepare=False)

        reader.record_first_pcm(123.5)

        self.assertEqual(reader.get_pipeline_metrics().last_first_pcm_at, 123.5)

    def test_observation_and_stable_sentence_record_pipeline_timestamps(self):
        reader = self.create_reader()

        reader._report_observation("Alice", "Visible text")
        with reader.state_lock:
            reader._record_speech_metrics_locked(sentence_ready=True)
        metrics = reader.get_pipeline_metrics()

        self.assertIsNotNone(metrics.last_text_visible_at)
        self.assertIsNotNone(metrics.last_speaker_resolved_at)
        self.assertIsNotNone(metrics.last_ocr_stable_at)

    def test_auto_advance_runs_once_for_ready_focused_generation(self):
        auto_advance = Mock(return_value=True)
        reader = self.create_reader(auto_advance=auto_advance)
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        reader._run_auto_advance(3)
        reader._run_auto_advance(3)

        auto_advance.assert_called_once_with()
        self.assertEqual(reader.advanced_generation, 3)

    def test_new_dialog_allows_active_playback_to_finish_cleanly(self):
        interrupt_speech = Mock(return_value=True)
        reader = self.create_reader(interrupt_speech=interrupt_speech)
        reader.active_generation = 1
        reader.current_chunk = SpeechChunk(1, "Alice", "Old text.")

        reader._set_generation(2)

        interrupt_speech.assert_not_called()
        self.assertEqual(reader.active_generation, 2)

    def test_new_dialog_interrupts_backend_that_supports_safe_replacement(self):
        interrupt_speech = Mock(return_value=True)
        reader = self.create_reader(
            interrupt_speech=interrupt_speech,
            interrupt_on_dialog_replacement=True,
        )
        reader.active_generation = 1
        old_chunk = SpeechChunk(1, "Alice", "Old text.")
        reader.current_chunk = old_chunk

        reader._set_generation(2)

        interrupt_speech.assert_called_once_with()
        self.assertFalse(reader.wait_until_playable(old_chunk))

    def test_new_dialog_cancels_stale_queued_speech(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        reader = self.create_reader(speech_executor=speech_executor)
        stale_future = Future()
        reader.active_generation = 1
        reader.speech_futures[stale_future] = SpeechChunk(1, "Alice", "Old text.")

        reader._set_generation(2)

        self.assertTrue(stale_future.cancelled())

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

    def test_emergency_stop_blocks_pending_ocr_until_explicit_resume(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        interrupt_speech = Mock()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=interrupt_speech,
        )
        reader.active_generation = 4
        reader.current_chunk = SpeechChunk(4, "Alice", "Current text.")

        self.assertTrue(reader.emergency_stop())
        reader._schedule([SpeechChunk(5, "Bob", "Pending OCR result.")])

        interrupt_speech.assert_called_once_with()
        speech_executor.submit.assert_not_called()
        self.assertTrue(reader.resume_after_emergency())
        reader._schedule([SpeechChunk(5, "Bob", "Explicit new read.")])
        speech_executor.submit.assert_called_once()

    def test_speech_queue_is_bounded_and_merges_overflow_text(self):
        futures = [Future(), Future(), Future()]
        speech_executor = Mock()
        speech_executor.submit.side_effect = futures
        reader = self.create_reader(
            speech_executor=speech_executor,
            max_speech_jobs=2,
        )
        reader.active_generation = 1
        chunks = [
            SpeechChunk(1, "Alice", "One."),
            SpeechChunk(1, "Alice", "Two."),
            SpeechChunk(1, "Alice", "Three."),
            SpeechChunk(1, "Alice", "Four."),
        ]

        reader._schedule(chunks)

        self.assertEqual(speech_executor.submit.call_count, 2)
        self.assertEqual(reader.deferred_chunk.text, "Three. Four.")
        self.assertEqual(reader.get_pipeline_metrics().speech_queue_depth, 3)

        reader._speech_finished(futures[0])

        self.assertEqual(speech_executor.submit.call_count, 3)
        self.assertIsNone(reader.deferred_chunk)
        self.assertEqual(
            speech_executor.submit.call_args.args[1].text,
            "Three. Four.",
        )

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

    def test_clearing_dialog_reports_session_boundary_once(self):
        dialog_observed = Mock()
        reader = self.create_reader(dialog_observed=dialog_observed)
        reader._report_observation("Alice", "Hello")

        reader._report_observation(None, "")
        reader._report_observation(None, "")

        self.assertEqual(
            dialog_observed.call_args_list,
            [
                unittest.mock.call("Alice", "Hello"),
                unittest.mock.call("Narrator", ""),
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

    def test_capture_continues_while_speech_is_active(self):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        reader = self.create_reader(interval_seconds=0.2)
        reader.current_chunk = SpeechChunk(1, "Alice", "Speaking now.")

        reader._run(stop_event)

        reader.read_snapshot.assert_called_once_with()
        stop_event.wait.assert_called_once_with(0.1)

    def test_split_capture_keeps_only_the_latest_unprocessed_frame(self):
        stop_event = Event()
        frames = []

        def capture_frame():
            frames.append(len(frames) + 1)
            if len(frames) == 3:
                stop_event.set()
            return frames[-1]

        reader = self.create_reader(
            ocr_executor=Mock(),
            capture_frame=capture_frame,
            recognize_frame=Mock(),
            frame_fingerprint=lambda frame: frame,
        )

        reader._run_capture(stop_event)

        metrics = reader.get_pipeline_metrics()
        self.assertEqual(metrics.captured_frames, 3)
        self.assertEqual(metrics.replaced_frames, 2)
        self.assertEqual(reader.latest_frame, 3)

    def test_split_ocr_reuses_unchanged_frame_until_auto_advance(self):
        stop_event = Event()
        first_observed = Event()
        second_observed = Event()

        class Tracker:
            generation = 1

            def __init__(self):
                self.observations = 0

            def observe(self, _character, _text):
                self.observations += 1
                if self.observations == 1:
                    first_observed.set()
                elif self.observations == 2:
                    second_observed.set()
                else:
                    stop_event.set()
                return []

            def flush(self):
                return []

            def is_idle_complete(self):
                return False

        recognize_frame = Mock(return_value=("Alice", "Unchanged text."))
        reader = self.create_reader(
            ocr_executor=Mock(),
            capture_frame=Mock(),
            recognize_frame=recognize_frame,
            frame_fingerprint=Mock(),
            tracker_factory=Tracker,
        )
        worker = Thread(target=reader._run_ocr, args=(stop_event,))
        worker.start()
        with reader.pause_condition:
            reader.latest_frame = "frame one"
            reader.latest_frame_fingerprint = "same text"
            reader.frame_version = 1
            reader.pause_condition.notify_all()
        self.assertTrue(first_observed.wait(timeout=1))
        with reader.pause_condition:
            reader.latest_frame = "frame two with animated background"
            reader.latest_frame_fingerprint = "same text"
            reader.frame_version = 2
            reader.pause_condition.notify_all()
        self.assertTrue(second_observed.wait(timeout=1))
        with reader.pause_condition:
            reader.advanced_generation = reader.active_generation
            reader.latest_frame = "first frame after auto advance"
            reader.latest_frame_fingerprint = "same text"
            reader.frame_version = 3
            reader.pause_condition.notify_all()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            recognize_frame.call_args_list,
            [
                unittest.mock.call("frame one"),
                unittest.mock.call("first frame after auto advance"),
            ],
        )
        metrics = reader.get_pipeline_metrics()
        self.assertEqual(metrics.recognized_frames, 2)
        self.assertEqual(metrics.reused_frames, 1)


if __name__ == "__main__":
    unittest.main()
