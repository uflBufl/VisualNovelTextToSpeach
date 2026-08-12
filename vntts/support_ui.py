from PySide6.QtCore import QTimer, Signal
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
        self.setWindowTitle("Diagnostics and logs")
        self.resize(760, 520)

        self.events = QTextEdit()
        self.events.setReadOnly(True)
        self.events.setPlaceholderText("No runtime events recorded yet.")
        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.addWidget(
            QLabel(
                "Paths and other local identifiers are redacted in exported reports."
            )
        )
        log_layout.addWidget(self.events)

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
        for text, signal in (
            ("Live diagnostics", self.diagnostics_requested),
            ("Export support report", self.export_requested),
            ("Open settings folder", self.settings_folder_requested),
        ):
            button = QPushButton(text)
            button.clicked.connect(signal.emit)
            actions.addWidget(button)
        actions.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addLayout(actions)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)

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
        text = "\n".join(rendered)
        if text != self.events.toPlainText():
            self.events.setPlainText(text)
            self.events.moveCursor(self.events.textCursor().MoveOperation.End)
