import json
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from vntts.diagnostics import DiagnosticSnapshot
from vntts.settings import AppSettings
from vntts.support import (
    GenerationTimelineLog,
    RuntimeSupportLog,
    SupportBundleBuilder,
    collect_ocr_metrics,
    redact_text,
    sequence_timeline_stages,
)


class GenerationTimelineLogTest(unittest.TestCase):
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

    def test_accepts_privacy_safe_sequence_control_evidence(self):
        timelines = GenerationTimelineLog()

        self.assertFalse(timelines.record("sequence-audio-auto", 0, 0.5))
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

        self.assertEqual(
            [event["stage"] for event in events],
            [
                "sequence-audio-auto",
                "sequence-successor-prefetch",
                "sequence-key-dispatch-authorized",
            ],
        )
        self.assertEqual(events[0]["event_id"], "event-1")
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


class SupportBundleBuilderTest(unittest.TestCase):
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
            ).build(directory / "support.zip")
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                combined = b"\n".join(archive.read(name) for name in names).decode()
                metrics = json.loads(archive.read("ocr-metrics.json"))

        self.assertEqual(
            names,
            {
                "manifest.json",
                "sanitized-settings.json",
                "runtime-events.json",
                "generation-timelines.json",
                "ocr-metrics.json",
                "diagnostics.json",
                "dependencies.json",
            },
        )
        self.assertNotIn("PRIVATE CHARACTER", combined)
        self.assertNotIn("PRIVATE DIALOGUE", combined)
        self.assertNotIn("PRIVATE -> SECRET", combined)
        self.assertNotIn(str(Path.home()), combined)
        self.assertEqual(metrics["sample_count"], 1)
        self.assertEqual(metrics["average_confidence"], 42)

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
