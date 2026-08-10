from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
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
        self.setWindowTitle("macOS permissions")
        self.setMinimumWidth(620)

        self.screen_status = QLabel()
        self.accessibility_status = QLabel()
        request_screen = QPushButton("Request")
        open_screen = QPushButton("Open Settings")
        request_accessibility = QPushButton("Request")
        open_accessibility = QPushButton("Open Settings")
        request_screen.clicked.connect(self.request_screen)
        open_screen.clicked.connect(lambda: self.open_settings("screen_capture"))
        request_accessibility.clicked.connect(self.request_accessibility)
        open_accessibility.clicked.connect(lambda: self.open_settings("accessibility"))

        screen_actions = QHBoxLayout()
        screen_actions.addWidget(self.screen_status)
        screen_actions.addStretch()
        screen_actions.addWidget(request_screen)
        screen_actions.addWidget(open_screen)
        accessibility_actions = QHBoxLayout()
        accessibility_actions.addWidget(self.accessibility_status)
        accessibility_actions.addStretch()
        accessibility_actions.addWidget(request_accessibility)
        accessibility_actions.addWidget(open_accessibility)
        form = QFormLayout()
        form.addRow("Screen recording", screen_actions)
        form.addRow("Accessibility", accessibility_actions)

        note = QLabel(
            "After granting a permission, quit and reopen the application. "
            "Screen recording is required for OCR capture; Accessibility is "
            "required for global hotkeys."
        )
        note.setWordWrap(True)
        refresh = QPushButton("Refresh status")
        refresh.clicked.connect(self.refresh)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(refresh)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self):
        status = self.status_provider()
        self.screen_status.setText(self._status_text(status["screen_capture"]))
        self.accessibility_status.setText(self._status_text(status["accessibility"]))

    def request_screen(self):
        self.screen_request()
        self.refresh()

    def request_accessibility(self):
        self.accessibility_request()
        self.refresh()

    def open_settings(self, permission):
        self.url_opener(QUrl(privacy_urls[permission]))

    @staticmethod
    def _status_text(value):
        if value is None:
            return "Status unavailable"
        return "Granted" if value else "Not granted"
