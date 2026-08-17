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
    from PySide6.QtCore import QProcess, QSettings, Qt
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtTest import QTest
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
    Qt = None
    QCloseEvent = None
    QTest = None
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
            QTest.keyClick(dialog.collection_tree, Qt.Key.Key_Tab)
            self.application.processEvents()
            self.assertTrue(dialog.readiness_details.hasFocus())
            QTest.keyClick(dialog.readiness_details, Qt.Key.Key_Tab)
            self.application.processEvents()
            self.assertTrue(dialog.recent_choice.hasFocus())
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

    def test_collection_selection_persists_and_drives_exact_child_filter(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.clear_authoring_state(workspace)
            settings = self.settings(root)
            process = FakeProcess(QProcess.ProcessState.NotRunning)
            dialog = AuthoringWorkbenchDialog(
                workspace, settings=settings, process=process
            )
            captured = []

            def command(_workspace, *, queue_ids=None, **_kwargs):
                captured.append(queue_ids)
                return (os.sys.executable, "-c", "pass")

            with patch(
                "vntts.authoring.workbench_ui.generation_command",
                side_effect=command,
            ):
                dialog.start_generation()

            self.assertEqual(
                captured, [dialog.collection_selection.readiness.queue_ids]
            )
            self.assertEqual(len(captured[0]), 1)
            process.state_value = QProcess.ProcessState.NotRunning
            dialog.collection_tree.topLevelItem(0).setCheckState(
                0, Qt.CheckState.Unchecked
            )
            self.application.processEvents()
            self.assertEqual(
                dialog.collection_selection.collection_ids, ("source-only",)
            )
            self.assertIn("NO QUEUED ITEMS IN SELECTION", dialog.status.text())
            for index in range(dialog.collection_tree.topLevelItemCount()):
                dialog.collection_tree.topLevelItem(index).setCheckState(
                    0, Qt.CheckState.Unchecked
                )
            self.application.processEvents()

            self.assertEqual(dialog.collection_selection.collection_ids, ())
            self.assertEqual(dialog.collection_selection.queue_ids, ())
            self.assertFalse(dialog.generate.isEnabled())
            self.assertIn("NO COLLECTION SELECTED", dialog.status.text())
            before = process.start_calls
            dialog.start_generation()
            self.assertEqual(process.start_calls, before)
            self.assertIn("no ready queue IDs", dialog.status.text())
            dialog.close()

            reopened = AuthoringWorkbenchDialog(workspace, settings=settings)
            self.assertEqual(reopened.collection_selection.collection_ids, ())
            self.assertTrue(
                all(
                    reopened.collection_tree.topLevelItem(index).checkState(0)
                    == Qt.CheckState.Unchecked
                    for index in range(reopened.collection_tree.topLevelItemCount())
                )
            )

    def test_recent_reference_choices_are_contained_validated_and_searchable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            settings = self.settings(root)
            workspace_hash = sha256_file(workspace / "workspace.json")
            dialog = AuthoringWorkbenchDialog(workspace, settings=settings)

            dialog._move_reference(1)
            stored_key = dialog._workspace_settings_key("recent-references")
            stored = settings.value(stored_key)
            self.assertTrue(any('"index":1' in str(value) for value in stored))
            settings.setValue(
                stored_key,
                [
                    *stored,
                    json.dumps({"character": "../../escape", "index": 0}),
                    json.dumps({"character": "Rhiannon", "index": 99}),
                    "/absolute/path.wav",
                ],
            )
            settings.sync()
            dialog.close()

            reopened = AuthoringWorkbenchDialog(workspace, settings=settings)
            choices = [
                tuple(reopened.recent_choice.itemData(index))
                for index in range(reopened.recent_choice.count())
            ]
            self.assertIn(("Rhiannon", 1), choices)
            self.assertNotIn(("../../escape", 0), choices)
            self.assertNotIn(("Rhiannon", 99), choices)
            choice_index = choices.index(("Rhiannon", 1))
            reopened.recent_choice.setCurrentIndex(choice_index)
            reopened._choose_recent_reference(choice_index)
            reference = reopened.voice_controller.current("Rhiannon")

            self.assertEqual(reference.index, 1)
            self.assertTrue(reference.path.is_relative_to(workspace / "inputs/voice"))
            self.assertEqual(sha256_file(workspace / "workspace.json"), workspace_hash)
            reopened.recent_choice.setEditText("does not exist")
            reopened._choose_typed_recent_reference()
            self.assertIn("RECENT PREVIEW UNAVAILABLE", reopened.status.text())

    def test_cached_transient_external_reference_is_never_played_or_reused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            manifest = workspace / "inputs/voice/manifest.json"
            original = manifest.read_bytes()
            outside = root / "outside.wav"
            outside.write_bytes(b"external")
            manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Rhiannon",
                                "speaker": "Rhiannon",
                                "reference": str(outside),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stale = VoiceReferenceController(manifest)
            manifest.write_bytes(original)
            dialog.voice_controller = stale
            dialog.voice_character.clear()
            dialog.voice_character.addItem("Rhiannon")
            dialog.player = Mock()

            dialog.play_reference()
            played = Path(dialog.player.setSource.call_args.args[0].toLocalFile())
            dialog._apply_recent_reference("Rhiannon", 0)
            recent = dialog.voice_controller.current("Rhiannon")

            self.assertNotEqual(played, outside)
            self.assertTrue(played.is_relative_to(workspace / "inputs/voice"))
            self.assertTrue(recent.path.is_relative_to(workspace / "inputs/voice"))

    def test_readiness_details_show_immutable_utc_history_and_persist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            settings = self.settings(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=settings)

            self.assertIn("Source created: ", dialog.readiness_text.text())
            self.assertIn("Source updated: ", dialog.readiness_text.text())
            self.assertIn("Imported: ", dialog.readiness_text.text())
            self.assertIn("Workspace created: ", dialog.readiness_text.text())
            self.assertIn(" UTC", dialog.readiness_text.text())
            self.assertNotIn(
                "Source job time: unavailable", dialog.readiness_text.text()
            )
            self.assertIn("inputs/story-index.jsonl", dialog.readiness_text.text())
            self.assertTrue(dialog.recent_choice.accessibleName())
            self.assertTrue(dialog.readiness_details.accessibleName())
            dialog.readiness_details.setChecked(True)
            dialog._save_settings()
            dialog.close()

            reopened = AuthoringWorkbenchDialog(workspace, settings=settings)
            self.assertTrue(reopened.readiness_details.isChecked())
            self.assertTrue(reopened.readiness_text.isVisibleTo(reopened))

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

    def test_idle_poll_skips_full_refresh_until_authority_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
            )
            real_refresh = dialog.refresh
            dialog.refresh = Mock()

            dialog.status_timer.timeout.emit()
            dialog.refresh.assert_not_called()

            state = workspace / "generated-audio/generation-state.json"
            state.write_bytes(state.read_bytes() + b"\n")
            dialog.status_timer.timeout.emit()
            dialog.refresh.assert_called_once_with()
            dialog.refresh = real_refresh

    def test_polling_error_banner_and_stale_stop_token(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            process = FakeProcess(QProcess.ProcessState.Running)
            dialog = AuthoringWorkbenchDialog(
                workspace, settings=self.settings(root), process=process
            )

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
