import json
import os
import shutil
import struct
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
    DiagnosticSessionEntry,
    ReadingDiagnosticSession,
    SavedDiagnosticSession,
    delete_saved_diagnostic_session,
    latest_finished_diagnostic_session,
    list_diagnostic_sessions,
    load_saved_diagnostic_session,
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

    def test_interrupted_session_exports_recovered_audio_without_rewriting_originals(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            metadata = session.directory / "session.json"
            report = json.loads(metadata.read_text())
            report.update(status="recording", started_at="2020-01-01T00:00:00+00:00")
            report.pop("owner", None)
            metadata.write_text(json.dumps(report))
            original_metadata = metadata.read_bytes()
            runtime = session.directory / "runtime.jsonl"
            runtime.write_bytes(runtime.read_bytes().rsplit(b"\n", 2)[0] + b"\n")
            original_runtime = runtime.read_bytes()
            audio = session.directory / "process.caf"
            desc = struct.pack(">d4sIIIII", 16000, b"lpcm", 3, 8, 1, 2, 32)
            audio.write_bytes(
                b"caff\x00\x01\x00\x00desc"
                + struct.pack(">q", 32)
                + desc
                + b"data"
                + struct.pack(">q", -1)
                + b"\0" * (4 + 160 * 8)
            )
            original_audio = audio.read_bytes()
            (session.directory / "sources.json").write_text('{"entries":[]}')
            (session.directory / "analysis.json").write_bytes(
                b"previous analysis evidence"
            )

            entry = list_diagnostic_sessions(root)[0]
            self.assertEqual(entry.report["status"], "interrupted")
            self.assertIsNotNone(entry.saved)
            self.assertIsNone(latest_finished_diagnostic_session(root))
            saved = load_saved_diagnostic_session(root, session.session_id)
            entries = dict(saved.export_entries())
            self.assertIn("reading-session/process.recovered.caf", entries)
            self.assertIn("reading-session/recovery.json", entries)
            self.assertIn("reading-session/recovery-analysis.json", entries)
            self.assertEqual(
                (session.directory / "analysis.json").read_bytes(),
                b"previous analysis evidence",
            )
            self.assertEqual(
                json.loads((session.directory / "recovery-analysis.json").read_text())[
                    "status"
                ],
                "partial",
            )
            import soundfile as sf

            with sf.SoundFile(
                entries["reading-session/process.recovered.caf"]
            ) as recovered:
                self.assertEqual(recovered.frames, 160)
            self.assertEqual(audio.read_bytes(), original_audio)
            self.assertEqual(runtime.read_bytes(), original_runtime)
            self.assertEqual(metadata.read_bytes(), original_metadata)
            delete_saved_diagnostic_session(root, session.session_id)
            self.assertFalse(session.directory.exists())

    def test_live_owner_or_helper_blocks_interrupted_export_and_delete(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            metadata = session.directory / "session.json"
            report = json.loads(metadata.read_text())
            report["status"] = "recording"
            metadata.write_text(json.dumps(report))
            self.assertIsNone(load_saved_diagnostic_session(root, session.session_id))
            with self.assertRaises(ValueError):
                delete_saved_diagnostic_session(root, session.session_id)
            owner = report.pop("owner")
            report["started_at"] = "2020-01-01T00:00:00+00:00"
            report["recording"] = {
                "captures": [
                    {"helper_pid": owner["pid"], "helper_created": owner["created"]}
                ]
            }
            metadata.write_text(json.dumps(report))
            self.assertIsNone(load_saved_diagnostic_session(root, session.session_id))
            with self.assertRaisesRegex(ValueError, "media is still finalizing"):
                delete_saved_diagnostic_session(root, session.session_id)
            report["recording"]["captures"][0]["helper_created"] = None
            metadata.write_text(json.dumps(report))
            self.assertIsNone(load_saved_diagnostic_session(root, session.session_id))

    def test_dead_owner_allows_native_watchdog_to_finish_without_real_wait(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            metadata = session.directory / "session.json"
            report = json.loads(metadata.read_text())
            report.update(
                status="recording", target={"pid": 50}, recording={"captures": []}
            )
            metadata.write_text(json.dumps(report))
            with (
                patch(
                    "vntts.reading_diagnostics._process_is_running", return_value=False
                ),
                patch("vntts.reading_diagnostics._dead_owners_seen", {}),
                patch("vntts.reading_diagnostics.monotonic", return_value=100) as clock,
            ):
                self.assertIsNone(
                    load_saved_diagnostic_session(root, session.session_id)
                )
                clock.return_value = 114.9
                self.assertIsNone(
                    load_saved_diagnostic_session(root, session.session_id)
                )
                clock.return_value = 115
                self.assertEqual(
                    load_saved_diagnostic_session(root, session.session_id).report[
                        "status"
                    ],
                    "interrupted",
                )

    def test_diagnostic_export_uses_zip64_and_refuses_insufficient_space(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root / "sessions")
            saved = load_saved_diagnostic_session(root / "sessions", session.session_id)
            builder = SupportBundleBuilder(
                AppSettings(ocr_diagnostics_directory=str(root / "ocr")),
                RuntimeSupportLog(),
                dependency_probe=lambda: {},
                diagnostic_session=saved,
            )
            with (
                patch("vntts.support.collect_build_identity", return_value={}),
                patch("zipfile.ZIP64_LIMIT", 128),
            ):
                archive = builder.build(root / "session.zip")
            with zipfile.ZipFile(archive) as zipped:
                self.assertEqual(
                    zipped.read("reading-session/runtime.jsonl"),
                    (session.directory / "runtime.jsonl").read_bytes(),
                )
            with patch(
                "vntts.support.shutil.disk_usage", return_value=SimpleNamespace(free=1)
            ):
                with self.assertRaisesRegex(OSError, "free disk space"):
                    builder.build(root / "cannot-fit.zip")
            self.assertFalse((root / "cannot-fit.zip").exists())

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

    def test_history_and_latest_include_more_than_128_sessions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            template = self.recorded_session(root)
            for index in range(130):
                copied = root / f"{index:032x}"
                shutil.copytree(template.directory, copied)
                metadata = copied / "session.json"
                report = json.loads(metadata.read_text())
                report.update(
                    session_id=copied.name,
                    started_at=f"2999-01-01T00:{index // 60:02}:{index % 60:02}+00:00",
                )
                metadata.write_text(json.dumps(report))
            entries = list_diagnostic_sessions(root)
            self.assertEqual(len(entries), 131)
            self.assertEqual(entries[0].session_id, f"{129:032x}")
            self.assertEqual(
                latest_finished_diagnostic_session(root).session_id,
                entries[0].session_id,
            )
            self.assertEqual(
                load_saved_diagnostic_session(root, template.session_id).directory,
                template.directory,
            )

    def test_latest_restores_without_scanning_recorded_media(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            with patch(
                "vntts.reading_diagnostics._session_inventory",
                side_effect=AssertionError("media scan"),
            ):
                saved = latest_finished_diagnostic_session(root)
            self.assertEqual(saved.session_id, session.session_id)

    def test_history_inventory_counts_journals_without_inventing_missing_media(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            metadata = session.directory / "session.json"
            report = json.loads(metadata.read_text())
            report.update(
                target={"title": "Game"},
                media_status="stopped",
                recording={"video": "missing.mp4", "audio": "missing.wav"},
            )
            metadata.write_text(json.dumps(report))

            entry = list_diagnostic_sessions(root)[0]

            self.assertEqual(entry.file_counts, {".json": 1, ".jsonl": 2})
            self.assertIsNotNone(entry.saved)

    def test_history_inventory_counts_actual_files_by_lowercase_suffix_once(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            (session.directory / "recording.MP4").write_bytes(b"video")
            sources = session.directory / "sources"
            sources.mkdir()
            (sources / "first.WAV").write_bytes(b"audio")
            (sources / "second.wav").write_bytes(b"audio")
            (sources / "manifest.JSON").write_text("{}")
            (sources / "README").write_text("evidence")
            with patch("vntts.reading_diagnostics.os.walk", wraps=os.walk) as walk:
                entry = list_diagnostic_sessions(root)[0]

            self.assertEqual(
                entry.file_counts,
                {".json": 2, ".jsonl": 2, ".mp4": 1, ".wav": 2, "": 1},
            )
            walk.assert_called_once()

    def test_history_inventory_counts_zero_byte_media_as_present(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            journal_size = list_diagnostic_sessions(root)[0].size_bytes
            (session.directory / "recording.mp4").touch()
            (session.directory / "recording.wav").touch()

            entry = list_diagnostic_sessions(root)[0]

            self.assertEqual(
                entry.file_counts,
                {".json": 1, ".jsonl": 2, ".mp4": 1, ".wav": 1},
            )
            self.assertEqual(entry.size_bytes, journal_size)

    def test_history_distinguishes_missing_root_from_unreadable_or_unsafe_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(list_diagnostic_sessions(root / "missing"), [])
            linked = root / "linked"
            linked.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root is unsafe"):
                list_diagnostic_sessions(linked)
            with patch.object(
                Path, "iterdir", side_effect=PermissionError("unreadable")
            ):
                with self.assertRaises(PermissionError):
                    list_diagnostic_sessions(root)
                self.assertIsNone(latest_finished_diagnostic_session(root))

    def test_history_shows_invalid_and_incomplete_sessions_and_stat_sizes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            finished = self.recorded_session(root)
            media = finished.directory / "recording.wav"
            with media.open("wb") as output:
                output.truncate(4 * 1024**2)
            invalid = root / uuid4().hex
            invalid.mkdir()
            (invalid / "session.json").write_text("not JSON")
            invalid_file = root / uuid4().hex
            invalid_file.write_text("not a directory")
            incomplete = root / uuid4().hex
            shutil.copytree(finished.directory, incomplete)
            metadata = incomplete / "session.json"
            report = json.loads(metadata.read_text())
            report.update(session_id=incomplete.name, status="finalizing")
            metadata.write_text(json.dumps(report))
            entries = {
                entry.session_id: entry for entry in list_diagnostic_sessions(root)
            }
            expected_size = sum(
                path.stat().st_size
                for path in finished.directory.rglob("*")
                if path.is_file()
            )
            self.assertEqual(entries[finished.session_id].size_bytes, expected_size)
            self.assertIsNotNone(entries[finished.session_id].saved)
            self.assertIsNone(entries[invalid.name].report)
            self.assertIsNone(entries[invalid.name].saved)
            self.assertTrue(entries[invalid.name].error)
            self.assertIsNone(entries[invalid_file.name].size_bytes)
            self.assertIsNone(entries[invalid_file.name].file_counts)
            self.assertIsNone(entries[invalid_file.name].saved)
            self.assertIn("unsafe", entries[invalid_file.name].error)
            self.assertEqual(entries[incomplete.name].report["status"], "finalizing")
            self.assertIn("not finished", entries[incomplete.name].error)
            self.assertIsNone(entries[incomplete.name].saved)
            with self.assertRaisesRegex(ValueError, "not finished"):
                delete_saved_diagnostic_session(root, incomplete.name)
            self.assertTrue(incomplete.exists())

    def test_history_reports_unknown_size_when_evidence_is_unreadable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root)
            evidence = session.directory / "unreadable.wav"
            evidence.write_bytes(b"audio")
            original_stat = Path.stat

            def denied_stat(path, *args, **kwargs):
                if path == evidence:
                    raise PermissionError("unreadable")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", denied_stat):
                entry = list_diagnostic_sessions(root)[0]
            self.assertEqual(entry.session_id, session.session_id)
            self.assertIsNone(entry.size_bytes)
            self.assertIsNone(entry.file_counts)
            self.assertIn("unreadable", entry.error)

    def test_delete_revalidates_finished_journals_and_keeps_other_sessions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.recorded_session(root)
            second = self.recorded_session(root)
            list_diagnostic_sessions(root)
            runtime = first.directory / "runtime.jsonl"
            original = runtime.read_bytes()
            runtime.write_bytes(original.rsplit(b"\n", 2)[0] + b"\n")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                delete_saved_diagnostic_session(root, first.session_id)
            self.assertTrue(first.directory.exists())
            runtime.write_bytes(original)
            delete_saved_diagnostic_session(root, first.session_id)
            self.assertFalse(first.directory.exists())
            self.assertTrue(second.directory.exists())

    def test_delete_refuses_unowned_paths_and_symlink_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = self.recorded_session(root / "sessions")
            external = root / "external"
            external.mkdir()
            (external / "keep").write_text("keep")
            linked_root = root / "linked"
            linked_root.symlink_to(root / "sessions", target_is_directory=True)
            linked_id = uuid4().hex
            (session.directory.parent / linked_id).symlink_to(
                external, target_is_directory=True
            )
            for unsafe_root, identity in (
                (root / "sessions", "../external"),
                (root / "sessions", str(external)),
                (linked_root, session.session_id),
                (root / "sessions", linked_id),
            ):
                with self.subTest(root=unsafe_root, identity=identity):
                    with self.assertRaises(ValueError):
                        delete_saved_diagnostic_session(unsafe_root, identity)
            (session.directory / "linked").symlink_to(
                external, target_is_directory=True
            )
            entry = next(
                entry
                for entry in list_diagnostic_sessions(root / "sessions")
                if entry.session_id == session.session_id
            )
            self.assertIsNone(entry.size_bytes)
            self.assertIsNone(entry.file_counts)
            self.assertIn("symlink", entry.error)
            self.assertIsNone(entry.saved)
            with self.assertRaisesRegex(ValueError, "symlink"):
                delete_saved_diagnostic_session(root / "sessions", session.session_id)
            self.assertEqual((external / "keep").read_text(), "keep")
            self.assertTrue(session.directory.exists())

    def test_saved_session_ui_shows_finished_and_explicit_export_dispatches(self):
        with TemporaryDirectory() as directory:
            data_root = Path(directory)
            root = data_root / "reading-diagnostics"
            live = self.recorded_session(root)
            saved = latest_finished_diagnostic_session(root)
            dialog = SupportCenterDialog(RuntimeSupportLog())
            self.addCleanup(dialog.close)
            dialog.reading_status_provider = saved.status
            dialog.set_diagnostic_sessions(list_diagnostic_sessions(root))
            dialog.refresh_reading_session()
            self.assertTrue(dialog.reading_export.isEnabled())
            self.assertTrue(dialog.reading_start.isEnabled())
            self.assertFalse(dialog.reading_stop.isEnabled())
            tray = SimpleNamespace(
                reading_diagnostic_session=saved,
                support_dialog=dialog,
                support_export_runner=Mock(active=False),
                support_history_runner=Mock(active=False),
                _diagnostic_deletion_id=None,
                controller=SimpleNamespace(
                    get_latest_diagnostic=Mock(return_value=None)
                ),
                _build_support_bundle=Mock(),
            )
            with patch("vntts.app.get_local_data_directory", return_value=data_root):
                TrayApplication.export_reading_diagnostics(tray, saved.session_id)
            tray.support_export_runner.start.assert_called_once_with(
                tray._build_support_bundle,
                str(root / (live.session_id + ".zip")),
                None,
                saved,
            )

    def test_start_failure_survives_refresh_and_successful_retry_clears_it(self):
        for failure_at, message in (
            ("session", "Diagnostic storage is unavailable"),
            ("target", "Selected game window changed; recalibrate before recording"),
        ):
            with self.subTest(failure_at=failure_at):
                dialog = SupportCenterDialog(RuntimeSupportLog())
                self.addCleanup(dialog.close)
                report = {
                    "status": "finished",
                    "media_status": "stopped",
                    "analysis_status": "complete",
                    "target": {"window_id": 10, "pid": 20, "title": "Old game"},
                }
                previous = SavedDiagnosticSession(
                    Path("old-session"), uuid4().hex, report
                )
                dialog.set_diagnostic_sessions(
                    [
                        DiagnosticSessionEntry(
                            previous.directory,
                            previous.session_id,
                            report,
                            100,
                            previous,
                        )
                    ]
                )
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
                    support_export_runner=Mock(active=False),
                    support_history_runner=Mock(active=False),
                    _diagnostic_deletion_id=None,
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
