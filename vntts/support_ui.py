from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class SupportCenterDialog(QDialog):
    diagnostics_requested = Signal()
    export_requested = Signal()
    settings_folder_requested = Signal()

    def __init__(self, event_log, parent=None):
        super().__init__(parent)
        self.event_log = event_log
        self.setWindowTitle("Support and logs")
        self.setMinimumSize(620, 420)
        self.resize(760, 520)

        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setPlaceholderText("No runtime events recorded yet.")
        self._rendered_lines = []
        self.pending_event_count = 0
        self.new_events_button = QPushButton("Show new events")
        self.new_events_button.setAccessibleName("Show new runtime log events")
        self.new_events_button.clicked.connect(self.show_new_events)
        self.new_events_button.hide()
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.addWidget(
            QLabel(
                "Paths and other local identifiers are redacted in exported reports."
            )
        )
        log_layout.addWidget(self.events)
        log_layout.addWidget(self.new_events_button)

        help_text = QTextEdit()
        help_text.setReadOnly(True)
        help_text.setPlainText(
            "Use Live diagnostics to inspect the latest captured region, OCR result, "
            "selected voice, and latency. Export a support report after a crash, "
            "audio artifact, missed speaker, or incorrect capture. The report excludes "
            "screenshots, recognized dialogue, voice recordings, models, and secret values."
        )
        help_page = QWidget()
        help_layout = QVBoxLayout(help_page)
        help_layout.addWidget(help_text)

        tabs = QTabWidget()
        tabs.addTab(log_page, "Runtime log")
        tabs.addTab(help_page, "Problem report")
        actions = QHBoxLayout()
        self.diagnostics_button = QPushButton("Live diagnostics")
        self.export_button = QPushButton("Export support report")
        self.settings_button = QPushButton("Open settings folder")
        self.diagnostics_button.setAccessibleName("Open live diagnostics")
        self.diagnostics_button.setAccessibleDescription(
            "Open the latest capture, OCR, routing and latency diagnostics"
        )
        self.diagnostics_button.setShortcut(QKeySequence("Ctrl+D"))
        self.export_button.setAccessibleName("Export privacy-safe support report")
        self.export_button.setShortcut(QKeySequence("Ctrl+E"))
        self.settings_button.setAccessibleName("Open application settings folder")
        self.settings_button.setAccessibleDescription(
            "Open the local folder containing application settings"
        )
        self.settings_button.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.diagnostics_button.clicked.connect(self.request_diagnostics)
        self.export_button.clicked.connect(self.request_export)
        self.settings_button.clicked.connect(self.request_settings_folder)
        actions.addWidget(self.diagnostics_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.settings_button)
        actions.addStretch()
        self.operation_status = QLabel("Support actions are ready.")
        self.operation_status.setAccessibleName("Support operation status")
        self.operation_status.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addLayout(actions)
        layout.addWidget(self.operation_status)
        layout.addWidget(buttons)
        self.setTabOrder(self.events, self.new_events_button)
        self.setTabOrder(self.new_events_button, self.diagnostics_button)
        self.setTabOrder(self.diagnostics_button, self.export_button)
        self.setTabOrder(self.export_button, self.settings_button)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

    def request_diagnostics(self):
        if not self.diagnostics_button.isEnabled():
            return
        self.diagnostics_button.setEnabled(False)
        self.operation_status.setText("Opening live diagnostics...")
        self.diagnostics_requested.emit()

    def request_settings_folder(self):
        if not self.settings_button.isEnabled():
            return
        self.settings_button.setEnabled(False)
        self.operation_status.setText("Opening the application settings folder...")
        self.settings_folder_requested.emit()

    def set_launcher_result(self, launcher, successful, message):
        buttons = {
            "diagnostics": self.diagnostics_button,
            "settings-folder": self.settings_button,
        }
        button = buttons[launcher]
        button.setEnabled(True)
        if successful:
            self.operation_status.setText(message)
        else:
            self.operation_status.setText(f"{message}. Select the action to retry.")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        entries = self.event_log.snapshot()
        rendered = []
        for entry in entries:
            timestamp = str(entry.get("recorded_at", ""))
            if "T" in timestamp:
                timestamp = timestamp.split("T", 1)[1][:8]
            rendered.append(
                f"{timestamp} [{str(entry.get('level', '')).upper()}] "
                f"{entry.get('message', '')}"
            )
        if rendered == self._rendered_lines:
            return
        scroll_bar = self.events.verticalScrollBar()
        saved_cursor = self.events.textCursor()
        was_at_end = (
            scroll_bar.value() >= scroll_bar.maximum() - 1
            and not saved_cursor.hasSelection()
        )
        saved_scroll = scroll_bar.value()
        saved_anchor = saved_cursor.anchor()
        saved_position = saved_cursor.position()
        old_lines = self._rendered_lines
        appended = (
            len(rendered) >= len(old_lines) and rendered[: len(old_lines)] == old_lines
        )
        new_count = len(rendered) - len(old_lines) if appended else len(rendered)

        if appended and old_lines:
            document_cursor = QTextCursor(self.events.document())
            document_cursor.movePosition(QTextCursor.MoveOperation.End)
            for line in rendered[len(old_lines) :]:
                document_cursor.insertBlock()
                document_cursor.insertText(line)
        else:
            self.events.setPlainText("\n".join(rendered))

        self._rendered_lines = list(rendered)
        if was_at_end or not old_lines:
            self.show_new_events()
            return

        if appended:
            restored_cursor = QTextCursor(self.events.document())
            restored_cursor.setPosition(saved_anchor)
            restored_cursor.setPosition(
                saved_position,
                QTextCursor.MoveMode.KeepAnchor,
            )
            self.events.setTextCursor(restored_cursor)
        scroll_bar.setValue(min(saved_scroll, scroll_bar.maximum()))
        self.pending_event_count += max(new_count, 0)
        self.new_events_button.setText(
            f"Show {self.pending_event_count} new event"
            f"{'s' if self.pending_event_count != 1 else ''}"
        )
        self.new_events_button.show()

    def show_new_events(self):
        cursor = self.events.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.events.setTextCursor(cursor)
        self.events.ensureCursorVisible()
        self.pending_event_count = 0
        self.new_events_button.hide()

    def request_export(self):
        if not self.export_button.isEnabled():
            return
        self.export_button.setEnabled(False)
        self.operation_status.setText(
            "Choosing a destination for the support report..."
        )
        self.export_requested.emit()

    def set_export_result(self, successful, message):
        self.export_button.setEnabled(True)
        if successful is True:
            self.operation_status.setText(f"Support report saved to {message}")
        elif successful is False:
            self.operation_status.setText(
                f"Support report export failed: {message}. Select Export to retry."
            )
        else:
            self.operation_status.setText(message or "Support report export cancelled.")
