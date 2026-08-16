import json
import os
import socket
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.bulk_generation import process_started_at
from vntts.authoring.workbench import list_review_items

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QProcess, QSettings
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

    from vntts.authoring.workbench_ui import (
        AuthoringWorkbenchDialog,
        VoiceReferenceController,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QProcess = None
    QSettings = None
    QCloseEvent = None
    QMessageBox = None
    QPushButton = None
    AuthoringWorkbenchDialog = None
    VoiceReferenceController = None


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *arguments):
        for callback in tuple(self.callbacks):
            callback(*arguments)


class FakeProcess:
    def __init__(self, state):
        self.state_value = state
        self.readyReadStandardOutput = FakeSignal()
        self.readyReadStandardError = FakeSignal()
        self.started = FakeSignal()
        self.finished = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.program = None
        self.arguments = None
        self.cwd = None
        self.environment = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.start_calls = 0
        self.channel_mode = None

    def state(self):
        return self.state_value

    def processId(self):
        return 4321

    def setWorkingDirectory(self, value):
        self.cwd = value

    def setProcessChannelMode(self, value):
        self.channel_mode = value

    def setProcessEnvironment(self, value):
        self.environment = value

    def setProgram(self, value):
        self.program = value

    def setArguments(self, value):
        self.arguments = value

    def start(self):
        self.start_calls += 1

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def readAllStandardOutput(self):
        return b""

    def readAllStandardError(self):
        return b""


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class AuthoringWorkbenchUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, AuthoringWorkbenchDialog):
                widget.process = FakeProcess(QProcess.ProcessState.NotRunning)
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    def create_workspace(self, root):
        return create_test_workspace(root)[2].directory

    def settings(self, root):
        return QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)

    def clear_authoring_state(self, workspace):
        state_path = workspace / "generated-audio" / "generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active"] = None
        state["items"] = {}
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def test_voice_reference_search_navigation_and_duration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            references = root / "references"
            first = references / "ada-1.wav"
            second = references / "ada-2.wav"
            write_pcm16_wav(first, np.zeros(1_600, dtype=np.float32), 16_000)
            write_pcm16_wav(second, np.zeros(3_200, dtype=np.float32), 16_000)
            manifest = root / "manifest.json"
            write_voice_manifest(
                manifest,
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Ada",
                            "speaker": "ada",
                            "aliases": ["Narrator Ada"],
                            "references": [
                                "references/ada-1.wav",
                                "references/ada-2.wav",
                            ],
                        },
                        {
                            "character": "Zed",
                            "speaker": "zed",
                            "references": [],
                        },
                    ],
                },
            )

            controller = VoiceReferenceController(manifest)
            current = controller.current("Ada")
            moved = controller.move("Ada", 1)

        self.assertEqual(controller.characters(), ("Ada", "Zed"))
        self.assertEqual(controller.characters("narrator"), ("Ada",))
        self.assertEqual(current.index, 0)
        self.assertAlmostEqual(current.duration_seconds, 0.1)
        self.assertEqual(moved.index, 1)
        self.assertAlmostEqual(moved.duration_seconds, 0.2)
        self.assertIsNone(controller.current("Zed"))

    def test_textual_status_accessibility_focus_and_settings_round_trip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            settings = self.settings(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=settings)
            dialog.show()
            self.application.processEvents()

            self.assertIn("INTERRUPTED", dialog.status.text())
            self.assertIn("Resolve source audio:", dialog.counts.text())
            self.assertIn("Other skipped actions:", dialog.counts.text())
            dialog.status.setStyleSheet("")
            self.assertIn("INTERRUPTED", dialog.status.text())
            self.assertTrue(dialog.collection_tree.hasFocus())
            for button in dialog.findChildren(QPushButton):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())

            dialog.technical.setChecked(True)
            dialog.splitter.setSizes([321, 654])
            dialog._save_settings()
            dialog.close()
            replacement = AuthoringWorkbenchDialog(workspace, settings=settings)

            self.assertTrue(replacement.technical.isChecked())
            self.assertGreater(replacement.splitter.sizes()[0], 0)
            replacement.close()

    def test_current_attempt_projects_exact_fields_and_live_elapsed_time(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id, item = next(iter(state["items"].items()))
            queue_item = next(
                candidate
                for candidate in VoiceGenerationQueue.load(
                    workspace / "queue.jsonl"
                ).items
                if candidate.queue_id == queue_id
            )
            state["active"] = {
                "queue_id": queue_id,
                "line_id": item["line_id"],
                "speaker": "Rhiannon",
                "text": queue_item.text,
                "phase": "retrying",
                "attempt": 2,
                "attempt_limit": 3,
                "total_attempts": 4,
                "seed": 12,
                "started_at": "2026-08-17T00:00:00+00:00",
                "updated_at": "2026-08-17T00:01:00+00:00",
                "last_error": "Earlier limited render",
            }
            atomic_write_json(state_path, state, sort_keys=True)
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                clock=lambda: datetime(2026, 8, 17, 0, 2, 5, tzinfo=timezone.utc),
            )

            self.assertIn(item["line_id"], dialog.active.text())
            self.assertIn("Rhiannon", dialog.active.text())
            self.assertIn("retrying", dialog.active.text())
            self.assertIn("attempt 2 of 3", dialog.active.text())
            self.assertIn("Earlier limited render", dialog.active.text())
            self.assertIn("elapsed 2:05", dialog.active.text())

    def test_child_program_arguments_cwd_and_failed_retry_are_separate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.clear_authoring_state(workspace)
            process = FakeProcess(QProcess.ProcessState.NotRunning)
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                process=process,
            )

            dialog.start_generation()

            self.assertEqual(process.start_calls, 1)
            self.assertEqual(process.program, os.sys.executable)
            self.assertIn("--workspace", process.arguments)
            self.assertEqual(process.cwd, str(workspace))
            self.assertIsNotNone(process.environment)
            self.assertNotIn(
                " ".join([process.program, *process.arguments]), process.arguments
            )

    def test_stop_escalates_and_close_confirmation_never_orphans_child(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            process = FakeProcess(QProcess.ProcessState.Running)
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                process=process,
                stop_timeout_ms=0,
                clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
            )

            dialog.stop_child()
            self.application.processEvents()
            self.assertEqual(process.terminate_calls, 1)
            self.assertEqual(process.kill_calls, 1)

            event = QCloseEvent()
            with patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                dialog.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertTrue(dialog.close_after_stop)
            self.assertEqual(process.terminate_calls, 2)

    def test_output_folder_uses_contained_workspace_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
            )

            with patch(
                "vntts.authoring.workbench_ui.QDesktopServices.openUrl"
            ) as open_url:
                dialog.open_output_folder()

            requested = Path(open_url.call_args.args[0].toLocalFile()).resolve()
            self.assertTrue(requested.is_relative_to(workspace))
            self.assertEqual(requested, workspace / "generated-audio")

    def test_review_actions_follow_selection_and_fail_closed_on_integrity_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            dialog.review_table.setCurrentCell(0, 0)
            self.application.processEvents()

            self.assertTrue(dialog.approve.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())

            (workspace / "queue.jsonl").write_bytes(b"tampered")
            dialog.refresh()

            self.assertIsNone(dialog.summary)
            for action in (
                dialog.approve,
                dialog.reject,
                dialog.generate,
                dialog.retry_failed,
                dialog.open_output,
                dialog.reference_play,
            ):
                self.assertFalse(action.isEnabled(), action.accessibleName())

    def test_mid_refresh_control_mutation_is_caught_and_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))

            def mutate_after_review_rows(path):
                rows = list_review_items(path)
                (workspace / "queue.jsonl").write_bytes(b"tampered after rows")
                return rows

            with patch(
                "vntts.authoring.workbench_ui.list_review_items",
                side_effect=mutate_after_review_rows,
            ):
                dialog.refresh()

            self.assertIsNone(dialog.summary)
            self.assertIn("BLOCKED", dialog.status.text())
            self.assertEqual(dialog.review_table.rowCount(), 0)
            for action in (
                dialog.approve,
                dialog.reject,
                dialog.review_play,
                dialog.generate,
                dialog.retry_failed,
                dialog.open_output,
                dialog.reference_play,
            ):
                self.assertFalse(action.isEnabled(), action.accessibleName())

    def test_failed_row_and_external_owner_disable_review_and_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id = next(iter(state["items"]))
            state["active"] = None
            state["items"][queue_id] = {
                "status": "failed",
                "attempts": 3,
                "seed": 2,
                "last_error": "synthetic failure",
                "updated_at": "2026-08-17T00:00:00+00:00",
            }
            atomic_write_json(state_path, state, sort_keys=True)
            atomic_write_json(
                workspace / "generated-audio/.generation-lease.json",
                {
                    "schema": "vntts.authoring-generation-lease",
                    "schema_version": 1,
                    "queue_sha256": sha256_file(workspace / "queue.jsonl"),
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "process_started_at": process_started_at(os.getpid()),
                    "lease_id": "external-owner",
                    "started_at": "2026-08-17T00:00:00+00:00",
                },
            )
            process = FakeProcess(QProcess.ProcessState.NotRunning)
            dialog = AuthoringWorkbenchDialog(
                workspace, settings=self.settings(root), process=process
            )
            dialog.review_table.setCurrentCell(0, 0)
            self.application.processEvents()

            self.assertIn("RUNNING ELSEWHERE", dialog.status.text())
            self.assertFalse(dialog.retry_failed.isEnabled())
            self.assertIn("Another process", dialog.retry_failed.toolTip())
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())

    def test_polling_error_banner_and_stale_stop_token(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            process = FakeProcess(QProcess.ProcessState.Running)
            dialog = AuthoringWorkbenchDialog(
                workspace, settings=self.settings(root), process=process
            )
            real_refresh = dialog.refresh
            dialog.refresh = Mock()
            dialog.status_timer.timeout.emit()
            dialog.refresh.assert_called_once_with()
            dialog.refresh = real_refresh

            process.state_value = QProcess.ProcessState.NotRunning
            dialog._process_error(QProcess.ProcessError.FailedToStart)
            self.assertIn("PROCESS ERROR", dialog.status.text())
            self.assertIn("FailedToStart", dialog.process_log.toPlainText())

            process.state_value = QProcess.ProcessState.Running
            dialog.local_process_started_at = "preserved-start"
            dialog._stop_generation_token = 7
            dialog._process_error(QProcess.ProcessError.WriteError)
            self.assertEqual(dialog.local_process_started_at, "preserved-start")
            self.assertEqual(dialog._stop_generation_token, 7)
            self.assertIn("PROCESS I/O ERROR WHILE RUNNING", dialog.status.text())

            dialog._process_generation = 1
            dialog._stop_generation_token = 1
            dialog._process_generation = 2
            dialog._kill_if_running(1)
            self.assertEqual(process.kill_calls, 0)

    def test_generated_review_audio_is_revalidated_and_media_status_persists(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            dialog.review_table.setCurrentCell(0, 0)
            selected = dialog._selected_review_item()
            self.assertIsNotNone(selected.audio)
            dialog.player = Mock()

            dialog.play_selected_outcome()
            source = Path(dialog.player.setSource.call_args.args[0].toLocalFile())
            self.assertEqual(source, selected.audio)
            self.assertTrue(source.is_relative_to(workspace / "generated-audio"))
            self.assertIn("PLAYING GENERATED REVIEW AUDIO", dialog.status.text())
            dialog.refresh()
            self.assertIn("PLAYING GENERATED REVIEW AUDIO", dialog.status.text())

            selected.audio.write_bytes(selected.audio.read_bytes() + b"tampered")
            dialog.player.reset_mock()
            dialog.play_selected_outcome()

            dialog.player.setSource.assert_not_called()
            self.assertIsNone(dialog.summary)
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertFalse(dialog.review_play.isEnabled())

    def test_empty_focused_retry_never_becomes_unfiltered_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            process = FakeProcess(QProcess.ProcessState.NotRunning)
            dialog = AuthoringWorkbenchDialog(
                workspace, settings=self.settings(root), process=process
            )

            with (
                patch(
                    "vntts.authoring.workbench_ui.list_review_items",
                    return_value=[],
                ),
                patch("vntts.authoring.workbench_ui.generation_command") as command,
            ):
                dialog.start_failed_retry()

            command.assert_not_called()
            self.assertEqual(process.start_calls, 0)
            self.assertIn("no failed queue IDs remain", dialog.status.text())

    def test_raw_merged_log_preserves_chunk_boundaries_and_split_utf8(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            process = FakeProcess(QProcess.ProcessState.NotRunning)
            dialog = AuthoringWorkbenchDialog(
                workspace, settings=self.settings(root), process=process
            )
            process.readAllStandardOutput = Mock(
                side_effect=[b"abc", b"def\n\xe2", b"\x82\xac", b""]
            )

            dialog._append_process_output()
            dialog._append_process_output()
            dialog._append_process_output()
            dialog._append_process_output(final=True)

            self.assertEqual(dialog.process_log.toPlainText(), "abcdef\n€")

    def test_open_and_play_revalidate_paths_at_click_time(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))

            output = workspace / "generated-audio"
            external_output = root / "external-output"
            output.rename(external_output)
            output.symlink_to(external_output, target_is_directory=True)
            with patch(
                "vntts.authoring.workbench_ui.QDesktopServices.openUrl"
            ) as open_url:
                dialog.open_output_folder()
            open_url.assert_not_called()

            output.unlink()
            external_output.rename(output)
            reference = workspace / "inputs/voice/rhiannon.wav"
            external_reference = root / "external-reference.wav"
            reference.rename(external_reference)
            reference.symlink_to(external_reference)
            dialog.player = Mock()
            dialog.play_reference()

            dialog.player.setSource.assert_not_called()
            self.assertIn("BLOCKED", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
