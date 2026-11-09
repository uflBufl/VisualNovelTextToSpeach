import unittest
from threading import Thread
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_live import FakeClock, ManualTimerScheduler, QueuedExecutor
from vntts.live import LiveDialogReader, RecognizedDialog
from vntts.live_tracking import IncrementalDialogTracker


class LiveLayoutsTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.speech = QueuedExecutor()
        self.playback = QueuedExecutor()
        self.errors = []
        self.played = []
        self.recognized = []
        self.tracker_factory = Mock(side_effect=IncrementalDialogTracker)
        self.reader = LiveDialogReader(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=self.speech,
            playback_executor=self.playback,
            capture_frame=Mock(),
            recognize_frame=self.recognize,
            ocr_purpose=lambda: None,
            prepare_chunk=lambda chunk: chunk.text,
            play_prepared=lambda chunk, _prepared: self.played.append(chunk.text),
            report_error=self.errors.append,
            frame_equivalent=lambda _before, _after: True,
            tracker_factory=self.tracker_factory,
            tracker_options={
                "clock": self.clock,
                "stability_frames": 2,
                "idle_flush_seconds": 0.1,
                "min_chunk_characters": 1,
            },
        )
        self.worker = Thread(
            target=self.reader._run_ocr, args=(self.reader.stop_event,)
        )
        self.worker.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.reader.stop_event.set()
        with self.reader.pause_condition:
            self.reader.pause_condition.notify_all()
        self.worker.join(timeout=2)
        self.assertFalse(self.worker.is_alive())

    def recognize(self, frame):
        self.recognized.append(frame)
        return "Narrator", frame.text

    def push(self, text, *, kind="dialogue", allowed=None):
        frame = SimpleNamespace(
            text=text,
            layout_id=kind,
            layout_kind=kind,
            navigation_allowed=kind == "dialogue" if allowed is None else allowed,
        )
        self.clock.advance(0.2)
        with self.reader.pause_condition:
            self.reader.latest_frame = frame
            self.reader.latest_frame_fingerprint = text
            self.reader.latest_frame_visible = True
            self.reader.latest_frame_captured_at = monotonic()
            self.reader.frame_version += 1
            version = self.reader.frame_version
            self.reader.pause_condition.notify_all()
            self.assertTrue(
                self.reader.pause_condition.wait_for(
                    lambda: self.reader.processed_frame_version >= version, timeout=2
                ),
                self.errors,
            )
        self.assertEqual(self.errors, [])
        return frame

    def stable(self, text, **options):
        self.push(text, **options)
        self.push(text, **options)

    def test_caption_is_read_when_dialogue_ocr_is_visual_only(self):
        self.stable("A temporary caption.", kind="caption")
        self.assertEqual(len(self.recognized), 2)
        self.assertEqual(
            [chunk.text for chunk in self.reader.accepted_chunks.values()],
            ["A temporary caption."],
        )
        self.assertIsNone(self.reader.dialog_ready_generation)
        self.assertFalse(self.reader.layout_navigation_allowed())

    def test_dialogue_caption_return_keeps_fifo_without_repeating_dialogue(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.stable("The lower dialogue.")
        first = next(iter(self.reader.accepted_chunks.values()))
        self.stable("A temporary caption.", kind="caption")
        caption = list(self.reader.accepted_chunks.values())[-1]
        self.stable("The lower dialogue.")
        self.assertEqual(
            [chunk.text for chunk in self.reader.accepted_chunks.values()],
            ["The lower dialogue.", "A temporary caption."],
        )
        self.assertGreater(caption.generation, first.generation)
        self.assertGreater(self.reader.active_generation, caption.generation)
        self.assertIsNone(self.reader.dialog_ready_generation)
        self.speech.run_next()
        self.speech.run_next()
        self.playback.run_next()
        self.playback.run_next()
        self.assertEqual(self.played, ["The lower dialogue.", "A temporary caption."])

    def test_caption_return_is_a_new_occurrence(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.stable("Repeated caption.", kind="caption")
        self.stable("The lower dialogue.")
        self.stable("Repeated caption.", kind="caption")
        chunks = list(self.reader.accepted_chunks.values())
        self.assertEqual(
            [chunk.text for chunk in chunks],
            ["Repeated caption.", "The lower dialogue.", "Repeated caption."],
        )
        self.assertEqual(
            [chunk.generation for chunk in chunks],
            sorted({chunk.generation for chunk in chunks}),
        )

    def test_adjacent_equivalent_layouts_keep_one_ocr_occurrence(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.stable("The same visible words.")
        self.stable("The same visible words.", kind="caption")
        self.stable("The same visible words.")
        self.assertEqual(
            [chunk.text for chunk in self.reader.accepted_chunks.values()],
            ["The same visible words."],
        )

    def test_adjacent_layouts_keep_one_authoritative_source_occurrence(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.reader.recognize_frame = lambda frame: RecognizedDialog(
            "Narrator", frame.text, "same-source-line"
        )
        self.push("The same visible words.")
        self.push("The same visible words.", kind="caption")
        self.assertEqual(
            [chunk.line_id for chunk in self.reader.accepted_chunks.values()],
            ["same-source-line"],
        )

    def test_changing_coalesced_caption_does_not_replace_suspended_dialogue(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.stable("The unchanged lower dialogue.")
        dialogue_generation = self.reader.active_generation
        self.stable("The unchanged lower dialogue.", kind="caption")
        self.assertGreater(self.reader.active_generation, dialogue_generation)
        self.assertEqual(
            self.reader.reading_session.tracker.generation,
            self.reader.active_generation,
        )
        self.stable("A different caption.", kind="caption")
        self.stable("The unchanged lower dialogue.")
        self.assertEqual(
            [chunk.text for chunk in self.reader.accepted_chunks.values()],
            ["The unchanged lower dialogue.", "A different caption."],
        )

    def test_coalesced_caption_typing_does_not_mutate_suspended_tracker_state(self):
        self.reader.ocr_purpose = lambda: "legacy"
        text = "A completed sentence. More words"
        self.stable(text)
        session = self.reader.reading_session
        dialogue = session.tracker
        snapshot = (
            dialogue.latest_text,
            dialogue.committed_text,
            tuple(dialogue.history),
            tuple(dialogue.pending_history),
            set(dialogue.accepted_line_ids),
        )
        self.push(text, kind="caption")
        caption = session.tracker
        self.assertIsNot(caption, dialogue)
        self.push(text + " appeared.", kind="caption")
        self.push("Another occurrence begins.", kind="caption")
        caption.accepted_line_ids.add("caption-source-identity")
        self.assertEqual(
            (
                dialogue.latest_text,
                dialogue.committed_text,
                tuple(dialogue.history),
                tuple(dialogue.pending_history),
                set(dialogue.accepted_line_ids),
            ),
            snapshot,
        )

    def test_different_source_id_is_a_new_occurrence_even_with_same_text(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.reader.recognize_frame = lambda frame: RecognizedDialog(
            "Narrator", frame.text, "source:" + frame.layout_kind
        )
        self.push("The same visible words.")
        self.push("The same visible words.", kind="caption")
        self.assertEqual(
            [chunk.line_id for chunk in self.reader.accepted_chunks.values()],
            ["source:dialogue", "source:caption"],
        )

    def test_pending_detection_holds_admission_without_restarting_dialogue(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.stable("The lower dialogue.")
        accepted = tuple(self.reader.accepted_chunks.values())
        recognized = tuple(self.recognized)
        self.push("Unconfirmed text.", kind="pending", allowed=False)
        self.assertEqual(tuple(self.recognized), recognized)
        self.assertEqual(tuple(self.reader.accepted_chunks.values()), accepted)
        self.assertFalse(self.reader.layout_navigation_allowed())
        self.stable("The lower dialogue.")
        self.assertEqual(tuple(self.reader.accepted_chunks.values()), accepted)

    def test_initial_pending_frames_do_not_allocate_layout_trackers(self):
        self.push("Unconfirmed text.", kind="pending", allowed=False)
        self.push("Unconfirmed text.", kind="pending", allowed=False)
        self.assertIsNone(self.reader.reading_session)
        self.tracker_factory.assert_called_once()
        self.assertEqual(self.recognized, [])
        self.assertEqual(self.reader.accepted_chunks, {})
        self.stable("First confirmed caption.", kind="caption")
        self.tracker_factory.assert_called_once()
        self.assertEqual(
            [chunk.text for chunk in self.reader.accepted_chunks.values()],
            ["First confirmed caption."],
        )

    def test_caption_result_cannot_rebind_a_replaced_visual_target(self):
        def recognize(frame):
            self.reader.latest_frame = SimpleNamespace(
                layout_id="dialogue", layout_kind="dialogue", navigation_allowed=True
            )
            return "Narrator", frame.text

        self.reader.recognize_frame = recognize
        self.push("A late caption.", kind="caption")
        self.assertEqual(self.reader.accepted_chunks, {})
        self.assertIsNone(self.reader.routed_frame)
        self.assertIsNone(self.reader.dialog_ready_generation)

    def test_source_identity_is_optional_and_scoped_to_caption_occurrence(self):
        self.reader.ocr_purpose = lambda: "legacy"
        self.reader.recognize_frame = lambda frame: RecognizedDialog(
            "Narrator", frame.text, "source:" + frame.text
        )
        self.push("Repeated caption.", kind="caption")
        self.push("The lower dialogue.")
        self.push("Repeated caption.", kind="caption")
        chunks = list(self.reader.accepted_chunks.values())
        self.assertEqual(
            [chunk.line_id for chunk in chunks],
            [
                "source:Repeated caption.",
                "source:The lower dialogue.",
                "source:Repeated caption.",
            ],
        )

    def test_caption_revokes_delayed_key_without_cancelling_admitted_speech(self):
        self.reader.ocr_purpose = lambda: "legacy"
        scheduler = ManualTimerScheduler()
        with patch("vntts.live.Timer", side_effect=scheduler.create):
            self.stable("The lower dialogue.")
            self.speech.run_next()
            self.playback.run_next()
            self.push("The lower dialogue.")
            self.reader.auto_advance = Mock()
            self.reader._maybe_auto_advance()
            timer = scheduler.timers[-1]
            admitted = tuple(self.reader.accepted_chunks.values())
            self.push("A temporary caption.", kind="caption")
            timer.function(*timer.args)
            self.reader._dispatch_auto_advance(self.reader.active_generation)
            self.reader.auto_advance.assert_not_called()
            self.assertEqual(tuple(self.reader.accepted_chunks.values()), admitted)

    def test_navigation_requires_same_layout_and_explicit_permission(self):
        self.reader.latest_frame = SimpleNamespace(
            layout_id="lower", layout_kind="dialogue", navigation_allowed=True
        )
        self.reader.routed_frame = SimpleNamespace(
            layout_id="other", layout_kind="dialogue", navigation_allowed=True
        )
        self.assertFalse(self.reader.layout_navigation_allowed())
        self.reader.routed_frame.layout_id = "lower"
        self.assertTrue(self.reader.layout_navigation_allowed())
        self.reader.latest_frame.navigation_allowed = False
        self.assertFalse(self.reader.layout_navigation_allowed())


if __name__ == "__main__":
    unittest.main()
