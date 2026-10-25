import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.reading_diagnostics import (  # noqa: E402
    DiagnosticSessionEntry,
    ReadingDiagnosticSession,
    SavedDiagnosticSession,
    arm_audio_worker_recording,
    delete_saved_diagnostic_session,
    record_diagnostic_event,
    selected_recording_target,
)
from vntts.support import GenerationTimelineLog  # noqa: E402
from vntts.support_ui import SupportCenterDialog  # noqa: E402
from vntts.window_capture import (  # noqa: E402
    WindowCaptureTarget,
    WindowGeometry,
    WindowInfo,
)


class ReadingDiagnosticSessionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.timelines = GenerationTimelineLog(maximum_entries=2)
        self.settings = SimpleNamespace(generated_audio_manifest=None)

    def session(self, **kwargs):
        session = ReadingDiagnosticSession(self.root, self.timelines, **kwargs)
        self.addCleanup(session.stop, 3)
        return session

    def test_audio_worker_arming_is_disabled_without_explicit_media_recording(self):
        identity = {"worker_pid": 123, "worker_path": "/worker", "worker_id": "first"}
        interrupted = Mock(return_value=False)
        arm_audio_worker_recording(identity, interrupted)
        session = self.session()
        arm_audio_worker_recording(identity, interrupted)
        interrupted.assert_not_called()
        session.stop(3)
        self.assertNotIn(
            "rust-recording-arm", (session.directory / "runtime.jsonl").read_text()
        )

    def test_audio_worker_arming_routes_only_to_current_explicit_recorder(self):
        identity = {"worker_pid": 123, "worker_path": "/worker", "worker_id": "first"}
        interrupted = Mock(return_value=False)
        recorder = Mock()
        recorder.wait_for_worker.return_value = False, "capture-failed"
        session = SimpleNamespace(recording=recorder, phase="recording")
        with (
            patch("vntts.reading_diagnostics._active", session),
            patch("vntts.reading_diagnostics.record_diagnostic_event") as record,
        ):
            arm_audio_worker_recording(identity, interrupted)
            recorder.wait_for_worker.assert_called_once_with(identity, interrupted)
            self.assertEqual(record.call_args.kwargs["reason"], "capture-failed")
            self.assertFalse(record.call_args.kwargs["ready"])
            session.phase = "finalizing"
            arm_audio_worker_recording(identity, interrupted)
            self.assertEqual(recorder.wait_for_worker.call_count, 1)

    def test_unexpected_recorder_error_preserves_playback_and_records_failure(self):
        recorder = Mock()
        recorder.wait_for_worker.side_effect = OSError("metadata unavailable")
        session = SimpleNamespace(recording=recorder, phase="recording")
        with (
            patch("vntts.reading_diagnostics._active", session),
            patch("vntts.reading_diagnostics.record_diagnostic_event") as record,
        ):
            self.assertTrue(
                arm_audio_worker_recording({"worker_id": "first"}, lambda: False)
            )
        recorder._fail_capture.assert_called_once()
        self.assertEqual(record.call_args.kwargs["reason"], "capture-failed")

    def test_early_evidence_survives_eviction_and_runtime_is_allowlisted(self):
        session = self.session()
        for generation in range(1, 9):
            self.timelines.record(
                "capture", generation=generation, occurred_at=generation + 1
            )
        record_diagnostic_event(
            "generation",
            "generation-render-result",
            line_id="line-1",
            synthesis_ms=34.5,
            text="SECRET DIALOGUE",
            path="/private/SECRET",
            cache_source="generated",
        )
        session.stop(3)
        self.assertTrue(session.finished.is_set())
        events = [
            json.loads(line)
            for line in (session.directory / "timeline.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(e.get("stage") == "capture" for e in events), 8)
        runtime = (session.directory / "runtime.jsonl").read_text()
        self.assertIn("34.5", runtime)
        self.assertNotIn("SECRET", runtime)
        self.assertEqual(session.status()["media_status"], "disabled")
        self.assertTrue(session.export_entries())

    def test_media_failure_keeps_journals_accepting_until_explicit_stop(self):
        failed = threading.Event()
        recorder = Mock(status="failed", error_message="process tap failed")
        recorder.target = {"pid": 50}
        recorder.run.side_effect = failed.set
        recorder.report.return_value = {"status": "failed"}
        with patch(
            "vntts.reading_diagnostics.NativeDiagnosticRecording", return_value=recorder
        ):
            session = self.session(target={"pid": 50})
        self.assertTrue(failed.wait(1))
        self.assertFalse(session.finished.is_set())
        self.assertFalse(session.runtime.stop_requested.is_set())
        self.assertFalse(session.timeline.stop_requested.is_set())
        self.assertEqual(session.status()["media_error"], "process tap failed")
        self.timelines.record("capture", generation=99, occurred_at=100)
        record_diagnostic_event("audio", "after-capture-failure", worker_pid=456)
        with patch("vntts.reading_diagnostics_analysis.analyze_session"):
            session.stop(3)
        self.assertTrue(session.finished.is_set())
        self.assertIn(
            "after-capture-failure", (session.directory / "runtime.jsonl").read_text()
        )
        self.assertIn(
            '"generation":99', (session.directory / "timeline.jsonl").read_text()
        )

    def test_original_deadline_closes_journals_before_slow_media_finalization(self):
        entered, finalizing, release = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        recorder = Mock(status="preparing", error_message=None)
        recorder.target = {"pid": 50}
        recorder.cancelled = threading.Event()
        recorder.report.return_value = {"status": "stopped"}

        def run():
            entered.set()
            recorder.cancelled.wait(3)
            finalizing.set()
            release.wait(3)
            recorder.status = "stopped"

        recorder.run.side_effect = run
        with (
            patch(
                "vntts.reading_diagnostics.NativeDiagnosticRecording",
                return_value=recorder,
            ) as create,
            patch("vntts.reading_diagnostics_analysis.analyze_session"),
        ):
            session = self.session(seconds=1, target={"pid": 50})
            self.addCleanup(release.set)
            self.assertTrue(entered.wait(1))
            self.assertEqual(
                create.call_args.kwargs["deadline"], session.started_monotonic + 1
            )
            self.assertTrue(finalizing.wait(2))
            self.assertEqual(session.phase, "finalizing")
            self.assertTrue(session.timeline.stop_requested.is_set())
            self.assertTrue(session.runtime.stop_requested.is_set())
            self.assertFalse(session.finished.is_set())
            with self.assertRaisesRegex(RuntimeError, "already active"):
                self.session()
            with self.assertRaisesRegex(RuntimeError, "finalization"):
                session.export_entries()
            release.set()
            self.assertTrue(session.finished.wait(2))

    def test_single_owner_and_no_export_while_running(self):
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, "already active"):
            self.session()
        with self.assertRaisesRegex(RuntimeError, "Stop"):
            session.export_entries()
        session.stop(3)
        self.session().stop(3)

    def test_failed_acquisition_does_not_replace_existing_writer(self):
        self.timelines.start_acquisition(self.root / "existing.jsonl")
        self.addCleanup(self.timelines.stop_acquisition)
        with self.assertRaises(RuntimeError):
            self.session()
        self.assertFalse(self.timelines.acquisition.stop_requested.is_set())

    def test_export_rejects_symlink_and_new_sessions_never_delete_old_data(self):
        session = self.session()
        session.stop(3)
        (session.directory / "linked").symlink_to(self.root / "external")
        with self.assertRaisesRegex(ValueError, "symlink"):
            session.export_entries()
        for _ in range(11):
            self.session().stop(3)
        self.assertEqual(len(list(self.root.iterdir())), 12)
        self.assertTrue(session.directory.exists())

    def test_history_deletion_refuses_active_and_finalizing_sessions(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, "not finished"):
            delete_saved_diagnostic_session(self.root, session.session_id)
        session.stop(3)
        session.finished.clear()
        session.phase = "finalizing"
        with patch("vntts.reading_diagnostics._active", session):
            with self.assertRaisesRegex(ValueError, "active or finalizing"):
                delete_saved_diagnostic_session(self.root, session.session_id)
        session.finished.set()
        self.assertTrue(session.directory.exists())

    def test_late_writer_completion_does_not_permanently_block_export(self):
        session = self.session()
        session.stop(3)
        with patch.object(
            session.timeline, "status", return_value={"writer_running": True}
        ):
            with self.assertRaisesRegex(RuntimeError, "finalizing"):
                session.export_entries()
        self.assertTrue(session.export_entries())
        self.assertFalse(session.status()["timeline"]["writer_running"])

    def test_retained_source_is_exact_admitted_bytes_and_change_fails_closed(self):
        session = self.session()
        session.stop(3)
        source = self.root / "source.wav"
        source.write_bytes(b"admitted source bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        session.sources[("line-54", digest)] = source
        session._retain_sources()
        entries = json.loads((session.directory / "sources.json").read_text())[
            "entries"
        ]
        self.assertEqual(entries[0]["audio_sha256"], digest)
        self.assertEqual(
            (session.directory / entries[0]["audio"]).read_bytes(), source.read_bytes()
        )
        source.write_bytes(b"replacement after publication")
        with self.assertRaisesRegex(ValueError, "changed since playback"):
            session._retain_sources()

    def test_exact_recording_target_never_falls_back_to_first_window(self):
        backend = Mock()
        backend.list_windows.return_value = [
            WindowInfo(1, "Game one", 11),
            WindowInfo(2, "Game two", 12),
        ]
        target = WindowCaptureTarget("Game", backend)
        with self.assertRaisesRegex(RuntimeError, "exact game window"):
            selected_recording_target(target)
        target.window_title = "Game one"
        backend.get_client_geometry.return_value = WindowGeometry(0, 0, 900, 600)
        with patch(
            "vntts.diagnostic_recording.process_executable", return_value="/game"
        ):
            result = selected_recording_target(target)
        self.assertEqual((result["pid"], result["window_id"]), (11, 1))
        target.last_window = WindowInfo(1, "Game one", 11)
        backend.get_window.return_value = WindowInfo(1, "Game one", 999)
        with self.assertRaisesRegex(RuntimeError, "changed"):
            selected_recording_target(target)


class ReadingDiagnosticUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_controls_opt_in_and_status(self):
        dialog = SupportCenterDialog(SimpleNamespace(snapshot=lambda: []))
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.reading_media.isChecked())
        calls = []
        dialog.reading_start_requested.connect(lambda *args: calls.append(args))
        dialog.reading_start.click()
        self.assertEqual(calls, [(False, 600)])
        report = {
            "session_id": "a" * 32,
            "status": "recording",
            "media_status": "disabled",
            "analysis_status": "not-requested",
        }
        dialog.reading_status_provider = lambda: report
        dialog.refresh()
        self.assertFalse(dialog.reading_export.isEnabled())
        self.assertFalse(dialog.reading_start.isEnabled())
        self.assertTrue(dialog.reading_stop.isEnabled())
        report["status"] = "finished"
        dialog.refresh()
        self.assertFalse(dialog.reading_export.isEnabled())
        saved = SavedDiagnosticSession(Path("session"), "a" * 32, report)
        dialog.set_diagnostic_sessions(
            [
                DiagnosticSessionEntry(
                    saved.directory, saved.session_id, report, 100, saved
                )
            ]
        )
        self.assertTrue(dialog.reading_export.isEnabled())
        self.assertFalse(dialog.reading_stop.isEnabled())
