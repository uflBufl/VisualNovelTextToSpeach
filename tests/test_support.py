import json
import unittest
import zipfile
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from vntts.diagnostics import DiagnosticSnapshot
from vntts.settings import AppSettings
from vntts.support import (
    AudioLifecycleLog,
    GameImportLog,
    GenerationTimelineLog,
    NativeSpeechLog,
    PerformanceLog,
    PregenerationSupportState,
    RuntimeSupportLog,
    SupportBundleBuilder,
    collect_active_content_identity,
    collect_build_identity,
    collect_ocr_metrics,
    correlate_active_preparation,
    native_speech_context,
    preserve_previous_session,
    record_game_import,
    record_native_speech,
    redact_text,
    sanitize_event,
    sanitize_settings,
    sequence_timeline_stages,
)


class NativeSpeechLogTest(unittest.TestCase):
    def test_bounded_sanitized_log_survives_restart(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "native-speech.log"
            log = NativeSpeechLog(maximum_entries=2, path=path)
            log.record(
                {
                    "operation": "server-start",
                    "outcome": "complete",
                    "native_version": "0.3.0",
                    "text": "PRIVATE",
                }
            )
            log.record(
                {
                    "operation": "request-start",
                    "attempt_id": "attempt-a",
                    "request_key": "request-a",
                    "reason": str(Path.home() / "private" / "server.log"),
                }
            )
            restarted = NativeSpeechLog(maximum_entries=2, path=path)
            persisted = path.read_text(encoding="utf-8")

        report = restarted.report()
        self.assertEqual(report["total_events"], 2)
        self.assertEqual(report["latest_runtime"]["native_version"], "0.3.0")
        self.assertEqual(report["active_requests"][0]["attempt_id"], "attempt-a")
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertNotIn(str(Path.home()), persisted)

    def test_rollover_preserves_session_counts_and_runtime(self):
        log = NativeSpeechLog(maximum_entries=2)
        with patch("vntts.support.native_speech_log", log):
            record_native_speech(operation="server-start", native_version="0.3.0")
            with native_speech_context.set({"attempt_id": "a", "logical_key": "key"}):
                record_native_speech(
                    operation="fresh-generation",
                    outcome="complete",
                    request_s=12,
                    text="PRIVATE",
                    reference_path="/secret/location.wav",
                )
                record_native_speech(
                    operation="preview-outcome",
                    outcome="quality_failed",
                    stage="quality",
                    reason="silence",
                )
            record_native_speech(operation="cached-wav", outcome="complete")
        report = log.report()
        self.assertEqual(report["total_events"], 4)
        self.assertEqual(report["dropped_events"], 2)
        self.assertEqual(report["latest_runtime"]["native_version"], "0.3.0")
        self.assertEqual(report["outcomes"]["fresh-generation/complete"], 1)
        self.assertEqual(report["outcomes"]["preview-outcome/quality_failed"], 1)
        self.assertEqual(report["request_seconds"]["fresh-generation/complete"], 12)
        self.assertEqual(report["events"][0]["native"]["attempt_id"], "a")
        self.assertNotIn("attempt_id", report["events"][1]["native"])
        self.assertNotIn("PRIVATE", str(report))
        self.assertNotIn("/secret", str(report))

    def test_missing_git_does_not_prevent_build_report(self):
        with patch("vntts.support.subprocess.run", side_effect=FileNotFoundError):
            report = collect_build_identity()
        self.assertIsNone(report["git_commit"])
        self.assertIn("psutil", report["versions"])


class GenerationTimelineLogTest(unittest.TestCase):
    def test_keeps_restarted_reader_generations_in_separate_timelines(self):
        timelines = GenerationTimelineLog()
        first_session, second_session = uuid4().hex, uuid4().hex

        timelines.record("capture", 1, 1.0, session_id=first_session)
        timelines.record("capture", 1, 2.0, session_id=second_session)

        snapshot = timelines.snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertEqual(
            {timeline["session_id"] for timeline in snapshot},
            {first_session, second_session},
        )
        self.assertEqual([timeline["generation"] for timeline in snapshot], [1, 1])

    def test_keeps_one_ordered_privacy_safe_timeline_per_generation(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "timelines.json"
            timelines = GenerationTimelineLog(path=path)

            timelines.record("stable-text", 3, 10.2)
            timelines.record("capture", 3, 10.0)
            timelines.record("ocr", 3, 10.1)
            timelines.record(
                "route-decision",
                3,
                10.3,
                effective_source="moss-tts:fresh-generation",
                line_id="reverse1999:3",
                private_text="must not be retained",
            )
            timelines.record("key-dispatch", 3, 11.0, attempt=1)
            timelines.record("auto-advance-timeout", 3, 20.0, attempt=1)

            snapshot = timelines.snapshot()
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["generation"], 3)
        self.assertEqual(
            [event["stage"] for event in snapshot[0]["events"]],
            [
                "capture",
                "ocr",
                "stable-text",
                "route-decision",
                "key-dispatch",
                "auto-advance-timeout",
            ],
        )
        self.assertEqual(snapshot[0]["events"][1]["elapsed_ms"], 100.0)
        self.assertNotIn("private_text", str(snapshot))
        self.assertEqual(persisted["timelines"], snapshot)

    def test_reports_latency_component_percentiles_without_dialogue_text(self):
        timelines = GenerationTimelineLog()
        for generation, value in enumerate((100, 200, 300), 1):
            timelines.record(
                "first-pcm",
                generation,
                float(generation),
                from_text_visible_ms=value,
                from_playback_started_ms=value / 10,
            )
        timelines.record(
            "sequence-successor-prefetch",
            3,
            3.5,
            prefetch_ms=12,
        )
        timelines.record(
            "canonical-full-text",
            3,
            3.25,
            first_pcm_before_canonical_full_ms=40,
        )
        timelines.record(
            "playback-outcome",
            3,
            3.75,
            source_audio_lead_ms=1600,
        )
        timelines.record(
            "speaker-announcement-outcome",
            3,
            3.9,
            playback_ms=425,
        )

        summary = timelines.latency_summary()

        self.assertEqual(
            summary["visible_to_first_pcm_ms"],
            {"samples": 3, "p50_ms": 200.0, "p95_ms": 290.0},
        )
        self.assertEqual(
            summary["playback_to_first_pcm_ms"],
            {"samples": 3, "p50_ms": 20.0, "p95_ms": 29.0},
        )
        self.assertEqual(
            summary["successor_preflight_ms"],
            {"samples": 1, "p50_ms": 12.0, "p95_ms": 12.0},
        )
        self.assertEqual(
            summary["first_pcm_before_canonical_full_ms"],
            {"samples": 1, "p50_ms": 40.0, "p95_ms": 40.0},
        )
        self.assertEqual(
            summary["source_audio_lead_ms"],
            {"samples": 1, "p50_ms": 1600.0, "p95_ms": 1600.0},
        )
        self.assertEqual(
            summary["speaker_announcement_playback_ms"],
            {"samples": 1, "p50_ms": 425.0, "p95_ms": 425.0},
        )

    def test_merges_details_when_a_stage_is_reported_twice(self):
        timelines = GenerationTimelineLog()

        timelines.record("playback-completion", 1, 3.0, underflowed=True)
        timelines.record("playback-completion", 1, 3.1)

        event = timelines.snapshot()[0]["events"][0]
        self.assertTrue(event["underflowed"])

    def test_preserves_playback_completion_audio_metrics(self):
        timelines = GenerationTimelineLog()

        timelines.record(
            "playback-completion",
            1,
            3.0,
            source_sample_rate=24_000,
            playback_sample_rate=48_000,
            sample_count=960_000,
            expected_playback_ms=20_000.0,
            private_text="must not be retained",
        )

        event = timelines.snapshot()[0]["events"][0]
        self.assertEqual(event["source_sample_rate"], 24_000)
        self.assertEqual(event["playback_sample_rate"], 48_000)
        self.assertEqual(event["sample_count"], 960_000)
        self.assertEqual(event["expected_playback_ms"], 20_000.0)
        self.assertNotIn("private_text", event)

    def test_keeps_distinct_privacy_safe_route_events_for_multiple_chunks(self):
        timelines = GenerationTimelineLog()

        timelines.record(
            "route-decision",
            1,
            1.0,
            chunk_id="chunk-a",
            chunk_ordinal=1,
            chunk_characters=12,
        )
        timelines.record(
            "route-decision",
            1,
            2.0,
            chunk_id="chunk-b",
            chunk_ordinal=2,
            chunk_characters=4,
        )

        events = timelines.snapshot()[0]["events"]
        self.assertEqual(
            [event["chunk_id"] for event in events], ["chunk-a", "chunk-b"]
        )
        self.assertNotIn("dialogue", str(events))

    def test_keeps_every_chunk_scoped_stage_and_merges_same_chunk_reports(self):
        timelines = GenerationTimelineLog()
        stages = (
            "generation-start",
            "route-decision",
            "voice-resolution",
            "first-pcm",
            "playback-completion",
            "playback-outcome",
            "duplicate-chunk-suppressed",
        )
        for ordinal, chunk_id in enumerate(("chunk-a", "chunk-b"), 1):
            for offset, stage in enumerate(stages):
                timelines.record(
                    stage,
                    1,
                    ordinal + offset / 100,
                    chunk_id=chunk_id,
                    chunk_ordinal=ordinal,
                )
        timelines.record(
            "playback-completion",
            1,
            9.0,
            chunk_id="chunk-a",
            underflowed=True,
        )

        events = timelines.snapshot()[0]["events"]

        self.assertEqual(len(events), len(stages) * 2)
        self.assertEqual(
            sum(event["stage"] == "playback-completion" for event in events), 2
        )
        completion_a = next(
            event
            for event in events
            if event["stage"] == "playback-completion"
            and event["chunk_id"] == "chunk-a"
        )
        self.assertTrue(completion_a["underflowed"])

    def test_rejects_unknown_stage_and_ignores_generation_zero(self):
        timelines = GenerationTimelineLog()

        self.assertFalse(timelines.record("capture", 0, 1.0))
        with self.assertRaisesRegex(ValueError, "Unknown generation timeline stage"):
            timelines.record("dialogue-text", 1, 1.0)
        with self.assertRaisesRegex(ValueError, "session ID must be a UUID"):
            timelines.record("capture", 1, 1.0, session_id="not-a-session")

    def test_accepts_privacy_safe_sequence_control_evidence(self):
        timelines = GenerationTimelineLog()

        self.assertFalse(timelines.record("sequence-audio-auto", 0, 0.5))
        timelines.record(
            "canonical-prefix-visual-recheck",
            1,
            0.75,
            fingerprint="same-glyphs",
            visible=True,
            focused=True,
            owner="event-1",
            recheck_interval_ms=600,
            private_text="must not be retained",
        )
        timelines.record(
            "sequence-audio-auto",
            1,
            1.0,
            state="locked",
            event_id="event-1",
            line_id="reverse1999:1:1",
            private_text="must not be retained",
        )
        timelines.record(
            "sequence-key-dispatch-authorized",
            1,
            2.0,
            event_id="event-1",
            next_event_count=1,
        )
        timelines.record(
            "sequence-successor-prefetch",
            1,
            1.25,
            event_id="event-1",
            target_event_id="event-2",
            line_id="reverse1999:1:2",
            outcome="reserved",
            prefetch_ms=12,
        )

        events = timelines.snapshot()[0]["events"]
        recheck = next(
            event
            for event in events
            if event["stage"] == "canonical-prefix-visual-recheck"
        )
        self.assertEqual(recheck["owner"], "event-1")
        self.assertEqual(recheck["recheck_interval_ms"], 600)
        self.assertNotIn("private_text", str(recheck))

        self.assertEqual(
            [event["stage"] for event in events],
            [
                "canonical-prefix-visual-recheck",
                "sequence-audio-auto",
                "sequence-successor-prefetch",
                "sequence-key-dispatch-authorized",
            ],
        )
        self.assertEqual(events[1]["event_id"], "event-1")
        self.assertNotIn("private_text", str(events))

    def test_accepts_every_declared_sequence_control_stage(self):
        timelines = GenerationTimelineLog()

        for offset, stage in enumerate(sequence_timeline_stages, start=1):
            self.assertTrue(timelines.record(stage, 1, float(offset)))

        self.assertEqual(
            {event["stage"] for event in timelines.snapshot()[0]["events"]},
            set(sequence_timeline_stages),
        )


class RuntimeSupportLogTest(unittest.TestCase):
    def test_audio_lifecycle_log_replaces_previous_session_then_appends(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "audio-lifecycle.log"
            previous = AudioLifecycleLog(path=path)
            previous.record("open", stream_id="old")

            current = AudioLifecycleLog(path=path)
            current.record("open", stream_id="new")
            current.record("close", stream_id="new")
            restored = AudioLifecycleLog(path=path, load_existing=True)

        self.assertEqual(
            [(event["operation"], event["stream_id"]) for event in restored.snapshot()],
            [("open", "new"), ("close", "new")],
        )

    def test_preserves_sanitized_previous_session_before_logs_are_replaced(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            runtime = RuntimeSupportLog(path=directory / "runtime.log")
            runtime.add("status", f"Live reading at {Path.home() / 'private'}")
            performance = PerformanceLog(path=directory / "performance.log")
            performance.record("live-start", 120, "complete")
            timelines = GenerationTimelineLog(
                path=directory / "generation-timelines.json"
            )
            timelines.record("capture", 1, 1.0, session_id=uuid4().hex)
            native = NativeSpeechLog(path=directory / "native-speech.log")
            native.record(
                {
                    "operation": "server-start",
                    "outcome": "complete",
                    "stage": "ready",
                    "reference": str(Path.home() / "private.wav"),
                }
            )
            audio = AudioLifecycleLog(path=directory / "audio-lifecycle.log")
            audio.record(
                "abort",
                stream_id="stream-1",
                outcome="complete",
                owner="dialog-playback_0",
                reason="shutdown",
            )
            (directory / "server.log").write_text("PRIVATE DIALOGUE", encoding="utf-8")

            snapshot = preserve_previous_session(directory)
            persisted = json.loads(
                (directory / "previous-session.json").read_text(encoding="utf-8")
            )

        self.assertEqual(snapshot, persisted)
        self.assertTrue(snapshot["available"])
        self.assertEqual(
            snapshot["generation_timelines"][0]["events"][0]["stage"],
            "capture",
        )
        self.assertEqual(snapshot["native_speech"]["latest_runtime"]["stage"], "ready")
        self.assertEqual(snapshot["audio_lifecycle"]["events"][0]["operation"], "abort")
        self.assertNotIn(str(Path.home()), repr(snapshot))
        self.assertNotIn("PRIVATE DIALOGUE", repr(snapshot))

    def test_log_is_bounded_and_returns_a_copy(self):
        log = RuntimeSupportLog(
            maximum_entries=2,
            clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
        )
        log.add("status", "one")
        log.add("status", "two")
        log.add("error", "three")

        entries = log.snapshot()
        entries.clear()

        self.assertEqual(
            [entry["message"] for entry in log.snapshot()], ["two", "three"]
        )

    def test_user_home_is_redacted_from_unix_and_windows_paths(self):
        self.assertNotIn(str(Path.home()), redact_text(Path.home() / "secret"))
        self.assertEqual(
            redact_text(r"C:\Users\Ada\private\settings.json"),
            r"<home>\private\settings.json",
        )
        missing = FileNotFoundError(2, "No such file", r"C:\Users\Ada\game\index.json")
        redacted = redact_text(str(missing))
        self.assertNotIn("Ada", redacted)
        self.assertIn(r"<home>\\game\\index.json", redacted)

    def test_log_can_persist_redacted_json_lines(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime.log"
            log = RuntimeSupportLog(
                clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
                path=path,
            )

            log.add("error", f"Failed under {Path.home() / 'private'}")

            entry = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(entry["level"], "error")
        self.assertIn("<home>", entry["message"])
        self.assertNotIn(str(Path.home()), entry["message"])

    def test_persistent_log_remains_valid_jsonl_and_bounded_in_long_session(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime.log"
            log = RuntimeSupportLog(
                maximum_entries=100,
                maximum_bytes=700,
                clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
                path=path,
            )

            for index in range(100):
                log.add("status", f"event-{index}-" + "x" * 80)

            payload = path.read_bytes()
            persisted = [json.loads(line) for line in payload.splitlines()]

        self.assertLessEqual(len(payload), 700)
        self.assertEqual(persisted, log.snapshot())
        self.assertGreater(len(persisted), 1)
        self.assertTrue(persisted[-1]["message"].startswith("event-99-"))

    def test_single_oversized_runtime_event_is_replaced_by_bounded_marker(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime.log"
            log = RuntimeSupportLog(maximum_bytes=256, path=path)

            log.add("error", "x" * 10_000, fallback_reason="y" * 10_000)

            payload = path.read_bytes()
            persisted = json.loads(payload)

        self.assertLessEqual(len(payload), 256)
        self.assertEqual(
            persisted["message"],
            "<runtime event exceeded persistent log limit>",
        )
        self.assertEqual(log.snapshot(), [persisted])

    def test_audio_route_fields_are_kept_in_one_sanitized_record(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "runtime.log"
            log = RuntimeSupportLog(
                clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
                path=path,
            )

            log.add(
                "audio-route",
                "Audio route selected",
                generation=7,
                effective_source="moss-tts:fresh-generation",
                match_result="exact",
                fallback_reason="generated-audio-entry-not-found",
                voice_reference_id="voice:rhiannon-v2:reference-1",
                line_id="reverse1999:24006:12",
                artifact_preflight_state="generated-audio-entry-not-found",
            )

            entry = log.snapshot()[0]
            persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(entry["generation"], 7)
        self.assertEqual(entry["match_result"], "exact")
        self.assertEqual(
            entry["voice_reference_id"],
            "voice:rhiannon-v2:reference-1",
        )
        self.assertEqual(
            entry["artifact_preflight_state"],
            "generated-audio-entry-not-found",
        )
        self.assertEqual(persisted["line_id"], "reverse1999:24006:12")
        self.assertEqual(persisted["generation"], 7)

    def test_live_scope_match_diagnostics_keep_metrics_without_dialogue(self):
        log = RuntimeSupportLog()

        log.add(
            "live-scope",
            "Initial story matcher result: expected-no-match",
            match_result="expected-no-match",
            eligible_line_count=42,
            normalized_text_characters=25,
            normalized_text_sha256="a" * 64,
            normalized_speaker_sha256="b" * 64,
            speaker_candidate_count=3,
            candidate_rejection_reason="bounded-threshold-not-met",
            best_candidate_line_id="reverse1999:314605:87",
            best_bounded_similarity=0.73,
            raw_text="private dialogue",
            raw_speaker="private speaker",
        )

        entry = sanitize_event(log.snapshot()[0])

        self.assertEqual(entry["eligible_line_count"], 42)
        self.assertEqual(entry["best_bounded_similarity"], 0.73)
        self.assertEqual(entry["normalized_text_sha256"], "a" * 64)
        self.assertEqual(entry["speaker_candidate_count"], 3)
        self.assertEqual(
            entry["candidate_rejection_reason"],
            "bounded-threshold-not-met",
        )
        self.assertNotIn("raw_text", entry)
        self.assertNotIn("private dialogue", repr(entry))


class PerformanceLogTest(unittest.TestCase):
    def test_reports_only_slow_successes_and_all_failures(self):
        log = PerformanceLog()

        log.record("fast", 99.9, "complete")
        log.record(
            "scan",
            125.4,
            "complete",
            files_examined=12,
            bytes_examined=5000,
            cache_state="miss",
        )
        log.record("scan", 25.0, "failed")

        report = log.report()
        self.assertEqual(set(report["summary"]), {"scan"})
        self.assertEqual(report["summary"]["scan"]["count"], 2)
        self.assertEqual(report["summary"]["scan"]["max_ms"], 125.4)
        self.assertEqual(report["summary"]["scan"]["max_files_examined"], 12)
        self.assertEqual(report["events"][0]["cache_state"], "miss")


class GameImportLogTest(unittest.TestCase):
    def test_bounded_redacted_log_survives_restart(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "game-import.log"
            log = GameImportLog(maximum_entries=2, path=path)
            log.record(
                "folder-result",
                index=Path.home() / "private" / "index.json",
                roots=[Path.home() / "Games", r"D:\\Games\\Reverse1999"],
                missing=["datacfg_1.dat"],
                cache_state={"narrator_index": True, "bank_count": 4},
                reason="token=do-not-export",
            )
            log.record(
                "complete",
                characters=12,
                references=34,
                reference_bytes=606500,
                duration_seconds=12.634,
                reference_sha256="a" * 64,
            )
            log.record("cleanup", cancelled=False)
            restarted = GameImportLog(maximum_entries=2, path=path)
            persisted_size = len(path.read_bytes())

        events = restarted.snapshot()
        self.assertEqual([event["stage"] for event in events], ["complete", "cleanup"])
        self.assertEqual(events[0]["reference_bytes"], 606500)
        self.assertEqual(events[0]["duration_seconds"], 12.634)
        self.assertEqual(events[0]["reference_sha256"], "a" * 64)
        self.assertLessEqual(persisted_size, 512 * 1024)

    def test_fields_keep_diagnostic_structure_but_redact_secrets_and_users(self):
        log = GameImportLog()
        log.record(
            "folder-result",
            index=Path.home() / "private" / "index.json",
            roots=[Path.home() / "Games", r"C:\Users\Ada\Games\Reverse1999"],
            missing=["datacfg_1.dat"],
            cache_state={"narrator_index": True, "bank_count": 4},
            stderr_tail=(
                "failed at D:/Games/Reverse1999; token=do-not-export; "
                'Authorization: Bearer no-export; {"token":"no-export"}'
            ),
        )

        event = log.snapshot()[0]
        serialized = json.dumps(event)
        self.assertEqual(
            event["cache_state"], {"narrator_index": True, "bank_count": 4}
        )
        self.assertEqual(event["missing"], ["datacfg_1.dat"])
        self.assertIn("<home>", event["index"])
        self.assertIn("D:/Games/Reverse1999", event["stderr_tail"])
        self.assertNotIn("Ada", serialized)
        self.assertNotIn("do-not-export", serialized)
        self.assertNotIn("no-export", serialized)

    def test_logging_is_fail_soft_when_persistence_is_unwritable(self):
        with TemporaryDirectory() as temporary_directory:
            log = GameImportLog(path=Path(temporary_directory) / "game-import.log")
            with patch("vntts.support.atomic_output_path", side_effect=OSError):
                log.record("failed", reason="permission denied")

        self.assertEqual(log.snapshot()[0]["stage"], "failed")

    def test_global_helper_is_fail_soft_and_uses_global_log(self):
        log = GameImportLog()
        with patch("vntts.support.game_import_log", log):
            record_game_import("available", package_version="1.2.3")

        self.assertEqual(log.snapshot()[0]["package_version"], "1.2.3")


class SupportBundleBuilderTest(unittest.TestCase):
    def test_every_sensitive_settings_path_is_redacted_by_metadata(self):
        sensitive = {
            definition.name: definition.metadata["support_sensitivity"]
            for definition in fields(AppSettings)
            if "support_sensitivity" in definition.metadata
        }
        sentinel = "/Volumes/private-user-data/sentinel"
        settings = AppSettings(**{name: f"{sentinel}/{name}" for name in sensitive})

        sanitized = sanitize_settings(settings)

        self.assertEqual(
            set(sensitive),
            {
                "screenshot_directory",
                "ocr_diagnostics_directory",
                "tts_model",
                "tts_speaker_wav",
                "game_pack",
                "voice_manifest",
                "story_index",
                "live_sequence_plan",
                "live_speaker_corpus",
                "generated_audio_manifest",
            },
        )
        self.assertNotIn(sentinel, json.dumps(sanitized))
        self.assertTrue(all(sanitized[name] == "<path>" for name in sensitive))

    def test_remote_model_identifier_remains_available_for_diagnostics(self):
        settings = AppSettings(tts_model="OpenMOSS-Team/MOSS-TTS-v1.5")

        self.assertEqual(
            sanitize_settings(settings)["tts_model"],
            "OpenMOSS-Team/MOSS-TTS-v1.5",
        )

    def test_bundle_excludes_dialog_images_text_and_environment_values(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            diagnostics_directory = directory / "ocr"
            diagnostics_directory.mkdir()
            (diagnostics_directory / "uncertain-one.json").write_text(
                json.dumps(
                    {
                        "character": "PRIVATE CHARACTER",
                        "text": "PRIVATE DIALOGUE",
                        "confidence": 42,
                        "attempts": 3,
                        "preprocessing_profile": "balanced",
                    }
                ),
                encoding="utf-8",
            )
            log = RuntimeSupportLog()
            log.add("status", f"Settings at {Path.home() / 'private'}")
            imports = GameImportLog()
            imports.record("failed", missing=["game configuration"])
            settings = AppSettings(
                ocr_diagnostics_directory=str(diagnostics_directory),
                screenshot_directory=str(Path.home() / "screenshots"),
            )
            diagnostic = DiagnosticSnapshot(
                Image.new("RGB", (10, 10), "red"),
                character="PRIVATE CHARACTER",
                text="PRIVATE DIALOGUE",
                confidence=42,
                preprocessing_profile="balanced",
                corrections=("PRIVATE -> SECRET",),
            )

            output = SupportBundleBuilder(
                settings,
                log,
                diagnostic=diagnostic,
                dependency_probe=lambda: {"test": "ok"},
                generation_timelines=GenerationTimelineLog(),
                game_import_log=imports,
                previous_session={
                    "available": True,
                    "runtime_events": [{"message": "prior crash"}],
                },
            ).build(directory / "support.zip")
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                combined = b"\n".join(archive.read(name) for name in names).decode()
                metrics = json.loads(archive.read("ocr-metrics.json"))
                imports = json.loads(archive.read("game-import.json"))
                previous = json.loads(archive.read("previous-session.json"))

            self.assertEqual(
                names,
                {
                    "manifest.json",
                    "sanitized-settings.json",
                    "active-content.json",
                    "pregeneration.json",
                    "runtime-events.json",
                    "game-import.json",
                    "performance.json",
                    "native-speech.json",
                    "build.json",
                    "generation-timelines.json",
                    "previous-session.json",
                    "audio-lifecycle.json",
                    "ocr-metrics.json",
                    "diagnostics.json",
                    "dependencies.json",
                },
            )
        self.assertNotIn("PRIVATE CHARACTER", combined)
        self.assertNotIn("PRIVATE DIALOGUE", combined)
        self.assertNotIn("PRIVATE -> SECRET", combined)
        self.assertTrue(previous["available"])
        self.assertNotIn(str(Path.home()), combined)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["average_confidence"], 42)
        self.assertEqual(imports["events"][0]["missing"], ["game configuration"])

    def test_support_correlates_active_pack_with_bounded_preparation_failure(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            pack = directory / "game-pack.json"
            story = directory / "story-index.jsonl"
            story.write_text(
                json.dumps(
                    {
                        "record_type": "story_line",
                        "collection_id": "story-one",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            pack.write_text(
                json.dumps(
                    {
                        "components": {
                            "story_index": {
                                "path": "story-index.jsonl",
                                "sha256": "a" * 64,
                            },
                        },
                        "vntts.self-service": {
                            "identity": "b" * 64,
                            "job_id": "c" * 24,
                            "source_queue_sha256": "d" * 64,
                            "source_state_sha256": "e" * 64,
                            "story_line_count": 493,
                            "approved_count": 489,
                            "live_fallback_count": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = directory / "generation-state.json"
            state.write_text(
                json.dumps(
                    {
                        "queue_sha256": "f" * 64,
                        "items": {
                            "reverse1999:314605:113:04d5e08b450a8d7a": {
                                "status": "failed",
                                "line_id": "reverse1999:314605:113",
                                "speaker": "Aderyn",
                                "provider": "moss-tts",
                                "model": str(Path.home() / "private-model"),
                                "attempts": 4,
                                "attempts_by_provider": {
                                    "moss-tts": 3,
                                    "pocket-tts": 1,
                                },
                                "failure": {
                                    "kind": "speech_silence",
                                    "error_type": "SpeechQualityError",
                                },
                                "failure_repair": {
                                    "strategy": "offline_fallback_backend",
                                    "source_failure": {
                                        "source_provider": "moss-tts",
                                        "source_failure_kind": "speech_silence",
                                    },
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            support = PregenerationSupportState(directory / "pregeneration.json")
            support.record(
                "Unable to recover offline audio",
                "Offline fallback attempt is exhausted",
                job=SimpleNamespace(
                    job_id="c" * 24,
                    status="planned",
                    provider_id="reverse1999",
                    story_index_sha256="1" * 64,
                    selected_story_ids=("story-one",),
                    selected_line_ids=tuple(str(value) for value in range(493)),
                ),
                generation_input=SimpleNamespace(
                    identity="2" * 64,
                    queue_sha256="f" * 64,
                    queue_items=490,
                    ready_items=490,
                ),
                voice_plan=SimpleNamespace(
                    synthesis_backend="moss-tts",
                    synthesis_model=str(Path.home() / "private-model"),
                    synthesis_profile="stable",
                    synthesis_controls_sha256="3" * 64,
                ),
                state_path=state,
            )

            active = collect_active_content_identity(AppSettings(game_pack=str(pack)))
            report = support.report()

        self.assertEqual(active["active_pregeneration_job_id"], "c" * 24)
        self.assertEqual(active["active_story_line_count"], 493)
        correlation = correlate_active_preparation(active, report)
        self.assertTrue(correlation["selected_stories_present"])
        self.assertEqual(correlation["classification"], "different-preparation")
        self.assertEqual(report["job"]["selected_story_ids"], ["story-one"])
        failure = report["generation_state"]["failed_items"][0]
        self.assertEqual(
            failure["attempts_by_provider"], {"moss-tts": 3, "pocket-tts": 1}
        )
        self.assertEqual(failure["repair_strategy"], "offline_fallback_backend")
        self.assertNotIn(str(Path.home()), json.dumps(report))

    def test_persisted_pregeneration_support_is_whitelisted_on_reload(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "pregeneration.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation": "retry generation",
                        "error": str(Path.home() / "private-token"),
                        "job": {
                            "available": True,
                            "job_id": "job-one",
                            "unexpected": "secret",
                        },
                        "unexpected": "secret",
                    }
                ),
                encoding="utf-8",
            )
            report = PregenerationSupportState(path).report()

        self.assertNotIn("unexpected", report)
        self.assertNotIn("unexpected", report["job"])
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn(str(Path.home()), json.dumps(report))

    def test_active_pack_ignores_non_object_story_index_records(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "story-index.jsonl").write_text(
                '1\n[]\n{"record_type":"story_line","collection_id":"story-one"}\n',
                encoding="utf-8",
            )
            pack = directory / "game-pack.json"
            pack.write_text(
                json.dumps(
                    {"components": {"story_index": {"path": "story-index.jsonl"}}}
                ),
                encoding="utf-8",
            )
            active = collect_active_content_identity(AppSettings(game_pack=str(pack)))

        self.assertEqual(active["active_story_ids"], ["story-one"])

    def test_ocr_metrics_report_resolved_pending_and_invalid_counts(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "uncertain-pending.json").write_text(
                json.dumps(
                    {
                        "confidence": 40,
                        "attempts": 2,
                        "preprocessing_profile": "balanced",
                    }
                ),
                encoding="utf-8",
            )
            (directory / "uncertain-resolved.json").write_text(
                json.dumps(
                    {
                        "confidence": 60,
                        "attempts": 4,
                        "preprocessing_profile": "balanced",
                        "resolved": True,
                    }
                ),
                encoding="utf-8",
            )
            (directory / "uncertain-invalid.json").write_text(
                "bad json",
                encoding="utf-8",
            )

            metrics = collect_ocr_metrics(directory)

        self.assertEqual(metrics["sample_count"], 2)
        self.assertEqual(metrics["resolved_count"], 1)
        self.assertEqual(metrics["pending_count"], 1)
        self.assertEqual(metrics["invalid_metadata_count"], 1)
        self.assertEqual(metrics["average_confidence"], 50)
        self.assertEqual(metrics["average_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
