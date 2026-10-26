import json
import unittest
import zipfile
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from vntts import support
from vntts.settings import AppSettings
from vntts.support import (
    GenerationTimelineLog,
    RuntimeSupportLog,
    SupportBundleBuilder,
    preserve_previous_session,
)
from vntts.support_timeline_acquisition import (
    PREROUTE_DIAGNOSTIC_REASONS,
    TimelineAcquisition,
)


def read_journal(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class PrerouteSupportDiagnosticsTests(unittest.TestCase):
    def test_preroute_fields_survive_export_acquisition_and_generation_zero_restart(
        self,
    ):
        session, run, capture = [uuid4().hex for _ in range(3)]
        details = {
            "capture_id": capture,
            "frame_version": 4,
            "started_monotonic": 10.0,
            "completed_monotonic": 10.025,
            "frame_age_ms": 3.25,
            "ocr_call_ms": 20.5,
            "attempt_ms": 25.0,
            "span_ms": 150.0,
            "observations": 3,
            "committed_characters": 0,
            "latest_characters": 12,
            "stable_characters": 8,
            "transition_expected": False,
            "route_kind": "ordinary",
            "outcome": "completed",
            "error_type": "TimeoutError",
            "phase": "summary",
            "ocr_confidence": 0.25,
            "minimum_confidence": 0.8,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log = GenerationTimelineLog(path=root / "generation-timelines.json")
            journal = root / "timeline.jsonl"
            log.start_acquisition(journal)
            for index, reason in enumerate(sorted(PREROUTE_DIAGNOSTIC_REASONS)):
                self.assertTrue(
                    log.record(
                        "ocr-attempt"
                        if reason.startswith("ocr-")
                        else "tracker-admission",
                        0,
                        10.025 + index,
                        session_id=session,
                        run_id=run,
                        reason=reason,
                        text="PRIVATE dialogue",
                        exception="PRIVATE failure",
                        **details,
                    )
                )
            log.stop_acquisition()
            previous = preserve_previous_session(root)
            bundle = SupportBundleBuilder(
                AppSettings(ocr_diagnostics_directory=str(root)),
                RuntimeSupportLog(),
                dependency_probe=lambda: {},
                generation_timelines=log,
                previous_session=previous,
            ).build(root / "support.zip")
            with zipfile.ZipFile(bundle) as archive:
                exported = json.loads(archive.read("generation-timelines.json"))
                exported_previous = json.loads(archive.read("previous-session.json"))
            events = [
                event for event in read_journal(journal) if event["kind"] == "event"
            ]
        self.assertEqual(exported_previous, previous)
        self.assertEqual(exported["timelines"], log.snapshot())
        restored = previous["generation_timelines"][0]
        self.assertEqual((restored["generation"], restored["run_id"]), (0, run))
        self.assertEqual(restored["session_id"], session)
        self.assertEqual(len(restored["events"]), len(PREROUTE_DIAGNOSTIC_REASONS))
        self.assertEqual(
            {event["reason"] for event in events}, PREROUTE_DIAGNOSTIC_REASONS
        )
        for event in events + restored["events"]:
            for key, value in details.items():
                self.assertEqual(event[key], value)
        self.assertNotIn("PRIVATE", repr(exported) + repr(previous) + repr(events))

    def test_generation_zero_remains_restricted_to_preroute_stages(self):
        log = GenerationTimelineLog()
        self.assertFalse(log.record("capture", 0, 1.0))
        self.assertFalse(log.record("sequence-audio-auto", 0, 1.0))
        self.assertFalse(log.record("ocr-attempt", -1, 1.0))
        restored = support._sanitize_previous_timeline(
            {
                "generation": 0,
                "events": [
                    {"stage": "capture"},
                    {"stage": "tracker-admission", "reason": "waiting-stability"},
                ],
            }
        )
        self.assertEqual(
            restored["events"],
            [{"stage": "tracker-admission", "reason": "waiting-stability"}],
        )
        self.assertIsNone(
            support._sanitize_previous_timeline(
                {"generation": 0, "events": [{"stage": "capture"}]}
            )
        )

    def test_new_stages_reject_free_text_and_non_numeric_metrics_on_record_and_restart(
        self,
    ):
        log = GenerationTimelineLog()
        unsafe = {
            "reason": "PRIVATE dialogue",
            "error_type": "PRIVATE exception: /private/file",
            "route_kind": "PRIVATE route",
            "outcome": "PRIVATE outcome",
            "phase": "PRIVATE phase",
            "frame_age_ms": "PRIVATE metric",
            "ocr_call_ms": float("inf"),
            "attempt_ms": 10**400,
            "observations": True,
            "transition_expected": "PRIVATE transition",
            "text": "PRIVATE dialogue",
            "exception": "PRIVATE exception",
        }
        log.record("ocr-attempt", 0, 1.0, **unsafe)
        event = log.snapshot()[0]["events"][0]
        restored = support._sanitize_previous_timeline(
            {"generation": 0, "events": [{"stage": "ocr-attempt", **unsafe}]}
        )
        for key in unsafe:
            self.assertNotIn(key, event)
            self.assertNotIn(key, restored["events"][0])
        self.assertNotIn("PRIVATE", repr(event) + repr(restored))

    def test_runtime_worker_and_resolver_metrics_survive_safe_acquisition(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.jsonl"
            journal = TimelineAcquisition(path)
            for operation in ("worker_request_completed", "story-result-resolution"):
                journal.record(
                    {
                        "category": "ocr",
                        "operation": operation,
                        "worker_pid": 123,
                        "queue_wait_ms": 1.25,
                        "worker_call_ms": 12.5,
                        "resolver_ms": 3.75,
                        "outcome": "error",
                        "error_type": "TimeoutError",
                        "reason": "ocr-not-current",
                        "text": "PRIVATE dialogue",
                        "exception": "PRIVATE exception",
                    }
                )
            journal.stop()
            records = read_journal(path)
        events = records[1:-1]
        self.assertEqual(len(events), 2)
        self.assertEqual(
            {event["operation"] for event in events},
            {"worker_request_completed", "story-result-resolution"},
        )
        for event in events:
            for key, value in {
                "worker_pid": 123,
                "queue_wait_ms": 1.25,
                "worker_call_ms": 12.5,
                "resolver_ms": 3.75,
                "reason": "ocr-not-current",
            }.items():
                self.assertEqual(event[key], value)
        self.assertNotIn("PRIVATE", repr(records))

    def test_preroute_event_and_byte_caps_report_drops(self):
        for limits in ({"maximum_events": 2}, {"maximum_bytes": 4096}):
            with self.subTest(limits=limits), TemporaryDirectory() as directory:
                path = Path(directory) / "timeline.jsonl"
                log = GenerationTimelineLog()
                log.start_acquisition(path, **limits)
                for index in range(50):
                    log.record(
                        "ocr-attempt",
                        0,
                        float(index),
                        reason="ocr-not-current",
                        phase="summary",
                        attempt_ms=5.0,
                    )
                status = log.stop_acquisition()
                records = read_journal(path)
                self.assertLessEqual(
                    path.stat().st_size, limits.get("maximum_bytes", 8 * 1024 * 1024)
                )
            self.assertTrue(status["capped"])
            self.assertGreater(status["dropped_events"], 0)
            self.assertEqual(status["accepted_events"] + status["dropped_events"], 50)
            self.assertEqual(records[-1]["kind"], "stop")
            self.assertEqual(records[-1]["dropped_events"], status["dropped_events"])
            self.assertFalse(records[-1]["complete"])

    def test_simulated_ten_minutes_has_bounded_memory_and_acquisition_byte_budget(self):
        log = GenerationTimelineLog()
        session, run = uuid4().hex, uuid4().hex
        # The instant simulation measures storage limits, independent of writer scheduling.
        with (
            TemporaryDirectory() as directory,
            patch(
                "vntts.support_timeline_acquisition.Queue",
                side_effect=lambda maxsize: Queue(),
            ),
        ):
            timeline_path = Path(directory) / "timeline.jsonl"
            runtime_path = Path(directory) / "runtime.jsonl"
            log.start_acquisition(timeline_path)
            runtime = TimelineAcquisition(runtime_path)
            for frame_version in range(1, 600 * 7 + 1):
                started = (frame_version - 1) / 7
                completed = started + 0.05
                identity = {
                    "session_id": session,
                    "run_id": run,
                    "capture_id": uuid4().hex,
                    "frame_version": frame_version,
                }
                log.record(
                    "capture",
                    1,
                    started,
                    **identity,
                    started_monotonic=started - 0.005,
                    completed_monotonic=started,
                    success=True,
                    window_id=123,
                    owner_pid=456,
                    window_bounds=[0, 0, 1920, 1080],
                    capture_bounds=[0, 800, 1920, 280],
                    output_dimensions=[1920, 280],
                )
                log.record(
                    "ocr-attempt",
                    1,
                    completed,
                    **identity,
                    started_monotonic=started,
                    completed_monotonic=completed,
                    phase="summary",
                    reason="ocr-current",
                    outcome="accepted",
                    frame_age_ms=5.0,
                    ocr_call_ms=45.0,
                    attempt_ms=50.0,
                    route_kind="ocr",
                )
                if frame_version % 3 == 0:
                    log.record(
                        "stable-frame-gate",
                        1,
                        completed,
                        **identity,
                        fingerprint="a" * 16,
                        candidate_frames=frame_version % 70 + 1,
                        ready=False,
                        settled_ms=100.0,
                        visible=True,
                        focused=True,
                    )
                if frame_version % 70 in (1, 0):
                    summary = frame_version % 70 == 0
                    log.record(
                        "tracker-admission",
                        1,
                        completed,
                        **identity,
                        phase="summary" if summary else "started",
                        reason="waiting-stability",
                        route_kind="ocr",
                        observations=70 if summary else 1,
                        span_ms=10_000.0 if summary else 0.0,
                        committed_characters=0,
                        latest_characters=120,
                        stable_characters=110,
                        transition_expected=False,
                    )
                for operation, metrics in (
                    (
                        "worker_request_completed",
                        {"queue_wait_ms": 5.0, "worker_call_ms": 40.0},
                    ),
                    ("story-result-resolution", {"resolver_ms": 3.0}),
                ):
                    runtime.record(
                        {
                            "category": "ocr",
                            "operation": operation,
                            **identity,
                            "generation": 1,
                            "occurred_at": completed,
                            "worker_pid": 789,
                            "outcome": "completed",
                            **metrics,
                        }
                    )
            statuses = (log.stop_acquisition(), runtime.stop())
            expected_counts = (4200 * 2 + 1400 + 120, 4200 * 2)
            for path, status, expected_count in zip(
                (timeline_path, runtime_path), statuses, expected_counts, strict=True
            ):
                with self.subTest(journal=path.name):
                    self.assertFalse(status["writer_running"])
                    self.assertEqual(status["written_events"], expected_count)
                    self.assertEqual(status["dropped_events"], 0)
                    self.assertFalse(status["capped"])
                    self.assertLessEqual(expected_count, 20_000)
                    self.assertLessEqual(path.stat().st_size, 8 * 1024 * 1024)
                    records = read_journal(path)
                    self.assertEqual(records[-1]["kind"], "stop")
                    self.assertEqual(records[1]["frame_version"], 1)
                    self.assertEqual(records[-2]["frame_version"], 4200)
            retained = log.snapshot()[0]
            self.assertEqual(len(retained["events"]), 512)
            self.assertEqual(retained["dropped_events"], expected_counts[0] - 512)
            self.assertEqual(
                log.retention_summary()["recorded_events"], expected_counts[0]
            )

    def test_runs_and_sessions_keep_independent_generation_zero_evidence(self):
        log = GenerationTimelineLog()
        other = GenerationTimelineLog()
        session, other_session, run, other_run = [uuid4().hex for _ in range(4)]
        for session_id, run_id in (
            (session, run),
            (session, other_run),
            (other_session, run),
        ):
            log.record(
                "ocr-attempt",
                0,
                1.0,
                session_id=session_id,
                run_id=run_id,
                reason="ocr-not-current",
            )
        other.record("tracker-admission", 0, 2.0, reason="empty")
        self.assertEqual(len(log.snapshot()), 3)
        self.assertEqual(len(other.snapshot()), 1)
        self.assertEqual(
            {timeline["events"][0]["reason"] for timeline in log.snapshot()},
            {"ocr-not-current"},
        )
        self.assertEqual(other.snapshot()[0]["events"][0]["reason"], "empty")


if __name__ == "__main__":
    unittest.main()
