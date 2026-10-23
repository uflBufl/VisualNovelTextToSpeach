import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from vntts import support
from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.settings import AppSettings
from vntts.support import AudioLifecycleLog, RuntimeSupportLog, SupportBundleBuilder


class SupportReadingDiagnosticsTest(unittest.TestCase):
    def test_native_timing_metrics_keep_submillisecond_precision_and_identities(self):
        log = AudioLifecycleLog()
        log.record(
            "rust-worker-finished",
            worker_pid=4321,
            worker_id="worker-identity-1",
            generation_owner_pid=1234,
            source_sha256="a" * 64,
            source_samples=48000,
            timing_output_frames=48000,
            timing_sample_rate=48000.0,
            callback_timing={
                "observation_count": 3,
                "discontinuity_count": 1,
                "unavailable_count": 0,
                "max_gap_seconds": 0.0000208333333,
                "max_offset_seconds": -0.0000208333333,
                "max_offset_position_frames": 128,
                "max_offset_output_frame": 256,
            },
            dac_timing={"max_offset_seconds": 0.0000104166667},
        )
        event = support.sanitize_event(log.report()["events"][0])
        self.assertEqual(event["worker_pid"], 4321)
        self.assertEqual(event["worker_id"], "worker-identity-1")
        self.assertEqual(event["generation_owner_pid"], 1234)
        self.assertEqual(event["source_sha256"], "a" * 64)
        self.assertEqual(event["source_samples"], 48000)
        self.assertEqual(event["timing_output_frames"], 48000)
        self.assertEqual(event["callback_timing"]["max_gap_seconds"], 0.000020833)
        self.assertEqual(event["callback_timing"]["max_offset_seconds"], -0.000020833)
        self.assertEqual(event["dac_timing"]["max_offset_seconds"], 0.000010417)
        json.dumps(event, allow_nan=False)

    def test_capture_arming_evidence_survives_support_and_runtime_privacy_filters(self):
        from vntts.support_timeline_acquisition import TimelineAcquisition

        log = AudioLifecycleLog()
        log.record(
            "rust-recording-arm",
            worker_pid=4321,
            worker_id="worker-1",
            ready=False,
            reason="timeout",
            wait_ms=10001.5,
            worker_path="/PRIVATE/worker",
        )
        evidence = support.sanitize_event(log.report()["events"][0])
        self.assertEqual(evidence["operation"], "rust-recording-arm")
        self.assertEqual(evidence["wait_ms"], 10001.5)
        self.assertIs(evidence["ready"], False)
        self.assertEqual(evidence["reason"], "timeout")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.jsonl"
            journal = TimelineAcquisition(path)
            for reason in (
                "armed",
                "cancelled",
                "capture-failed",
                "timeout",
                "recorder-stopped",
            ):
                journal.record(
                    {**evidence, "reason": reason, "worker_path": "/PRIVATE/worker"}
                )
            journal.stop()
            raw = path.read_text()
            events = [
                json.loads(line)
                for line in raw.splitlines()
                if json.loads(line)["kind"] == "event"
            ]
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0]["reason"], "armed")
        self.assertEqual(events[-1]["reason"], "recorder-stopped")
        self.assertEqual(events[0]["wait_ms"], 10001.5)
        self.assertEqual(events[0]["worker_id"], "worker-1")
        self.assertNotIn("PRIVATE", raw + json.dumps(evidence))

    def test_nested_timing_drops_unknown_text_invalid_numbers_and_nonmetrics(self):
        log = AudioLifecycleLog()
        log.record(
            "rust-worker-finished",
            source_sha256="PRIVATEsource",
            callback_timing={
                "PRIVATE numeric key": 4,
                "text": "PRIVATEdialogue",
                "observation_count": "PRIVATEcount",
                "unavailable_count": True,
                "discontinuity_count": -1,
                "max_gap_seconds": float("nan"),
                "max_offset_seconds": 10**400,
                "max_offset_position_frames": 2**100,
                "max_offset_output_frame": 32,
            },
            dac_timing="PRIVATEtiming",
            device_name={"text": "PRIVATEdevice"},
            unknown_metric="PRIVATEunknown",
        )
        event = support.sanitize_event(log.report()["events"][0])
        self.assertEqual(event["callback_timing"], {"max_offset_output_frame": 32})
        self.assertNotIn("source_sha256", event)
        self.assertNotIn("PRIVATE", json.dumps(event))
        self.assertNotIn("unknown_metric", event)
        json.dumps(event, allow_nan=False)

    def test_audio_journal_hook_keeps_captured_context_and_survives_log_failure(self):
        session_id, run_id, playback_id = [uuid4().hex for _ in range(3)]
        token = audio_lifecycle_context.set(
            {
                "session_id": session_id,
                "run_id": run_id,
                "playback_id": playback_id,
                "generation": 7,
            }
        )
        try:
            with (
                patch.object(
                    support.audio_lifecycle_log, "record", side_effect=OSError
                ),
                patch("vntts.reading_diagnostics.record_diagnostic_event") as record,
            ):
                support.record_audio_lifecycle("rust-worker-finished", worker_pid=4321)
        finally:
            audio_lifecycle_context.reset(token)
        record.assert_called_once_with(
            "audio",
            "rust-worker-finished",
            session_id=session_id,
            run_id=run_id,
            playback_id=playback_id,
            generation=7,
            worker_pid=4321,
        )

    def test_journal_failure_cannot_break_ordinary_audio_logging(self):
        with (
            patch.object(support.audio_lifecycle_log, "record") as ordinary,
            patch(
                "vntts.reading_diagnostics.record_diagnostic_event", side_effect=OSError
            ),
        ):
            support.record_audio_lifecycle("close", outcome="complete")
        ordinary.assert_called_once_with("close", outcome="complete")

    def builder(self, root, **options):
        return SupportBundleBuilder(
            AppSettings(ocr_diagnostics_directory=str(root / "ocr")),
            RuntimeSupportLog(),
            dependency_probe=lambda: {"available": False},
            **options,
        )

    def test_ordinary_export_does_not_collect_any_diagnostic_session_media(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = SimpleNamespace(
                export_entries=Mock(side_effect=AssertionError("not requested"))
            )
            with (
                patch("vntts.reading_diagnostics._active", session),
                patch.object(support, "collect_build_identity", return_value={}),
            ):
                output = self.builder(root).build(root / "ordinary.zip")
            with zipfile.ZipFile(output) as archive:
                self.assertFalse(
                    any(
                        name.startswith("reading-session/")
                        for name in archive.namelist()
                    )
                )
                manifest = json.loads(archive.read("manifest.json"))
        session.export_entries.assert_not_called()
        self.assertIn("voice audio", manifest["privacy"])
        self.assertIn("excluded", manifest["privacy"])

    def test_explicit_session_export_includes_exact_media_and_updates_privacy_note(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "recording.bin"
            source.write_bytes(b"explicit media evidence")
            session = SimpleNamespace(
                export_entries=Mock(
                    return_value=[
                        ("reading-session/game.mov", source),
                    ]
                )
            )
            with patch.object(support, "collect_build_identity", return_value={}):
                output = self.builder(root, diagnostic_session=session).build(
                    root / "explicit.zip"
                )
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.read("reading-session/game.mov"), b"explicit media evidence"
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertIn("audio-lifecycle.json", archive.namelist())
        session.export_entries.assert_called_once_with()
        self.assertIn(
            "Explicit diagnostic session evidence is included", manifest["privacy"]
        )

    def test_failed_explicit_export_preserves_preexisting_bundle_atomically(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "support.zip"
            destination.write_bytes(b"previous complete bundle")
            session = SimpleNamespace(
                export_entries=Mock(
                    return_value=[
                        ("reading-session/missing.wav", root / "missing.wav"),
                    ]
                )
            )
            with patch.object(support, "collect_build_identity", return_value={}):
                with self.assertRaises(OSError):
                    self.builder(root, diagnostic_session=session).build(destination)
            self.assertEqual(destination.read_bytes(), b"previous complete bundle")
