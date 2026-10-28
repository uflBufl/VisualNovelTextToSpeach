import unittest
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from tests.test_live import QueuedExecutor
from vntts.audio_output import pcm_admission_context, resolve_audio_output
from vntts.live import LiveDialogReader, RecognizedDialog
from vntts.live_tracking import IncrementalDialogTracker

OLD = "turer places the manu on the desk."
FULL = "The lecturer places the manuscript on the desk."


class LiveUnheardReplacementTest(unittest.TestCase):
    def candidate(self):
        speech, playback = QueuedExecutor(), QueuedExecutor()
        writes = []
        reader = LiveDialogReader(
            capture_executor=QueuedExecutor(),
            ocr_executor=QueuedExecutor(),
            speech_executor=speech,
            playback_executor=playback,
            capture_frame=lambda: "frame",
            recognize_frame=lambda _: RecognizedDialog("Narrator", FULL, "line"),
            prepare_chunk=lambda chunk: chunk.text,
            play_prepared=Mock(),
            interrupt_speech=Mock(return_value=True),
            report_error=Mock(),
            first_pcm_on_prepare=False,
        )

        def play(chunk, _prepared):
            output = resolve_audio_output(
                SimpleNamespace(
                    play=lambda *_args, **_kwargs: writes.append(chunk.text),
                    wait=lambda: None,
                )
            )
            output.play(np.ones(4), 24000, latency="low")

        reader.play_prepared = play
        tracker = IncrementalDialogTracker(stability_frames=1)
        tracker.observe("Narrator", OLD)
        chunks = tracker.observe("Narrator", OLD)
        self.assertEqual(len(chunks), 1)
        reader._schedule_tracked_chunks(tracker, chunks)
        chunk = speech.jobs[0][2][0]
        self.assertTrue(reader.allow_unheard_replacement(chunk))
        reader.latest_frame = "frame"
        reader.latest_frame_fingerprint = "frame"
        reader.latest_render_fingerprint = "frame"
        reader.latest_frame_visible = True
        reader.frame_version = 1
        authority = reader._recognize_current_frame(
            "frame", "legacy", "frame", True, False, "frame", Mock(), 1, None
        )
        self.assertIsNotNone(authority)
        return reader, tracker, chunk, authority, speech, playback, writes

    def correct(self, reader, tracker, authority):
        return reader._replace_unheard_candidate(tracker, authority, "Narrator", FULL)

    def test_replacement_cancels_unprepared_old_job_and_plays_canonical_once(self):
        reader, tracker, old, authority, speech, playback, writes = self.candidate()
        replacement = self.correct(reader, tracker, authority)
        self.assertEqual(replacement[0].line_id, "line")
        reader._schedule_tracked_chunks(tracker, replacement)
        self.assertTrue(speech.jobs[0][0].cancelled())
        speech.run_next()
        speech.run_next()
        playback.run_next()
        reader._play_if_current(old, OLD)
        self.assertEqual(writes, [FULL])
        self.assertIsNone(self.correct(reader, tracker, authority))
        self.assertEqual(tracker.observe_canonical("Narrator", FULL, "line"), [])
        self.assertEqual(reader.active_generation, tracker.generation)
        reader.report_error.assert_not_called()

    def test_pcm_and_replacement_compete_under_one_lock(self):
        for pcm_wins in (False, True):
            with self.subTest(pcm_wins=pcm_wins):
                reader, tracker, old, authority, *_ = self.candidate()
                reader.current_chunk = old
                entered = Event()
                outcomes = []

                def contender():
                    entered.set()
                    outcomes.append(
                        self.correct(reader, tracker, authority)
                        if pcm_wins
                        else reader.claim_first_pcm(old)
                    )

                with reader.state_lock:
                    thread = Thread(target=contender)
                    thread.start()
                    self.assertTrue(entered.wait(1))
                    if pcm_wins:
                        self.assertTrue(reader.claim_first_pcm(old))
                    else:
                        self.assertIsNotNone(self.correct(reader, tracker, authority))
                thread.join(1)
                self.assertFalse(thread.is_alive())
                if pcm_wins:
                    self.assertEqual(outcomes, [None])
                    self.assertTrue(reader.wait_until_playable(old))
                    self.assertEqual(tracker.committed_text, OLD)
                else:
                    self.assertEqual(outcomes, [False])
                    self.assertFalse(reader.wait_until_playable(old))
                    self.assertEqual(tracker.committed_text, FULL)

    def test_delayed_old_playback_cannot_send_pcm_or_clear_new_generation(self):
        reader, tracker, old, authority, speech, playback, writes = self.candidate()
        entered, release = Event(), Event()
        original_play = reader.play_prepared

        def delayed_play(chunk, prepared):
            if chunk is old:
                entered.set()
                if not release.wait(1):
                    raise AssertionError("Test did not release old playback")
            return original_play(chunk, prepared)

        reader.play_prepared = delayed_play
        thread = Thread(target=reader._play_if_current, args=(old, OLD))
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            replacement = self.correct(reader, tracker, authority)
            self.assertIsNotNone(replacement)
            reader._schedule_tracked_chunks(tracker, replacement)
            new_generation = reader.active_generation
        finally:
            release.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(writes, [])
        self.assertEqual(reader.active_generation, new_generation)
        speech.run_next()
        speech.run_next()
        playback.run_next()
        self.assertEqual(writes, [FULL])
        reader.report_error.assert_not_called()

    def test_announcement_claim_closes_replacement_window_for_dialogue(self):
        reader, tracker, old, authority, *_ = self.candidate()
        results = []

        def play(chunk, prepared):
            gate = pcm_admission_context.get()
            self.assertIsNotNone(gate)
            gate.admit()  # Speaker announcement sends the first PCM.
            results.append(self.correct(reader, tracker, authority))
            gate.admit()  # Dialogue shares that authority.

        reader.play_prepared = play
        reader._play_if_current(old, OLD)
        self.assertEqual(results, [None])
        self.assertIsNone(pcm_admission_context.get())
        self.assertIn(old.generation, reader.pcm_admitted_generations)

    def test_stop_pause_replay_and_admitted_or_stale_owners_cannot_be_replaced(self):
        for barrier in (
            "stop",
            "pause",
            "emergency",
            "suppressed",
            "sealed",
            "run",
            "epoch",
            "frame",
            "owner",
            "focus",
            "rewrite",
            "pcm",
            "replay",
        ):
            with self.subTest(barrier=barrier):
                reader, tracker, old, authority, *_ = self.candidate()
                if barrier == "stop":
                    reader.stop_event.set()
                elif barrier == "pause":
                    reader.paused = True
                elif barrier == "emergency":
                    reader.emergency_stopped = True
                elif barrier == "suppressed":
                    reader.suppressed_generation = old.generation
                elif barrier == "sealed":
                    reader.sealed_generations.add(old.generation)
                elif barrier == "run":
                    reader.run_id = "other-run"
                elif barrier == "epoch":
                    reader.frame_route_epoch += 1
                elif barrier == "frame":
                    reader.latest_frame_fingerprint = "other"
                    reader.latest_frame = "other"
                elif barrier == "owner":
                    reader.stable_frame_owner = lambda: "other"
                elif barrier == "focus":
                    reader.focus_probe = lambda: False
                elif barrier == "pcm":
                    reader.current_chunk = old
                    self.assertTrue(reader.claim_first_pcm(old))
                elif barrier == "replay":
                    reader.unheard_candidate = None
                    self.assertFalse(
                        reader.allow_unheard_replacement(
                            replace(old, explicit_replay=True)
                        )
                    )
                if barrier == "rewrite":
                    result = reader._replace_unheard_candidate(
                        tracker, authority, "Someone else", FULL
                    )
                else:
                    result = self.correct(reader, tracker, authority)
                self.assertIsNone(result)
                self.assertEqual(tracker.committed_text, OLD)


if __name__ == "__main__":
    unittest.main()
