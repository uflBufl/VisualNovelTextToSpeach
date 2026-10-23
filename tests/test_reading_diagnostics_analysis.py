import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import numpy as np
import soundfile as sf

from vntts.reading_diagnostics_analysis import (
    RATE,
    _audio,
    _capture_clock,
    analyze_session,
    normalized_match,
)


class ReadingDiagnosticsAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_json(self, path, value):
        path.write_text(json.dumps(value))

    def journal(self, path, values):
        path.write_text("".join(json.dumps(value) + "\n" for value in values))

    def fixture(
        self,
        *,
        gap=0,
        owner=True,
        admission=True,
        source_lead_ms=0,
        admission_at=10.21,
        onset_seconds=0.25,
    ):
        source = (
            np.random.default_rng(42).normal(0, 0.1, RATE * 3 // 5).astype(np.float32)
        )
        source_path = self.root / "source.wav"
        sf.write(source_path, source, RATE, subtype="FLOAT")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        self.write_json(
            self.root / "sources.json",
            {
                "entries": [
                    dict(line_id="line-54", audio="source.wav", audio_sha256=digest)
                ]
            },
        )
        self.journal(
            self.root / "timeline.jsonl",
            [
                dict(
                    stage="route-decision",
                    effective_source="generated",
                    line_id="line-54",
                    playback_id="play-1",
                    occurred_at=10.1,
                ),
                dict(
                    stage="playback-completion",
                    playback_id="play-1",
                    occurred_at=10 + onset_seconds + source_lead_ms / 1000 + 0.7,
                    outcome="complete",
                ),
                dict(stage="playback-start", playback_id="play-1", occurred_at=10.2),
            ],
        )
        runtime = []
        if owner:
            runtime.append(
                dict(
                    operation="rust-worker-finished",
                    playback_id="play-1",
                    worker_pid=123,
                    occurred_at=10 + onset_seconds + source_lead_ms / 1000 + 0.7,
                    outcome="complete",
                )
            )
        if admission:
            runtime.append(
                dict(
                    operation="prepared-source",
                    playback_id="play-1",
                    source_sha256=digest,
                    source_audio_lead_ms=source_lead_ms,
                    occurred_at=admission_at,
                )
            )
        self.journal(self.root / "runtime.jsonl", runtime)
        output = np.zeros(48000 * max(2, int(onset_seconds) + 2), dtype=np.float32)
        original = np.repeat(source, 3)
        if gap:
            original = np.r_[
                original[: len(original) // 2],
                np.zeros(gap, dtype=np.float32),
                original[len(original) // 2 :],
            ]
        begin = round(48000 * (onset_seconds + source_lead_ms / 1000))
        output[begin : begin + len(original)] = original
        capture = self.root / "rust-123"
        capture.mkdir()
        sf.write(
            capture / "process.caf",
            np.column_stack((output, output)),
            48000,
            subtype="FLOAT",
        )
        metadata = dict(
            status="captured_unqualified",
            mechanism="CoreAudio explicit process tap",
            target={"pid": 123},
            microphone=False,
            global_fallback=False,
            errors=[],
            sample_rate=48000,
            channels=2,
            frames=len(output),
            peak=float(np.max(np.abs(output))),
        )
        self.write_json(capture / "session.json", metadata)
        self.journal(
            capture / "audio.jsonl",
            [
                dict(
                    kind="audio",
                    file_frame_start=frame,
                    frame_count=480,
                    sample_time=1000 + frame,
                    monotonic_s=10 + frame / 48000,
                )
                for frame in range(0, len(output), 480)
            ],
        )
        return capture, digest

    def test_generated_source_matches_exact_admitted_digest_and_true_owner(self):
        _capture, digest = self.fixture()
        # A later replacement for the same line must never replace the admitted WAV.
        entries = json.loads((self.root / "sources.json").read_text())["entries"]
        entries.append(
            dict(line_id="line-54", audio="missing-new.wav", audio_sha256="f" * 64)
        )
        self.write_json(self.root / "sources.json", {"entries": entries})
        report = analyze_session(self.root)
        self.assertEqual(report["qualification"], "digital-source-correlated")
        clip = report["clips"][0]
        self.assertEqual(clip["source_sha256"], digest)
        self.assertEqual(clip["status"], "correlated")
        self.assertEqual(clip["source_audio_lead_ms"], 0)
        self.assertAlmostEqual(clip["candidate_dispatch_to_onset_ms"], 50, places=2)
        self.assertEqual(report["unmatched_routes"], [])
        self.assertTrue((self.root / "analysis.json").is_file())

    def test_dispatch_and_prepared_source_phases_separate_announcement_delay(self):
        for announced in (False, True):
            with (
                self.subTest(announced=announced),
                tempfile.TemporaryDirectory() as folder,
            ):
                original_root = self.root
                self.root = Path(folder)
                try:
                    self.fixture(
                        admission_at=12.84 if announced else 10.21,
                        onset_seconds=2.925 if announced else 0.25,
                    )
                    clip = analyze_session(self.root)["clips"][0]
                    self.assertEqual(clip["status"], "correlated")
                    self.assertAlmostEqual(
                        clip["candidate_dispatch_to_prepared_source_admission_ms"],
                        2640 if announced else 10,
                        places=2,
                    )
                    self.assertAlmostEqual(
                        clip[
                            "candidate_prepared_source_admission_to_captured_onset_ms"
                        ],
                        85 if announced else 40,
                        places=2,
                    )
                    self.assertAlmostEqual(
                        clip["candidate_dispatch_to_onset_ms"],
                        2725 if announced else 50,
                        places=2,
                    )
                finally:
                    self.root = original_root

    def test_invalid_missing_or_conflicting_phase_times_remain_unknown(self):
        self.fixture()
        runtime_path, timeline_path = (
            self.root / "runtime.jsonl",
            self.root / "timeline.jsonl",
        )
        runtime = [json.loads(line) for line in runtime_path.read_text().splitlines()]
        timeline = [json.loads(line) for line in timeline_path.read_text().splitlines()]
        for timestamp in (
            None,
            True,
            False,
            float("nan"),
            float("inf"),
            "10.21",
            10.19,
            10.26,
        ):
            with self.subTest(admission_at=timestamp):
                runtime[-1]["occurred_at"] = timestamp
                self.journal(runtime_path, runtime)
                clip = analyze_session(self.root)["clips"][0]
                self.assertIsNone(
                    clip["candidate_dispatch_to_prepared_source_admission_ms"]
                )
                self.assertIsNone(
                    clip["candidate_prepared_source_admission_to_captured_onset_ms"]
                )
                self.assertAlmostEqual(
                    clip["candidate_dispatch_to_onset_ms"], 50, places=2
                )
        runtime[-1]["occurred_at"] = 10.21
        for extra_stage, timestamp in (
            ("admission", 10.21),
            ("admission", 10.22),
            ("dispatch", 10.2),
            ("dispatch", 10.22),
            ("dispatch", True),
            ("dispatch", float("nan")),
            ("dispatch", None),
        ):
            with self.subTest(extra_stage=extra_stage, timestamp=timestamp):
                self.journal(
                    runtime_path,
                    runtime + [dict(runtime[-1], occurred_at=timestamp)]
                    if extra_stage == "admission"
                    else runtime,
                )
                self.journal(
                    timeline_path,
                    timeline + [dict(timeline[-1], occurred_at=timestamp)]
                    if extra_stage == "dispatch"
                    else timeline,
                )
                clip = analyze_session(self.root)["clips"][0]
                self.assertIsNone(
                    clip["candidate_dispatch_to_prepared_source_admission_ms"]
                )
                self.assertIsNone(
                    clip["candidate_prepared_source_admission_to_captured_onset_ms"]
                )

    def test_admission_timestamp_requires_matching_playback_and_source_identity(self):
        _capture, digest = self.fixture(admission_at=None)
        path = self.root / "runtime.jsonl"
        runtime = [json.loads(line) for line in path.read_text().splitlines()]
        for playback_id, source_sha256 in (
            ("other-playback", digest),
            ("play-1", "f" * 64),
        ):
            with self.subTest(playback_id=playback_id, source_sha256=source_sha256):
                # Another playback bounds this attempt; another digest makes its source ambiguous.
                self.journal(
                    path,
                    runtime
                    + [
                        dict(
                            runtime[-1],
                            playback_id=playback_id,
                            source_sha256=source_sha256,
                            occurred_at=10.21,
                        )
                    ],
                )
                report = analyze_session(self.root)
                self.assertEqual(report["clips"], [])
                self.assertEqual(report["qualification"], "unqualified")
                if playback_id == "other-playback":
                    self.assertEqual(
                        report["unmatched_routes"][0]["reason"],
                        "partial-or-ambiguous-playback-lifetime",
                    )

    def test_intentional_source_lead_is_separate_and_contradictory_lead_stays_unknown(
        self,
    ):
        _capture, digest = self.fixture(source_lead_ms=150)
        clip = analyze_session(self.root)["clips"][0]
        self.assertEqual(clip["source_audio_lead_ms"], 150)
        self.assertAlmostEqual(clip["candidate_dispatch_to_onset_ms"], 200, places=2)
        self.assertAlmostEqual(
            clip["candidate_latency_after_source_wait_ms"], 50, places=2
        )
        with (self.root / "runtime.jsonl").open("a") as journal:
            journal.write(
                json.dumps(
                    dict(
                        operation="prepared-source",
                        playback_id="play-1",
                        source_sha256=digest,
                        source_audio_lead_ms=200,
                    )
                )
                + "\n"
            )
        clip = analyze_session(self.root)["clips"][0]
        self.assertIsNone(clip["source_audio_lead_ms"])
        self.assertIsNone(clip["candidate_latency_after_source_wait_ms"])

    def test_short_inserted_silence_leaves_local_shift_evidence_without_qualifying_quality(
        self,
    ):
        self.fixture(gap=192)
        report = analyze_session(self.root)
        self.assertEqual(report["qualification"], "unqualified")
        clip = report["clips"][0]
        self.assertEqual(clip["status"], "review-required")
        self.assertAlmostEqual(clip["window_shift_range_ms"], 4, places=2)
        self.assertEqual(len(report["unmatched_routes"]), 1)

    def test_failed_capture_missing_admission_and_missing_owner_remain_explicit(self):
        for case in ("failed", "missing-source", "missing-owner"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as folder:
                    original_root = self.root
                    self.root = Path(folder)
                    try:
                        capture, _digest = self.fixture(
                            owner=case != "missing-owner",
                            admission=case != "missing-source",
                        )
                        if case == "failed":
                            document = json.loads(
                                (capture / "session.json").read_text()
                            )
                            document["status"] = "failed"
                            self.write_json(capture / "session.json", document)
                        report = analyze_session(self.root)
                        self.assertEqual(report["qualification"], "unqualified")
                        self.assertEqual(len(report["unmatched_routes"]), 1)
                        if case == "missing-owner":
                            self.assertFalse(
                                report["clips"][0]["worker_identity_verified"]
                            )
                    finally:
                        self.root = original_root

    def test_later_valid_capture_does_not_hide_failed_segment_or_acquisition_gap(self):
        self.fixture()
        failed = self.root / "rust-456"
        failed.mkdir()
        self.write_json(
            failed / "session.json",
            {
                "status": "failed",
                "errors": ["Target process unavailable"],
                "target": {"pid": 456, "executable": "/worker", "start_seconds": 12},
            },
        )
        report = analyze_session(self.root)
        self.assertEqual(report["clips"][0]["status"], "correlated")
        self.assertEqual(report["qualification"], "unqualified")
        unavailable = next(
            item for item in report["captures"] if item["capture"] == "rust-456"
        )
        self.assertEqual(unavailable["native_status"], "failed")
        self.assertEqual(unavailable["native_errors"], ["Target process unavailable"])
        self.assertEqual(unavailable["target"]["start_seconds"], 12)
        gap = {"capture": "worker-limit", "status": "failed-or-partial"}
        self.write_json(
            self.root / "session.json", {"recording": {"acquisition_gaps": [gap]}}
        )
        report = analyze_session(self.root)
        self.assertEqual(report["acquisition_gaps"], [gap])
        self.assertEqual(report["qualification"], "unqualified")

    def test_reused_pid_requires_matching_worker_token_for_correlation(self):
        capture, _digest = self.fixture()
        capture.rename(self.root / "rust-123-2")
        runtime_path = self.root / "runtime.jsonl"
        events = [json.loads(line) for line in runtime_path.read_text().splitlines()]
        events[0]["worker_id"] = "retired-worker"
        self.journal(runtime_path, events)
        self.write_json(
            self.root / "session.json",
            {
                "recording": {
                    "captures": [
                        {
                            "directory": "rust-123-2",
                            "worker_identity": {"worker_id": "current-worker"},
                        }
                    ]
                }
            },
        )
        report = analyze_session(self.root)
        self.assertEqual(report["clips"], [])
        self.assertEqual(report["qualification"], "unqualified")
        events[0]["worker_id"] = "current-worker"
        self.journal(runtime_path, events)
        report = analyze_session(self.root)
        self.assertEqual(report["clips"][0]["status"], "correlated")
        self.assertEqual(report["qualification"], "digital-source-correlated")

    def replay_fixture(self, *, original_end=10.4, pause=False, run_id="run-1"):
        capture, digest = self.fixture(onset_seconds=1.25)
        timeline = [
            json.loads(line)
            for line in (self.root / "timeline.jsonl").read_text().splitlines()
        ]
        runtime = [
            json.loads(line)
            for line in (self.root / "runtime.jsonl").read_text().splitlines()
        ]
        for event in timeline + runtime:
            event["run_id"] = "run-1"
        timeline = [
            event for event in timeline if event.get("stage") != "playback-completion"
        ]
        if original_end is not None:
            timeline.append(
                dict(
                    stage="playback-completion",
                    playback_id="play-1",
                    run_id="run-1",
                    occurred_at=original_end,
                    outcome="interrupted",
                )
            )
        if original_end is None:
            runtime = runtime[1:]
        else:
            runtime[0]["occurred_at"] = original_end
            runtime[0]["outcome"] = "interrupted"
        if pause:
            timeline += [
                dict(
                    stage="reading-control",
                    action="pause",
                    run_id="run-1",
                    occurred_at=10.4,
                ),
                dict(
                    stage="reading-control",
                    action="resume",
                    run_id="run-1",
                    occurred_at=11.1,
                ),
            ]
        replay_id = "play-1" if run_id != "run-1" else "play-2"
        timeline += [
            dict(
                stage="route-decision",
                effective_source="generated",
                line_id="line-54",
                playback_id=replay_id,
                run_id=run_id,
                occurred_at=11.1,
            ),
            dict(
                stage="playback-start",
                playback_id=replay_id,
                run_id=run_id,
                occurred_at=11.2,
            ),
            dict(
                stage="playback-completion",
                playback_id=replay_id,
                run_id=run_id,
                occurred_at=11.95,
                outcome="complete",
            ),
        ]
        runtime += [
            dict(
                operation="prepared-source",
                playback_id=replay_id,
                run_id=run_id,
                source_sha256=digest,
                source_audio_lead_ms=0,
                occurred_at=11.21,
            ),
            dict(
                operation="rust-worker-finished",
                playback_id=replay_id,
                run_id=run_id,
                worker_pid=123,
                occurred_at=11.95,
                outcome="complete",
            ),
        ]
        self.journal(self.root / "timeline.jsonl", timeline)
        self.journal(self.root / "runtime.jsonl", runtime)
        return capture

    def test_interrupted_attempt_cannot_steal_later_identical_source_replay(self):
        self.replay_fixture()
        report = analyze_session(self.root)
        self.assertEqual(len(report["clips"]), 1)
        self.assertEqual(report["clips"][0]["playback_id"], "play-2")
        self.assertAlmostEqual(
            report["clips"][0]["candidate_dispatch_to_onset_ms"], 50, places=2
        )
        self.assertEqual(report["clips"][0]["status"], "correlated")
        self.assertEqual(report["qualification"], "unqualified")
        self.assertEqual(report["unmatched_routes"][0]["playback_id"], "play-1")

    def test_interrupted_prefix_has_own_onset_and_never_qualifies_continuity(self):
        capture = self.replay_fixture()
        output, rate = sf.read(capture / "process.caf", dtype="float32", always_2d=True)
        source, _ = sf.read(self.root / "source.wav", dtype="float32")
        output[12000:18720] = np.repeat(source[:2240], 3)[:, None]
        sf.write(capture / "process.caf", output, rate, subtype="FLOAT")
        clips = analyze_session(self.root)["clips"]
        partial = next(clip for clip in clips if clip["playback_id"] == "play-1")
        self.assertEqual(partial["status"], "review-required")
        self.assertLess(partial["matched_source_seconds"], 0.6)
        self.assertAlmostEqual(partial["candidate_dispatch_to_onset_ms"], 50, places=2)
        self.assertAlmostEqual(
            partial["candidate_dispatch_to_prepared_source_admission_ms"], 10, places=2
        )
        self.assertAlmostEqual(
            partial["candidate_prepared_source_admission_to_captured_onset_ms"],
            40,
            places=2,
        )

    def test_missing_terminal_is_bounded_by_next_admission_and_stays_unqualified(self):
        self.replay_fixture(original_end=None)
        report = analyze_session(self.root)
        self.assertEqual([clip["playback_id"] for clip in report["clips"]], ["play-2"])
        self.assertEqual(
            report["unmatched_routes"][0]["reason"],
            "partial-or-ambiguous-playback-lifetime",
        )

    def test_next_admission_bounds_attempt_even_when_its_dispatch_is_missing(self):
        self.replay_fixture(original_end=None)
        path = self.root / "timeline.jsonl"
        timeline = [json.loads(line) for line in path.read_text().splitlines()]
        self.journal(
            path,
            [
                event
                for event in timeline
                if not (
                    event.get("stage") == "playback-start"
                    and event.get("playback_id") == "play-2"
                )
            ],
        )
        report = analyze_session(self.root)
        self.assertEqual(report["clips"], [])
        self.assertEqual(report["qualification"], "unqualified")
        self.assertEqual(
            report["unmatched_routes"][0]["reason"],
            "partial-or-ambiguous-playback-lifetime",
        )

    def test_pause_resume_cannot_extend_attempt_search_into_replay(self):
        self.replay_fixture(original_end=None, pause=True)
        report = analyze_session(self.root)
        self.assertEqual([clip["playback_id"] for clip in report["clips"]], ["play-2"])
        self.assertEqual(report["qualification"], "unqualified")

    def test_reused_playback_id_in_another_run_keeps_its_own_timing(self):
        self.replay_fixture(run_id="run-2")
        report = analyze_session(self.root)
        self.assertEqual(len(report["clips"]), 1)
        clip = report["clips"][0]
        self.assertEqual(clip["run_id"], "run-2")
        self.assertAlmostEqual(clip["candidate_dispatch_to_onset_ms"], 50, places=2)
        self.assertAlmostEqual(
            clip["candidate_dispatch_to_prepared_source_admission_ms"], 10, places=2
        )

    def test_missing_or_invalid_lifetime_does_not_report_captured_latency(self):
        self.fixture()
        timeline_path = self.root / "timeline.jsonl"
        timeline = [json.loads(line) for line in timeline_path.read_text().splitlines()]
        runtime_path = self.root / "runtime.jsonl"
        runtime = [json.loads(line) for line in runtime_path.read_text().splitlines()]
        timeline = [
            event for event in timeline if event.get("stage") != "playback-completion"
        ]
        for timestamp in (None, True, "11", float("nan"), 10.1):
            with self.subTest(timestamp=timestamp):
                runtime[0]["occurred_at"] = timestamp
                self.journal(timeline_path, timeline)
                self.journal(runtime_path, runtime)
                report = analyze_session(self.root)
                self.assertEqual(report["clips"], [])
                self.assertEqual(report["qualification"], "unqualified")
                self.assertEqual(
                    report["unmatched_routes"][0]["reason"],
                    "ambiguous-playback-lifetime",
                )

    def test_real_terminal_schema_accepts_completed_timeline_and_complete_worker(self):
        self.fixture()
        path = self.root / "timeline.jsonl"
        timeline = [json.loads(line) for line in path.read_text().splitlines()]
        terminal = next(
            event for event in timeline if event.get("stage") == "playback-completion"
        )
        terminal["outcome"] = "completed"
        timeline += [
            dict(terminal, stage="playback-outcome", occurred_at=10.96),
            dict(stage="playback-completion", playback_id="play-1", occurred_at=10.97),
        ]
        self.journal(path, timeline)
        report = analyze_session(self.root)
        self.assertEqual(report["qualification"], "digital-source-correlated")
        self.assertEqual(report["clips"][0]["playback_lifetime"], "complete")

    def test_ambiguous_terminal_timestamp_never_claims_precise_candidate_latency(self):
        self.fixture()
        path = self.root / "timeline.jsonl"
        timeline = [json.loads(line) for line in path.read_text().splitlines()]
        self.journal(
            path,
            timeline
            + [
                dict(
                    stage="playback-completion", playback_id="play-1", occurred_at=None
                )
            ],
        )
        report = analyze_session(self.root)
        self.assertEqual(report["clips"], [])
        self.assertEqual(
            report["unmatched_routes"][0]["reason"], "ambiguous-playback-lifetime"
        )

    def test_pause_boundary_precedes_completion_even_when_full_source_fits(self):
        self.fixture()
        path = self.root / "timeline.jsonl"
        timeline = [json.loads(line) for line in path.read_text().splitlines()]
        self.journal(
            path,
            timeline
            + [dict(stage="reading-control", action="pause", occurred_at=10.9)],
        )
        report = analyze_session(self.root)
        self.assertEqual(report["qualification"], "unqualified")
        self.assertEqual(report["clips"][0]["status"], "review-required")
        self.assertEqual(
            report["clips"][0]["playback_lifetime"],
            "partial-or-ambiguous-playback-lifetime",
        )

    def test_malformed_run_identity_is_rejected_without_hashing_arbitrary_json(self):
        self.fixture()
        path = self.root / "timeline.jsonl"
        timeline = [json.loads(line) for line in path.read_text().splitlines()]
        for run_id in ([], {}, 42, True):
            with self.subTest(run_id=run_id):
                self.journal(path, [dict(event, run_id=run_id) for event in timeline])
                with self.assertRaisesRegex(ValueError, "diagnostic playback identity"):
                    analyze_session(self.root)

    def test_clock_continuity_rejects_missing_frames_and_audio_size_mismatch(self):
        capture, _digest = self.fixture()
        rows = [
            json.loads(line)
            for line in (capture / "audio.jsonl").read_text().splitlines()
        ]
        rows[1]["file_frame_start"] += 1
        self.journal(capture / "audio.jsonl", rows)
        with self.assertRaisesRegex(ValueError, "callback timeline"):
            _capture_clock(capture)

    def test_single_precision_fft_cannot_select_near_silence_over_the_source(self):
        rng = np.random.default_rng(73)
        source = rng.normal(0, 0.1, RATE // 10).astype(np.float32)
        audio = rng.normal(0, 1e-12, RATE // 2).astype(np.float32)
        offset = RATE // 4
        audio[offset : offset + len(source)] = source
        result = normalized_match(audio, source)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], offset)
        self.assertAlmostEqual(result[1], 1, places=12)

    def test_silent_and_constant_candidates_have_no_centered_correlation(self):
        source = np.random.default_rng(7).normal(0, 0.1, 1600).astype(np.float32)
        for level in (0, 1e-12, 0.125, 100):
            with self.subTest(level=level):
                self.assertIsNone(
                    normalized_match(np.full(8000, level, np.float32), source)
                )

    def test_returned_score_is_direct_centered_correlation_for_the_winning_window(self):
        rng = np.random.default_rng(14)
        source = rng.normal(0, 0.1, 1600).astype(np.float32)
        audio = rng.normal(0, 0.01, 8000).astype(np.float32)
        audio[4000:5600] += source
        position, score = normalized_match(audio, source)
        self.assertEqual(position, 4000)
        self.assertAlmostEqual(
            score, np.corrcoef(audio[4000:5600], source)[0, 1], places=12
        )
        self.assertLess(score, 1)
        self.assertGreater(score, 0.9)

    def test_nonfinite_silence_size_budget_and_shutdown_cancellation(self):
        self.assertIsNone(normalized_match(np.zeros(128), np.zeros(32)))
        with self.assertRaisesRegex(ValueError, "finite mono"):
            normalized_match(np.full(128, np.nan), np.ones(32))
        with patch("vntts.reading_diagnostics_analysis.MAX_MATCH_SAMPLES", 64):
            with self.assertRaisesRegex(ValueError, "memory budget"):
                normalized_match(np.ones(128), np.ones(32))
        self.fixture()
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            analyze_session(self.root, cancelled=cancelled)
        with self.assertRaises(InterruptedError):
            _audio(self.root / "source.wav", cancelled=cancelled)
