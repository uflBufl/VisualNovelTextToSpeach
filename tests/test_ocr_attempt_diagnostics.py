import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.dialog_capture import OCRUncertainError
from vntts.live import LiveDialogReader, _TrackerAdmissionDiagnostics
from vntts.live_tracking import IncrementalDialogTracker
from vntts.ocr import OCRPreviewCancelled, OCRResult


class OCRAttemptDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.clock = patch("vntts.live.monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.events = []
        self.focused = True
        self.reader = LiveDialogReader(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=Mock(),
            playback_executor=Mock(),
            capture_frame=Mock(),
            recognize_frame=lambda _: ("Narrator", "private dialogue"),
            prepare_chunk=Mock(),
            play_prepared=Mock(),
            report_error=Mock(),
            focus_probe=lambda: self.focused,
            pipeline_event_handler=lambda *a, **kw: self.events.append((a, kw)),
        )
        self.frame = SimpleNamespace(
            provenance=SimpleNamespace(
                capture_id="capture-one",
                completed_monotonic=9.5,
            )
        )
        self.reader.latest_frame = self.frame
        self.reader.latest_frame_fingerprint = "pixels"
        self.reader.latest_frame_visible = True
        self.policy = Mock(observe=Mock(return_value=0.1))

    def recognize(self):
        return self.reader._recognize_current_frame(
            self.frame,
            "legacy",
            "pixels",
            True,
            False,
            "pixels",
            self.policy,
            7,
            9.6,
        )

    def event(self):
        attempts = [e for e in self.events if e[0][0] == "ocr-attempt"]
        self.assertEqual(len(attempts), 1)
        return attempts[0]

    def test_current_attempt_before_first_generation_keeps_capture_age_and_no_text(
        self,
    ):
        def recognize(_):
            self.now += 0.25
            return "Narrator", "private dialogue"

        self.reader.recognize_frame = recognize
        self.assertEqual(self.recognize(), ("Narrator", "private dialogue"))
        args, event = self.event()
        self.assertEqual(args[1], 0)
        self.assertEqual(event["outcome"], "accepted")
        self.assertEqual(event["frame_version"], 7)
        self.assertEqual(event["capture_id"], "capture-one")
        self.assertEqual(event["frame_age_ms"], 500)
        self.assertEqual(event["ocr_call_ms"], 250)
        self.assertEqual(event["attempt_ms"], 250)
        self.assertNotIn("private dialogue", repr(event))
        self.assertNotIn("Narrator", repr(event))

    def test_late_results_keep_old_run_and_never_enter_tracker(self):
        for barrier in ("stop", "pause", "focus", "epoch", "run", "pixels"):
            with self.subTest(barrier=barrier):
                self.events.clear()
                self.reader.stop_event.clear()
                self.reader.paused = False
                self.focused = True
                self.reader.latest_frame_fingerprint = "pixels"
                run_id = self.reader.run_id

                def recognize(_):
                    if barrier == "stop":
                        self.reader.stop_event.set()
                    elif barrier == "pause":
                        self.reader.paused = True
                    elif barrier == "focus":
                        self.focused = False
                    elif barrier == "epoch":
                        self.reader.frame_route_epoch += 1
                    elif barrier == "run":
                        self.reader.run_id = "new-run"
                    else:
                        self.reader.latest_frame_fingerprint = "replacement"
                    return "Narrator", "private dialogue"

                self.reader.recognize_frame = recognize
                self.assertIsNone(self.recognize())
                event = self.event()[1]
                self.assertEqual(event["outcome"], "stale")
                self.assertEqual(event["reason"], "ocr-not-current")
                self.assertEqual(event["run_id"], run_id)

    def test_uncertainty_reports_confidence_but_stale_uncertainty_is_not_admitted(self):
        error = OCRUncertainError(
            OCRResult("Name", "private dialogue", 39, "sauvola", 1), 60
        )
        self.reader.recognize_frame = Mock(side_effect=error)
        self.reader.ocr_uncertain = Mock()
        self.assertIsNone(self.recognize())
        self.reader.ocr_uncertain.assert_called_once()
        event = self.event()[1]
        self.assertEqual(
            (event["outcome"], event["ocr_confidence"], event["minimum_confidence"]),
            ("uncertain", 39, 60),
        )
        self.events.clear()
        self.reader.ocr_uncertain.reset_mock()
        self.focused = False
        self.assertIsNone(self.recognize())
        self.reader.ocr_uncertain.assert_not_called()
        self.assertEqual(self.event()[1]["outcome"], "stale")

    def test_cancel_and_error_always_record_one_terminal_outcome(self):
        for error, outcome in [
            (OCRPreviewCancelled(), "cancelled"),
            (RuntimeError("private error text"), "error"),
        ]:
            with self.subTest(outcome=outcome):
                self.events.clear()
                self.reader.recognize_frame = Mock(side_effect=error)
                if outcome == "cancelled":
                    self.assertIsNone(self.recognize())
                else:
                    with self.assertRaises(RuntimeError) as caught:
                        self.recognize()
                    self.assertIs(caught.exception, error)
                event = self.event()[1]
                self.assertEqual(event["outcome"], outcome)
                self.assertEqual(event["error_type"], type(error).__name__)
                self.assertNotIn("private error text", repr(event))

    def test_broken_diagnostics_and_error_reporter_preserve_result_and_error(self):
        self.reader.pipeline_event_handler = Mock(side_effect=ValueError("sink"))
        self.reader.report_error = Mock(side_effect=ValueError("reporter"))
        self.assertEqual(self.recognize(), ("Narrator", "private dialogue"))
        error = RuntimeError("recognition failed")
        self.reader.recognize_frame = Mock(side_effect=error)
        with self.assertRaises(RuntimeError) as caught:
            self.recognize()
        self.assertIs(caught.exception, error)

    def test_unavailable_capture_metadata_does_not_mask_result_or_error(self):
        class BrokenFrame:
            @property
            def provenance(self):
                raise ValueError("unavailable")

        for frame in (Mock(), BrokenFrame()):
            with self.subTest(frame=type(frame).__name__):
                self.frame = frame
                self.events.clear()
                self.reader.recognize_frame = Mock(return_value=("Narrator", "text"))
                self.assertEqual(self.recognize(), ("Narrator", "text"))
                self.assertIsNone(self.event()[1]["capture_id"])
                self.events.clear()
                failure = RuntimeError("recognition error")
                self.reader.recognize_frame = Mock(side_effect=failure)
                with self.assertRaises(RuntimeError) as caught:
                    self.recognize()
                self.assertIs(caught.exception, failure)
                self.assertEqual(self.event()[1]["outcome"], "error")

    def test_custom_tracker_and_failing_diagnostic_sink_cannot_abort_admission(self):
        for tracker in (Mock(), SimpleNamespace(admission_reason="empty")):
            reporter = _TrackerAdmissionDiagnostics(
                Mock(side_effect=ValueError("sink"))
            )
            reporter.observe(tracker, "run", "visual", self.frame, 7, "visual-only")
            reporter.finish()
        tracker = IncrementalDialogTracker()
        reporter.observe(tracker, "run", "ocr", self.frame, 7)
        reporter.observe(tracker, "run", "ocr", self.frame, 8)
        reporter.finish()

    def test_tracker_wait_coalesces_cached_observations_and_flushes_once(self):
        tracker = IncrementalDialogTracker(clock=lambda: self.now)
        tracker.observe("Narrator", "private dialogue")
        reporter = _TrackerAdmissionDiagnostics(self.reader._report_pipeline_event)
        for i in range(2000):
            self.now = 10 + i * 0.1
            reporter.observe(tracker, self.reader.run_id, "cached", self.frame, i)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1]["phase"], "started")
        reporter.finish()
        reporter.finish()
        self.assertEqual(len(self.events), 2)
        summary = self.events[1][1]
        self.assertEqual(summary["observations"], 2000)
        self.assertAlmostEqual(summary["span_ms"], 199900)
        self.assertNotIn("private dialogue", repr(summary))
        self.assertEqual(summary["frame_version"], 1999)

    def test_tracker_run_and_route_changes_are_separate_spans(self):
        tracker = IncrementalDialogTracker(clock=lambda: self.now)
        tracker.observe("Narrator", "private dialogue")
        reporter = _TrackerAdmissionDiagnostics(self.reader._report_pipeline_event)
        for run_id, route in [
            ("first", "ocr"),
            ("first", "cached"),
            ("second", "cached"),
        ]:
            reporter.observe(tracker, run_id, route, self.frame, 7)
        self.assertEqual(len(self.events), 3)
        self.assertEqual(
            [e[1]["run_id"] for e in self.events], ["first", "first", "second"]
        )

    def test_loop_stop_flushes_repeated_wait_without_scheduling_new_speech(self):
        tracker = IncrementalDialogTracker(clock=lambda: self.now)
        self.reader.tracker_factory = lambda **_: tracker
        self.reader.adaptive_policy_factory = lambda **_: self.policy

        def consume(stop, current, policy, admission):
            current.observe("Narrator", "private dialogue")
            admission.observe(current, self.reader.run_id, "cached", self.frame, 7)
            self.now += 1
            admission.observe(current, self.reader.run_id, "cached", self.frame, 8)
            stop.set()
            self.reader.stop_event.set()

        self.reader._consume_ocr_frames = consume
        self.reader._schedule_tracked_chunks = Mock()
        self.reader._run_ocr(Event())
        self.reader._schedule_tracked_chunks.assert_not_called()
        self.assertEqual(self.events[-1][1]["phase"], "summary")
        self.assertEqual(self.events[-1][1]["observations"], 2)
