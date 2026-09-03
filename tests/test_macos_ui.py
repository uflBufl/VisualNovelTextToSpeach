import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.macos_ui import MacOSPermissionsDialog, privacy_urls  # noqa: E402


class MacOSPermissionsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_dialog_shows_status_and_refreshes_after_request(self):
        status_provider = Mock(
            side_effect=[
                {"screen_capture": False, "accessibility": None},
                {"screen_capture": True, "accessibility": False},
            ]
        )
        screen_request = Mock()
        dialog = MacOSPermissionsDialog(
            status_provider=status_provider,
            screen_request=screen_request,
        )

        self.assertEqual(dialog.screen_status.text(), "Not granted")
        self.assertEqual(dialog.accessibility_status.text(), "Status unavailable")
        self.assertIn("auto advance", dialog.note.text())
        self.assertIn("Global hotkeys are unavailable", dialog.note.text())

        dialog.request_screen()

        screen_request.assert_called_once_with()
        self.assertEqual(dialog.screen_status.text(), "Granted")
        self.assertEqual(dialog.accessibility_status.text(), "Not granted")
        self.assertTrue(dialog.request_screen_button.isHidden())
        self.assertEqual(dialog.open_screen_button.text(), "Manage in Settings")
        self.assertFalse(dialog.request_accessibility_button.isHidden())
        self.assertEqual(dialog.open_accessibility_button.text(), "Open Settings")
        dialog.deleteLater()

    def test_dialog_opens_specific_system_settings_page(self):
        url_opener = Mock(return_value=True)
        dialog = MacOSPermissionsDialog(
            status_provider=lambda: {
                "screen_capture": False,
                "accessibility": False,
            },
            url_opener=url_opener,
        )

        dialog.open_settings("accessibility")

        opened_url = url_opener.call_args.args[0]
        self.assertEqual(opened_url.toString(), privacy_urls["accessibility"])
        self.assertEqual(
            dialog.accessibility_status.text(),
            "System Settings opened; status will refresh on return.",
        )
        dialog.deleteLater()

    def test_request_shows_row_local_busy_and_error_state(self):
        dialog_holder = {}

        def failing_request():
            dialog = dialog_holder["dialog"]
            self.assertFalse(dialog.request_screen_button.isEnabled())
            self.assertEqual(dialog.screen_status.text(), "Requesting permission...")
            raise RuntimeError("native request failed")

        dialog = MacOSPermissionsDialog(
            status_provider=lambda: {
                "screen_capture": False,
                "accessibility": False,
            },
            screen_request=failing_request,
        )
        dialog_holder["dialog"] = dialog

        dialog.request_screen()

        self.assertTrue(dialog.request_screen_button.isEnabled())
        self.assertEqual(
            dialog.screen_status.text(), "Request failed: native request failed"
        )
        self.assertEqual(dialog.accessibility_status.text(), "Not granted")
        self.assertEqual(
            dialog.screen_status.accessibleName(),
            "Screen recording permission status",
        )
        dialog.deleteLater()

    def test_status_refreshes_after_returning_from_system_settings(self):
        status_provider = Mock(
            side_effect=[
                {"screen_capture": False, "accessibility": False},
                {"screen_capture": True, "accessibility": False},
            ]
        )
        dialog = MacOSPermissionsDialog(
            status_provider=status_provider,
            url_opener=Mock(return_value=True),
        )

        dialog.open_settings("screen_capture")
        dialog.changeEvent(QEvent(QEvent.Type.WindowActivate))
        self.application.processEvents()

        self.assertEqual(dialog.screen_status.text(), "Granted")
        self.assertEqual(status_provider.call_count, 2)
        dialog.deleteLater()

    def test_status_provider_failure_is_reported_without_modal_dialog(self):
        dialog = MacOSPermissionsDialog(
            status_provider=Mock(side_effect=RuntimeError("status unavailable")),
        )

        self.assertEqual(
            dialog.screen_status.text(),
            "Status check failed: status unavailable",
        )
        self.assertEqual(
            dialog.accessibility_status.text(),
            "Status check failed: status unavailable",
        )
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
