import unittest
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

from tests.test_live import FakeClock, QueuedExecutor
from vntts.live import LiveDialogReader, RecognizedDialog
from vntts.live_tracking import IncrementalDialogTracker


class LiveUnheardFreshnessTest(unittest.TestCase):
    raw = "The damaged story has missng words."
    canonical = "The complete story has the missing words."

    def harness(self, *, qualify_before=False, barrier=None, rewrite=False):
        clock = FakeClock()
        tracker = IncrementalDialogTracker(
            clock=clock,
            complete_dialogue_only=True,
            stability_frames=1,
            idle_flush_seconds=0,
        )
        tracker.observe("Narrator", self.raw)
        initial = tracker.observe("Narrator", self.raw)
        self.assertEqual(len(initial), 1)
        loop_stop = Event()
        context = {"focused": True, "owner": "owner"}
        executor = QueuedExecutor()
        errors = []
        recognized = RecognizedDialog("Narrator", self.canonical, "canonical")
        recognize = Mock(side_effect=[recognized, ("Narrator", self.canonical)])
        observations = []
        reader = None
        candidate = None

        def push():
            with reader.pause_condition:
                reader.frame_version += 1
                reader.pause_condition.notify_all()

        def observe(character, text, *, focused=True):
            observations.append((character, text))
            if len(observations) == 1:
                if not qualify_before:
                    self.assertTrue(reader.allow_unheard_replacement(candidate))
                self.mutate_barrier(reader, context, barrier)
                push()
            elif len(observations) < 3 and barrier is None and not rewrite:
                push()
            else:
                loop_stop.set()
            return 0

        reader = LiveDialogReader(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=executor,
            playback_executor=QueuedExecutor(),
            capture_frame=Mock(),
            recognize_frame=recognize,
            prepare_chunk=Mock(),
            play_prepared=Mock(),
            report_error=errors.append,
            focus_probe=lambda: context["focused"],
            stable_frame_owner=lambda: context["owner"],
            stable_frame_route=lambda *_args: None,
            frame_equivalent=lambda first, second: first == second,
            dialog_observed=(lambda *_args: ("Narrator", "Different dialogue."))
            if rewrite
            else None,
            tracker_factory=lambda **_options: tracker,
            adaptive_policy_factory=lambda **_options: SimpleNamespace(observe=observe),
            auto_advance=lambda: False,
        )
        reader.active_generation = tracker.generation
        reader.latest_frame = "canonical frame"
        reader.latest_frame_fingerprint = "canonical glyphs"
        reader.latest_frame_visible = True
        reader.frame_version = 1
        reader._schedule(initial)
        candidate = next(iter(reader.speech_futures.values()))
        if qualify_before:
            self.assertTrue(reader.allow_unheard_replacement(candidate))
        self.run_ocr_frames(reader, loop_stop)
        self.assertLessEqual(len(observations), 3)
        self.assertEqual(errors, [])
        queued = [arguments[0] for _, _, arguments in executor.jobs]
        return reader, tracker, recognize, queued

    def mutate_barrier(self, reader, context, barrier):
        if barrier == "invalidation":
            reader.invalidate_observation()
        elif barrier == "run":
            reader.run_id = "another-run"
        elif barrier == "frame":
            reader.latest_frame = "different frame"
            reader.latest_frame_fingerprint = "different glyphs"
        elif barrier == "owner":
            context["owner"] = "another-owner"
        elif barrier == "focus":
            context["focused"] = False
        elif barrier == "stop":
            reader.stop_event.set()
        elif barrier == "pause":
            reader.paused = True

    def run_ocr_frames(self, reader, loop_stop):
        worker = Thread(target=reader._run_ocr, args=(loop_stop,), daemon=True)
        worker.start()
        worker.join(timeout=2)
        if worker.is_alive():
            loop_stop.set()
            with reader.pause_condition:
                reader.pause_condition.notify_all()
            worker.join(timeout=1)
            self.fail("OCR consumer did not finish the bounded frame sequence")

    def test_cached_canonical_can_replace_late_qualified_candidate_once(self):
        reader, tracker, recognize, queued = self.harness()
        canonical = [chunk for chunk in queued if chunk.line_id == "canonical"]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0].text, self.canonical)
        self.assertEqual(tracker.canonical_line_id, "canonical")
        self.assertEqual(reader.active_generation, canonical[0].generation)
        recognize.assert_called_once_with("canonical frame")

    def test_barriers_do_not_promote_cached_canonical_authority(self):
        for barrier in (
            "invalidation",
            "run",
            "frame",
            "owner",
            "focus",
            "stop",
            "pause",
        ):
            with self.subTest(barrier=barrier):
                _, tracker, recognize, queued = self.harness(barrier=barrier)
                self.assertEqual([chunk for chunk in queued if chunk.line_id], [])
                self.assertIsNone(tracker.canonical_line_id)
                self.assertEqual(
                    recognize.call_count,
                    2 if barrier in {"invalidation", "frame"} else 1,
                )

    def test_callback_rewrite_does_not_reuse_canonical_identity(self):
        _, tracker, _, queued = self.harness(qualify_before=True, rewrite=True)
        self.assertEqual([chunk for chunk in queued if chunk.line_id], [])
        self.assertIsNone(tracker.canonical_line_id)
