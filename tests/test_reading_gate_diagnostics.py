import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

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
        reader.candidate_frame_count = 2
        reader.latest_render_changed_at = 12.5
        reader.latest_render_owner = "private-owner"
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
        reader.auto_advance.assert_not_called()
        self.assertEqual(len(events), 1)
        args, event = events[0]
        self.assertEqual(args[:2], ("auto-advance-withheld", 3))
        self.assertEqual(event["reason"], "owned-frame-not-visible-and-stable")
        self.assertIs(event["routed_latest_equal"], False)
        self.assertIs(event["visible"], True)
        self.assertIs(event["completion_cue"], True)
        self.assertIs(event["focused"], True)
        self.assertEqual(event["frame_version"], 25)
        self.assertEqual(event["processed_frame_version"], 24)
        self.assertEqual(event["frame_route_epoch"], 4)
        self.assertEqual(event["candidate_frames"], 2)
        self.assertEqual(event["render_changed_at"], 12.5)
        self.assertEqual(event["render_quiet_minimum_ms"], 800)
        self.assertNotIn("private-owner", repr(event))

    def test_equivalent_frame_can_advance_without_moving_route_anchor(self):
        reader = self.reader(frame_equivalent=Mock(return_value=True))
        anchor = reader.routed_frame = object()
        latest = reader.latest_frame = object()
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
        reader.frame_equivalent.assert_called_once_with(anchor, latest)
        reader.auto_advance.assert_called_once()
        self.assertIs(reader.routed_frame, anchor)
        self.assertEqual(reader.routed_frame_fingerprint, b"routed")
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertIs(event["routed_frame_equivalent"], True)
        self.assertGreaterEqual(event["frame_equivalence_ms"], 0)

    def test_equivalence_cannot_accept_stale_or_unowned_frame(self):
        for mutation in (
            lambda reader: setattr(reader, "frame_version", 26),
            lambda reader: setattr(reader, "frame_route_epoch", 5),
            lambda reader: setattr(reader, "run_id", "new-run"),
            lambda reader: setattr(reader, "latest_frame", object()),
            lambda reader: setattr(reader, "routed_frame", object()),
            lambda reader: setattr(reader, "observation_uncertain", True),
            lambda reader: setattr(reader, "paused", True),
            lambda reader: setattr(reader, "emergency_stopped", True),
            lambda reader: setattr(reader, "is_shutting_down", True),
            lambda reader: setattr(reader, "current_chunk", object()),
            lambda reader: setattr(reader, "deferred_chunk", object()),
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
                reader.auto_advance.assert_not_called()

    def test_focus_lost_during_comparison_reports_focus_wait(self):
        reader = self.reader(focus_probe=Mock(side_effect=[True, False]))
        reader.routed_frame, reader.latest_frame = object(), object()
        reader.frame_equivalent = Mock(return_value=True)
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
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
                reader.auto_advance.assert_not_called()

    def test_stale_timer_keeps_current_timer_and_reports_both_serials(self):
        reader = self.reader()
        pending = reader.auto_advance_timer = Mock()
        reader._auto_advance_timer_serial = 6
        reader._run_auto_advance(3, 5)
        self.assertIs(reader.auto_advance_timer, pending)
        reader.auto_advance.assert_not_called()
        details = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(details["reason"], "stale-auto-advance-timer")
        self.assertEqual(details["timer_serial"], 5)
        self.assertEqual(details["current_timer_serial"], 6)

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
            events.append(details)

        reader.pipeline_event_handler = record
        with patch("vntts.live.Timer"), patch("vntts.live.monotonic") as clock:
            for now in (10.0, 10.1, 10.9, 11.0):
                clock.return_value = now
                reader._run_auto_advance(3)
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
                with patch("vntts.live.Timer"):
                    reader._run_auto_advance(3)
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
            log.record("capture", 4, 30.0)
            log.stop_acquisition()
            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(log.snapshot()[0]["generation"], 4)
        event = next(e for e in events if e.get("stage") == "auto-advance-withheld")
        self.assertEqual(event["reason"], "owned-frame-not-visible-and-stable")
        self.assertEqual(event["generation"], 3)
        self.assertEqual(event["run_id"], reader.run_id)
        self.assertIs(event["routed_latest_equal"], False)
        self.assertEqual(event["frame_version"], 25)
        self.assertEqual(event["processed_frame_version"], 24)
        self.assertEqual(event["render_changed_at"], 12.5)

    def test_journal_retains_known_reasons_and_hashes_owner_without_private_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reading.jsonl"
            journal = TimelineAcquisition(path)
            journal.record(
                {
                    "stage": "stable-frame-gate",
                    "reason": "prefix-render-not-quiet",
                    "owner": "PRIVATEowner",
                    "fingerprint": LiveDialogReader._privacy_safe_fingerprint(
                        "PRIVATEpixels"
                    ),
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
        reader._run_auto_advance(3)
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(event["reason"], "callback-blocked")
        self.assertNotIn("PRIVATE", repr(event))
        self.assertEqual(reader.failed_auto_advance_generation, 3)

    def prefix_controller(self, reader):
        from threading import RLock
        from types import SimpleNamespace

        from vntts.controller import AppController
        from vntts.live_sequence import StoryCursorState
        from vntts.settings import AppSettings

        controller = object.__new__(AppController)
        controller.story_cursor_lock = RLock()
        controller.settings = AppSettings(live_sequence_mode="audio-auto")
        controller.live_reader = reader
        controller.story_cursor = SimpleNamespace(
            state=StoryCursorState.LOCKED,
            current_event_id="event-1",
            current_event=SimpleNamespace(event_id="event-1"),
        )
        controller.sequence_prefix_confirmation_event_id = "event-1"
        controller._canonical_sequence_line_locked = Mock(
            return_value=SimpleNamespace(line_id="line-1")
        )
        reader.current_frame_render_quiet_ms = Mock(return_value=None)
        reader.bind_current_frame_route = Mock(return_value=True)
        reader.record_canonical_full_text = Mock()
        return controller

    def test_prefix_quiet_refusal_records_actual_pending_owner_outside_cursor_lock(
        self,
    ):
        reader = self.reader()
        controller = self.prefix_controller(reader)
        events = []

        def record(*args, **details):
            self.assertFalse(controller.story_cursor_lock._is_owned())
            self.assertFalse(reader.state_lock._is_owned())
            events.append(details)

        reader.pipeline_event_handler = record
        self.assertFalse(controller._confirm_sequence_render_completion())
        self.assertEqual(controller.sequence_prefix_confirmation_event_id, "event-1")
        reader.bind_current_frame_route.assert_not_called()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "prefix-render-not-quiet")
        self.assertIs(events[0]["prefix_confirmation_pending"], True)
        self.assertIs(events[0]["prefix_owner_current"], True)
        self.assertEqual(events[0]["event_id"], "event-1")
        self.assertEqual(events[0]["target_event_id"], "event-1")

    def test_prefix_owner_change_and_missing_observation_are_distinct_refusals(self):
        reader = self.reader()
        controller = self.prefix_controller(reader)
        controller.story_cursor.current_event_id = "event-2"
        self.assertFalse(controller._confirm_sequence_render_completion())
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(event["reason"], "prefix-owner-changed")
        self.assertIs(event["prefix_owner_current"], False)
        self.assertEqual(event["event_id"], "event-2")
        reader.current_frame_render_quiet_ms.assert_not_called()
        controller.story_cursor.current_event_id = "event-1"
        reader.current_frame_render_quiet_ms.return_value = 900
        reader.bind_current_frame_route.return_value = False
        self.assertFalse(controller._confirm_sequence_render_completion())
        event = reader.pipeline_event_handler.call_args.kwargs
        self.assertEqual(event["reason"], "prefix-observation-unavailable")
        self.assertEqual(event["render_quiet_ms"], 900)
        self.assertEqual(controller.sequence_prefix_confirmation_event_id, "event-1")

    def test_no_prefix_barrier_does_not_create_a_false_refusal_event(self):
        reader = self.reader()
        controller = self.prefix_controller(reader)
        controller.sequence_prefix_confirmation_event_id = None
        self.assertFalse(controller._confirm_sequence_render_completion())
        reader.pipeline_event_handler.assert_not_called()
        reader.current_frame_render_quiet_ms.assert_not_called()

    def test_prefix_success_still_closes_barrier_once_without_wait_event(self):
        reader = self.reader()
        controller = self.prefix_controller(reader)
        reader.current_frame_render_quiet_ms.return_value = 900
        self.assertTrue(controller._confirm_sequence_render_completion())
        self.assertIsNone(controller.sequence_prefix_confirmation_event_id)
        reader.record_canonical_full_text.assert_called_once_with(
            line_id="line-1", reason="owner-render-quiet", settled_ms=900
        )
        reader.pipeline_event_handler.assert_not_called()
