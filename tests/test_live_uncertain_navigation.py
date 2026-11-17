import unittest
from contextlib import contextmanager
from unittest.mock import patch

from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.dialog_capture import OCRUncertainError
from vntts.live import RecognizedDialog
from vntts.ocr import OCRResult


class UncertainNavigationTest(unittest.TestCase):
    @contextmanager
    def ready_dialogue(self, *, canonical=True):
        harness = AutoAdvanceFakeFrameHarness()
        if canonical:
            harness.reader.recognize_frame = lambda frame: RecognizedDialog(
                frame["character"], frame["text"], frame["text"]
            )
        harness.reader.ocr_uncertain = lambda *_args: (
            harness.reader.invalidate_observation()
        )
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                harness.prepare_ready_dialogue()
                harness.finish_playback()
                yield harness
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def uncertain_frame(self, harness):
        error = OCRUncertainError(OCRResult("Rhiannon", "noise", 23, "test", 1), 60)
        with patch.object(harness.reader, "recognize_frame", side_effect=error):
            harness.push(
                "Rhiannon", "noise", background="uncertain", expected=(None, "")
            )

    def test_fresh_same_dialogue_recovers_request_across_uncertain_frames(self):
        for canonical in (False, True):
            for uncertainty_before_timer in (False, True):
                with (
                    self.subTest(
                        canonical=canonical,
                        uncertainty_before_timer=uncertainty_before_timer,
                    ),
                    self.ready_dialogue(canonical=canonical) as harness,
                ):
                    if uncertainty_before_timer:
                        self.uncertain_frame(harness)
                    harness.timer_scheduler.fire_next("_run_auto_advance")
                    self.assertEqual(harness.advance_calls, [])
                    self.uncertain_frame(harness)
                    self.uncertain_frame(harness)
                    self.assertEqual(harness.advance_calls, [])
                    harness.push("Rhiannon", "I, erhm ...", background="recovered")
                    self.assertEqual(harness.advance_calls, [1])
                    self.assertEqual(len(harness.played), 1)
                    self.assertEqual(harness.speech_executor.jobs, [])
                    self.uncertain_frame(harness)
                    harness.push("Rhiannon", "I, erhm ...", background="held")
                    self.assertEqual(harness.advance_calls, [1])

    def test_recovery_still_requires_a_capture_after_the_request(self):
        with self.ready_dialogue() as harness:
            harness.timer_scheduler.fire_next("_run_auto_advance")
            requested_at = harness.reader._advance_requested_at
            self.uncertain_frame(harness)
            harness.push(
                "Rhiannon",
                "I, erhm ...",
                background="old-capture",
                captured_at=requested_at - 1,
            )
            self.assertEqual(harness.advance_calls, [])
            harness.push("Rhiannon", "I, erhm ...", background="fresh-capture")
            self.assertEqual(harness.advance_calls, [1])

    def test_changed_dialogue_and_controls_cancel_the_recoverable_request(self):
        for barrier in ("dialogue", "layout", "clear", "stop", "failed-speech"):
            with self.subTest(barrier=barrier), self.ready_dialogue() as harness:
                reader = harness.reader
                harness.timer_scheduler.fire_next("_run_auto_advance")
                self.uncertain_frame(harness)
                reader.frame_equivalent = lambda *_frames: True
                if barrier == "dialogue":
                    harness.push("Rhiannon", "Another reply.", background="successor")
                elif barrier == "layout":
                    from types import SimpleNamespace

                    with reader.state_lock:
                        reader._observe_frame_layout_locked(
                            SimpleNamespace(layout_id="story", layout_kind="caption")
                        )
                elif barrier == "clear":
                    reader.clear_queue()
                elif barrier == "stop":
                    reader.emergency_stop()
                else:
                    reader.block_auto_advance_for_generation(1, "Audio failed")
                self.assertIsNone(reader._advance_requested_generation)
                reader._dispatch_auto_advance(1)
                self.assertEqual(harness.advance_calls, [])
