import unittest
from unittest.mock import patch

from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.live import RecognizedDialog


class IdentifiedReadingTest(unittest.TestCase):
    def test_routing_held_fragment_keeps_capture_time_through_playback_and_replay(self):
        complete = "The garden is quiet today."
        harness = AutoAdvanceFakeFrameHarness(
            dialog_observed=lambda _speaker, text: text == complete,
            tracker_options={"complete_dialogue_only": True},
        )
        harness.reader.recognize_frame = lambda frame: (
            RecognizedDialog("Ada", complete, "A", exact_match=True)
            if frame["text"] == complete
            else ("Ada", frame["text"])
        )
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                harness.push("Ada", "The garden", background="typing", captured_at=10.0)
                self.assertFalse(harness.speech_executor.jobs)
                harness.push("Ada", complete, background="full", captured_at=12.0)
                harness.finish_playback()
                chunk = harness.played[0][0]
                self.assertEqual(chunk.first_observed_at, 10.0)
                self.assertEqual(chunk.line_id, "A")
                self.assertTrue(harness.reader.repeat_last())
                harness.finish_playback()
                replay = harness.played[1][0]
                self.assertTrue(replay.explicit_replay)
                self.assertIsNone(replay.first_observed_at)
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def harness(self, *, exact=True):
        harness = AutoAdvanceFakeFrameHarness(
            tracker_options={"complete_dialogue_only": True}
        )
        recognized = []

        def recognize(frame):
            recognized.append(frame["text"])
            return RecognizedDialog(
                frame["character"],
                frame["text"],
                frame["text"],
                exact_match=exact,
            )

        harness.reader.recognize_frame = recognize
        return harness, recognized

    def test_one_frame_exact_lines_are_queued_in_order_without_a_game_key(self):
        harness, recognized = self.harness()
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                for text in ("First.", "Ouch!", "Third."):
                    harness.push("Ada", text, background=text, fingerprint=text)
                for _ in range(3):
                    harness.finish_playback()
                self.assertEqual(
                    [chunk.text for chunk, _ in harness.played],
                    ["First.", "Ouch!", "Third."],
                )
                self.assertEqual(recognized, ["First.", "Ouch!", "Third."])
                self.assertEqual(harness.advance_calls, [])
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_unseen_middle_line_and_repeated_frames_do_not_block_or_repeat(self):
        harness, _ = self.harness()
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                for text in ("First.", "Third.", "Third.", "First.", "Third."):
                    harness.push("Ada", text, background=text, fingerprint=text)
                for _ in range(2):
                    harness.finish_playback()
                self.assertEqual(
                    [chunk.text for chunk, _ in harness.played], ["First.", "Third."]
                )
                self.assertFalse(harness.reader.deferred_chunks)
                self.assertFalse(harness.reader.speech_futures)
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_approximate_identity_requires_fresh_ocr_even_for_unchanged_pixels(self):
        harness, recognized = self.harness(exact=False)
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                harness.push("Ada", "Ouch!", background="a", fingerprint="a")
                self.assertFalse(harness.speech_executor.jobs)
                harness.push("Ada", "Ouch!", background="a", fingerprint="a")
                self.assertEqual(recognized, ["Ouch!", "Ouch!"])
                harness.finish_playback()
                harness.push("Ada", "Ouch!", background="a", fingerprint="a")
                self.assertEqual(len(harness.played), 1)
                self.assertFalse(harness.speech_executor.jobs)
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_route_barrier_cannot_reauthorize_identified_ocr(self):
        harness, _ = self.harness()

        def observe(*_):
            harness.reader.invalidate_observation()
            return True

        harness.reader.dialog_observed = observe
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                harness.push("Ada", "Ouch!", background="a", fingerprint="a")
                self.assertFalse(harness.speech_executor.jobs)
                self.assertEqual(harness.reader.active_generation, 0)
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()


if __name__ == "__main__":
    unittest.main()
