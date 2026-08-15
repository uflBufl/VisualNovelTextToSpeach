import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
        dialog.deleteLater()

    def test_dialog_opens_specific_system_settings_page(self):
        url_opener = Mock()
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
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
