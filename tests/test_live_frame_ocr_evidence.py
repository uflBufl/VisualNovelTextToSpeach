import unittest
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from tests import test_controller_transition_observation
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.dialog_capture import (
    CapturedDialogFrame,
    DialogFrameEquivalence,
    OCRUncertainError,
)
from vntts.live import RecognizedDialog
from vntts.ocr import OCRResult
from vntts.settings import AppSettings
from vntts.voices import CharacterVoiceRegistry


class LiveFrameOCREvidenceTest(unittest.TestCase):
    def setUp(self):
        self.controller = AppController(AppSettings(ocr_minimum_confidence=80))
        self.controller.voice_router = SimpleNamespace(
            registry=CharacterVoiceRegistry()
        )
        self.controller.uncertain_frame_recorder = None
        self.controller._publish_diagnostic = Mock()
        self.controller._resolve_voice_label = Mock(return_value="Story voice")
        self.controller._stable_live_frame_owner = Mock(return_value="event-1")
        self.controller._is_game_focused = Mock(return_value=True)
        self.controller.status_handler = Mock()
        self.controller.thread_pool_executor_factory = Mock(return_value=Mock())
        with patch(
            "vntts.controller_components.RuntimeLifecycleComponent._configure_dialog_read"
        ):
            self.controller.runtime_lifecycle._construct_live_runtime()
        self.reader = self.controller.live_reader

    @staticmethod
    def frame():
        return CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)

    def recognize(self, frame, result):
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=result
        ):
            return self.controller._recognize_live_frame(frame)

    def test_runtime_wires_raw_ocr_comparison_without_changing_spoken_text(self):
        before, after = self.frame(), self.frame()
        self.assertIsInstance(self.reader.frame_equivalent, DialogFrameEquivalence)
        self.assertFalse(self.reader.frame_equivalent(before, after))
        original = OCRResult("", "We meet again...", 95, "balanced", 1)
        changed = replace(original, text="We meet again!!!")

        first = self.recognize(before, original)
        second = self.recognize(after, changed)

        self.assertEqual(first[1], original.text)
        self.assertEqual(second[1], changed.text)
        self.assertTrue(self.reader.frame_equivalent(before, after))
        self.assertEqual((original.character, changed.character), ("", ""))

    def test_settings_refresh_replaces_cached_evidence_and_confidence_threshold(self):
        before, after = self.frame(), self.frame()
        result = OCRResult("Ada", "Visible dialogue.", 85, "balanced", 1)
        self.recognize(before, result)
        self.recognize(after, result)
        previous_comparison = self.reader.frame_equivalent
        self.assertTrue(previous_comparison(before, after))
        self.controller.settings = replace(
            self.controller.settings, ocr_minimum_confidence=90
        )

        with patch(
            "vntts.controller_components.RuntimeLifecycleComponent._configure_dialog_read"
        ):
            self.controller.runtime_lifecycle._refresh_live_reader_settings()

        comparison = self.reader.frame_equivalent
        self.assertIsNot(comparison, previous_comparison)
        self.assertEqual(comparison.minimum_confidence, 90)
        self.assertFalse(comparison(before, after))
        for frame in (before, after):
            with self.assertRaises(OCRUncertainError):
                self.recognize(frame, result)
        self.assertFalse(comparison(before, after))
        for frame in (before, after):
            self.recognize(frame, replace(result, confidence=95))
        self.assertTrue(comparison(before, after))

    def test_canonical_speaker_resolution_does_not_replace_raw_speaker_evidence(self):
        text = "The prepared story contains this complete sentence."
        line = ChapterDialogue("line", "chapter", 1, "Ada", text, text_sha256(text))
        self.controller.chapter_voice_preloader = ChapterVoicePreloader((line,))
        before, after = self.frame(), self.frame()

        first = self.recognize(before, OCRResult("Unreadable", text, 95, "balanced", 1))
        second = self.recognize(after, OCRResult("", text, 95, "balanced", 1))

        self.assertIsInstance(first, RecognizedDialog)
        self.assertEqual(first.character, "Ada")
        self.assertEqual(second.character, "Ada")
        self.assertFalse(self.reader.frame_equivalent(before, after))

    def test_low_raw_confidence_rescued_by_story_cannot_authorize_semantics(self):
        text = "The prepared story contains this complete sentence."
        line = ChapterDialogue("line", "chapter", 1, "Ada", text, text_sha256(text))
        self.controller.chapter_voice_preloader = ChapterVoicePreloader((line,))
        before, after = self.frame(), self.frame()
        confident = OCRResult("Unreadable", text, 95, "balanced", 1)
        uncertain = replace(confident, confidence=50, dialogue_confidence=95)

        first = self.recognize(before, confident)
        second = self.recognize(after, uncertain)

        self.assertIsInstance(first, RecognizedDialog)
        self.assertEqual(second, first)
        self.assertFalse(self.reader.frame_equivalent(before, after))

    def test_owner_and_run_changes_invalidate_raw_evidence(self):
        for barrier in ("owner", "run"):
            with self.subTest(barrier=barrier):
                before, after = self.frame(), self.frame()
                result = OCRResult("Ada", "Visible dialogue.", 95, "balanced", 1)
                self.recognize(before, result)
                self.recognize(after, result)
                self.assertTrue(self.reader.frame_equivalent(before, after))
                if barrier == "owner":
                    self.controller._stable_live_frame_owner.return_value = "event-2"
                else:
                    self.reader.run_id = "replacement-run"
                self.assertFalse(self.reader.frame_equivalent(before, after))

    def test_ocr_finishing_after_context_change_keeps_its_starting_context(self):
        before, after = self.frame(), self.frame()
        result = OCRResult("Ada", "Visible dialogue.", 95, "balanced", 1)

        def change_owner(*_args, **_kwargs):
            self.controller._stable_live_frame_owner.return_value = "event-2"
            return result

        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result",
            side_effect=change_owner,
        ):
            self.controller._recognize_live_frame(before)
        self.recognize(after, result)

        self.assertFalse(self.reader.frame_equivalent(before, after))

    def test_initial_canonical_route_keeps_its_raw_anchor_under_new_owner(self):
        fixture = (
            test_controller_transition_observation.ControllerTransitionObservationTest()
        )
        controller, lines = fixture.controller()
        controller.voice_router = SimpleNamespace(registry=CharacterVoiceRegistry())
        controller.uncertain_frame_recorder = None
        controller._publish_diagnostic = Mock()
        controller._resolve_voice_label = Mock(return_value="Story voice")
        controller._is_game_focused = Mock(return_value=True)
        controller.thread_pool_executor_factory = Mock(return_value=Mock())
        with patch(
            "vntts.controller_components.RuntimeLifecycleComponent._configure_dialog_read"
        ):
            controller.runtime_lifecycle._construct_live_runtime()
        reader = controller.live_reader
        reader._schedule_tracked_chunks = Mock()
        before, after = self.frame(), self.frame()
        reader.latest_frame = before
        reader.latest_frame_fingerprint = "initial"
        reader.latest_frame_visible = True
        reader.frame_version = 1
        stop = Event()
        result = OCRResult("Ada", lines[0].text, 95, "balanced", 1)

        def recognize(*_args, **_kwargs):
            stop.set()
            return result

        self.assertIsNone(controller._stable_live_frame_owner())
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", side_effect=recognize
        ):
            reader._run_ocr(stop)
        self.assertEqual(controller._stable_live_frame_owner(), "event-1")
        self.assertIs(reader.routed_frame, before)
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=result
        ):
            controller._recognize_live_frame(after)

        self.assertTrue(reader.frame_equivalent(before, after))

    def test_invalidated_or_mismatched_route_cannot_rebind_old_owner_evidence(self):
        for barrier in ("uncertain", "frame", "fingerprint"):
            with self.subTest(barrier=barrier):
                before, after = self.frame(), self.frame()
                result = OCRResult("Ada", "Visible dialogue.", 95, "balanced", 1)
                self.controller._stable_live_frame_owner.return_value = "event-1"
                self.recognize(before, result)
                self.controller._stable_live_frame_owner.return_value = "event-2"
                self.recognize(after, result)
                self.reader.observation_uncertain = barrier == "uncertain"
                self.reader.routed_frame = after if barrier == "frame" else before
                self.reader.routed_frame_fingerprint = (
                    "different" if barrier == "fingerprint" else "route"
                )

                self.controller._bind_live_frame_evidence(
                    before, "route", "ocr", result.character, result.text
                )

                self.assertFalse(self.reader.frame_equivalent(before, after))
                self.reader.observation_uncertain = False
                self.reader.routed_frame = before
                self.reader.routed_frame_fingerprint = "route"
                self.controller._bind_live_frame_evidence(
                    before, "route", "ocr", result.character, result.text
                )
                self.assertTrue(self.reader.frame_equivalent(before, after))

    def test_unrecognized_latest_replacing_source_during_ocr_stays_stale(self):
        anchor, source, latest = self.frame(), self.frame(), self.frame()
        result = OCRResult("Ada", "Visible dialogue.", 95, "balanced", 1)
        self.recognize(anchor, result)
        self.reader.routed_frame = anchor
        self.reader.latest_frame = source
        self.reader.latest_frame_fingerprint = "source"
        self.reader.latest_frame_visible = True
        self.reader.frame_version = 1
        self.reader.pipeline_event_handler = Mock()
        policy = Mock()

        def replace_capture(*_args, **_kwargs):
            self.reader.latest_frame = latest
            self.reader.latest_frame_fingerprint = "unrecognized-latest"
            self.reader.frame_version += 1
            return result

        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result",
            side_effect=replace_capture,
        ) as recognize:
            observation = self.reader._recognize_current_frame(
                source, "legacy", "source", True, False, None, policy, 1, None
            )

        self.assertIsNone(observation)
        recognize.assert_called_once()
        policy.observe.assert_not_called()
        self.assertTrue(self.reader.frame_equivalent(anchor, source))
        self.assertFalse(self.reader.frame_equivalent(source, latest))
        details = self.reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(details["outcome"], "stale")
        self.assertEqual(details["reason"], "ocr-frame-not-equivalent")

    def test_reader_and_controller_dispatch_share_semantic_visual_evidence(self):
        before, after = self.frame(), self.frame()
        result = OCRResult("Ada", "Visible dialogue...", 95, "balanced", 1)
        self.recognize(before, result)
        self.recognize(after, replace(result, text="Visible dialogue!!!"))
        self.reader.routed_frame = before
        self.reader.latest_frame = after
        self.reader.routed_frame_fingerprint = "route"
        self.reader.latest_frame_fingerprint = "latest"
        self.reader.latest_frame_visible = True
        self.reader.active_generation = 3
        self.reader.dialog_ready_generation = 3
        dispatch = Mock(return_value=True)

        def guarded_dispatch():
            if not self.controller._reader_visual_dispatch_guard(self.reader)():
                return False
            return dispatch()

        self.reader.auto_advance = guarded_dispatch
        with patch("vntts.live.Timer"):
            self.reader._run_auto_advance(3)

        dispatch.assert_called_once_with()
        self.assertEqual(self.reader.pending_auto_advance_generation, 3)

    def test_dispatch_guard_rejects_capture_replacement_after_comparison(self):
        before, after = self.frame(), self.frame()
        result = OCRResult("Ada", "Visible dialogue.", 95, "balanced", 1)
        self.recognize(before, result)
        self.recognize(after, result)
        self.reader.routed_frame = before
        self.reader.latest_frame = after
        self.reader.latest_frame_visible = True
        self.reader.routed_frame_fingerprint = "route"
        self.reader.latest_frame_fingerprint = "latest"
        guard = self.controller._reader_visual_dispatch_guard(self.reader)
        self.assertTrue(guard())

        self.reader.latest_frame = self.frame()
        self.reader.frame_version += 1

        self.assertFalse(guard())


if __name__ == "__main__":
    unittest.main()
