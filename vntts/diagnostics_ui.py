from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vntts.diagnostics import DiagnosticSnapshot
from vntts.qt_images import pixmap_from_pil
from vntts.ui_text import copy_text_button, make_text_copyable


class DiagnosticsDialog(QDialog):
    refresh_requested = Signal()
    calibration_requested = Signal()
    remediation_requested = Signal(str)
    closed = Signal()

    def __init__(
        self, parent: QWidget | None = None, *, refresh_timeout_ms: int = 10_000
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Live diagnostics")
        self.resize(700, 540)
        self.refresh_in_flight = False
        self.refresh_timeout_ms = refresh_timeout_ms
        self.refresh_generation = 0
        self.concealed_for_capture = False
        self.source_pixmap: QPixmap | None = None
        self.last_capture_time: str | None = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self._current_refresh_timed_out)

        self.preview = QLabel("Waiting for a captured dialogue region...")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(170)
        self.preview.setMaximumHeight(190)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.preview.setStyleSheet(
            "QLabel { background: #202124; color: #d0d0d0; border: 1px solid #555; }"
        )
        self.enlarge_button = QPushButton("Enlarge capture")
        self.enlarge_button.setEnabled(False)
        self.enlarge_button.clicked.connect(self.enlarge_capture)
        self.calibrate_button = QPushButton("Change capture region...")
        self.calibrate_button.setAccessibleName("Change captured dialogue region")
        self.calibrate_button.clicked.connect(self.calibration_requested.emit)

        self.speaker = QLabel("-")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
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
        for label in (self.speaker, self.text, self.voice, self.corrections):
            label.setTextFormat(Qt.TextFormat.PlainText)

        summary = QFormLayout()
        summary.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        summary.addRow("Speaker", self.speaker)
        summary.addRow("Recognized text", self.text)
        summary.addRow("OCR confidence", self.confidence)
        summary.addRow("Selected voice", self.voice)
        summary.addRow("OCR corrections", self.corrections)

        technical_details = QFormLayout()
        technical_details.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        technical_details.addRow("Preprocessing", self.preprocessing)
        technical_details.addRow("Capture latency", self.capture_latency)
        technical_details.addRow("OCR latency", self.ocr_latency)
        technical_details.addRow("Synthesis latency", self.synthesis_latency)
        technical_details.addRow("Playback latency", self.playback_latency)
        technical_details.addRow("Capture interval", self.capture_interval)
        technical_details.addRow("First audio", self.first_audio)
        technical_details.addRow("Speech queue", self.queue_depth)
        technical_details.addRow("Game focused", self.game_focus)
        self.technical_panel = QWidget()
        self.technical_panel.setLayout(technical_details)
        self.technical_panel.hide()
        self.technical_toggle = QCheckBox("Show capture and timing details")
        self.technical_toggle.toggled.connect(self.technical_panel.setVisible)

        self.warning = QLabel()
        self.warning.setTextFormat(Qt.TextFormat.PlainText)
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "QLabel { color: #8a3b12; background: #fff1df; padding: 8px; "
            "border: 1px solid #e3b37d; }"
        )
        self.warning.hide()
        self.warning_action = QPushButton()
        self.warning_action.setAccessibleName("Resolve diagnostics warning")
        self.warning_action.clicked.connect(self._request_remediation)
        self.warning_action.hide()
        self.warning_remediation: str | None = None

        self.refresh_button = QPushButton("Refresh now")
        self.refresh_button.setAccessibleName("Refresh live diagnostics")
        self.refresh_button.clicked.connect(self.request_refresh)
        self.refresh_status = QLabel(
            "No capture yet. Select Refresh now to inspect the current dialogue."
        )
        self.refresh_status.setAccessibleName("Diagnostics refresh status")
        self.refresh_status.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(self.refresh_status, 1)
        controls.addStretch()
        controls.addWidget(self.refresh_button)
        self.copy_error_button = copy_text_button("Copy warning", self.warning.text)
        self.copy_error_button.hide()
        controls.addWidget(self.copy_error_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        capture_header = QHBoxLayout()
        capture_header.addWidget(QLabel("Captured dialogue region"))
        capture_header.addStretch()
        capture_header.addWidget(self.calibrate_button)
        capture_header.addWidget(self.enlarge_button)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.addLayout(capture_header)
        body_layout.addWidget(self.preview)
        body_layout.addLayout(summary)
        body_layout.addWidget(self.technical_toggle)
        body_layout.addWidget(self.technical_panel)
        body_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout = QVBoxLayout(self)
        layout.addWidget(self.warning)
        layout.addWidget(self.warning_action, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(scroll, 1)
        layout.addLayout(controls)
        layout.addWidget(buttons)
        make_text_copyable(self)

    def set_calibration_pending(self, pending: bool) -> None:
        self.calibrate_button.setEnabled(not pending)
        self.calibrate_button.setText(
            "Stopping reading..." if pending else "Change capture region..."
        )
        self.calibrate_button.setAccessibleName(
            "Stopping reading before capture calibration"
            if pending
            else "Change captured dialogue region"
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self._scale_preview)

    def request_refresh(self) -> None:
        if self.refresh_in_flight:
            return
        self.refresh_in_flight = True
        self.refresh_generation += 1
        generation = self.refresh_generation
        self.refresh_button.setEnabled(False)
        self.refresh_status.setText(
            "Capturing current dialogue... "
            + (
                f"Previous capture from {self.last_capture_time} remains visible."
                if self.last_capture_time
                else "Waiting for the first capture."
            )
        )
        self.refresh_timer.setProperty("refresh_generation", generation)
        self.refresh_timer.start(self.refresh_timeout_ms)
        self.refresh_requested.emit()

    def _current_refresh_timed_out(self) -> None:
        generation = self.refresh_timer.property("refresh_generation")
        if isinstance(generation, int):
            self._refresh_timed_out(generation)

    def _refresh_timed_out(self, generation: int) -> None:
        if not self.refresh_in_flight or generation != self.refresh_generation:
            return
        self._finish_refresh(
            "Refresh timed out. "
            + (
                f"Previous capture from {self.last_capture_time} remains visible. "
                if self.last_capture_time
                else ""
            )
            + "Restore the game window and select Refresh now to retry."
        )

    def _finish_refresh(self, message: str) -> None:
        self.refresh_timer.stop()
        self.refresh_in_flight = False
        self.refresh_generation += 1
        self.refresh_button.setEnabled(True)
        self.refresh_status.setText(message)
        self.restore_after_capture()

    def closeEvent(self, event: QCloseEvent) -> None:
        # Route hidden-window close and Escape through the same completion path.
        self.reject()
        event.accept()

    def done(self, result: int) -> None:
        self.refresh_timer.stop()
        self.refresh_in_flight = False
        self.refresh_generation += 1
        self.concealed_for_capture = False
        self.closed.emit()
        super().done(result)

    def conceal_for_capture(self) -> bool:
        if not self.isVisible():
            return False
        self.concealed_for_capture = True
        self.hide()
        return True

    def restore_after_capture(self) -> None:
        if not self.concealed_for_capture:
            return
        self.show()
        self.concealed_for_capture = False
        self.raise_()
        self.activateWindow()

    def set_snapshot(self, snapshot: DiagnosticSnapshot) -> None:
        source_pixmap = (
            pixmap_from_pil(snapshot.image) if snapshot.image is not None else None
        )
        self.last_capture_time = snapshot.captured_at.astimezone().strftime(
            "%b %d, %Y %H:%M:%S"
        )
        self._finish_refresh(f"Showing capture from {self.last_capture_time}.")
        self.set_warning("")
        self.speaker.setText(snapshot.character or "Narrator")
        self.text.setText(snapshot.text)
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
        self.source_pixmap = source_pixmap
        self.enlarge_button.setEnabled(source_pixmap is not None)
        if source_pixmap is None:
            self.preview.clear()
            self.preview.setText("No capture image is available for this snapshot.")
        else:
            self._scale_preview()

    def set_warning(
        self, message: str, *, remediation: tuple[str, str] | None = None
    ) -> None:
        if self.refresh_in_flight:
            self._finish_refresh(
                "Refresh failed. Resolve the warning above, then select Refresh now."
            )
        if message and self.last_capture_time:
            self.refresh_status.setText(
                f"Showing previous capture from {self.last_capture_time}. "
                "Resolve the warning above, then refresh."
            )
        self.warning.setText(message)
        self.warning.setVisible(bool(message))
        self.copy_error_button.setVisible(bool(message))
        self.warning_remediation = remediation[0] if remediation else None
        self.warning_action.setText(remediation[1] if remediation else "")
        self.warning_action.setVisible(bool(message and remediation))

    def set_permission_warnings(
        self,
        warnings: list[str],
        *,
        remediation: tuple[str, str] | None = None,
    ) -> None:
        self.set_warning("\n\n".join(warnings), remediation=remediation)

    def _request_remediation(self) -> None:
        if self.warning_remediation:
            self.remediation_requested.emit(self.warning_remediation)

    def enlarge_capture(self) -> None:
        dialog = self._capture_dialog()
        if dialog is not None:
            dialog.exec()

    def _capture_dialog(self) -> QDialog | None:
        if self.source_pixmap is None:
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle("Captured dialogue region - enlarged")
        dialog.resize(900, 540)
        image = QLabel()
        image.setPixmap(
            self.source_pixmap.scaled(
                self.source_pixmap.size() * 2,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        scroll = QScrollArea()
        scroll.setWidget(image)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout = QVBoxLayout(dialog)
        layout.addWidget(scroll)
        layout.addWidget(buttons)
        return dialog

    def _scale_preview(self) -> None:
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
    def _format_latency(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} ms"
