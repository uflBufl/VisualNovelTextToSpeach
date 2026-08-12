from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class ControlDashboard(QMainWindow):
    read_requested = Signal()
    live_requested = Signal()
    pause_requested = Signal()
    skip_requested = Signal()
    repeat_requested = Signal()
    stop_requested = Signal()
    readiness_requested = Signal()
    calibration_requested = Signal()
    voices_requested = Signal()
    diagnostics_requested = Signal()
    settings_requested = Signal()
    quit_requested = Signal()
    hidden_to_background = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.keep_running_on_close = settings.keep_running_on_close
        self._quitting = False
        self._live = False
        self.setWindowTitle("Visual Novel Text to Speech")
        self.setMinimumWidth(620)
        self.resize(720, 430)

        self.status = QLabel("Starting...")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-weight: 600; font-size: 15px;")
        self.mode = QLabel("Stopped")
        self.speaker = QLabel("Narrator")
        self.voice = QLabel("Not loaded")
        self.confidence = QLabel("-")
        self.latency = QLabel("-")
        self.configuration = QLabel()
        self.configuration.setWordWrap(True)
        self.dialogue = QLabel("No dialogue detected")
        self.dialogue.setWordWrap(True)
        self.dialogue.setMinimumHeight(52)
        self.dialogue.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        details = QFormLayout()
        details.addRow("Mode", self.mode)
        details.addRow("Speaker", self.speaker)
        details.addRow("Voice", self.voice)
        details.addRow("OCR confidence", self.confidence)
        details.addRow("Latest latency", self.latency)
        details.addRow("Configuration", self.configuration)

        self.read_button = QPushButton("Read current dialogue")
        self.live_button = QPushButton("Start live reading")
        self.pause_button = QPushButton("Pause")
        self.skip_button = QPushButton("Skip")
        self.repeat_button = QPushButton("Replay")
        self.stop_button = QPushButton("Emergency stop")
        self.stop_button.setStyleSheet(
            "QPushButton { color: #a21818; font-weight: 600; }"
        )
        self.read_button.clicked.connect(self.read_requested.emit)
        self.live_button.clicked.connect(self.live_requested.emit)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.skip_button.clicked.connect(self.skip_requested.emit)
        self.repeat_button.clicked.connect(self.repeat_requested.emit)
        self.stop_button.clicked.connect(self.stop_requested.emit)

        primary = QHBoxLayout()
        primary.addWidget(self.read_button, 2)
        primary.addWidget(self.live_button, 2)
        primary.addWidget(self.pause_button)
        primary.addWidget(self.skip_button)
        primary.addWidget(self.repeat_button)
        primary.addWidget(self.stop_button)

        setup_buttons = (
            ("Check readiness", self.readiness_requested),
            ("Calibrate capture", self.calibration_requested),
            ("Manage voices", self.voices_requested),
            ("Diagnostics and logs", self.diagnostics_requested),
            ("Settings", self.settings_requested),
        )
        setup = QHBoxLayout()
        for label, signal in setup_buttons:
            button = QPushButton(label)
            button.clicked.connect(signal.emit)
            setup.addWidget(button)
        quit_button = QPushButton("Quit VNTTS")
        quit_button.clicked.connect(self.request_quit)
        setup.addWidget(quit_button)

        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(QLabel("Current dialogue"))
        card_layout.addWidget(self.dialogue)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self.status)
        layout.addLayout(details)
        layout.addWidget(card)
        layout.addLayout(primary)
        layout.addLayout(setup)
        self.setCentralWidget(central)
        self.set_ready(False)
        self.set_configuration(settings)

    def set_configuration(self, settings):
        self.keep_running_on_close = settings.keep_running_on_close
        capture = (
            settings.game_window_title or "No game window selected"
            if settings.capture_mode == "window"
            else "Calibrated screen region"
        )
        self.configuration.setText(
            f"{settings.speech_backend}; {capture}; OCR {settings.ocr_language}"
        )

    def set_status(self, message):
        self.status.setText(message)

    def set_dialogue(self, speaker, text):
        self.speaker.setText(speaker or "Narrator")
        self.dialogue.setText(text or "No dialogue detected")

    def set_ready(self, ready):
        for button in (
            self.read_button,
            self.live_button,
            self.pause_button,
            self.skip_button,
            self.repeat_button,
            self.stop_button,
        ):
            button.setEnabled(bool(ready))

    def set_live(self, running):
        self._live = bool(running)
        self.mode.setText("Live reading" if running else "Stopped")
        self.live_button.setText(
            "Stop live reading" if running else "Start live reading"
        )

    def set_paused(self, paused):
        self.mode.setText(
            "Paused" if paused else ("Live reading" if self._live else "Stopped")
        )
        self.pause_button.setText("Resume" if paused else "Pause")

    def set_diagnostic(self, snapshot):
        self.speaker.setText(snapshot.character or "Narrator")
        self.voice.setText(snapshot.voice or "Default narrator")
        self.confidence.setText(f"{snapshot.confidence:.1f}%")
        parts = []
        if snapshot.capture_ms is not None:
            parts.append(f"capture {snapshot.capture_ms:.0f} ms")
        if snapshot.ocr_ms is not None:
            parts.append(f"OCR {snapshot.ocr_ms:.0f} ms")
        if snapshot.synthesis_ms is not None:
            parts.append(f"speech {snapshot.synthesis_ms:.0f} ms")
        if snapshot.last_first_audio_ms is not None:
            parts.append(f"first audio {snapshot.last_first_audio_ms:.0f} ms")
        if snapshot.speech_queue_depth:
            parts.append(f"queue {snapshot.speech_queue_depth}")
        self.latency.setText(", ".join(parts) or "-")

    def request_quit(self):
        self._quitting = True
        self.close()
        self.quit_requested.emit()

    def closeEvent(self, event):
        if self.keep_running_on_close and not self._quitting:
            event.ignore()
            self.hide()
            self.hidden_to_background.emit()
            return
        if not self._quitting:
            self._quitting = True
            self.quit_requested.emit()
        super().closeEvent(event)
