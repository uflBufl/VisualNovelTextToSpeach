import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.test_live import process_advance_ocr
from vntts.live import AutoAdvanceAttempt, LiveDialogReader
from vntts.support import GenerationTimelineLog
from vntts.support_timeline_acquisition import TimelineAcquisition


class ReadingGateDiagnosticsTest(unittest.TestCase):
    def reader(self, **overrides):
        options = dict(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=Mock(),
            playback_executor=Mock(),
            capture_frame=Mock(),
            recognize_frame=Mock(),
            prepare_chunk=Mock(),
            play_prepared=Mock(),
            report_error=Mock(),
            auto_advance=Mock(return_value=True),
            require_visible_auto_advance=True,
            pipeline_event_handler=Mock(),
        )
        options.update(overrides)
        reader = LiveDialogReader(**options)
        reader.active_generation = reader.dialog_ready_generation = 3
        reader.latest_frame_visible = reader.latest_frame_complete = True
        reader.latest_frame_fingerprint = b"latest"
        reader.routed_frame_fingerprint = b"routed"
        reader.frame_version = 25
        reader.processed_frame_version = 24
        reader.frame_route_epoch = 4
        reader.last_observation = ("Alice", "Hello.")
        return reader

    def test_exact_visual_refusal_has_branch_state_without_changing_dispatch(self):
        events = []
        reader = self.reader(
            pipeline_event_handler=lambda *args, **details: events.append(
                (args, details)
            )
        )
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            process_advance_ocr(reader)
        reader.auto_advance.assert_not_called()
        events = [
            event
            for event in events
            if event[0][0] == "auto-advance-withheld"
            and event[1].get("reason") != "schedule-precondition"
        ]
        self.assertEqual(len(events), 1)
        args, event = events[0]
        self.assertEqual(args[:2], ("auto-advance-withheld", 3))
        self.assertEqual(event["reason"], "owned-frame-not-visible-and-stable")
        self.assertIs(event["routed_latest_equal"], True)
        self.assertIs(event["visible"], True)
        self.assertIs(event["completion_cue"], True)
        self.assertIs(event["focused"], True)
        self.assertEqual(event["frame_version"], 26)
        self.assertEqual(event["processed_frame_version"], 24)
        self.assertEqual(event["frame_route_epoch"], 5)

    def test_fresh_equivalent_ocr_compares_original_request_anchor(self):
        reader = self.reader(frame_equivalent=Mock(return_value=True))
        anchor = reader.routed_frame = object()
        latest = reader.latest_frame = object()
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            process_advance_ocr(reader)
        reader.frame_equivalent.assert_called_once_with(anchor, latest)
        reader.auto_advance.assert_called_once()
        self.assertIs(reader.routed_frame, latest)
        self.assertEqual(reader.routed_frame_fingerprint, b"latest")
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertIs(event["routed_frame_equivalent"], True)
        self.assertGreaterEqual(event["frame_equivalence_ms"], 0)

    def test_equivalence_cannot_accept_stale_or_unowned_frame(self):
        for mutation in (
            lambda reader: setattr(reader, "frame_version", reader.frame_version + 1),
            lambda reader: setattr(
                reader, "frame_route_epoch", reader.frame_route_epoch + 1
            ),
            lambda reader: setattr(reader, "run_id", "new-run"),
            lambda reader: setattr(reader, "stable_frame_owner", lambda: "new-owner"),
            lambda reader: setattr(reader, "latest_frame", object()),
            lambda reader: setattr(reader, "observation_uncertain", True),
            lambda reader: setattr(reader, "paused", True),
            lambda reader: setattr(reader, "emergency_stopped", True),
            lambda reader: setattr(reader, "is_shutting_down", True),
            lambda reader: setattr(reader, "current_chunk", object()),
            lambda reader: reader.deferred_chunks.append(object()),
            lambda reader: setattr(reader, "speech_futures", {object()}),
            lambda reader: setattr(reader, "dialog_ready_generation", None),
            lambda reader: setattr(reader, "suppressed_generation", 3),
            lambda reader: reader.stop_event.set(),
            lambda reader: setattr(reader, "focus_probe", lambda: False),
        ):
            with self.subTest(mutation=mutation):
                reader = self.reader()
                reader.routed_frame, reader.latest_frame = object(), object()

                def compare(*frames):
                    self.assertFalse(reader.state_lock._is_owned())
                    mutation(reader)
                    return True

                reader.frame_equivalent = compare
                with patch("vntts.live.Timer"):
                    reader._run_auto_advance(3)
                    process_advance_ocr(reader)
                reader.auto_advance.assert_not_called()

    def test_failed_key_callback_is_not_retried_by_later_ocr(self):
        error = RuntimeError("ambiguous key failure")
        reader = self.reader(
            frame_equivalent=Mock(return_value=True),
            auto_advance=Mock(side_effect=error),
        )
        reader.routed_frame, reader.latest_frame = object(), object()
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            process_advance_ocr(reader)
            process_advance_ocr(reader)
            process_advance_ocr(reader)
        reader.auto_advance.assert_called_once()
        reader.report_error.assert_called_once_with(error)
        self.assertIsNone(reader._advance_requested_generation)
        self.assertEqual(reader.failed_auto_advance_generation, 3)

    def test_focus_lost_during_comparison_reports_focus_wait(self):
        reader = self.reader()
        reader.routed_frame, reader.latest_frame = object(), object()

        def lose_focus(*_frames):
            reader.focus_probe = lambda: False
            return True

        reader.frame_equivalent = lose_focus
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            process_advance_ocr(reader)
        reader.auto_advance.assert_not_called()
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(event["reason"], "game-focus-lost-before-dispatch")
        self.assertIs(event["focused"], False)

    def test_changed_glyphs_and_comparison_errors_keep_visual_barrier(self):
        for comparator in (Mock(return_value=False), Mock(side_effect=ValueError)):
            with self.subTest(comparator=comparator):
                reader = self.reader(frame_equivalent=comparator)
                reader.routed_frame, reader.latest_frame = object(), object()
                with patch("vntts.live.Timer"):
                    reader._run_auto_advance(3)
                    process_advance_ocr(reader)
                reader.auto_advance.assert_not_called()

    def test_stale_timer_keeps_current_timer_without_requesting_ocr(self):
        reader = self.reader()
        pending = reader.auto_advance_timer = Mock()
        reader._auto_advance_timer_serial = 6
        reader._run_auto_advance(3, 5)
        self.assertIs(reader.auto_advance_timer, pending)
        reader.auto_advance.assert_not_called()
        self.assertIsNone(reader._advance_requested_generation)

    def test_scheduler_barrier_reports_why_no_timer_was_created(self):
        reader = self.reader()
        reader.observation_uncertain = True
        with patch("vntts.live.Timer") as timer:
            reader._maybe_auto_advance()
        timer.assert_not_called()
        reader.auto_advance.assert_not_called()
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(event["reason"], "schedule-precondition")
        self.assertIs(event["observation_uncertain"], True)
        self.assertEqual(event["dialog_ready_generation"], 3)

    def test_repeated_wait_is_sampled_once_per_second_and_outside_state_lock(self):
        reader = self.reader()
        events = []

        def record(*args, **details):
            self.assertFalse(reader.state_lock._is_owned())
            if (
                args[0] == "auto-advance-withheld"
                and details.get("reason") != "schedule-precondition"
            ):
                events.append(details)

        reader.pipeline_event_handler = record
        with patch("vntts.live.Timer"), patch("vntts.live.monotonic") as clock:
            for now in (10.0, 10.1, 10.9, 11.0):
                clock.return_value = now
                reader._run_auto_advance(3)
                process_advance_ocr(reader)
        self.assertEqual(len(events), 2)
        reader.auto_advance.assert_not_called()

    def test_typed_control_and_prefix_barriers_are_preserved(self):
        for refusal, reason in (
            ("reading-control-barrier", "reading-control-barrier"),
            ("visual-wait", "canonical-full-text-not-confirmed"),
        ):
            with self.subTest(refusal=refusal):
                reader = self.reader(
                    auto_advance=Mock(return_value=AutoAdvanceAttempt(False, refusal))
                )
                reader.routed_frame_fingerprint = reader.latest_frame_fingerprint
                reader.frame_equivalent = lambda *_: True
                with patch("vntts.live.Timer"):
                    reader._run_auto_advance(3)
                    process_advance_ocr(reader)
                self.assertIsNone(reader.pending_auto_advance_generation)
                self.assertIsNone(reader.failed_auto_advance_generation)
                self.assertEqual(
                    reader.pipeline_event_handler.call_args.kwargs["reason"], reason
                )

    def test_visual_wait_evidence_survives_ordinary_timeline_eviction(self):
        log = GenerationTimelineLog(maximum_entries=1)
        reader = self.reader(pipeline_event_handler=log.record)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reading.jsonl"
            log.start_acquisition(path)
            with patch("vntts.live.Timer"):
                reader._run_auto_advance(3)
                process_advance_ocr(reader)
            log.record("capture", 4, 30.0)
            log.stop_acquisition()
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(log.snapshot()[0]["generation"], 4)
        event = next(
            e
            for e in events
            if e.get("stage") == "auto-advance-withheld"
            and e.get("reason") == "owned-frame-not-visible-and-stable"
        )
        self.assertEqual(event["reason"], "owned-frame-not-visible-and-stable")
        self.assertEqual(event["generation"], 3)
        self.assertEqual(event["run_id"], reader.run_id)
        self.assertIs(event["routed_latest_equal"], True)
        self.assertEqual(event["frame_version"], 26)
        self.assertEqual(event["processed_frame_version"], 24)

    def test_journal_retains_known_reasons_and_hashes_owner_without_private_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reading.jsonl"
            journal = TimelineAcquisition(path)
            journal.record(
                {
                    "stage": "stable-frame-gate",
                    "reason": "prefix-render-not-quiet",
                    "owner": "PRIVATEowner",
                    "fingerprint": sha256(b"PRIVATEpixels").hexdigest()[:16],
                    "candidate_frames": 2,
                    "text": "PRIVATEdialogue",
                }
            )
            journal.record(
                {
                    "reason": "PRIVATEreason",
                    "fingerprint": "PRIVATEfingerprint",
                    "source_sha256": "PRIVATEsource",
                    "PRIVATE numeric key": 12,
                    "PRIVATEkey": 13,
                    "text": 14,
                    "path": "PRIVATEpath",
                }
            )
            journal.stop()
            raw = path.read_text()
            events = [
                json.loads(line)
                for line in raw.splitlines()
                if json.loads(line)["kind"] == "event"
            ]
        self.assertNotIn("PRIVATE", raw)
        self.assertEqual(events[0]["reason"], "prefix-render-not-quiet")
        self.assertEqual(events[0]["owner"], sha256(b"PRIVATEowner").hexdigest()[:16])
        self.assertEqual(events[0]["candidate_frames"], 2)
        self.assertNotIn("reason", events[1])
        self.assertNotIn("text", events[1])

    def test_queue_cancellation_and_ocr_uncertainty_keep_causal_evidence(self):
        log = GenerationTimelineLog()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.jsonl"
            log.start_acquisition(path)
            for stage, reason in (
                ("observation-invalidated", "ocr-uncertain"),
                ("queue-cancelled", "clear-queue"),
                ("queue-cancelled", "emergency-stop"),
                ("queue-cancelled", "explicit-sequence-replacement"),
            ):
                log.record(
                    stage,
                    3,
                    12.5,
                    reason=reason,
                    ocr_confidence=42.5,
                    minimum_confidence=60,
                    playback_id="prepared-7",
                    current_chunk_present=True,
                    speech_future_count=2,
                    text="PRIVATEdialogue",
                    worker_path="PRIVATEpath",
                )
            log.stop_acquisition()
            raw = path.read_text()
            events = [
                json.loads(line)
                for line in raw.splitlines()
                if json.loads(line)["kind"] == "event"
            ]
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["stage"], "observation-invalidated")
        self.assertEqual(events[0]["reason"], "ocr-uncertain")
        self.assertEqual(events[0]["ocr_confidence"], 42.5)
        self.assertEqual(events[0]["minimum_confidence"], 60)
        self.assertEqual(events[0]["playback_id"], "prepared-7")
        self.assertEqual(events[0]["speech_future_count"], 2)
        self.assertIs(events[0]["current_chunk_present"], True)
        self.assertEqual(
            [event["reason"] for event in events[1:]],
            ["clear-queue", "emergency-stop", "explicit-sequence-replacement"],
        )
        self.assertNotIn("PRIVATE", raw)

    def test_native_and_generation_lanes_keep_only_bounded_schema(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.jsonl"
            journal = TimelineAcquisition(path)
            journal.record(
                {
                    "category": "audio",
                    "operation": "rust-worker-finished",
                    "backend": "rust",
                    "callback_count": 10,
                    "callback_timing_max_gap_seconds": 0.3,
                    "dac_timing_max_offset_position_frames": 20,
                    "source_sha256": "a" * 64,
                    "model": "PRIVATE-model",
                    "device_name": "PRIVATE-device",
                }
            )
            journal.record(
                {
                    "category": "generation",
                    "operation": "generation-published",
                    "provider": "moss-tts",
                    "completion": "complete",
                    "cache_source": "fresh-generation",
                    "chunk_count": 4,
                    "sample_count": 48000,
                }
            )
            journal.stop()
            raw = path.read_text()
            events = [
                json.loads(line)
                for line in raw.splitlines()
                if json.loads(line)["kind"] == "event"
            ]
        self.assertNotIn("PRIVATE", raw)
        self.assertEqual(events[0]["callback_timing_max_gap_seconds"], 0.3)
        self.assertEqual(events[0]["source_sha256"], "a" * 64)
        self.assertEqual(events[1]["provider"], "moss-tts")
        self.assertEqual(events[1]["chunk_count"], 4)

    def test_callback_refusal_cannot_inject_private_reason_into_timeline(self):
        reader = self.reader(
            auto_advance=Mock(return_value=AutoAdvanceAttempt(False, "PRIVATEreason"))
        )
        reader.routed_frame_fingerprint = reader.latest_frame_fingerprint
        reader.frame_equivalent = lambda *_: True
        reader._run_auto_advance(3)
        process_advance_ocr(reader)
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(event["reason"], "callback-blocked")
        self.assertNotIn("PRIVATE", repr(event))
        self.assertEqual(reader.failed_auto_advance_generation, 3)
