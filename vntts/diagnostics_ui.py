from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
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

    def __init__(self, parent=None, *, refresh_timeout_ms=10_000):
        super().__init__(parent)
        self.setWindowTitle("Live diagnostics")
        self.resize(700, 540)
        self.refresh_in_flight = False
        self.refresh_timeout_ms = refresh_timeout_ms
        self.refresh_generation = 0
        self.concealed_for_capture = False
        self.source_pixmap = None

        self.preview = QLabel("Waiting for a captured dialog region...")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(170)
        self.preview.setStyleSheet(
            "QLabel { background: #202124; color: #d0d0d0; border: 1px solid #555; }"
        )

        self.speaker = QLabel("-")
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setMinimumHeight(60)
        self.confidence = QLabel("-")
        self.preprocessing = QLabel("-")
        self.voice = QLabel("-")
        self.capture_latency = QLabel("-")
        self.ocr_latency = QLabel("-")
        self.synthesis_latency = QLabel("-")
        self.playback_latency = QLabel("-")
        self.capture_interval = QLabel("-")
        self.first_audio = QLabel("-")
        self.queue_depth = QLabel("0")
        self.game_focus = QLabel("-")
        self.corrections = QLabel("None")
        self.corrections.setWordWrap(True)

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
        details.addRow("First audio", self.first_audio)
        details.addRow("Speech queue", self.queue_depth)
        details.addRow("Game focused", self.game_focus)
        details.addRow("OCR corrections", self.corrections)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "QLabel { color: #8a3b12; background: #fff1df; padding: 8px; "
            "border: 1px solid #e3b37d; }"
        )
        self.warning.hide()

        self.refresh_button = QPushButton("Refresh now")
        self.refresh_button.setAccessibleName("Refresh live diagnostics")
        self.refresh_button.clicked.connect(self.request_refresh)
        self.refresh_status = QLabel("Refresh captures one current dialogue snapshot.")
        self.refresh_status.setAccessibleName("Diagnostics refresh status")
        self.refresh_status.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(self.refresh_status, 1)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_preview()

    def request_refresh(self):
        if self.refresh_in_flight:
            return
        self.refresh_in_flight = True
        self.refresh_generation += 1
        generation = self.refresh_generation
        self.refresh_button.setEnabled(False)
        self.refresh_status.setText("Capturing and inspecting the current dialogue...")
        self.refresh_requested.emit()
        QTimer.singleShot(
            self.refresh_timeout_ms,
            lambda: self._refresh_timed_out(generation),
        )

    def _refresh_timed_out(self, generation):
        if not self.refresh_in_flight or generation != self.refresh_generation:
            return
        self._finish_refresh(
            "Refresh timed out. Restore the game window and select Refresh now to retry."
        )

    def _finish_refresh(self, message):
        self.refresh_in_flight = False
        self.refresh_generation += 1
        self.refresh_button.setEnabled(True)
        self.refresh_status.setText(message)

    def conceal_for_capture(self):
        if not self.isVisible():
            return False
        self.concealed_for_capture = True
        self.hide()
        return True

    def restore_after_capture(self):
        if not self.concealed_for_capture:
            return
        self.show()
        self.concealed_for_capture = False
        self.raise_()
        self.activateWindow()

    def set_snapshot(self, snapshot):
        self._finish_refresh("Diagnostics refreshed from the latest current snapshot.")
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
        self.first_audio.setText(self._format_latency(snapshot.last_first_audio_ms))
        self.queue_depth.setText(
            f"{snapshot.speech_queue_depth} pending "
            f"(session peak {snapshot.max_speech_queue_depth})"
        )
        self.game_focus.setText(
            "-"
            if snapshot.game_focused is None
            else ("Yes" if snapshot.game_focused else "No")
        )
        self.corrections.setText(
            "\n".join(snapshot.corrections) if snapshot.corrections else "None"
        )
        if snapshot.image is not None:
            self.source_pixmap = self._pixmap_from_image(snapshot.image)
            self._scale_preview()

    def set_warning(self, message):
        if self.refresh_in_flight:
            self._finish_refresh(
                "Refresh failed. Resolve the warning below, then select Refresh now."
            )
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
