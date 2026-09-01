import hashlib
import json
import os
import socket
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import VoiceManifestError, write_voice_manifest

from tests.symlink_support import symlink_or_skip
from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.bulk_generation import ReviewCommit, process_started_at
from vntts.authoring.cohort_bundle import CohortReviewBundle
from vntts.authoring.workbench import (
    ReviewItem,
    list_review_items,
    prepare_review_audio,
    review_workspace_item,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QProcess, QSettings, Qt, QTimer
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QMessageBox,
        QPushButton,
    )

    from vntts.authoring.workbench_ui import (
        AuthoringWorkbenchDialog,
        VoiceReferenceController,
        _load_workbench_projection,
        _prepare_review_playback,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QProcess = None
    QPoint = None
    QSettings = None
    Qt = None
    QTimer = None
    QCloseEvent = None
    QTest = None
    QMediaPlayer = None
    QMessageBox = None
    QPushButton = None
    QAbstractItemView = None
    AuthoringWorkbenchDialog = None
    VoiceReferenceController = None
    _load_workbench_projection = None
    _prepare_review_playback = None


if AuthoringWorkbenchDialog is not None:
    _ProductionAuthoringWorkbenchDialog = AuthoringWorkbenchDialog

    class AuthoringWorkbenchDialog(_ProductionAuthoringWorkbenchDialog):
        """Keep small unit fixtures synchronous; async behavior has explicit tests."""

        def __init__(self, *arguments, **options):
            options.setdefault("synchronous_projection", True)
            super().__init__(*arguments, **options)


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

    def assert_inspector_control_reachable(self, dialog, control):
        content = dialog.inspector_scroll.widget()
        top = control.mapTo(content, QPoint(0, 0)).y()
        bottom = top + control.height()
        viewport_height = dialog.inspector_scroll.viewport().height()
        scrollbar = dialog.inspector_scroll.verticalScrollBar()
        self.assertGreaterEqual(top, 0)
        self.assertLessEqual(bottom - viewport_height, scrollbar.maximum() + 2)

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for Qt background work")

    def clear_authoring_state(self, workspace):
        state_path = workspace / "generated-audio" / "generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active"] = None
        state["items"] = {}
        state_path.write_text(json.dumps(state), encoding="utf-8")

    def mark_fixture_pending_review(self, workspace):
        state_path = workspace / "generated-audio" / "generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        queue_id = next(iter(state["items"]))
        state["active"] = None
        state["items"][queue_id]["status"] = "generated"
        state["items"][queue_id]["review_status"] = "pending_review"
        atomic_write_json(state_path, state, sort_keys=True)
        return queue_id

    def mark_selected_review_heard(self, dialog):
        selected = dialog._selected_review_item()
        dialog._review_evidence.heard.add(dialog._review_evidence.identity(selected))
        dialog._update_review_actions(preserve_queue_id=True)

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
            self.assertIn("Resolve source audio:", dialog.outcome_details_text.text())
            self.assertIn("Other actions:", dialog.outcome_details_text.text())
            self.assertEqual(dialog.counts.text().count("<br>"), 2)
            dialog.status.setStyleSheet("")
            self.assertIn("INTERRUPTED", dialog.status.text())
            with patch(
                "vntts.authoring.workbench_ui.QAccessible.updateAccessibility"
            ) as announce:
                dialog.review_action_reason.setText(
                    "Review ready for keyboard decision"
                )
            self.assertEqual(
                announce.call_args.args[0].message(),
                "Review ready for keyboard decision",
            )
            self.assertTrue(dialog.review_table.hasFocus())
            for button in dialog.findChildren(QPushButton):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())

            self.assertFalse(dialog.technical.isChecked())
            dialog.review_status.setCurrentText("All statuses")
            dialog.review_character.setCurrentText("Rhiannon")
            dialog.review_collection.setCurrentText("main")
            dialog.review_search.setText("dialogue")
            dialog.technical.setChecked(True)
            dialog.splitter.setSizes([321, 654])
            dialog._save_settings()
            dialog.close()
            replacement = AuthoringWorkbenchDialog(workspace, settings=settings)

            self.assertTrue(replacement.technical.isChecked())
            self.assertGreater(replacement.splitter.sizes()[0], 0)
            self.assertEqual(replacement.review_status.currentText(), "All statuses")
            self.assertEqual(replacement.review_character.currentText(), "Rhiannon")
            self.assertEqual(replacement.review_collection.currentText(), "main")
            self.assertEqual(replacement.review_search.text(), "dialogue")
            replacement.close()

    def test_scrollable_inspector_keeps_every_expanded_section_reachable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            settings = self.settings(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=settings)
            dialog.show()
            self.application.processEvents()

            self.assertEqual(dialog.minimumWidth(), 900)
            self.assertEqual(dialog.minimumHeight(), 640)
            self.assertFalse(dialog.generation_section.isChecked())
            self.assertFalse(dialog.outcome_details.isChecked())
            self.assertFalse(dialog.readiness_details.isChecked())
            self.assertFalse(dialog.voice_box.isChecked())
            self.assertFalse(dialog.technical.isChecked())
            self.assertGreaterEqual(dialog.splitter.widget(0).height(), 320)
            for section in (
                dialog.generation_section,
                dialog.outcome_details,
                dialog.readiness_details,
                dialog.voice_box,
                dialog.technical,
            ):
                self.assertTrue(section.header.isCheckable())
                self.assertTrue(section.header.accessibleName())
                self.assert_inspector_control_reachable(dialog, section.header)

            dialog.resize(1_440, 900)
            for section in (
                dialog.generation_section,
                dialog.outcome_details,
                dialog.readiness_details,
                dialog.voice_box,
                dialog.technical,
            ):
                section.setChecked(True)
            self.application.processEvents()
            self.assertGreaterEqual(dialog.splitter.widget(0).height(), 320)
            for control in (
                dialog.narrator,
                dialog.outcome_details_text,
                dialog.readiness_text,
                dialog.recent_choice,
                dialog.process_log,
                dialog.copy_diagnostics,
            ):
                self.assertTrue(control.isVisibleTo(dialog.inspector_scroll))
                self.assert_inspector_control_reachable(dialog, control)

            dialog._save_settings()
            dialog.close()
            reopened = AuthoringWorkbenchDialog(workspace, settings=settings)
            reopened.show()
            self.application.processEvents()
            self.assertTrue(reopened.generation_section.isChecked())
            self.assertTrue(reopened.outcome_details.isChecked())
            self.assertTrue(reopened.readiness_details.isChecked())
            self.assertTrue(reopened.voice_box.isChecked())
            self.assertTrue(reopened.technical.isChecked())
            for control in (
                reopened.narrator,
                reopened.outcome_details_text,
                reopened.readiness_text,
                reopened.recent_choice,
                reopened.process_log,
            ):
                self.assert_inspector_control_reachable(reopened, control)

            reopened._reset_layout()
            self.assertFalse(reopened.generation_section.isChecked())
            self.assertFalse(reopened.outcome_details.isChecked())
            self.assertFalse(reopened.readiness_details.isChecked())
            self.assertFalse(reopened.voice_box.isChecked())
            self.assertFalse(reopened.technical.isChecked())
            self.assertEqual(reopened.inspector_scroll.verticalScrollBar().value(), 0)

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

    def test_review_scope_is_independent_from_empty_generation_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            queue_id = self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))

            self.assertEqual(dialog.review_status.currentText(), "Awaiting review")
            self.assertEqual(dialog.review_table.rowCount(), 1)
            self.assertEqual(dialog._selected_review_item().queue_id, queue_id)
            for index in range(dialog.collection_tree.topLevelItemCount()):
                dialog.collection_tree.topLevelItem(index).setCheckState(
                    0, Qt.CheckState.Unchecked
                )
            self.application.processEvents()

            self.assertEqual(dialog.collection_selection.collection_ids, ())
            self.assertEqual(dialog.review_table.rowCount(), 1)
            self.assertIn(
                "review remains independently available", dialog.status.text()
            )
            self.assertIn("showing 1 of 1", dialog.review_scope.text())

    def test_successful_empty_review_uses_refresh_not_retry_language(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.clear_authoring_state(workspace)

            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))

            self.assertIsNotNone(dialog.summary)
            self.assertEqual(dialog.review_table.rowCount(), 0)
            self.assertEqual(dialog.reload_authority.text(), "Refresh authority")
            self.assertIn("Review complete", dialog.review_scope.text())
            self.assertIn("no review outcomes", dialog.review_action_reason.text())

    def test_review_filters_are_explicit_and_empty_result_never_means_all(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            reviews = (
                ReviewItem(
                    "rhiannon-pending",
                    "line-1",
                    "Rhiannon",
                    "Rhiannon",
                    "A very particular apple line.",
                    "generated",
                    "pending_review",
                    1,
                    0,
                    None,
                    workspace / "generated-audio/audio/rhiannon.wav",
                    "main",
                    duration_seconds=2.5,
                    words_per_minute=144.0,
                    peak=0.99,
                    technical_flags=("near clipping",),
                ),
                ReviewItem(
                    "narrator-pending",
                    "line-2",
                    "???",
                    "Narrator",
                    "A narrator bridge.",
                    "generated",
                    "pending_review",
                    1,
                    0,
                    None,
                    workspace / "generated-audio/audio/narrator.wav",
                    "side",
                ),
                ReviewItem(
                    "rhiannon-approved",
                    "line-3",
                    "Rhiannon",
                    "Rhiannon",
                    "Already accepted.",
                    "approved",
                    "approved",
                    1,
                    0,
                    None,
                    workspace / "generated-audio/audio/approved.wav",
                    "main",
                ),
                ReviewItem(
                    "narrator-failed",
                    "line-4",
                    "???",
                    "Narrator",
                    "A failed narrator bridge.",
                    "failed",
                    None,
                    3,
                    2,
                    "Typed render completed as limited",
                    None,
                    "side",
                    failure_category="audio limit / missed EOS",
                ),
            )
            dialog._all_reviews = reviews
            dialog._populate_review_filter_choices()
            dialog.review_status.setCurrentText("Awaiting review")
            dialog._apply_review_filters()

            self.assertEqual(dialog.review_table.rowCount(), 2)
            dialog.review_status.setCurrentText("Technical attention")
            self.assertEqual(dialog.review_table.rowCount(), 1)
            self.assertEqual(
                dialog._selected_review_item().queue_id, "rhiannon-pending"
            )
            self.assertIn("near clipping", dialog.review_table.item(0, 6).text())
            dialog.review_status.setCurrentText("Awaiting review")
            dialog.review_character.setCurrentText("Rhiannon")
            self.assertEqual(dialog.review_table.rowCount(), 1)
            self.assertEqual(
                dialog._selected_review_item().queue_id, "rhiannon-pending"
            )
            self.assertFalse(hasattr(dialog, "rhiannon_only"))
            dialog.review_character.setCurrentText("All characters")
            dialog.narrator_only.click()
            self.assertEqual(dialog.review_table.rowCount(), 1)
            self.assertEqual(
                dialog._selected_review_item().queue_id, "narrator-pending"
            )
            self.assertEqual(dialog.review_table.item(0, 2).text(), "Rhiannon")
            dialog.exclude_narrator.setChecked(True)
            self.assertEqual(dialog.review_character.currentText(), "All characters")
            self.assertEqual(dialog.review_table.rowCount(), 1)
            dialog.review_collection.setCurrentText("side")
            self.assertEqual(dialog.review_table.rowCount(), 0)
            dialog.review_search.setText("not present")
            self.assertEqual(dialog.review_table.rowCount(), 0)
            self.assertIn("No outcomes match", dialog.review_scope.text())
            dialog.review_search.clear()
            dialog.exclude_narrator.setChecked(False)
            dialog.review_status.setCurrentText("Failed: audio limit")
            self.assertEqual(dialog.review_table.rowCount(), 1)
            self.assertEqual(dialog._selected_review_item().queue_id, "narrator-failed")
            self.assertIn("missed EOS", dialog.review_table.item(0, 6).text())
            self.assertEqual(dialog.review_table.item(0, 4).text(), "3")

    def test_sequential_pending_navigation_and_592_item_projection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            reviews = tuple(
                ReviewItem(
                    f"queue-{index:03d}",
                    f"line-{index:03d}",
                    "Rhiannon" if index % 2 == 0 else "???",
                    "Rhiannon" if index % 2 == 0 else "Narrator",
                    f"Representative review line {index:03d}",
                    "generated" if index < 590 else "approved",
                    "pending_review" if index < 590 else "approved",
                    1,
                    index,
                    None,
                    workspace / f"generated-audio/audio/{index:03d}.wav",
                    "main" if index % 3 else "side",
                )
                for index in range(592)
            )
            started = time.monotonic()
            dialog._all_reviews = reviews
            dialog._populate_review_filter_choices()
            dialog.review_status.setCurrentText("Awaiting review")
            dialog._apply_review_filters()
            elapsed = time.monotonic() - started

            self.assertEqual(dialog.review_table.rowCount(), 590)
            self.assertEqual(dialog._selected_review_item().queue_id, "queue-000")
            dialog.next_pending.click()
            self.assertEqual(dialog._selected_review_item().queue_id, "queue-001")
            dialog.previous_pending.click()
            self.assertEqual(dialog._selected_review_item().queue_id, "queue-000")
            self.assertLess(elapsed, 2.5)
            self.assertEqual(
                {
                    dialog.review_table.item(row, 0).data(256).queue_id
                    for row in range(dialog.review_table.rowCount())
                },
                {f"queue-{index:03d}" for index in range(590)},
            )

    def test_review_keyboard_shortcuts_invoke_navigation_replay_and_decisions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            first = dialog._selected_review_item()
            second = ReviewItem(
                "second-queue",
                "second-line",
                first.speaker,
                first.voice_character,
                "Second pending line",
                "generated",
                "pending_review",
                first.attempts,
                first.seed,
                None,
                first.audio,
                first.collection_id,
                first.authority,
            )
            dialog._all_reviews = (first, second)
            dialog._apply_review_filters()
            dialog.show()
            dialog.activateWindow()
            dialog.review_table.setFocus()
            self.application.processEvents()
            self.assertEqual(
                dialog.review_table.editTriggers(),
                QAbstractItemView.EditTrigger.NoEditTriggers,
            )
            modifiers = (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
            )

            QTest.keyClick(dialog, Qt.Key.Key_Right, modifiers)
            self.application.processEvents()
            self.assertEqual(dialog._selected_review_item().queue_id, "second-queue")
            dialog.play_selected_outcome = Mock()
            dialog.review_play.setEnabled(True)
            QTest.keyClick(
                dialog,
                Qt.Key.Key_R,
                Qt.KeyboardModifier.ControlModifier,
            )
            dialog.play_selected_outcome.assert_called_once_with()
            dialog.review_selected = Mock()
            dialog.approve.setEnabled(True)
            dialog.reject.setEnabled(True)
            QTest.keyClick(
                dialog.review_table,
                Qt.Key.Key_Enter,
                Qt.KeyboardModifier.ControlModifier,
            )
            QTest.keyClick(
                dialog.review_table,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.ControlModifier,
            )
            QTest.keyClick(
                dialog,
                Qt.Key.Key_Backspace,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.assertEqual(
                dialog.review_selected.call_args_list,
                [call("approved"), call("approved"), call("rejected")],
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

    def test_transient_external_reference_is_rejected_before_it_can_be_cached(self):
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
            with self.assertRaisesRegex(
                VoiceManifestError, "safe.*relative|POSIX separators"
            ):
                VoiceReferenceController(manifest)
            manifest.write_bytes(original)
            self.assertTrue(
                dialog.voice_controller.current("Rhiannon").path.is_relative_to(
                    workspace / "inputs/voice"
                )
            )

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

    def test_review_save_is_nonblocking_and_updates_only_after_durable_return(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            queue_id = self.mark_fixture_pending_review(workspace)
            worker_started = False
            release = False

            def reviewer(path, selected_queue_id, decision, authority):
                nonlocal worker_started
                worker_started = True
                while not release:
                    time.sleep(0.005)
                return review_workspace_item(
                    path, selected_queue_id, decision, authority
                )

            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                reviewer=reviewer,
            )
            self.mark_selected_review_heard(dialog)
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))
            started = time.monotonic()
            dialog.approve.click()
            call_elapsed = time.monotonic() - started
            self.wait_for(lambda: worker_started and bool(heartbeat))

            self.assertLess(call_elapsed, 0.1)
            self.assertTrue(dialog._review_save_active)
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertIn("Saving review", dialog.review_action_reason.text())
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            self.assertIn("Close deferred", dialog.review_action_reason.text())
            dialog.review_selected("rejected")
            self.assertIn("wait for the current", dialog.review_action_reason.text())
            state = json.loads(
                (workspace / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                state["items"][queue_id]["review_status"], "pending_review"
            )

            release = True
            self.wait_for(lambda: not dialog._review_save_active)
            self.wait_for(
                lambda: (
                    json.loads(
                        (workspace / "generated-audio/generation-state.json").read_text(
                            encoding="utf-8"
                        )
                    )["items"][queue_id]["review_status"]
                    == "approved"
                )
            )
            self.application.processEvents()

            self.assertEqual(dialog.review_table.rowCount(), 0)
            self.assertIn("Approved: 1", dialog.counts.text())

    def test_592_item_approve_and_reject_keep_qt_heartbeat_responsive(self):
        for decision in ("approved", "rejected"):
            with self.subTest(decision=decision), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = self.create_workspace(root)
                self.mark_fixture_pending_review(workspace)
                started = False
                release = False

                def reviewer(_path, queue_id, selected_decision, authority):
                    nonlocal started
                    started = True
                    while not release:
                        time.sleep(0.005)
                    return ReviewCommit(
                        queue_id=queue_id,
                        status=(
                            "approved"
                            if selected_decision == "approved"
                            else "generated"
                        ),
                        review_status=selected_decision,
                        updated_at="2026-08-20T12:00:00+00:00",
                        authority=replace(
                            authority,
                            state_sha256="a" * 64,
                            item_sha256="b" * 64,
                        ),
                    )

                dialog = AuthoringWorkbenchDialog(
                    workspace,
                    settings=self.settings(root),
                    reviewer=reviewer,
                )
                first = dialog._selected_review_item()
                dialog._all_reviews = tuple(
                    replace(
                        first,
                        queue_id=(
                            first.queue_id if index == 0 else f"queue-{index:03d}"
                        ),
                        line_id=f"line-{index:03d}",
                        text=f"Review outcome {index}.",
                    )
                    for index in range(592)
                )
                dialog._apply_review_filters()
                self.mark_selected_review_heard(dialog)
                heartbeat = []
                timer = QTimer()
                timer.setInterval(5)
                timer.timeout.connect(lambda: heartbeat.append(time.monotonic()))
                timer.start()

                before = time.monotonic()
                dialog.review_selected(decision)
                call_elapsed = time.monotonic() - before
                self.wait_for(lambda: started and len(heartbeat) >= 2)

                self.assertLess(call_elapsed, 0.1)
                self.assertTrue(dialog._review_save_active)
                self.assertGreaterEqual(len(heartbeat), 2)
                release = True
                self.wait_for(lambda: not dialog._review_save_active)
                timer.stop()
                self.assertIn(f"Saved {decision}", dialog.review_action_reason.text())

    def test_nonterminal_review_updates_next_row_without_full_projection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            result = None

            def reviewer(_path, queue_id, decision, authority):
                nonlocal result
                result = ReviewCommit(
                    queue_id=queue_id,
                    status="approved",
                    review_status=decision,
                    updated_at="2026-08-17T20:00:00+00:00",
                    authority=replace(
                        authority,
                        state_sha256="a" * 64,
                        item_sha256="b" * 64,
                    ),
                )
                return result

            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                reviewer=reviewer,
            )
            first = dialog._selected_review_item()
            second = replace(
                first,
                queue_id="synthetic-second-pending",
                line_id="synthetic:second",
                text="A second pending outcome.",
            )
            dialog._all_reviews = (first, second)
            dialog._selected_review_queue_id = first.queue_id
            dialog._apply_review_filters()
            projection = Mock(side_effect=AssertionError("unexpected full projection"))
            dialog._projection_loader = projection
            self.mark_selected_review_heard(dialog)

            dialog.approve.click()
            self.wait_for(lambda: not dialog._review_save_active)

            self.assertIsNotNone(result)
            projection.assert_not_called()
            selected = dialog._selected_review_item()
            self.assertEqual(selected.queue_id, second.queue_id)
            self.assertEqual(selected.authority.state_sha256, "a" * 64)
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog._projection_active)

    def test_large_authority_projection_keeps_qt_heartbeat_responsive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            (workspace / "generated-audio/audio/transitive-large.bin").write_bytes(
                b"x" * 70_000
            )
            started = False
            release = False

            def slow_loader(*arguments):
                nonlocal started
                started = True
                while not release:
                    time.sleep(0.005)
                return _load_workbench_projection(*arguments)

            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))
            before = time.monotonic()
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                projection_loader=slow_loader,
                synchronous_projection=False,
            )
            elapsed = time.monotonic() - before
            self.wait_for(lambda: started and bool(heartbeat))

            self.assertLess(elapsed, 0.1)
            self.assertTrue(dialog._projection_active)
            self.assertIsNone(dialog.summary)
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            self.assertIn("Close deferred", dialog.status.text())
            release = True
            self.wait_for(lambda: not dialog._projection_active)
            self.assertIsNotNone(dialog.summary)

    def test_transient_review_failure_can_retry_in_dialog_without_file_change(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            queue_id = self.mark_fixture_pending_review(workspace)
            attempts = 0

            def flaky_reviewer(path, selected_queue_id, decision, authority):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("transient review worker failure")
                return review_workspace_item(
                    path, selected_queue_id, decision, authority
                )

            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                reviewer=flaky_reviewer,
            )
            self.mark_selected_review_heard(dialog)
            dialog.approve.click()
            self.wait_for(lambda: not dialog._review_save_active)

            self.assertIsNone(dialog.summary)
            self.assertTrue(dialog.reload_authority.isEnabled())
            self.assertEqual(dialog.reload_authority.text(), "Retry workspace load")
            self.assertIn("transient review worker failure", dialog.status.text())
            dialog.reload_authority.click()

            self.assertIsNotNone(dialog.summary)
            self.assertEqual(dialog.reload_authority.text(), "Refresh authority")
            self.assertEqual(dialog._selected_review_item().queue_id, queue_id)
            self.assertTrue(dialog.approve.isEnabled())

    def test_post_save_authority_projection_does_not_freeze_qt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            load_count = 0
            second_started = False
            release_second = False

            def loader(*arguments):
                nonlocal load_count, second_started
                load_count += 1
                if load_count > 1:
                    second_started = True
                    while not release_second:
                        time.sleep(0.005)
                return _load_workbench_projection(*arguments)

            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                projection_loader=loader,
                synchronous_projection=False,
            )
            self.wait_for(lambda: not dialog._projection_active)
            self.mark_selected_review_heard(dialog)
            heartbeat = []
            timer = QTimer()
            timer.setInterval(5)
            timer.timeout.connect(lambda: heartbeat.append(time.monotonic()))
            timer.start()
            dialog.approve.click()
            self.wait_for(lambda: second_started and len(heartbeat) >= 2)

            self.assertTrue(dialog._projection_active)
            self.assertGreaterEqual(len(heartbeat), 2)
            release_second = True
            self.wait_for(lambda: not dialog._projection_active)
            timer.stop()
            self.assertIn("Approved: 1", dialog.counts.text())

    def test_new_collection_choice_wins_over_stale_background_projection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            calls = 0
            first_started = False
            release_first = False

            def loader(*arguments):
                nonlocal calls, first_started
                calls += 1
                if calls == 1:
                    first_started = True
                    while not release_first:
                        time.sleep(0.005)
                return _load_workbench_projection(*arguments)

            dialog._projection_loader = loader
            dialog._synchronous_projection = False
            dialog.refresh()
            self.wait_for(lambda: first_started)
            main = next(
                dialog.collection_tree.topLevelItem(index)
                for index in range(dialog.collection_tree.topLevelItemCount())
                if dialog.collection_tree.topLevelItem(index).data(
                    0, Qt.ItemDataRole.UserRole
                )
                == "main"
            )
            main.setCheckState(0, Qt.CheckState.Unchecked)
            self.application.processEvents()
            expected = ("source-only",)
            self.assertEqual(dialog._selected_collection_ids, expected)
            release_first = True
            self.wait_for(lambda: calls >= 2 and not dialog._projection_active)

            self.assertEqual(dialog._selected_collection_ids, expected)
            self.assertEqual(dialog.collection_selection.collection_ids, expected)

    def test_consecutive_review_decisions_remain_ordered_and_manifest_is_derived(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            queue_id = self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            dialog.review_status.setCurrentText("All statuses")
            dialog.review_table.setCurrentCell(0, 0)
            self.mark_selected_review_heard(dialog)

            dialog.approve.click()
            self.wait_for(lambda: not dialog._review_save_active)
            self.application.processEvents()
            self.assertEqual(dialog._selected_review_item().review_status, "approved")
            self.mark_selected_review_heard(dialog)
            dialog.reject.click()
            self.wait_for(lambda: not dialog._review_save_active)
            self.application.processEvents()

            state = json.loads(
                (workspace / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads(
                (workspace / "generated-audio/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["items"][queue_id]["status"], "generated")
            self.assertEqual(state["items"][queue_id]["review_status"], "rejected")
            self.assertEqual(manifest["entries"], [])

    def test_stale_visible_row_cannot_overwrite_newer_review_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            queue_id = self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            displayed_authority = dialog._selected_review_item().authority
            self.mark_selected_review_heard(dialog)
            review_workspace_item(workspace, queue_id, "approved")

            dialog.reject.click()
            self.wait_for(lambda: not dialog._review_save_active)
            state = json.loads(
                (workspace / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertIsNotNone(displayed_authority)
            self.assertEqual(state["items"][queue_id]["review_status"], "approved")
            self.assertIsNone(dialog.summary)
            self.assertIn("authority changed", dialog.status.text())

    def test_async_review_failure_is_fail_closed_without_stale_actions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            selected = dialog._selected_review_item()
            self.mark_selected_review_heard(dialog)
            selected.audio.write_bytes(selected.audio.read_bytes() + b"tampered")

            dialog.approve.click()
            self.wait_for(lambda: not dialog._review_save_active)
            self.application.processEvents()

            self.assertIsNone(dialog.summary)
            self.assertIn("BLOCKED", dialog.status.text())
            self.assertIn("Unable to save review", dialog.status.text())
            self.assertEqual(dialog.review_table.rowCount(), 0)
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertFalse(dialog.review_play.isEnabled())

    def test_review_actions_follow_selection_and_fail_closed_on_integrity_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            dialog.review_status.setCurrentText("All statuses")
            dialog.review_table.setCurrentCell(0, 0)
            self.application.processEvents()

            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.mark_selected_review_heard(dialog)
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
            dialog.review_status.setCurrentText("All statuses")
            dialog.review_table.setCurrentCell(0, 0)
            selected = dialog._selected_review_item()
            self.assertIsNotNone(selected.audio)
            dialog.player = Mock()

            dialog.play_selected_outcome()
            self.wait_for(lambda: not dialog._playback_prepare_active)
            played = bytes(dialog._review_playback_buffer.data())
            self.assertEqual(
                hashlib.sha256(played).hexdigest(), selected.authority.audio_sha256
            )
            dialog.player.setSourceDevice.assert_called_once()
            self.assertIn("PLAYING GENERATED REVIEW AUDIO", dialog.status.text())
            dialog.refresh()
            self.assertIn("PLAYING GENERATED REVIEW AUDIO", dialog.status.text())

            selected.audio.write_bytes(selected.audio.read_bytes() + b"tampered")
            dialog.player.reset_mock()
            dialog.play_selected_outcome()
            self.wait_for(lambda: not dialog._playback_prepare_active)

            self.assertTrue(
                all(
                    invocation.args[0].isEmpty()
                    for invocation in dialog.player.setSource.call_args_list
                )
            )
            self.assertIsNone(dialog.summary)
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertFalse(dialog.review_play.isEnabled())

    def test_review_playback_restores_actions_and_keeps_navigation_fixed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            first = dialog._selected_review_item()
            second = replace(
                first,
                queue_id="second-pending-review",
                line_id="second-line",
                text="A second pending line.",
            )
            dialog._all_reviews = (first, second)
            dialog._apply_review_filters()
            dialog.resize(900, 640)
            dialog.show()
            self.application.processEvents()
            dialog.player = Mock()
            controls = (
                dialog.previous_pending,
                dialog.next_pending,
                dialog.review_play,
                dialog.review_stop,
                dialog.approve,
                dialog.reject,
                dialog.reload_authority,
            )
            self.assertEqual(
                [dialog.review_actions_layout.indexOf(control) for control in controls],
                list(range(len(controls))),
            )
            self.assertEqual(
                {control.geometry().y() for control in controls[:4]},
                {controls[0].geometry().y()},
            )
            self.assertEqual(
                {control.geometry().y() for control in controls[4:]},
                {controls[4].geometry().y()},
            )
            self.assertGreater(controls[4].geometry().y(), controls[0].geometry().y())
            self.assertTrue(
                all(
                    control.width() >= control.sizeHint().width()
                    for control in controls
                )
            )
            initial_positions = tuple(control.geometry().x() for control in controls)

            dialog.play_selected_outcome()
            self.wait_for(lambda: not dialog._playback_prepare_active)
            self.application.processEvents()

            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertTrue(dialog.review_stop.isEnabled())
            self.assertEqual(
                tuple(control.geometry().x() for control in controls), initial_positions
            )

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertTrue(dialog.approve.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertFalse(dialog.review_stop.isEnabled())
            self.assertIsNone(dialog._review_playback_buffer)
            self.assertEqual(
                tuple(control.geometry().x() for control in controls), initial_positions
            )

            dialog.play_selected_outcome()
            self.wait_for(lambda: not dialog._playback_prepare_active)
            dialog.stop_preview()
            self.assertTrue(dialog.approve.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())

            dialog.play_selected_outcome()
            self.wait_for(lambda: not dialog._playback_prepare_active)
            dialog._media_error(None, "simulated playback failure")
            self.assertTrue(dialog.approve.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertIsNone(dialog._review_playback_buffer)

            dialog.play_selected_outcome()
            self.wait_for(lambda: not dialog._playback_prepare_active)
            dialog.review_table.setCurrentCell(1, 0)
            self.application.processEvents()
            self.assertEqual(
                dialog._selected_review_item().queue_id, "second-pending-review"
            )
            self.assertFalse(dialog.approve.isEnabled())
            self.assertFalse(dialog.reject.isEnabled())
            self.assertIsNone(dialog._review_playback_buffer)
            self.assertEqual(
                tuple(control.geometry().x() for control in controls), initial_positions
            )

    def test_review_playback_uses_captured_bytes_after_source_replacement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            dialog = AuthoringWorkbenchDialog(workspace, settings=self.settings(root))
            selected = dialog._selected_review_item()
            expected = selected.audio.read_bytes()

            def replace_after_validation(item):
                captured = prepare_review_audio(item)
                write_pcm16_wav(
                    selected.audio,
                    np.full(1_600, 0.2, dtype=np.float32),
                    16_000,
                )
                return captured

            with patch(
                "vntts.authoring.workbench_ui.prepare_review_audio",
                side_effect=replace_after_validation,
            ):
                dialog.play_selected_outcome()
                self.wait_for(lambda: not dialog._playback_prepare_active)

            self.assertIsNotNone(dialog.summary)
            self.assertEqual(dialog._review_playback_buffer.data().data(), expected)
            self.assertIn("PLAYING GENERATED REVIEW AUDIO", dialog.status.text())

    def test_workbench_builds_and_opens_dedicated_specialist_bundle(self):
        class FakeSpecialistDialog:
            def __init__(self):
                self.finished = FakeSignal()
                self.modal = None
                self.open_calls = 0

            def setModal(self, value):
                self.modal = value

            def open(self):
                self.open_calls += 1

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            captured = []
            bundle = CohortReviewBundle(
                "a" * 64,
                {"bundle_id": "a" * 64},
            )
            specialist = FakeSpecialistDialog()

            def build(paths):
                captured.append(tuple(paths))
                return bundle

            factory = Mock(return_value=specialist)
            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                cohort_bundle_builder=build,
                specialist_reviewer_factory=factory,
            )

            dialog.specialist_review.click()
            self.wait_for(lambda: not dialog._specialist_active)

            self.assertEqual(captured, [(workspace.resolve(),)])
            factory.assert_called_once_with(bundle, dialog)
            self.assertTrue(specialist.modal)
            self.assertEqual(specialist.open_calls, 1)
            self.assertFalse(dialog.specialist_review.isEnabled())
            self.assertIn("aaaaaaaaaaaa", dialog.specialist_review_status.text())

            with patch.object(dialog, "refresh") as refresh:
                specialist.finished.emit(0)

            refresh.assert_called_once_with()
            self.assertTrue(dialog.specialist_review.isEnabled())

    def test_specialist_bundle_build_failure_is_retriable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)

            def fail(_paths):
                raise RuntimeError("state changed during bundle build")

            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                cohort_bundle_builder=fail,
            )

            dialog.specialist_review.click()
            self.wait_for(lambda: not dialog._specialist_active)

            self.assertTrue(dialog.specialist_review.isEnabled())
            self.assertIn("state changed", dialog.specialist_review_status.text())
            self.assertIn("retry", dialog.specialist_review_status.text().lower())

    def test_review_playback_preparation_keeps_qt_heartbeat_responsive(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.create_workspace(root)
            self.mark_fixture_pending_review(workspace)
            started = False
            release = False

            def slow_preparer(path, selected):
                nonlocal started
                started = True
                while not release:
                    time.sleep(0.005)
                return _prepare_review_playback(path, selected)

            dialog = AuthoringWorkbenchDialog(
                workspace,
                settings=self.settings(root),
                playback_preparer=slow_preparer,
            )
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))
            before = time.monotonic()
            dialog.play_selected_outcome()
            elapsed = time.monotonic() - before
            self.wait_for(lambda: started and bool(heartbeat))

            self.assertLess(elapsed, 0.1)
            self.assertTrue(dialog._playback_prepare_active)
            self.assertFalse(dialog.review_play.isEnabled())
            release = True
            self.wait_for(lambda: not dialog._playback_prepare_active)
            self.assertIsNotNone(dialog._review_playback_buffer)

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
            symlink_or_skip(output, external_output, target_is_directory=True)
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
            symlink_or_skip(reference, external_reference)
            dialog.player = Mock()
            dialog.play_reference()

            dialog.player.setSource.assert_not_called()
            self.assertIn("BLOCKED", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
