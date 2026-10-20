import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

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
        self.assertEqual(dialog.accessibility_status.text(), "Could not check")
        self.assertIn("Grant Screen Recording", dialog.note.text())
        self.assertTrue(dialog.request_accessibility_button.isHidden())

        dialog.request_screen()

        screen_request.assert_called_once_with()
        self.assertEqual(dialog.screen_status.text(), "Granted")
        self.assertEqual(dialog.accessibility_status.text(), "Not granted")
        self.assertTrue(dialog.request_screen_button.isHidden())
        self.assertEqual(dialog.open_screen_button.text(), "Manage in Settings")
        self.assertFalse(dialog.request_accessibility_button.isHidden())
        self.assertEqual(dialog.open_accessibility_button.text(), "Open Settings")
        self.assertIn("If you just changed Screen Recording", dialog.note.text())
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
            dialog.operation_status.toPlainText(),
            "Accessibility: System Settings opened. Status will refresh on return.",
        )
        dialog.deleteLater()

    def test_request_preserves_permission_state_during_pending_and_failure(self):
        dialog_holder = {}

        def failing_request():
            dialog = dialog_holder["dialog"]
            self.assertFalse(dialog.request_screen_button.isEnabled())
            self.assertEqual(dialog.screen_status.text(), "Not granted")
            self.assertEqual(
                dialog.operation_status.toPlainText(),
                "Requesting Screen Recording permission...",
            )
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
            dialog.operation_status.toPlainText(),
            "Screen Recording: request failed. Try Open Settings.\nnative request failed",
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
        QApplication.sendEvent(dialog, QEvent(QEvent.Type.WindowActivate))
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
            "Could not check",
        )
        self.assertEqual(
            dialog.accessibility_status.text(),
            "Could not check",
        )
        self.assertTrue(dialog.request_screen_button.isHidden())
        self.assertTrue(dialog.request_accessibility_button.isHidden())
        self.assertIn(
            "Could not check permissions", dialog.operation_status.toPlainText()
        )
        self.assertEqual(
            dialog.operation_status.toPlainText().count("status unavailable"), 1
        )
        dialog.deleteLater()

    def test_native_request_refreshes_again_on_return(self):
        provider = Mock(
            side_effect=[
                {"screen_capture": False, "accessibility": False},
                {"screen_capture": False, "accessibility": False},
                {"screen_capture": True, "accessibility": False},
            ]
        )
        dialog = MacOSPermissionsDialog(status_provider=provider, screen_request=Mock())
        dialog.request_screen()
        self.assertEqual(dialog.screen_status.text(), "Not granted")
        QApplication.sendEvent(dialog, QEvent(QEvent.Type.WindowActivate))
        self.application.processEvents()
        self.assertEqual(dialog.screen_status.text(), "Granted")
        QApplication.sendEvent(dialog, QEvent(QEvent.Type.WindowActivate))
        self.application.processEvents()
        self.assertEqual(provider.call_count, 3)
        dialog.deleteLater()

    def test_settings_failure_preserves_known_permission_and_allows_retry(self):
        for failure in (False, RuntimeError("Settings service unavailable")):
            with self.subTest(failure=failure):
                opener = (
                    Mock(side_effect=failure)
                    if isinstance(failure, Exception)
                    else Mock(return_value=failure)
                )
                dialog = MacOSPermissionsDialog(
                    status_provider=lambda: {
                        "screen_capture": False,
                        "accessibility": True,
                    },
                    url_opener=opener,
                )
                dialog.open_settings("accessibility")
                self.assertEqual(dialog.accessibility_status.text(), "Granted")
                self.assertTrue(dialog.request_accessibility_button.isHidden())
                self.assertIn(
                    "Accessibility: could not open",
                    dialog.operation_status.toPlainText(),
                )
                self.assertFalse(dialog._refresh_on_activate)
                opener.side_effect = None
                opener.return_value = True
                dialog.open_settings("accessibility")
                self.assertIn(
                    "System Settings opened", dialog.operation_status.toPlainText()
                )
                self.assertNotIn(
                    "could not open", dialog.operation_status.toPlainText()
                )
                dialog.deleteLater()

    def test_pending_request_guards_other_actions_and_restores_controls_on_failure(
        self,
    ):
        provider = Mock(return_value={"screen_capture": False, "accessibility": False})
        other_request = Mock()
        opener = Mock(return_value=True)

        def request():
            self.assertFalse(dialog.permission_fields.isEnabled())
            self.assertFalse(dialog.refresh_button.isEnabled())
            dialog.request_accessibility()
            dialog.request_screen()
            dialog.open_settings("accessibility")
            dialog.refresh()
            QApplication.sendEvent(dialog, QEvent(QEvent.Type.WindowActivate))
            self.application.processEvents()
            self.assertEqual(provider.call_count, 1)
            self.assertIn(
                "Requesting Screen Recording", dialog.operation_status.toPlainText()
            )
            raise RuntimeError("native request failed")

        dialog = MacOSPermissionsDialog(
            status_provider=provider,
            screen_request=request,
            accessibility_request=other_request,
            url_opener=opener,
        )
        dialog.open_settings("screen_capture")
        dialog.request_screen()
        other_request.assert_not_called()
        self.assertEqual(opener.call_count, 1)
        self.assertTrue(dialog.permission_fields.isEnabled())
        self.assertTrue(dialog.refresh_button.isEnabled())
        self.assertFalse(dialog._request_active)
        self.assertIn("request failed", dialog.operation_status.toPlainText())
        dialog.deleteLater()

    def test_long_error_stays_scrollable_and_clears_after_refresh(self):
        error = "Native permission service unavailable. " * 40
        provider = Mock(
            side_effect=[
                RuntimeError(error),
                {"screen_capture": True, "accessibility": None},
            ]
        )
        dialog = MacOSPermissionsDialog(status_provider=provider)
        dialog.show()
        self.application.processEvents()
        self.assertLessEqual(dialog.width(), 640)
        self.assertGreater(dialog.operation_status.verticalScrollBar().maximum(), 0)
        self.assertIn(error, dialog.operation_status.toPlainText())
        dialog.operation_status.setFocus()
        scrollbar = dialog.operation_status.verticalScrollBar()
        for _ in range(40):
            QTest.keyClick(dialog.operation_status, Qt.Key.Key_PageDown)
            if scrollbar.value() == scrollbar.maximum():
                break
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        QTest.keySequence(dialog.operation_status, QKeySequence.StandardKey.SelectAll)
        QTest.keySequence(dialog.operation_status, QKeySequence.StandardKey.Copy)
        self.assertEqual(
            self.application.clipboard().text(), dialog.operation_status.toPlainText()
        )
        dialog.refresh()
        self.assertEqual(dialog.operation_status.toPlainText(), "")
        self.assertEqual(dialog.screen_status.text(), "Granted")
        self.assertEqual(dialog.accessibility_status.text(), "Could not check")
        self.assertTrue(dialog.request_accessibility_button.isHidden())
        self.assertEqual(dialog.open_accessibility_button.text(), "Open Settings")
        dialog.close()
        dialog.deleteLater()

    def test_keyboard_reaches_and_activates_permission_actions(self):
        screen_request, accessibility_request = Mock(), Mock()
        opener = Mock(return_value=True)
        provider = Mock(return_value={"screen_capture": False, "accessibility": False})
        dialog = MacOSPermissionsDialog(
            status_provider=provider,
            screen_request=screen_request,
            accessibility_request=accessibility_request,
            url_opener=opener,
        )
        dialog.show()
        self.application.processEvents()
        controls = [
            dialog.request_screen_button,
            dialog.open_screen_button,
            dialog.request_accessibility_button,
            dialog.open_accessibility_button,
            dialog.operation_status,
            dialog.refresh_button,
            dialog.findChild(QDialogButtonBox).button(
                QDialogButtonBox.StandardButton.Close
            ),
        ]
        controls[0].setFocus()
        for control in controls:
            self.assertIs(self.application.focusWidget(), control)
            QTest.keyClick(control, Qt.Key.Key_Tab)
        for control in reversed(controls):
            QTest.keyClick(self.application.focusWidget(), Qt.Key.Key_Backtab)
            self.assertIs(self.application.focusWidget(), control)
        for control in controls[:4]:
            QTest.keyClick(control, Qt.Key.Key_Space)
            self.assertTrue(control.accessibleDescription())
        screen_request.assert_called_once_with()
        accessibility_request.assert_called_once_with()
        self.assertEqual(
            [call.args[0].toString() for call in opener.call_args_list],
            [privacy_urls["screen_capture"], privacy_urls["accessibility"]],
        )
        calls_before_refresh = provider.call_count
        QTest.keyClick(dialog.refresh_button, Qt.Key.Key_Space)
        self.assertEqual(provider.call_count, calls_before_refresh + 1)
        self.assertEqual(dialog.operation_status.toPlainText(), "")
        QTest.keyClick(controls[-1], Qt.Key.Key_Space)
        self.assertFalse(dialog.isVisible())
        dialog.deleteLater()

    def test_escape_can_close_during_request_without_restarting_it(self):
        def request():
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            self.assertFalse(dialog.isVisible())

        dialog = MacOSPermissionsDialog(
            status_provider=lambda: {"screen_capture": False, "accessibility": False},
            screen_request=request,
        )
        dialog.show()
        self.application.processEvents()
        dialog.request_screen()
        self.assertFalse(dialog.isVisible())
        self.assertTrue(dialog.permission_fields.isEnabled())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
