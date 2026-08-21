import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.support_ui import SupportCenterDialog  # noqa: E402


class FakeEventLog:
    def __init__(self, entries=()):
        self.entries = list(entries)

    def snapshot(self):
        return list(self.entries)


def event(index):
    return {
        "recorded_at": f"2026-08-22T12:00:{index:02d}+03:00",
        "level": "status",
        "message": f"event {index}",
    }


class SupportCenterDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_new_events_preserve_manual_selection_until_requested(self):
        log = FakeEventLog(event(index) for index in range(100))
        dialog = SupportCenterDialog(log)
        dialog.show()
        self.application.processEvents()
        dialog.refresh()
        cursor = dialog.events.textCursor()
        cursor.setPosition(2)
        cursor.setPosition(9, QTextCursor.MoveMode.KeepAnchor)
        dialog.events.setTextCursor(cursor)
        original_selection = cursor.selectedText()

        log.entries.append(event(100))
        dialog.refresh()

        self.assertEqual(dialog.events.textCursor().selectedText(), original_selection)
        self.assertTrue(dialog.new_events_button.isVisible())
        self.assertEqual(dialog.pending_event_count, 1)
        dialog.show_new_events()
        self.application.processEvents()
        self.assertFalse(dialog.new_events_button.isVisible())
        self.assertEqual(
            dialog.events.verticalScrollBar().value(),
            dialog.events.verticalScrollBar().maximum(),
        )
        dialog.close()
        dialog.deleteLater()

    def test_log_rotation_is_visible_without_forcing_manual_viewport(self):
        log = FakeEventLog(event(index) for index in range(100))
        dialog = SupportCenterDialog(log)
        dialog.show()
        self.application.processEvents()
        dialog.refresh()
        dialog.events.verticalScrollBar().setValue(0)

        log.entries = [event(30), event(31)]
        dialog.refresh()

        self.assertIn("event 30", dialog.events.toPlainText())
        self.assertTrue(dialog.new_events_button.isVisible())
        self.assertEqual(dialog.pending_event_count, 2)
        dialog.close()
        dialog.deleteLater()

    def test_export_progress_cancel_failure_and_retry_are_local(self):
        dialog = SupportCenterDialog(FakeEventLog())
        requested = []
        dialog.export_requested.connect(lambda: requested.append(True))

        dialog.request_export()

        self.assertEqual(requested, [True])
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertIn("Choosing", dialog.operation_status.text())
        dialog.set_export_result(None, "Support report export cancelled.")
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("cancelled", dialog.operation_status.text())

        dialog.request_export()
        dialog.set_export_result(False, "disk full")
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("disk full", dialog.operation_status.text())

        dialog.request_export()
        dialog.set_export_result(True, "/tmp/support.zip")
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("/tmp/support.zip", dialog.operation_status.text())
        dialog.close()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
