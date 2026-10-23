import json
import os
import shutil
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.app import TrayApplication  # noqa: E402
from vntts.reading_diagnostics import (  # noqa: E402
    ReadingDiagnosticSession,
    SavedDiagnosticSession,
    latest_finished_diagnostic_session,
    record_diagnostic_event,
)
from vntts.settings import AppSettings  # noqa: E402
from vntts.support import (  # noqa: E402
    GenerationTimelineLog,
    RuntimeSupportLog,
    SupportBundleBuilder,
)
from vntts.support_ui import SupportCenterDialog  # noqa: E402


class SavedReadingDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def recorded_session(self, root):
        timelines = GenerationTimelineLog()
        session = ReadingDiagnosticSession(root, timelines)
        self.addCleanup(session.stop, 3)
        timelines.record("capture", 1, 1.0)
        record_diagnostic_event("audio", "rust-worker-finished", worker_pid=456)
        session.stop(3)
        self.assertTrue(session.finished.is_set())
        return session

    def test_recorded_session_restores_and_exports_with_exact_journals(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            live = self.recorded_session(root / "sessions")
            expected = (live.directory / "runtime.jsonl").read_bytes()
            saved = latest_finished_diagnostic_session(root / "sessions")
            self.assertIsInstance(saved, SavedDiagnosticSession)
            self.assertEqual(saved.session_id, live.session_id)
            self.assertEqual(saved.status()["status"], "finished")
            record_diagnostic_event("audio", "close", worker_pid=999)
            self.assertEqual((live.directory / "runtime.jsonl").read_bytes(), expected)
            with patch("vntts.support.collect_build_identity", return_value={}):
                output = SupportBundleBuilder(
                    AppSettings(ocr_diagnostics_directory=str(root / "ocr")),
                    RuntimeSupportLog(),
                    dependency_probe=lambda: {},
                    diagnostic_session=saved,
                ).build(root / "restored.zip")
                ordinary = SupportBundleBuilder(
                    AppSettings(ocr_diagnostics_directory=str(root / "ocr")),
                    RuntimeSupportLog(),
                    dependency_probe=lambda: {},
                ).build(root / "ordinary.zip")
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    archive.read("reading-session/runtime.jsonl"), expected
                )
                self.assertIn("reading-session/session.json", archive.namelist())
            with zipfile.ZipFile(ordinary) as archive:
                self.assertFalse(
                    any(
                        name.startswith("reading-session/")
                        for name in archive.namelist()
                    )
                )

    def test_latest_valid_finished_session_wins_and_bad_newer_metadata_is_ignored(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.recorded_session(root)
            second = self.recorded_session(root)
            self.assertEqual(
                latest_finished_diagnostic_session(root).session_id, second.session_id
            )
            invalid = root / uuid4().hex
            shutil.copytree(second.directory, invalid)
            metadata = invalid / "session.json"
            report = json.loads(metadata.read_text())
            report.update(
                session_id=invalid.name, started_at="2999-01-01T00:00:00+00:00"
            )
            for changes in (
                {"status": "recording"},
                {"status": "finished", "session_id": "../../outside"},
                {
                    "status": "finished",
                    "session_id": invalid.name,
                    "started_at": "invalid-date",
                },
            ):
                metadata.write_text(json.dumps({**report, **changes}))
                self.assertEqual(
                    latest_finished_diagnostic_session(root).session_id,
                    second.session_id,
                )
            metadata.write_text("not JSON")
            self.assertEqual(
                latest_finished_diagnostic_session(root).session_id, second.session_id
            )
            self.assertTrue(first.directory.exists())
            self.assertTrue(invalid.exists())

    def test_missing_or_incomplete_journal_is_not_restored(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(latest_finished_diagnostic_session(root / "missing"))
            session = self.recorded_session(root)
            runtime = session.directory / "runtime.jsonl"
            original = runtime.read_bytes()
            runtime.write_bytes(original.rsplit(b"\n", 2)[0] + b"\n")
            self.assertIsNone(latest_finished_diagnostic_session(root))
            runtime.unlink()
            self.assertIsNone(latest_finished_diagnostic_session(root))

    def test_restore_rejects_symlink_paths_and_bounded_oversize_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root / "real")
            linked = root / "linked"
            linked.symlink_to(root / "real", target_is_directory=True)
            self.assertIsNone(latest_finished_diagnostic_session(linked))
            metadata = session.directory / "session.json"
            payload = metadata.read_bytes()
            external = root / "external.json"
            external.write_bytes(payload)
            metadata.unlink()
            metadata.symlink_to(external)
            self.assertIsNone(latest_finished_diagnostic_session(root / "real"))
            metadata.unlink()
            metadata.write_bytes(b" " * 65537)
            self.assertIsNone(latest_finished_diagnostic_session(root / "real"))

    def test_saved_session_ui_shows_finished_and_explicit_export_dispatches(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            live = self.recorded_session(root)
            saved = latest_finished_diagnostic_session(root)
            dialog = SupportCenterDialog(RuntimeSupportLog())
            self.addCleanup(dialog.close)
            dialog.reading_status_provider = saved.status
            dialog.refresh_reading_session()
            self.assertTrue(dialog.reading_export.isEnabled())
            self.assertTrue(dialog.reading_start.isEnabled())
            self.assertFalse(dialog.reading_stop.isEnabled())
            tray = SimpleNamespace(
                reading_diagnostic_session=saved,
                support_dialog=dialog,
                support_export_runner=Mock(),
                controller=SimpleNamespace(
                    get_latest_diagnostic=Mock(return_value=None)
                ),
                _build_support_bundle=Mock(),
            )
            TrayApplication.export_reading_diagnostics(tray)
            tray.support_export_runner.start.assert_called_once_with(
                tray._build_support_bundle,
                str(root / (live.session_id + ".zip")),
                None,
                saved,
            )

    def test_start_failure_survives_refresh_and_successful_retry_clears_it(self):
        for failure_at, message in (
            ("session", "Diagnostic retention limit reached. Open sessions folder"),
            ("target", "Selected game window changed; recalibrate before recording"),
        ):
            with self.subTest(failure_at=failure_at):
                dialog = SupportCenterDialog(RuntimeSupportLog())
                self.addCleanup(dialog.close)
                previous = Mock()
                previous.status.return_value = {
                    "status": "finished",
                    "media_status": "stopped",
                    "analysis_status": "complete",
                    "target": {"window_id": 10, "pid": 20, "title": "Old game"},
                }
                target = {"window_id": 30, "pid": 40, "title": "New game"}
                replacement = Mock()
                replacement.status.return_value = {
                    "status": "recording",
                    "media_status": "preparing",
                    "analysis_status": "not-requested",
                    "target": target,
                }
                tray = SimpleNamespace(
                    reading_diagnostic_session=previous,
                    support_dialog=dialog,
                    controller=SimpleNamespace(capture_target=object()),
                    generation_timelines=GenerationTimelineLog(),
                    set_status=Mock(),
                )
                dialog.reading_status_provider = lambda: (
                    tray.reading_diagnostic_session.status()
                )
                dialog.reading_start_requested.connect(
                    lambda media, seconds: TrayApplication.start_reading_diagnostics(
                        tray, media, seconds
                    )
                )
                dialog.reading_media.setChecked(True)
                with (
                    patch(
                        "vntts.app.selected_recording_target", return_value=target
                    ) as select,
                    patch(
                        "vntts.app.ReadingDiagnosticSession", return_value=replacement
                    ) as create,
                ):
                    failing = select if failure_at == "target" else create
                    failing.side_effect = RuntimeError(message)
                    dialog.reading_start.click()
                    for _ in range(3):
                        dialog.refresh()
                    self.assertIs(tray.reading_diagnostic_session, previous)
                    self.assertIn(message, dialog.operation_status.toPlainText())
                    self.assertIn("Window 10", dialog.reading_status.text())
                    self.assertTrue(dialog.reading_start.isEnabled())
                    self.assertTrue(dialog.reading_export.isEnabled())
                    dialog.set_launcher_result("settings-folder", True, "Folder opened")
                    self.assertIn(message, dialog.operation_status.toPlainText())
                    failing.side_effect = None
                    dialog.reading_start.click()
                    dialog.refresh()
                    self.assertIs(tray.reading_diagnostic_session, replacement)
                    self.assertNotIn(message, dialog.operation_status.toPlainText())
                    self.assertIn(
                        "Folder opened", dialog.operation_status.toPlainText()
                    )
                    self.assertIn(
                        "Window 30, PID 40: New game", dialog.reading_status.text()
                    )
                    self.assertFalse(dialog.reading_start.isEnabled())
                    self.assertTrue(dialog.reading_stop.isEnabled())
