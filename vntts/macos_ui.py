from PySide6.QtCore import QEvent, QEventLoop, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from vntts.macos import (
    get_macos_permission_status,
    request_accessibility_permission,
    request_screen_capture_permission,
)

privacy_urls = {
    "screen_capture": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
    ),
    "accessibility": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
    ),
}


class MacOSPermissionsDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        status_provider=None,
        screen_request=None,
        accessibility_request=None,
        url_opener=None,
    ):
        super().__init__(parent)
        self.status_provider = status_provider or get_macos_permission_status
        self.screen_request = screen_request or request_screen_capture_permission
        self.accessibility_request = (
            accessibility_request or request_accessibility_permission
        )
        self.url_opener = url_opener or QDesktopServices.openUrl
        self._refresh_on_activate = False
        self.setWindowTitle("macOS permissions")
        self.setMinimumWidth(620)

        self.screen_status = QLabel()
        self.accessibility_status = QLabel()
        self.request_screen_button = QPushButton("Request")
        self.open_screen_button = QPushButton("Open Settings")
        self.request_accessibility_button = QPushButton("Request")
        self.open_accessibility_button = QPushButton("Open Settings")
        self.screen_status.setAccessibleName("Screen recording permission status")
        self.accessibility_status.setAccessibleName("Accessibility permission status")
        self.request_screen_button.setAccessibleDescription(
            "Request screen recording permission from macOS"
        )
        self.open_screen_button.setAccessibleDescription(
            "Open screen recording permissions in System Settings"
        )
        self.request_accessibility_button.setAccessibleDescription(
            "Request accessibility permission from macOS"
        )
        self.open_accessibility_button.setAccessibleDescription(
            "Open accessibility permissions in System Settings"
        )
        self.request_screen_button.clicked.connect(self.request_screen)
        self.open_screen_button.clicked.connect(
            lambda: self.open_settings("screen_capture")
        )
        self.request_accessibility_button.clicked.connect(self.request_accessibility)
        self.open_accessibility_button.clicked.connect(
            lambda: self.open_settings("accessibility")
        )

        screen_actions = QHBoxLayout()
        screen_actions.addWidget(self.screen_status)
        screen_actions.addStretch()
        screen_actions.addWidget(self.request_screen_button)
        screen_actions.addWidget(self.open_screen_button)
        accessibility_actions = QHBoxLayout()
        accessibility_actions.addWidget(self.accessibility_status)
        accessibility_actions.addStretch()
        accessibility_actions.addWidget(self.request_accessibility_button)
        accessibility_actions.addWidget(self.open_accessibility_button)
        form = QFormLayout()
        form.addRow("Screen recording", screen_actions)
        form.addRow("Accessibility", accessibility_actions)

        self.note = QLabel(
            "After granting a permission, quit and reopen the application. "
            "Screen recording is required for OCR capture; Accessibility is "
            "required only for auto advance. Global hotkeys are unavailable in "
            "the current macOS build; use the control window or compact controls."
        )
        self.note.setWordWrap(True)
        self.refresh_button = QPushButton("Refresh status")
        self.refresh_button.clicked.connect(self.refresh)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.note)
        layout.addWidget(self.refresh_button)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self):
        try:
            status = self.status_provider()
        except Exception as error:
            message = f"Status check failed: {error}"
            self.screen_status.setText(message)
            self.accessibility_status.setText(message)
            return
        self.screen_status.setText(self._status_text(status["screen_capture"]))
        self.accessibility_status.setText(self._status_text(status["accessibility"]))
        self._set_permission_actions(
            status["screen_capture"],
            self.request_screen_button,
            self.open_screen_button,
        )
        self._set_permission_actions(
            status["accessibility"],
            self.request_accessibility_button,
            self.open_accessibility_button,
        )

    @staticmethod
    def _set_permission_actions(granted, request_button, settings_button):
        request_button.setVisible(granted is not True)
        settings_button.setText(
            "Manage in Settings" if granted is True else "Open Settings"
        )

    def request_screen(self):
        self._request_permission(
            self.screen_request,
            self.request_screen_button,
            self.screen_status,
        )

    def request_accessibility(self):
        self._request_permission(
            self.accessibility_request,
            self.request_accessibility_button,
            self.accessibility_status,
        )

    def _request_permission(self, request, button, status_label):
        button.setEnabled(False)
        status_label.setText("Requesting permission...")
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        try:
            request()
        except Exception as error:
            status_label.setText(f"Request failed: {error}")
            button.setEnabled(True)
            return
        button.setEnabled(True)
        self.refresh()

    def open_settings(self, permission):
        label = (
            self.screen_status
            if permission == "screen_capture"
            else self.accessibility_status
        )
        try:
            opened = self.url_opener(QUrl(privacy_urls[permission]))
        except Exception as error:
            label.setText(f"Unable to open Settings: {error}")
            return
        if opened is False:
            label.setText("Unable to open System Settings")
            return
        label.setText("System Settings opened; status will refresh on return.")
        self._refresh_on_activate = True

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowActivate and self._refresh_on_activate:
            self._refresh_on_activate = False
            QTimer.singleShot(0, self.refresh)
        super().changeEvent(event)

    @staticmethod
    def _status_text(value):
        if value is None:
            return "Status unavailable"
        return "Granted" if value else "Not granted"
