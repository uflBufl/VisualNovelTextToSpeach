import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.live import RecognizedDialog
from vntts.live_tracking import IncrementalDialogTracker


class IdentifiedReadingTest(unittest.TestCase):
    def push_caption(self, harness, text, line_id, captured_at):
        frame = SimpleNamespace(
            text=text,
            line_id=line_id,
            layout_id="caption",
            layout_kind="caption",
            navigation_allowed=False,
        )
        with harness.reader.pause_condition:
            harness.reader.latest_frame = frame
            harness.reader.latest_frame_captured_at = captured_at
            harness.reader.latest_frame_fingerprint = text
            harness.reader.latest_frame_visible = True
            harness.reader.latest_frame_complete = False
            harness.reader.frame_version += 1
            version = harness.reader.frame_version
            harness.reader.pause_condition.notify_all()
            self.assertTrue(
                harness.reader.pause_condition.wait_for(
                    lambda: harness.reader.processed_frame_version >= version, timeout=1
                ),
                harness.errors,
            )

    def test_one_frame_unowned_caption_precedes_identified_successor_without_keys(self):
        harness = AutoAdvanceFakeFrameHarness(
            tracker_options={"complete_dialogue_only": True, "stability_frames": 3}
        )
        harness.reader.recognize_frame = lambda frame: RecognizedDialog(
            "Narrator", frame.text, frame.line_id, allow_unheard_replacement=False
        )
        harness.reader.frame_equivalent = lambda before, after: (
            before.text == after.text
        )
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                self.push_caption(harness, "She closes her eyes.", None, 10.0)
                tracker = harness.reader.reading_session.tracker
                self.assertIsNone(tracker.canonical_line_id)
                self.assertEqual(tracker.accepted_line_ids, set())
                self.assertTrue(harness.speech_executor.jobs)
                self.push_caption(harness, "She closes her eyes.", None, 11.0)
                self.push_caption(harness, "The room falls silent.", "successor", 12.0)
                harness.finish_playback()
                harness.finish_playback()
                self.assertEqual(
                    [(chunk.text, chunk.line_id) for chunk, _ in harness.played],
                    [
                        ("She closes her eyes.", None),
                        ("The room falls silent.", "successor"),
                    ],
                )
                self.assertEqual(harness.played[0][0].first_observed_at, 10.0)
                self.assertEqual(tracker.accepted_line_ids, {"successor"})
                self.assertFalse(harness.reader.layout_navigation_allowed())
                self.assertIsNone(harness.reader.dialog_ready_generation)
                self.assertEqual(harness.advance_calls, [])
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_different_one_frame_unowned_captions_are_queued_in_order(self):
        harness = AutoAdvanceFakeFrameHarness(
            tracker_options={"complete_dialogue_only": True, "stability_frames": 3}
        )
        harness.reader.recognize_frame = lambda frame: RecognizedDialog(
            "Narrator", frame.text, None, allow_unheard_replacement=False
        )
        harness.reader.frame_equivalent = lambda before, after: (
            before.text == after.text
        )
        texts = ("The room falls silent.", "The room falls dark.")
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                for captured_at, text in enumerate(texts, start=10):
                    self.push_caption(harness, text, None, captured_at)
                for _ in texts:
                    harness.finish_playback()
                self.assertEqual(
                    [chunk.text for chunk, _ in harness.played], list(texts)
                )
                self.assertTrue(
                    all(chunk.line_id is None for chunk, _ in harness.played)
                )
                self.assertEqual(
                    harness.reader.reading_session.tracker.accepted_line_ids, set()
                )
                self.assertEqual(harness.advance_calls, [])
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_verified_completion_coalesces_partial_text_and_committed_suffix(self):
        now = [10.0]
        tracker = IncrementalDialogTracker(clock=lambda: now[0], stability_frames=3)
        tracker.record_observation("Narrator", "She closes", 8.0)
        self.assertEqual(tracker.observe("Narrator", "She closes"), [])
        now[0] = 11.0
        first = tracker.observe_complete("Narrator", "She closes her eyes.")
        self.assertEqual(
            [(chunk.text, chunk.line_id) for chunk in first],
            [("She closes her eyes.", None)],
        )
        self.assertEqual(first[0].first_observed_at, 8.0)
        self.assertEqual(tracker.observe_complete("Narrator", first[0].text), [])
        suffix = tracker.observe_complete(
            "Narrator", first[0].text + " The room falls silent."
        )
        self.assertEqual([chunk.text for chunk in suffix], ["The room falls silent."])
        self.assertEqual(tracker.accepted_line_ids, set())

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
        for line_id in ("identified", None):
            with self.subTest(line_id=line_id):
                harness, _ = self.harness()
                harness.reader.recognize_frame = lambda frame: RecognizedDialog(
                    frame["character"],
                    frame["text"],
                    line_id,
                    allow_unheard_replacement=False,
                )

                def observe(*_):
                    harness.reader.invalidate_observation()
                    return True

                harness.reader.dialog_observed = observe
                with patch(
                    "vntts.live.Timer", side_effect=harness.timer_scheduler.create
                ):
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
