import unittest
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.hashing import text_sha256

from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.live_snapshot import read_live_snapshot
from vntts.ocr import OCRResult
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
            status_handler=Mock(),
        )
        self.controller.live_reader = Mock(
            is_running=False,
            state_lock=RLock(),
            current_chunk=None,
            speech_futures={},
        )
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
        if outcome != "ocr-uncertain":
            self.assertFalse(
                any(
                    "OCR uncertain" in call.args[0]
                    for call in self.controller.status_handler.call_args_list
                )
            )
        if outcome != "matched":
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

    def test_uncertain_snapshot_reports_uncertainty_without_matching(self):
        result = OCRResult(self.line.speaker, self.line.text, 25, "test", 1)
        serial = self.controller.observation_serial
        self.controller.indexed_current_recovery = object()
        self.read.side_effect = read_live_snapshot
        with (
            patch(
                "vntts.live_snapshot.analyze_dialog_snapshot",
                return_value=(Mock(), None, result),
            ),
            patch.object(
                self.controller, "_resolve_initial_live_sequence_line"
            ) as matcher,
        ):
            self.assertFalse(self.identify())

        self.assertEqual(
            self.controller.live_scope_identification_failure, "ocr-uncertain"
        )
        self.assertEqual(self.controller.observation_serial, serial + 1)
        self.assertIsNone(self.controller.indexed_current_recovery)
        self.controller.live_reader.invalidate_observation.assert_called_once_with()
        self.controller.status_handler.assert_called_once_with(
            "OCR uncertain: 25% (requires 60%). Waiting for clearer text."
        )
        self.assertNotIn(self.line.text, repr(self.controller.status_handler.call_args))
        matcher.assert_not_called()
        self.assert_outcome("ocr-uncertain")
        self.assertNotIn(self.line.text, repr(self.events.call_args))

    def test_stale_uncertain_snapshot_has_no_uncertainty_side_effects(self):
        controller = self.controller
        settings = controller.settings
        preloader = controller.chapter_voice_preloader
        window = self.target.last_window
        serial = controller.observation_serial
        result = OCRResult(self.line.speaker, self.line.text, 25, "test", 1)
        changes = (
            ("cancellation", self.cancellation.set, "cancelled", 0),
            ("stop", controller.stop_live, "cancelled", 1),
            (
                "token",
                lambda: setattr(controller, "live_scope_identification_token", "new"),
                "cancelled",
                0,
            ),
            (
                "observation",
                lambda: setattr(controller, "observation_serial", serial + 1),
                "cancelled",
                1,
            ),
            (
                "target",
                lambda: setattr(controller, "capture_target", Mock()),
                "target-changed",
                0,
            ),
            (
                "window",
                lambda: setattr(
                    self.target, "last_window", SimpleNamespace(handle=43, process_id=8)
                ),
                "target-changed",
                0,
            ),
            (
                "settings",
                lambda: setattr(controller, "settings", AppSettings()),
                "target-changed",
                0,
            ),
            (
                "preloader",
                lambda: setattr(
                    controller,
                    "chapter_voice_preloader",
                    ChapterVoicePreloader((self.line,)),
                ),
                "target-changed",
                0,
            ),
            (
                "focus",
                lambda: setattr(self.target.is_focused, "return_value", False),
                "focus-lost",
                0,
            ),
        )
        for name, change, outcome, serial_increment in changes:
            with self.subTest(change=name):
                self.cancellation.clear()
                controller.capture_target = self.target
                controller.settings = settings
                controller.chapter_voice_preloader = preloader
                controller.observation_serial = serial
                controller.dialog_handler.reset_mock()
                controller.status_handler.reset_mock()
                controller.live_reader.invalidate_observation.reset_mock()
                self.target.last_window = window
                self.target.is_focused.return_value = True

                def read(*args):
                    args[4](result, settings.ocr_minimum_confidence)
                    controller.dialog_handler.assert_not_called()
                    change()
                    return None, ""

                self.read.side_effect = read
                with patch.object(
                    controller, "_resolve_initial_live_sequence_line"
                ) as matcher:
                    self.assertFalse(self.identify())

                self.assertEqual(
                    controller.observation_serial, serial + serial_increment
                )
                controller.live_reader.invalidate_observation.assert_not_called()
                matcher.assert_not_called()
                self.assert_outcome(outcome)
                if name != "token":
                    self.assertEqual(
                        controller.live_scope_identification_failure, outcome
                    )

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
