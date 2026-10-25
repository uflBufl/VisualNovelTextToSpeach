import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from vntts.app import TrayApplication  # noqa: E402
from vntts.reading_diagnostics import (  # noqa: E402
    DiagnosticSessionEntry,
    SavedDiagnosticSession,
    delete_saved_diagnostic_session,
    list_diagnostic_sessions,
)
from vntts.support_ui import SupportCenterDialog  # noqa: E402


def session_entry(index, *, size=1500, available=True):
    session_id = f"{index:032x}"
    directory = Path("/sessions") / session_id
    report = {
        "session_id": session_id,
        "started_at": f"2026-10-25T12:00:{index:02}+00:00",
        "status": "finished" if available else "recording",
        "target": {"title": "Reverse: 1999"},
    }
    return DiagnosticSessionEntry(
        directory,
        session_id,
        report,
        size,
        SavedDiagnosticSession(directory, session_id, report) if available else None,
        "" if available else "Diagnostic session is not finished",
        {".jsonl": 2, ".mp4": 1, ".wav": 1, ".json": 1},
    )


class DiagnosticHistoryUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = SupportCenterDialog(Mock(snapshot=Mock(return_value=[])))
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.close)

    def test_names_sizes_and_selection_survive_refresh(self):
        older, newer = session_entry(1), session_entry(2, size=3_000_000)
        self.dialog.set_diagnostic_sessions([newer, older])
        row = self.dialog.reading_history.topLevelItem(0)
        self.assertIn("2026-10-25", row.text(0))
        self.assertEqual(row.text(1), "Reverse: 1999")
        self.assertEqual(self.dialog.reading_history.columnCount(), 3)
        self.assertEqual(row.text(2), "3.0 MB")
        self.assertIn("video: 1", self.dialog.reading_history_detail.toPlainText())
        self.assertIn("2", self.dialog.reading_storage.text())
        self.dialog.reading_history.setCurrentItem(
            self.dialog.reading_history.topLevelItem(1)
        )
        self.dialog.set_diagnostic_sessions([session_entry(3), newer, older])
        self.assertEqual(self.dialog.selected_diagnostic_session(), older)
        exported = []
        self.dialog.reading_export_requested.connect(exported.append)
        self.dialog.reading_export.click()
        self.assertEqual(exported, [older.session_id])

    def test_unavailable_and_empty_history_are_explicit(self):
        self.dialog.set_diagnostic_sessions(
            [session_entry(1, size=None, available=False)]
        )
        self.assertIn("size unavailable for 1", self.dialog.reading_storage.text())
        self.assertIn("not finished", self.dialog.reading_history_detail.toPlainText())
        self.assertIn("Unfinished", self.dialog.reading_history.topLevelItem(0).text(1))
        self.assertFalse(self.dialog.reading_export.isEnabled())
        self.assertFalse(self.dialog.reading_delete.isEnabled())
        self.dialog.set_diagnostic_sessions([])
        self.assertIn("0 B total", self.dialog.reading_storage.text())
        self.assertIsNone(self.dialog.selected_diagnostic_session())

    def test_file_details_use_observed_files_instead_of_requested_media(self):
        entry = replace(session_entry(1), file_counts={".jsonl": 2, ".json": 1})
        self.dialog.set_diagnostic_sessions([entry])
        details = self.dialog.reading_history_detail.toPlainText()
        self.assertIn("logs: 2", details)
        self.assertNotIn("video", details.lower())
        self.assertNotIn("audio", details.lower())
        self.dialog.set_diagnostic_sessions([replace(entry, file_counts=None)])
        self.assertEqual(
            self.dialog.reading_history_detail.toPlainText(),
            "File inventory unavailable.",
        )
        self.dialog.set_diagnostic_sessions([replace(entry, file_counts={})])
        self.assertEqual(
            self.dialog.reading_history_detail.toPlainText(), "No files found."
        )

    def test_finished_media_failure_is_visible_and_remains_exportable(self):
        entry = session_entry(1)
        entry.report.update(
            media_status="failed", media_error="Screen recording permission denied"
        )
        self.dialog.set_diagnostic_sessions([entry])
        self.assertIn(
            "Recording issue", self.dialog.reading_history.topLevelItem(0).text(1)
        )
        self.assertIn(
            "permission denied", self.dialog.reading_history_detail.toPlainText()
        )
        self.assertTrue(self.dialog.reading_export.isEnabled())
        self.assertTrue(self.dialog.reading_delete.isEnabled())

    def test_unreadable_entry_has_an_explicit_warning_and_unknown_files(self):
        entry = replace(
            session_entry(1),
            report=None,
            saved=None,
            size_bytes=None,
            file_counts=None,
            error="Unable to read session metadata",
        )
        self.dialog.set_diagnostic_sessions([entry])
        self.assertIn(
            "Unavailable", self.dialog.reading_history.topLevelItem(0).text(1)
        )
        self.assertIn(
            "Unable to read", self.dialog.reading_history_detail.toPlainText()
        )
        self.assertFalse(self.dialog.reading_export.isEnabled())

    def test_unexpected_media_status_does_not_break_history(self):
        for status in ([], {}, None):
            with self.subTest(status=status):
                entry = session_entry(1)
                entry.report["media_status"] = status
                self.dialog.set_diagnostic_sessions([entry])
                self.assertTrue(self.dialog.reading_export.isEnabled())

    def test_session_and_analysis_errors_are_not_reported_as_success(self):
        for changes, label, reason in (
            ({"error_type": "RuntimeError"}, "Session issue", "RuntimeError"),
            (
                {"analysis_status": "unavailable"},
                "Analysis issue",
                "Analysis is unavailable",
            ),
        ):
            with self.subTest(changes=changes):
                entry = session_entry(1)
                entry.report.update(changes)
                self.dialog.set_diagnostic_sessions([entry])
                self.assertIn(
                    label, self.dialog.reading_history.topLevelItem(0).text(1)
                )
                self.assertIn(reason, self.dialog.reading_history_detail.toPlainText())

    def test_refresh_does_not_reset_selected_details_scroll(self):
        entry = session_entry(1)
        entry.report.update(media_error="Capture failure\n" * 30)
        self.dialog.set_diagnostic_sessions([entry])
        self.dialog.show()
        self.application.processEvents()
        bar = self.dialog.reading_history_detail.verticalScrollBar()
        bar.setValue(bar.maximum())
        position = bar.value()
        self.assertGreater(position, 0)
        self.dialog.refresh_reading_session()
        self.assertEqual(bar.value(), position)

    def test_small_window_keeps_history_rows_and_actions_visible(self):
        self.dialog.set_diagnostic_sessions([session_entry(i) for i in range(1, 6)])
        font = self.dialog.font()
        font.setPointSize(16)
        self.dialog.setFont(font)
        self.dialog.resize(620, 420)
        self.dialog.show()
        self.application.processEvents()
        row_height = self.dialog.reading_history.visualItemRect(
            self.dialog.reading_history.topLevelItem(0)
        ).height()
        self.assertGreaterEqual(
            self.dialog.reading_history.viewport().height(), 3 * row_height
        )
        for widget in (
            self.dialog.reading_history,
            self.dialog.reading_export,
            self.dialog.reading_delete,
            self.dialog.export_button,
        ):
            self.assertTrue(self.dialog.rect().contains(widget.geometry()))

    def test_delete_confirmation_identifies_selected_session_and_cancel_is_safe(self):
        entry = session_entry(1)
        self.dialog.set_diagnostic_sessions([entry])
        deleted = []
        self.dialog.reading_delete_requested.connect(deleted.append)
        with patch(
            "vntts.support_ui.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ) as question:
            self.dialog.reading_delete.click()
        self.assertEqual(deleted, [])
        self.assertIn("2026-10-25", question.call_args.args[2])
        self.assertIn("Reverse: 1999", question.call_args.args[2])
        self.assertIn("1.5 KB", question.call_args.args[2])
        with patch(
            "vntts.support_ui.QMessageBox.question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            self.dialog.reading_delete.click()
        self.assertEqual(deleted, [entry.session_id])

    def test_active_session_and_busy_work_disable_actions_but_not_other_history(self):
        entry = session_entry(1)
        report = {**entry.report, "status": "finalizing"}
        self.dialog.reading_status_provider = lambda: report
        self.dialog.set_diagnostic_sessions([entry])
        self.dialog.refresh_reading_session()
        self.assertFalse(self.dialog.reading_delete.isEnabled())
        self.assertIn("Finishing", self.dialog.reading_history.topLevelItem(0).text(1))
        report["session_id"] = session_entry(2).session_id
        self.dialog.refresh_reading_session()
        self.assertTrue(self.dialog.reading_delete.isEnabled())
        self.assertEqual(
            self.dialog.reading_history.topLevelItem(0).text(1), "Reverse: 1999"
        )
        self.dialog.set_export_started()
        self.dialog.refresh_reading_session()
        self.assertFalse(self.dialog.reading_delete.isEnabled())
        self.dialog.set_export_result(False, "disk full")
        self.assertTrue(self.dialog.reading_delete.isEnabled())
        self.dialog.set_history_busy(True)
        self.assertFalse(self.dialog.reading_export.isEnabled())
        self.assertFalse(self.dialog.export_button.isEnabled())
        self.dialog.set_history_busy(False)
        self.assertTrue(self.dialog.reading_export.isEnabled())
        self.assertTrue(self.dialog.export_button.isEnabled())

    def test_choosing_export_destination_keeps_actions_disabled_during_refresh(self):
        self.dialog.set_diagnostic_sessions([session_entry(1)])
        self.dialog.request_export()
        self.dialog.refresh_reading_session()
        self.assertFalse(self.dialog.export_button.isEnabled())
        self.assertFalse(self.dialog.reading_delete.isEnabled())
        self.dialog.set_export_result(None, "cancelled")
        self.assertTrue(self.dialog.export_button.isEnabled())

    def test_session_state_change_requests_history_without_polling_disk_every_second(
        self,
    ):
        requested = []
        report = {**session_entry(1).report, "status": "recording"}
        self.dialog.reading_status_provider = lambda: report
        self.dialog.reading_history_requested.connect(lambda: requested.append(True))
        self.dialog.refresh_reading_session()
        self.dialog.refresh_reading_session()
        self.assertEqual(len(requested), 1)
        report["status"] = "finished"
        self.dialog.refresh_reading_session()
        self.assertEqual(len(requested), 2)


class DiagnosticHistoryDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tray = SimpleNamespace(
            reading_diagnostic_session=session_entry(2).saved,
            support_dialog=Mock(),
            support_export_runner=Mock(active=False),
            support_history_runner=Mock(active=False),
            _diagnostic_deletion_id=None,
            _diagnostic_history_pending=False,
            _shutting_down=False,
            _build_support_bundle=Mock(),
            controller=Mock(get_latest_diagnostic=Mock(return_value=None)),
            refresh_diagnostic_history=Mock(),
        )

    def test_export_uses_selected_saved_session_without_changing_current_owner(self):
        selected = session_entry(1).saved
        current = self.tray.reading_diagnostic_session
        with patch(
            "vntts.app.load_saved_diagnostic_session", return_value=selected
        ) as load:
            TrayApplication.export_reading_diagnostics(self.tray, selected.session_id)
        self.assertEqual(load.call_args.args[1], selected.session_id)
        self.assertEqual(
            self.tray.support_export_runner.start.call_args.args[-1], selected
        )
        self.assertIs(self.tray.reading_diagnostic_session, current)

    def test_active_export_blocks_delete_and_second_export(self):
        self.tray.support_export_runner.active = True
        TrayApplication.delete_reading_diagnostics(
            self.tray, session_entry(1).session_id
        )
        TrayApplication.export_reading_diagnostics(
            self.tray, session_entry(1).session_id
        )
        with patch("vntts.app.QFileDialog.getSaveFileName") as picker:
            TrayApplication.export_support_bundle(self.tray)
        picker.assert_not_called()
        self.tray.support_history_runner.start.assert_not_called()
        self.tray.support_export_runner.start.assert_not_called()

    def test_active_or_finalizing_target_cannot_be_deleted(self):
        current = SimpleNamespace(
            session_id=session_entry(1).session_id,
            finished=Mock(is_set=Mock(return_value=False)),
        )
        self.tray.reading_diagnostic_session = current
        TrayApplication.delete_reading_diagnostics(self.tray, current.session_id)
        self.tray.support_history_runner.start.assert_not_called()
        TrayApplication.delete_reading_diagnostics(
            self.tray, session_entry(2).session_id
        )
        self.assertIs(
            self.tray.support_history_runner.start.call_args.args[0],
            delete_saved_diagnostic_session,
        )

    def test_deletion_started_during_export_picker_restores_ui_request(self):
        def choose_path(*args):
            self.tray._diagnostic_deletion_id = session_entry(1).session_id
            return "support.zip", "ZIP archives (*.zip)"

        with patch("vntts.app.QFileDialog.getSaveFileName", side_effect=choose_path):
            TrayApplication.export_support_bundle(self.tray)
        self.tray.support_export_runner.start.assert_not_called()
        self.tray.support_dialog.set_export_result.assert_called_once()

    def test_delete_refreshes_history_and_only_clears_deleted_owner(self):
        for deleted_id in (session_entry(1).session_id, session_entry(2).session_id):
            with self.subTest(deleted_id=deleted_id):
                current = session_entry(2).saved
                self.tray.reading_diagnostic_session = current
                self.tray._diagnostic_deletion_id = deleted_id
                TrayApplication._diagnostic_history_finished(self.tray, None, None)
                self.assertEqual(
                    self.tray.reading_diagnostic_session,
                    None if deleted_id == current.session_id else current,
                )
        self.assertEqual(self.tray.refresh_diagnostic_history.call_count, 2)

    def test_refresh_during_worker_is_deferred_and_failure_is_visible(self):
        self.tray.support_history_runner.active = True
        TrayApplication.refresh_diagnostic_history(self.tray)
        self.tray.support_history_runner.start.assert_not_called()
        self.assertTrue(self.tray._diagnostic_history_pending)
        self.tray.support_history_runner.active = False
        TrayApplication._diagnostic_history_finished(
            self.tray, None, PermissionError("denied")
        )
        self.tray.refresh_diagnostic_history.assert_called_once_with()
        self.assertIn(
            "denied", self.tray.support_dialog.set_history_status.call_args.args[0]
        )
        TrayApplication.refresh_diagnostic_history(self.tray)
        self.assertIs(
            self.tray.support_history_runner.start.call_args.args[0],
            list_diagnostic_sessions,
        )


if __name__ == "__main__":
    unittest.main()
