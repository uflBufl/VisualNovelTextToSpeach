from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QPalette, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from vntts.action_icons import set_action_icon
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
        self.speaker_processed_pixmap: QPixmap | None = None
        self.processed_pixmap: QPixmap | None = None
        self.last_capture_time: str | None = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.timeout.connect(self._current_refresh_timed_out)

        self._create_preview_controls()
        self._create_text_fields()
        self._create_warning_controls()
        self._create_layout()

    def _create_preview_controls(self) -> None:
        self.preview = self._image_preview(
            "Waiting for a captured dialogue region...", min_height=120
        )
        self.processed_preview = self._image_preview(
            "Waiting for a processed dialogue OCR image...", min_height=120
        )
        self.speaker_processed_preview = self._image_preview(
            "Waiting for a processed speaker OCR image...",
            min_height=80,
            max_height=100,
        )
        self.speaker_preview = self.speaker_processed_preview
        self.enlarge_button = QPushButton("Enlarge")
        set_action_icon(self.enlarge_button, "expand")
        self.enlarge_button.setAccessibleName("Enlarge captured dialogue region")
        self.enlarge_button.setEnabled(False)
        self.enlarge_button.clicked.connect(self.enlarge_capture)
        self.enlarge_processed_button = QPushButton("Enlarge")
        set_action_icon(self.enlarge_processed_button, "expand")
        self.enlarge_processed_button.setAccessibleName(
            "Enlarge processed dialogue OCR image"
        )
        self.enlarge_processed_button.setEnabled(False)
        self.enlarge_processed_button.clicked.connect(self.enlarge_processed_image)
        self.enlarge_speaker_button = QPushButton("Enlarge")
        set_action_icon(self.enlarge_speaker_button, "expand")
        self.enlarge_speaker_button.setAccessibleName(
            "Enlarge processed speaker OCR image"
        )
        self.enlarge_speaker_button.setEnabled(False)
        self.enlarge_speaker_button.clicked.connect(self.enlarge_speaker_image)
        self.enlarge_speaker_processed_button = self.enlarge_speaker_button
        self.calibrate_button = QPushButton("Change capture area...")
        set_action_icon(self.calibrate_button, "scan")
        self.calibrate_button.setAccessibleName(
            "Change the capture area containing speaker and dialogue"
        )
        self.calibrate_button.clicked.connect(self.calibration_requested.emit)

    def _create_text_fields(self) -> None:
        self.speaker = QLabel("-")
        self.speaker_confidence = QLabel("-")
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.dialogue_confidence = QLabel("-")
        self.confidence = self.dialogue_confidence
        self.preprocessing = QLabel("-")
        self.voice = QLabel("-")
        self.capture_latency = QLabel("-")
        self.ocr_latency = QLabel("-")
        self.synthesis_latency = QLabel("-")
        self.playback_latency = QLabel("-")
        self.capture_interval = QLabel("-")
        self.first_audio = QLabel("-")
        self.queue_depth = QLabel("-")
        self.game_focus = QLabel("-")
        self.corrections = QLabel("-")
        self.corrections.setWordWrap(True)
        for label in (
            self.speaker,
            self.speaker_confidence,
            self.text,
            self.voice,
            self.preprocessing,
            self.corrections,
        ):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)

        self.technical_panel = QWidget()
        self.technical_panel.hide()
        self.technical_toggle = QCheckBox("Show OCR images and timing details")
        self.technical_toggle.toggled.connect(self.technical_panel.setVisible)
        self.technical_toggle.toggled.connect(
            lambda: QTimer.singleShot(0, self, self._scale_preview)
        )

    def _create_warning_controls(self) -> None:
        self.warning = QPlainTextEdit(readOnly=True, tabChangesFocus=True)
        self.warning.setAccessibleName("Diagnostics warning")
        palette = self.warning.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor("#fff1df"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#8a3b12"))
        self.warning.setPalette(palette)
        self.warning.document().setDocumentMargin(8)
        self.warning.setFixedHeight(self.warning.fontMetrics().lineSpacing() * 3 + 20)
        self.warning.installEventFilter(self)
        self.warning.hide()
        self.warning_action = QPushButton()
        set_action_icon(self.warning_action, "settings")
        self.warning_action.setAccessibleName("Resolve diagnostics warning")
        self.warning_action.clicked.connect(self._request_remediation)
        self.warning_action.hide()
        self.warning_remediation: str | None = None

    def _create_layout(self) -> None:
        self.refresh_button = QPushButton("Refresh now")
        set_action_icon(self.refresh_button, "refresh-cw")
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
        self.copy_error_button = copy_text_button(
            "Copy warning", self.warning.toPlainText
        )
        self.copy_error_button.hide()
        warning_actions = QHBoxLayout()
        warning_actions.addStretch()
        warning_actions.addWidget(self.copy_error_button)
        warning_actions.addWidget(self.warning_action)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.rejected.connect(self.close)
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setWidget(self._create_body())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.warning)
        layout.addLayout(warning_actions)
        layout.addWidget(self.body_scroll, 1)
        layout.addLayout(controls)
        layout.addWidget(buttons)
        make_text_copyable(self)

    def _create_body(self) -> QWidget:
        self.speaker_note = QLabel()
        self.speaker_note.setTextFormat(Qt.TextFormat.PlainText)
        self.speaker_note.setWordWrap(True)
        self.speaker_note.hide()
        summary = QFormLayout()
        summary.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        summary.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        summary.addRow("Speaker", self.speaker)
        summary.addRow("Recognized text", self.text)
        summary.addRow("Selected voice", self.voice)
        summary.addRow("OCR corrections", self.corrections)

        capture_header = QHBoxLayout()
        capture_header.addWidget(QLabel("Captured image"))
        capture_header.addStretch()
        capture_header.addWidget(self.calibrate_button)
        capture_header.addWidget(self.enlarge_button)
        self._create_technical_details()
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(10)
        body_layout.addLayout(capture_header)
        body_layout.addWidget(self.preview)
        body_layout.addLayout(summary)
        body_layout.addWidget(self.speaker_note)
        body_layout.addWidget(self.technical_toggle)
        body_layout.addWidget(self.technical_panel)
        body_layout.addStretch()
        return body

    def _create_technical_details(self) -> None:
        processed_header = QHBoxLayout()
        processed_header.addWidget(QLabel("Dialogue OCR image"))
        processed_header.addStretch()
        processed_header.addWidget(self.enlarge_processed_button)
        speaker_processed_header = QHBoxLayout()
        speaker_processed_header.addWidget(QLabel("Speaker OCR image"))
        speaker_processed_header.addStretch()
        speaker_processed_header.addWidget(self.enlarge_speaker_button)
        self.processed_preview.setToolTip(
            "The selected OCR attempt after preprocessing and any dialogue crop."
        )
        ocr_confidence = QFormLayout()
        ocr_confidence.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        ocr_confidence.addRow("Speaker OCR confidence", self.speaker_confidence)
        ocr_confidence.addRow("Dialogue OCR confidence", self.dialogue_confidence)
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
        technical = QVBoxLayout(self.technical_panel)
        technical.setContentsMargins(0, 0, 0, 0)
        technical.addLayout(speaker_processed_header)
        technical.addWidget(self.speaker_processed_preview)
        technical.addLayout(processed_header)
        technical.addWidget(self.processed_preview)
        technical.addLayout(ocr_confidence)
        technical.addLayout(technical_details)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.warning and event.type() == QEvent.Type.FontChange:
            self.warning.setFixedHeight(
                self.warning.fontMetrics().lineSpacing() * 3 + 20
            )
        return super().eventFilter(watched, event)

    @staticmethod
    def _image_preview(
        message: str, *, min_height: int = 120, max_height: int = 150
    ) -> QLabel:
        preview = QLabel(message)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumHeight(min_height)
        preview.setMaximumHeight(max_height)
        preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        preview.setWordWrap(True)
        palette = preview.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#202124"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#d0d0d0"))
        preview.setPalette(palette)
        preview.setAutoFillBackground(True)
        return preview

    def set_calibration_pending(self, pending: bool) -> None:
        self.calibrate_button.setEnabled(not pending)
        self.calibrate_button.setText(
            "Stopping reading..." if pending else "Change capture area..."
        )
        self.calibrate_button.setAccessibleName(
            "Stopping reading before capture calibration"
            if pending
            else "Change the capture area containing speaker and dialogue"
        )

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, self, self._scale_preview)

    def request_refresh(self) -> None:
        if self.refresh_in_flight:
            return
        self.set_warning("")
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

    def cancel_refresh(self) -> None:
        """Cancel a pending acquisition without reopening concealed controls."""
        self.concealed_for_capture = False
        if not self.refresh_in_flight:
            return
        self._finish_refresh("Refresh cancelled.")

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
        self.concealed_for_capture = False
        previous = self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        try:
            self.show()
        finally:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, previous)

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
        self.speaker_note.setText(snapshot.speaker_note)
        self.speaker_note.setVisible(bool(snapshot.speaker_note))
        speaker_confidence = getattr(snapshot, "speaker_confidence", None)
        dialogue_confidence = getattr(snapshot, "dialogue_confidence", None)
        if dialogue_confidence is None:
            dialogue_confidence = snapshot.confidence
        self.speaker_confidence.setText(self._format_confidence(speaker_confidence))
        self.text.setText(snapshot.text)
        self.dialogue_confidence.setText(self._format_confidence(dialogue_confidence))
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
        self.processed_pixmap = (
            pixmap_from_pil(snapshot.processed_image)
            if snapshot.processed_image is not None
            else None
        )
        speaker_image = getattr(snapshot, "speaker_image", None)
        self.speaker_processed_pixmap = (
            pixmap_from_pil(speaker_image) if speaker_image is not None else None
        )
        self.enlarge_button.setEnabled(source_pixmap is not None)
        self.enlarge_processed_button.setEnabled(self.processed_pixmap is not None)
        self.enlarge_speaker_button.setEnabled(
            self.speaker_processed_pixmap is not None
        )
        if source_pixmap is None:
            self.preview.clear()
            self.preview.setText("No capture image is available for this snapshot.")
        if self.processed_pixmap is None:
            self.processed_preview.clear()
            self.processed_preview.setText(
                "No processed OCR image is available for this snapshot."
            )
        if self.speaker_processed_pixmap is None:
            self.speaker_processed_preview.clear()
            self.speaker_processed_preview.setText(
                "No processed speaker OCR image is available for this snapshot."
            )
        self._scale_preview()

    def set_warning(
        self, message: str, *, remediation: tuple[str, str] | None = None
    ) -> None:
        was_refreshing = self.refresh_in_flight
        if was_refreshing:
            self._finish_refresh("Refresh failed.")
        if message:
            self.refresh_status.setText(
                f"Showing previous capture from {self.last_capture_time}."
                if self.last_capture_time
                else ("Refresh failed." if was_refreshing else "No capture yet.")
            )
        elif self.warning.toPlainText():
            self.refresh_status.setText(
                f"Showing capture from {self.last_capture_time}."
                if self.last_capture_time
                else "No capture yet. Select Refresh now to inspect the current dialogue."
            )
        self.warning.setPlainText(message)
        self.warning.setVisible(bool(message))
        self.copy_error_button.setVisible(bool(message))
        self.warning_remediation = remediation[0] if remediation else None
        self.warning_action.setText(remediation[1] if remediation else "")
        set_action_icon(
            self.warning_action,
            "key-round" if self.warning_remediation == "macos-permissions" else "settings",
        )
        self.warning_action.setVisible(bool(message and remediation))
        self.restore_after_capture()

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

    def enlarge_processed_image(self) -> None:
        dialog = self._capture_dialog(processed=True)
        if dialog is not None:
            dialog.exec()

    def enlarge_speaker_image(self) -> None:
        dialog = self._capture_dialog(speaker=True)
        if dialog is not None:
            dialog.exec()

    def _capture_dialog(
        self, *, processed: bool = False, speaker: bool = False
    ) -> QDialog | None:
        if speaker:
            pixmap = self.speaker_processed_pixmap
        else:
            pixmap = self.processed_pixmap if processed else self.source_pixmap
        if pixmap is None:
            return None
        dialog = QDialog(self)
        if speaker:
            title = "Processed speaker OCR image - enlarged"
        elif processed:
            title = "Processed OCR dialogue image - enlarged"
        else:
            title = "Captured dialogue region - enlarged"
        dialog.setWindowTitle(title)
        dialog.resize(900, 540)
        image = QLabel()
        image.setPixmap(
            pixmap.scaled(
                pixmap.size() * 2,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        scroll = QScrollArea()
        scroll.setWidget(image)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        set_action_icon(buttons.button(QDialogButtonBox.StandardButton.Close), "x")
        buttons.rejected.connect(dialog.close)
        layout = QVBoxLayout(dialog)
        layout.addWidget(scroll)
        layout.addWidget(buttons)
        return dialog

    def _scale_preview(self) -> None:
        for preview, pixmap in (
            (self.preview, self.source_pixmap),
            (self.processed_preview, self.processed_pixmap),
            (self.speaker_processed_preview, self.speaker_processed_pixmap),
        ):
            if pixmap is None:
                continue
            size = preview.size()
            if size.width() <= 1 or size.height() <= 1:
                continue
            preview.setPixmap(
                pixmap.scaled(
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    @staticmethod
    def _format_latency(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f} ms"

    @staticmethod
    def _format_confidence(value: float | None) -> str:
        return "Unavailable" if value is None else f"{value:.1f}%"
