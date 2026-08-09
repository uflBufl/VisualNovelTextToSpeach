from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)


class DiagnosticsDialog(QDialog):
    refresh_requested = Signal()

    def __init__(self, parent=None, *, refresh_interval_ms=750):
        super().__init__(parent)
        self.setWindowTitle("Live diagnostics")
        self.resize(860, 680)
        self.refresh_in_flight = False
        self.source_pixmap = None

        self.preview = QLabel("Waiting for a captured dialog region...")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(280)
        self.preview.setStyleSheet(
            "QLabel { background: #202124; color: #d0d0d0; border: 1px solid #555; }"
        )

        self.speaker = QLabel("-")
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(90)
        self.confidence = QLabel("-")
        self.preprocessing = QLabel("-")
        self.voice = QLabel("-")
        self.capture_latency = QLabel("-")
        self.ocr_latency = QLabel("-")
        self.synthesis_latency = QLabel("-")
        self.playback_latency = QLabel("-")
        self.capture_interval = QLabel("-")
        self.game_focus = QLabel("-")

        details = QFormLayout()
        details.addRow("Speaker", self.speaker)
        details.addRow("Recognized text", self.text)
        details.addRow("OCR confidence", self.confidence)
        details.addRow("Preprocessing", self.preprocessing)
        details.addRow("Selected voice", self.voice)
        details.addRow("Capture latency", self.capture_latency)
        details.addRow("OCR latency", self.ocr_latency)
        details.addRow("Synthesis latency", self.synthesis_latency)
        details.addRow("Playback latency", self.playback_latency)
        details.addRow("Capture interval", self.capture_interval)
        details.addRow("Game focused", self.game_focus)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "QLabel { color: #8a3b12; background: #fff1df; padding: 8px; "
            "border: 1px solid #e3b37d; }"
        )
        self.warning.hide()

        self.auto_refresh = QCheckBox("Live preview")
        self.auto_refresh.setChecked(True)
        self.refresh_button = QPushButton("Refresh now")
        self.refresh_button.clicked.connect(self.request_refresh)
        controls = QHBoxLayout()
        controls.addWidget(self.auto_refresh)
        controls.addStretch()
        controls.addWidget(self.refresh_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preview)
        layout.addLayout(details)
        layout.addWidget(self.warning)
        layout.addLayout(controls)
        layout.addWidget(buttons)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(refresh_interval_ms)
        self.refresh_timer.timeout.connect(self._refresh_if_enabled)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_timer.start()
        self.request_refresh()

    def hideEvent(self, event):
        self.refresh_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_preview()

    def request_refresh(self):
        if self.refresh_in_flight:
            return
        self.refresh_in_flight = True
        self.refresh_button.setEnabled(False)
        self.refresh_requested.emit()

    def _refresh_if_enabled(self):
        if self.auto_refresh.isChecked():
            self.request_refresh()

    def set_snapshot(self, snapshot):
        self.refresh_in_flight = False
        self.refresh_button.setEnabled(True)
        self.speaker.setText(snapshot.character or "Narrator")
        self.text.setPlainText(snapshot.text)
        self.confidence.setText(f"{snapshot.confidence:.1f}%")
        self.preprocessing.setText(snapshot.preprocessing_profile)
        self.voice.setText(snapshot.voice)
        self.capture_latency.setText(self._format_latency(snapshot.capture_ms))
        self.ocr_latency.setText(self._format_latency(snapshot.ocr_ms))
        self.synthesis_latency.setText(self._format_latency(snapshot.synthesis_ms))
        self.playback_latency.setText(self._format_latency(snapshot.playback_ms))
        self.capture_interval.setText(
            self._format_latency(snapshot.capture_interval_ms)
        )
        self.game_focus.setText(
            "-"
            if snapshot.game_focused is None
            else ("Yes" if snapshot.game_focused else "No")
        )
        if snapshot.image is not None:
            self.source_pixmap = self._pixmap_from_image(snapshot.image)
            self._scale_preview()

    def set_warning(self, message):
        self.refresh_in_flight = False
        self.refresh_button.setEnabled(True)
        self.warning.setText(message)
        self.warning.setVisible(bool(message))

    def set_permission_warnings(self, warnings):
        self.set_warning("\n\n".join(warnings))

    def _scale_preview(self):
        if self.source_pixmap is None:
            return
        size = self.preview.size()
        if size.width() <= 1 or size.height() <= 1:
            return
        self.preview.setPixmap(
            self.source_pixmap.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    @staticmethod
    def _pixmap_from_image(image):
        image = image.convert("RGB")
        data = image.tobytes("raw", "RGB")
        qimage = QImage(
            data,
            image.width,
            image.height,
            image.width * 3,
            QImage.Format.Format_RGB888,
        ).copy()
        return QPixmap.fromImage(qimage)

    @staticmethod
    def _format_latency(value):
        return "-" if value is None else f"{value:.1f} ms"
