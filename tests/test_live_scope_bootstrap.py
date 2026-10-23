import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.hashing import text_sha256

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.settings import AppSettings


class LiveScopeBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.line = ChapterDialogue(
            "line-1",
            "chapter-1",
            1,
            "Speaker",
            "The complete dialogue.",
            text_sha256("The complete dialogue."),
        )
        self.controller = AppController(
            AppSettings(),
            chapter_voice_preloader=ChapterVoicePreloader((self.line,)),
            dialog_handler=Mock(),
        )
        self.controller.live_reader = Mock(is_running=False)
        self.controller.voice_router = Mock()
        self.target = Mock(last_window=SimpleNamespace(handle=42, process_id=7))
        self.target.is_focused.return_value = True
        self.controller.capture_target = self.target
        self.cancellation = Event()
        self.read = self.enterContext(
            patch(
                "vntts.controller_components.read_live_snapshot",
                return_value=(self.line.speaker, self.line.text),
            )
        )
        self.events = self.enterContext(
            patch("vntts.controller_components.record_diagnostic_event")
        )

    def identify(self):
        return self.controller.identify_live_scope(cancellation=self.cancellation)

    def assert_outcome(self, outcome):
        self.assertEqual(
            self.events.call_args.args, ("runtime", "live-scope-bootstrap")
        )
        self.assertEqual(self.events.call_args.kwargs["outcome"], outcome)
        if outcome != "matched":
            self.controller.dialog_handler.assert_not_called()
            self.assertIsNone(self.controller.chapter_voice_preloader.current_match)

    def test_absent_focus_does_not_capture_despite_cached_focus(self):
        self.controller.game_focused = True
        self.target.is_focused.return_value = False
        self.assertFalse(self.identify())
        self.read.assert_not_called()
        self.assertEqual(
            self.controller.live_scope_identification_failure, "focus-wait"
        )
        self.assert_outcome("focus-wait")

    def test_fresh_focus_recovers_start_despite_stale_cached_focus(self):
        self.controller.game_focused = False
        self.assertTrue(self.identify())
        self.controller.dialog_handler.assert_called_once_with(
            self.line.speaker, self.line.text
        )
        self.assert_outcome("matched")
        self.assertNotIn(self.line.text, repr(self.events.call_args))
        self.assertNotIn(self.line.speaker, repr(self.events.call_args))

    def test_no_selected_target_never_reads_desktop(self):
        self.controller.capture_target = None
        self.assertFalse(self.identify())
        self.read.assert_not_called()
        self.assert_outcome("capture-target-unavailable")

    def test_focus_lost_during_ocr_never_reaches_matcher(self):
        def read(*_args):
            self.target.is_focused.return_value = False
            return self.line.speaker, self.line.text

        self.read.side_effect = read
        with patch.object(
            self.controller, "_resolve_initial_live_sequence_line"
        ) as matcher:
            self.assertFalse(self.identify())
        matcher.assert_not_called()
        self.assert_outcome("focus-lost")

    def test_focus_lost_during_match_restores_previous_story_context(self):
        resolve = self.controller._resolve_initial_live_sequence_line

        def match(character, text):
            result = resolve(character, text)
            self.target.is_focused.return_value = False
            return result

        with patch.object(
            self.controller, "_resolve_initial_live_sequence_line", side_effect=match
        ):
            self.assertFalse(self.identify())
        self.assert_outcome("focus-lost")

    def test_cancelled_before_capture_is_bounded(self):
        self.cancellation.set()
        self.assertFalse(self.identify())
        self.read.assert_not_called()
        self.assert_outcome("cancelled")

    def test_cancel_during_ocr_suppresses_diagnostic_callback_and_matching(self):
        def read(*args):
            self.cancellation.set()
            args[6](Mock())
            return self.line.speaker, self.line.text

        self.read.side_effect = read
        with patch.object(self.controller, "_publish_diagnostic") as publish:
            self.assertFalse(self.identify())
        publish.assert_not_called()
        self.assert_outcome("cancelled")

    def test_stop_during_ocr_rejects_late_result(self):
        def read(*_args):
            self.controller.stop_live()
            return self.line.speaker, self.line.text

        self.read.side_effect = read
        self.assertFalse(self.identify())
        self.assert_outcome("cancelled")

    def test_target_replacement_during_ocr_rejects_late_result(self):
        def read(*_args):
            self.controller.capture_target = Mock()
            return self.line.speaker, self.line.text

        self.read.side_effect = read
        self.assertFalse(self.identify())
        self.assert_outcome("target-changed")

    def test_native_window_replacement_during_ocr_rejects_late_result(self):
        def read(*_args):
            self.target.last_window = SimpleNamespace(handle=43, process_id=8)
            return self.line.speaker, self.line.text

        self.read.side_effect = read
        self.assertFalse(self.identify())
        self.assert_outcome("target-changed")

    def test_empty_ocr_remains_distinct_from_story_mismatch(self):
        self.read.return_value = (None, "")
        self.assertFalse(self.identify())
        self.assert_outcome("no-dialog-text")

    def test_story_mismatch_has_matcher_evidence_without_dialogue(self):
        self.read.return_value = ("Private name", "Unrelated private dialogue.")
        self.assertFalse(self.identify())
        self.assert_outcome("story-line-no-match")
        self.assertEqual(
            self.events.call_args.kwargs["match_result"], "expected-no-match"
        )
        self.assertNotIn("private", repr(self.events.call_args).lower())

    def test_capture_error_records_type_without_error_message(self):
        self.read.side_effect = ValueError("private screenshot location")
        with self.assertRaises(ValueError):
            self.identify()
        self.assert_outcome("error")
        self.assertEqual(self.events.call_args.kwargs["error_type"], "ValueError")
        self.assertNotIn("private", repr(self.events.call_args))
