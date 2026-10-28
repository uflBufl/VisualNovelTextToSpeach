import unittest
from threading import Event, Thread
from unittest.mock import Mock

from tests import test_live as live_tests
from vntts.live_tracking import IncrementalDialogTracker, SpeechChunk


class LiveQueueTransitionsTest(unittest.TestCase):
    def create_reader(self, **options):
        self.speech = live_tests.QueuedExecutor()
        self.playback = live_tests.QueuedExecutor()
        self.played = []
        return live_tests.LiveDialogReaderTest().create_reader(
            speech_executor=self.speech,
            playback_executor=self.playback,
            play_prepared=options.pop(
                "play_prepared", lambda chunk, _: self.played.append(chunk.text)
            ),
            **options,
        )

    def drain(self):
        for _ in range(30):
            if self.speech.jobs:
                self.speech.run_next()
            elif self.playback.jobs:
                self.playback.run_next()
            else:
                return
        self.fail("Queue did not drain")

    def test_clear_during_admission_rolls_back_old_acceptance(self):
        rejected = Mock()
        reader = None

        def accept(chunk):
            if chunk.text == "Old.":
                reader.clear_queue()
                reader.enqueue("Ada", "Fresh.")
            return chunk

        reader = self.create_reader(accept_chunk=accept, reject_chunk=rejected)
        reader.enqueue("Ada", "Old.")
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertEqual(rejected.call_args.args[0].text, "Old.")

    def test_repeated_scheduling_of_same_playback_identity_is_idempotent(self):
        reader = self.create_reader()
        chunk = SpeechChunk(1, "Ada", "Once.")
        reader._set_generation(1)
        reader._schedule([chunk, chunk])
        self.drain()
        reader._schedule([chunk])
        self.drain()

        self.assertEqual(self.played, ["Once."])

    def test_tracked_acceptance_callback_runs_without_reader_lock(self):
        lock_states = []
        reader = self.create_reader(
            accept_chunk=lambda chunk: (
                lock_states.append(reader.state_lock._is_owned()) or chunk
            )
        )
        tracker = IncrementalDialogTracker()
        chunks = tracker.observe_canonical("Ada", "Accepted.", "A")

        reader._schedule_tracked_chunks(tracker, chunks)
        self.drain()

        self.assertEqual(lock_states, [False])
        self.assertEqual(self.played, ["Accepted."])

    def test_prepared_first_line_survives_pause_ahead_of_second_preparation(self):
        reader = self.create_reader()
        reader._set_generation(1)
        reader._schedule([SpeechChunk(1, "Ada", "First.", ordinal=1)])
        reader._set_generation(2)
        reader._schedule([SpeechChunk(2, "Ada", "Second.", ordinal=1)])
        self.speech.run_next()

        reader.toggle_pause()
        reader.toggle_pause()
        self.drain()

        self.assertEqual(self.played, ["First.", "Second."])

    def test_distinct_explicit_repeats_are_preserved_while_paused(self):
        reader = self.create_reader()
        reader.enqueue("Ada", "Again.")
        self.drain()
        reader.toggle_pause()

        self.assertTrue(reader.repeat_last())
        self.assertTrue(reader.repeat_last())
        reader.toggle_pause()
        self.drain()

        self.assertEqual(self.played, ["Again.", "Again.", "Again."])

    def test_interrupted_first_line_retries_before_prepared_second_line(self):
        started, interrupted = Event(), Event()
        starts = []

        def play(chunk, _prepared):
            starts.append(chunk.text)
            if len(starts) == 1:
                started.set()
                if not interrupted.wait(2):
                    raise AssertionError("Pause did not interrupt playback")

        reader = self.create_reader(
            play_prepared=play,
            interrupt_speech=lambda: interrupted.set() or True,
        )
        reader.enqueue("Ada", "First.")
        reader.enqueue("Ada", "Second.")
        self.speech.run_next()
        self.speech.run_next()
        worker = Thread(target=self.playback.run_next)
        worker.start()
        self.assertTrue(started.wait(2))
        reader.toggle_pause()
        worker.join(2)
        self.assertFalse(worker.is_alive())

        reader.toggle_pause()
        self.drain()

        self.assertEqual(starts, ["First.", "First.", "Second."])

    def test_running_preparation_finishes_during_pause_without_losing_fifo(self):
        started, finish = Event(), Event()

        def prepare(chunk):
            if chunk.text == "First.":
                started.set()
                if not finish.wait(2):
                    raise AssertionError("Preparation was not released")
            return chunk.text

        reader = self.create_reader(prepare_chunk=prepare)
        reader.enqueue("Ada", "First.")
        reader.enqueue("Ada", "Second.")
        worker = Thread(target=self.speech.run_next)
        worker.start()
        self.assertTrue(started.wait(2))
        reader.toggle_pause()
        finish.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.playback.jobs, [])

        reader.toggle_pause()
        self.drain()

        self.assertEqual(self.played, ["First.", "Second."])

    def test_cancelled_buffered_preparation_releases_capacity_for_next_line(self):
        reader = self.create_reader(max_speech_jobs=1)
        reader.toggle_pause()
        reader.enqueue("Ada", "Old.")
        reader.toggle_pause()
        # Pause from preparation so its completed result is buffered.
        reader.prepare_chunk = lambda chunk: reader.toggle_pause() or chunk
        self.speech.run_next()
        old = next(iter(reader.accepted_chunks.values()))
        reader.cancelled_chunk_ids.add(old.playback_id)
        reader.prepare_chunk = lambda chunk: chunk
        reader.enqueue("Ada", "Fresh.")
        reader.toggle_pause()
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertFalse(reader.speech_futures)

    def test_clear_releases_buffered_preparation_capacity(self):
        reader = self.create_reader()
        reader.enqueue("Ada", "First.")
        reader.enqueue("Ada", "Second.")
        self.speech.run_next()
        self.speech.run_next()

        reader.clear_queue()
        reader.enqueue("Ada", "Fresh.")
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertFalse(reader.speech_futures)
        self.assertFalse(reader.prepared_results)

    def test_cancelled_playback_identity_cannot_be_resurrected_after_callback(self):
        reader = self.create_reader()
        chunk = SpeechChunk(1, "Ada", "Cancelled.")
        reader._set_generation(1)
        reader._schedule([chunk])
        self.speech.run_next()
        reader.clear_queue()
        self.drain()
        reader._set_generation(2)
        reader._schedule([chunk])
        self.drain()

        self.assertEqual(self.played, [])
        self.assertIn(chunk.playback_id, reader.cancelled_chunk_ids)

    def test_late_completed_playback_callback_cannot_replay_current_generation(self):
        reader = self.create_reader()
        reader.enqueue("Ada", "Once.")
        self.drain()
        chunk = next(iter(reader.accepted_chunks.values()))

        reader._play_if_current(chunk, "late prepared audio")

        self.assertEqual(self.played, ["Once."])

    def test_rejected_preparation_submission_releases_next_accepted_line(self):
        rejected = Mock()
        reader = self.create_reader(reject_chunk=rejected)
        submit = self.speech.submit
        failure = RuntimeError("Synthesis executor rejected job")
        attempts = []

        def submit_after_failure(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise failure
            return submit(*args)

        self.speech.submit = submit_after_failure
        reader.enqueue("Ada", "Rejected.")
        reader.enqueue("Ada", "Fresh.")
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertEqual(rejected.call_args.args[0].text, "Rejected.")
        self.assertFalse(reader.speech_futures)

    def test_rejected_playback_submission_releases_prepared_successor(self):
        rejected = Mock()
        reader = self.create_reader(reject_chunk=rejected)
        submit = self.playback.submit
        attempts = []

        def submit_after_failure(*args):
            attempts.append(args)
            if len(attempts) == 1:
                raise RuntimeError("Playback executor rejected job")
            return submit(*args)

        self.playback.submit = submit_after_failure
        reader.enqueue("Ada", "Rejected.")
        reader.enqueue("Ada", "Fresh.")
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertEqual(rejected.call_args.args[0].text, "Rejected.")
        self.assertFalse(reader.speech_futures)

    def test_new_enqueue_during_acceptance_rejects_unaccepted_old_candidate(self):
        reader = None
        rejected = Mock()

        def accept(chunk):
            if chunk.text == "Old.":
                reader.enqueue("Ada", "Fresh.")
            return chunk

        reader = self.create_reader(accept_chunk=accept, reject_chunk=rejected)
        reader.enqueue("Ada", "Old.")
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertEqual(rejected.call_args.args[0].text, "Old.")

    def test_seal_during_acceptance_does_not_leave_ghost_accepted_head(self):
        reader = None

        def accept(chunk):
            if chunk.text == "Old.":
                reader.seal_generation(chunk.generation)
            return chunk

        reader = self.create_reader(accept_chunk=accept)
        reader.enqueue("Ada", "Old.")
        reader.enqueue("Ada", "Fresh.")
        self.drain()

        self.assertEqual(self.played, ["Fresh."])
        self.assertFalse(reader.speech_futures)

    def test_sealing_deferred_suffix_cannot_strand_next_accepted_line(self):
        reader = self.create_reader(max_speech_jobs=1)

        def play(chunk, _prepared):
            self.played.append(chunk.text)
            if chunk.text == "First.":
                reader.seal_generation(chunk.generation)

        reader.play_prepared = play
        reader._set_generation(1)
        reader._schedule([SpeechChunk(1, "Ada", "First.", ordinal=1)])
        suffix = SpeechChunk(1, "Ada", "OCR suffix.", ordinal=2)
        reader._schedule([suffix])
        reader.enqueue("Ada", "Next.")
        self.drain()

        self.assertEqual(self.played, ["First.", "Next."])
        self.assertIn(suffix.playback_id, reader.completed_playback_ids)
        self.assertFalse(reader.speech_futures)

    def test_sealing_paused_suffix_cannot_strand_next_accepted_line(self):
        reader = self.create_reader()
        reader.toggle_pause()
        reader._set_generation(1)
        suffix = SpeechChunk(1, "Ada", "OCR suffix.", ordinal=2)
        reader._schedule([suffix])
        reader.enqueue("Ada", "Next.")

        reader.seal_generation(1)
        reader.toggle_pause()
        self.drain()

        self.assertEqual(self.played, ["Next."])
        self.assertIn(suffix.playback_id, reader.completed_playback_ids)

    def test_sealing_preserves_already_accepted_explicit_repeat(self):
        reader = self.create_reader()
        reader.toggle_pause()
        reader._set_generation(1)
        reader._schedule([SpeechChunk(1, "Ada", "Repeat.", explicit_replay=True)])

        reader.seal_generation(1)
        reader.toggle_pause()
        self.drain()

        self.assertEqual(self.played, ["Repeat."])

    def test_all_accepted_lines_survive_capacity_pressure_in_order(self):
        reader = self.create_reader(max_speech_jobs=2)
        for text in ("First.", "Second.", "Third.", "Fourth."):
            reader.enqueue("Ada", text)
        self.drain()

        self.assertEqual(self.played, ["First.", "Second.", "Third.", "Fourth."])
        self.assertFalse(reader.deferred_chunks)
        self.assertFalse(reader.speech_futures)


if __name__ == "__main__":
    unittest.main()
