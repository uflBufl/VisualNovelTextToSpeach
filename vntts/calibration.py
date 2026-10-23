from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event

import mss
from PIL import Image
from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRect,
    QSignalBlocker,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QHideEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from vntts.async_ui import LatestTaskRunner
from vntts.diagnostics_ui import DiagnosticsDialog
from vntts.ocr import (
    DialogRegion,
    OCRResult,
    get_dialog_region,
    get_dialog_region_file,
    save_dialog_region,
)
from vntts.ocr_worker import recognize_dialog_image_result
from vntts.qt_images import pixmap_from_pil
from vntts.window_capture import WindowGeometry

ReviewerFactory = Callable[[Image.Image], QDialog]
CALIBRATION_OCR_TIMEOUT_SECONDS = 10.0
DIALOG_REGION_GUIDANCE = (
    "Allow for the tallest dialogue box: extra lines can move the speaker name upwards. "
    "Leave space above the current name."
)
_active_overlay: DialogRegionOverlay | None


class CaptureWindowVisibility(QObject):
    """Hide current Qt windows and restore only surviving, still-owned controls."""

    def __init__(
        self,
        parent: QObject,
        *,
        may_restore: Callable[[QWidget], bool],
    ) -> None:
        super().__init__(parent)
        self.may_restore = may_restore
        windows = []
        for window in QApplication.topLevelWidgets():
            visible = window.isVisible()
            if isinstance(window, DiagnosticsDialog):
                visible = visible or window.concealed_for_capture
                window.cancel_refresh()
            if visible:
                windows.append(window)
        self.windows = tuple(windows)
        self.closed: set[QObject] = set()
        self.finished = Event()
        for window in self.windows:
            window.installEventFilter(self)
            window.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Close:
            self.closed.add(watched)
        return False

    def restore(self) -> None:
        if self.finished.is_set():
            return
        self.finished.set()
        for window in self.windows:
            if not isValid(window):
                continue
            window.removeEventFilter(self)
            if window in self.closed or not self.may_restore(window):
                continue
            previous = window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            try:
                window.show()
            finally:
                if isValid(window):
                    window.setAttribute(
                        Qt.WidgetAttribute.WA_ShowWithoutActivating, previous
                    )
        self.deleteLater()


def capture_calibration_background(
    geometry: WindowGeometry | None = None,
) -> Image.Image:
    with mss.mss() as capture:
        monitor = (
            capture.monitors[1]
            if geometry is None
            else {
                "left": geometry.left,
                "top": geometry.top,
                "width": geometry.width,
                "height": geometry.height,
            }
        )
        screenshot = capture.grab(monitor)
    return Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")


class CalibrationReviewDialog(QDialog):
    DrawAgain = 2

    def __init__(
        self,
        image: Image.Image,
        parent: QWidget | None = None,
        *,
        recognizer: Callable[[Image.Image], OCRResult] = recognize_dialog_image_result,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._ocr_cancelled = Event()
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._recognition_finished)
        self.setWindowTitle("Confirm dialogue capture")
        self.resize(760, 380)
        self.preview_pixmap = pixmap_from_pil(image)
        self.capture_preview: QDialog | None = None
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored
        )
        self.preview.installEventFilter(self)
        self.preview.setMinimumHeight(100)
        self.preview.setMaximumHeight(260)
        self.preview.setAccessibleName("Selected dialogue region preview")
        self.preview.setAccessibleDescription(
            "Frozen screenshot crop used for calibration review"
        )
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMinimumHeight(120)
        self.result_text.setAccessibleName("Calibration OCR result")
        self.result_text.setAccessibleDescription(
            "Recognized speaker, confidence and dialogue from the selected area"
        )
        self.result_text.setPlainText("Recognizing the selected dialogue region...")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Calibration OCR progress")
        self.progress.setAccessibleDescription(
            "Progress while recognizing the frozen calibration preview"
        )
        note = QLabel(
            "Compare the complete speaker name and dialogue in the image with the OCR result. "
            + DIALOG_REGION_GUIDANCE
            + ' Use "Draw again" if the area is too small.'
        )
        note.setWordWrap(True)
        buttons = self._create_buttons()
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.addWidget(note)
        body_layout.addWidget(self.preview, 1)
        body_layout.addWidget(
            self.enlarge_button, alignment=Qt.AlignmentFlag.AlignRight
        )
        body_layout.addWidget(self.result_text, 1)
        body_layout.addWidget(self.progress)
        self.body_scroll = QScrollArea()
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setWidget(body)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(self.body_scroll, 1)
        layout.addWidget(buttons)
        self.setTabOrder(self.enlarge_button, self.result_text)
        self.setTabOrder(self.result_text, self.save_button)
        self.setTabOrder(self.save_button, self.retry_button)
        self.setTabOrder(self.retry_button, self.cancel_button)
        self._start_recognition(image.copy(), recognizer)

    def show_enlarged_capture(self) -> None:
        if self.capture_preview is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Captured dialogue - full size")
            dialog.resize(900, 540)
            image = QLabel()
            image.setPixmap(self.preview_pixmap)
            image.setAccessibleName("Full-size captured dialogue image")
            scroll = QScrollArea()
            scroll.setAccessibleName("Full-size capture scroll area")
            scroll.setWidget(image)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(dialog.reject)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.addWidget(scroll)
            layout.addWidget(buttons)
            self.capture_preview = dialog
        self.capture_preview.open()

    def _create_buttons(self) -> QDialogButtonBox:
        self.enlarge_button = QPushButton("Enlarge capture")
        self.enlarge_button.setAutoDefault(False)
        self.enlarge_button.setAccessibleDescription(
            "View the captured image at full size with scrolling"
        )
        self.enlarge_button.clicked.connect(self.show_enlarged_capture)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Retry
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.retry_button = buttons.button(QDialogButtonBox.StandardButton.Retry)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.save_button.setText("Save region")
        self.save_button.setEnabled(False)
        self.save_button.setAccessibleName("Save calibrated dialogue region")
        self.save_button.setAccessibleDescription(
            "Save the current normalized dialogue region"
        )
        self.save_button.setShortcut(QKeySequence("Ctrl+Return"))
        self.retry_button.setText("Draw again")
        self.retry_button.setAccessibleName("Draw calibration region again")
        self.retry_button.setAccessibleDescription(
            "Return to the frozen screen and replace the current selection"
        )
        self.retry_button.setShortcut(QKeySequence("Ctrl+R"))
        self.cancel_button.setAccessibleName("Cancel calibration")
        self.cancel_button.setAccessibleDescription(
            "Close calibration without saving the selected region"
        )
        self.save_button.clicked.connect(self.accept)
        self.retry_button.clicked.connect(lambda: self.done(self.DrawAgain))
        buttons.rejected.connect(self.reject)
        return buttons

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.preview and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self, self._scale_preview)
        return super().eventFilter(watched, event)

    def _scale_preview(self) -> None:
        self.preview.setPixmap(
            self.preview_pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _start_recognition(
        self, image: Image.Image, recognizer: Callable[[Image.Image], OCRResult]
    ) -> None:
        if recognizer is recognize_dialog_image_result:
            self.runner.start(
                recognizer,
                image,
                timeout=CALIBRATION_OCR_TIMEOUT_SECONDS,
                cancelled=self._ocr_cancelled.is_set,
            )
        else:
            self.runner.start(recognizer, image)

    def _recognition_finished(self, result: object, error: Exception | None) -> None:
        self.progress.hide()
        if error is not None:
            self.result_text.setPlainText(
                f"OCR preview failed: {error}\n\nDraw again, or save the selected area without recognized text."
            )
            self.save_button.setText("Save without OCR")
            self.save_button.setAccessibleName("Save selected area without OCR preview")
            self.save_button.setEnabled(True)
            return
        if not isinstance(result, OCRResult):
            self.result_text.setPlainText(
                "OCR preview returned an invalid result.\n\nDraw again, or save the selected area without recognized text."
            )
            self.save_button.setText("Save without OCR")
            self.save_button.setAccessibleName("Save selected area without OCR preview")
            self.save_button.setEnabled(True)
            return
        speaker = result.speaker_note or result.character or "No name recognized"
        dialogue = result.text.strip()
        confidence = (
            f"Speaker OCR confidence: {result.speaker_confidence:.1f}%\n"
            if result.speaker_confidence is not None
            else ""
        )
        confidence += f"Dialogue OCR confidence: {(result.dialogue_confidence if result.dialogue_confidence is not None else result.confidence):.1f}%"
        self.result_text.setPlainText(
            f"Speaker: {speaker}\n\n"
            + (
                dialogue
                or "No dialogue recognized. Draw again, or save without OCR to keep the selected area."
            )
            + f"\n\n{confidence}"
        )
        self.save_button.setText("Save without OCR" if not dialogue else "Save region")
        self.save_button.setAccessibleName(self.save_button.text())
        self.save_button.setEnabled(True)

    def _cancel_recognition(self) -> None:
        self._ocr_cancelled.set()
        self.runner.cancel()

    def done(self, result: int) -> None:
        if self.capture_preview is not None:
            self.capture_preview.reject()
        self._cancel_recognition()
        super().done(result)


class DialogRegionOverlay(QWidget):
    selected = Signal(object)
    closed = Signal()
    save_failed = Signal(str)

    def __init__(
        self,
        output: str | Path | None = None,
        *,
        platform: str | None = None,
        background: Image.Image | None = None,
        reviewer: ReviewerFactory | None = None,
        save_region: Callable[[DialogRegion], None] | None = None,
        capture_region: DialogRegion | None = None,
    ) -> None:
        super().__init__()
        platform = sys.platform if platform is None else platform
        self.origin: QPoint | None = None
        self.current: QPoint | None = None
        self.output = output or get_dialog_region_file()
        self.background = background
        self.background_pixmap = (
            pixmap_from_pil(background) if background is not None else None
        )
        self.reviewer = reviewer or CalibrationReviewDialog
        self._review: QDialog | None = None
        self._closed = False
        self.save_region = save_region or self.persist
        self.capture_region = capture_region or get_dialog_region()
        self.save_error: str | None = None
        self.setWindowTitle("Select the visual-novel dialog region")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Dialogue region calibration overlay")
        self.setAccessibleDescription(
            "Select the whole dialogue box, including the speaker name and continue indicator. "
            + DIALOG_REGION_GUIDANCE
            + " Press Enter for a suggested region, use arrows to move it, Shift plus "
            "arrows to resize it, and Enter again to review. Escape cancels. "
            "F2 opens exact pixel coordinates."
        )
        self._create_bounds_editor()

    def _create_bounds_editor(self) -> None:
        self.bounds_editor = QWidget(
            self, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        )
        self.bounds_editor.setWindowTitle("Dialogue region coordinates")
        self.bounds_editor.setAccessibleName("Exact dialogue region bounds")
        self.bounds_editor.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.bounds_editor.setAttribute(
            Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow,
            self.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow),
        )
        layout = QFormLayout(self.bounds_editor)
        self.bounds_inputs: dict[str, QSpinBox] = {}
        for name in ("Left", "Top", "Width", "Height"):
            control = QSpinBox()
            control.setAccessibleName(f"Dialogue region {name.lower()} in pixels")
            control.setObjectName(f"calibration_bounds_{name.lower()}")
            control.valueChanged.connect(self._bounds_changed)
            self.bounds_inputs[name] = control
            layout.addRow(f"{name} (pixels)", control)
        review = QPushButton("Review region")
        review.clicked.connect(self._review_bounds)
        review.setShortcut(QKeySequence("Ctrl+Return"))
        layout.addRow(review)
        reset = QPushButton("Reset region")
        reset.clicked.connect(self._reset_selection)
        layout.addRow(reset)
        cancel = QPushButton("Cancel calibration")
        cancel.clicked.connect(self.close)
        cancel.setShortcut(QKeySequence("Esc"))
        layout.addRow(cancel)

    def _sync_bounds_editor(self) -> None:
        if self.origin is None or self.current is None:
            capture = self.capture_region
            rectangle = QRect(
                round(capture.left * self.width()),
                round(capture.top * self.height()),
                max(20, round(capture.width * self.width())),
                max(20, round(capture.height * self.height())),
            ).intersected(self.rect())
        else:
            rectangle = (
                QRect(self.origin, self.current).normalized().intersected(self.rect())
            )
        values = (
            rectangle.left(),
            rectangle.top(),
            rectangle.width(),
            rectangle.height(),
        )
        ranges = (
            (0, max(0, self.width() - 20)),
            (0, max(0, self.height() - 20)),
            (20, max(20, self.width() - rectangle.left())),
            (20, max(20, self.height() - rectangle.top())),
        )
        for control, value, limits in zip(
            self.bounds_inputs.values(), values, ranges, strict=True
        ):
            with QSignalBlocker(control):
                control.setRange(*limits)
                control.setValue(value)

    def _bounds_changed(self) -> None:
        left, top, width, height = (
            control.value() for control in self.bounds_inputs.values()
        )
        rectangle = QRect(
            left, top, min(width, self.width() - left), min(height, self.height() - top)
        )
        self.origin, self.current = rectangle.topLeft(), rectangle.bottomRight()
        self._sync_bounds_editor()
        self.update()

    def _reset_selection(self) -> None:
        self.origin = None
        self.current = None
        self.save_error = None
        self._sync_bounds_editor()
        self.update()

    def _review_bounds(self) -> None:
        self._bounds_changed()
        assert self.origin is not None and self.current is not None
        self._review_rectangle(QRect(self.origin, self.current))

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._sync_bounds_editor()
        self.bounds_editor.show()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_bounds_editor()

    def hideEvent(self, event: QHideEvent) -> None:
        self.bounds_editor.hide()
        super().hideEvent(event)

    def persist(self, region: DialogRegion) -> None:
        save_dialog_region(region, self.output)
        print(f"Saved dialog region to {self.output}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.current = self.origin
            self._sync_bounds_editor()
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.origin is not None:
            self.current = event.position().toPoint()
            self._sync_bounds_editor()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.origin is None:
            return
        self.current = event.position().toPoint()
        rectangle = QRect(self.origin, self.current).normalized()
        if rectangle.width() < 20 or rectangle.height() < 20:
            self.origin = None
            self.current = None
            self.update()
            return
        self._review_rectangle(rectangle)

    def _region_from_rectangle(self, rectangle: QRect) -> DialogRegion:
        rectangle = rectangle.normalized().intersected(self.rect())
        if rectangle.width() < 20 or rectangle.height() < 20:
            raise ValueError("Calibration region must be at least 20 by 20 pixels")
        return DialogRegion(
            rectangle.left() / self.width(),
            rectangle.top() / self.height(),
            rectangle.width() / self.width(),
            rectangle.height() / self.height(),
        )

    def _review_rectangle(self, rectangle: QRect) -> None:
        if self._closed or self._review is not None:
            return
        try:
            region = self._region_from_rectangle(rectangle)
            if self.background is None:
                self._save_selection(region)
                return
            crop = region.crop(self.background)
            self.hide()
            review = self.reviewer(crop)
            review.setParent(self, review.windowFlags())
            self._review = review
            review.finished.connect(
                lambda result, source=review, selected=region: self._complete_review(
                    source, selected, result
                )
            )
            review.open()
        except Exception as error:
            failed_review = self._review
            self._review = None
            if failed_review is not None:
                failed_review.reject()
            self._selection_failed(f"Unable to review the dialogue area: {error}")

    def _complete_review(
        self, source: QDialog, region: DialogRegion, result: int
    ) -> None:
        if self._review is not source:
            return
        self._review = None
        if self._closed:
            return
        if result == QDialog.DialogCode.Accepted:
            self._save_selection(region)
            return
        if result == QDialog.DialogCode.Rejected:
            self.close()
            return
        self.show()
        self.raise_()
        self.activateWindow()
        self._reset_selection()

    def _save_selection(self, region: DialogRegion) -> None:
        if self._closed:
            return
        try:
            self.save_region(region)
        except OSError as error:
            self._selection_failed(f"Unable to save the dialogue area: {error}")
            return
        if self._closed:
            return
        self.save_error = None
        self.selected.emit(region)
        self.close()

    def _selection_failed(self, message: str) -> None:
        if self._closed:
            return
        self.save_error = message
        self.save_failed.emit(message)
        self.show()
        self.raise_()
        self.activateWindow()
        self.update()

    def _set_suggested_keyboard_region(self) -> None:
        capture = self.capture_region
        rectangle = QRect(
            round(capture.left * self.width()),
            round(capture.top * self.height()),
            max(20, round(capture.width * self.width())),
            max(20, round(capture.height * self.height())),
        ).intersected(self.rect())
        self.origin = rectangle.topLeft()
        self.current = rectangle.bottomRight()
        self._sync_bounds_editor()
        self.update()

    def _adjust_keyboard_region(
        self, key: Qt.Key, modifiers: Qt.KeyboardModifier
    ) -> None:
        if self.origin is None or self.current is None:
            self._set_suggested_keyboard_region()
        if self.origin is None or self.current is None:
            return
        rectangle = QRect(self.origin, self.current).normalized()
        step = 10 if modifiers & Qt.KeyboardModifier.ControlModifier else 2
        horizontal = {
            Qt.Key.Key_Left: -step,
            Qt.Key.Key_Right: step,
        }.get(key, 0)
        vertical = {
            Qt.Key.Key_Up: -step,
            Qt.Key.Key_Down: step,
        }.get(key, 0)
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            if horizontal:
                rectangle.setRight(
                    max(
                        rectangle.left() + 19,
                        min(self.width() - 1, rectangle.right() + horizontal),
                    )
                )
            if vertical:
                rectangle.setBottom(
                    max(
                        rectangle.top() + 19,
                        min(self.height() - 1, rectangle.bottom() + vertical),
                    )
                )
        else:
            rectangle.translate(horizontal, vertical)
            if rectangle.left() < 0:
                rectangle.moveLeft(0)
            if rectangle.top() < 0:
                rectangle.moveTop(0)
            if rectangle.right() >= self.width():
                rectangle.moveRight(self.width() - 1)
            if rectangle.bottom() >= self.height():
                rectangle.moveBottom(self.height() - 1)
        self.origin = rectangle.topLeft()
        self.current = rectangle.bottomRight()
        self._sync_bounds_editor()
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_F2:
            self._sync_bounds_editor()
            self.bounds_editor.show()
            self.bounds_editor.raise_()
            self.bounds_editor.activateWindow()
            self.bounds_inputs["Left"].setFocus()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            if self.origin is None or self.current is None:
                self._set_suggested_keyboard_region()
            else:
                self._review_rectangle(QRect(self.origin, self.current).normalized())
            return
        if event.key() in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
        ):
            self._adjust_keyboard_region(Qt.Key(event.key()), event.modifiers())
            return
        if event.key() == Qt.Key.Key_R:
            self._reset_selection()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._closed:
            self._closed = True
            if isinstance(self._review, QDialog):
                self._review.reject()
            self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        if self.background_pixmap is not None:
            painter.drawPixmap(self.rect(), self.background_pixmap)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self.origin is not None and self.current is not None:
            rectangle = QRect(self.origin, self.current).normalized()
            if self.background_pixmap is not None:
                painter.save()
                painter.setClipRect(rectangle)
                painter.drawPixmap(self.rect(), self.background_pixmap)
                painter.restore()
            else:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Clear
                )
                painter.fillRect(rectangle, Qt.GlobalColor.transparent)
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
            painter.setPen(QPen(QColor(0, 220, 255), 3))
            painter.drawRect(rectangle)

        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        instruction = self._instruction_text()
        text_width = max(1, self.width() - 80)
        text_bounds = painter.fontMetrics().boundingRect(
            QRect(0, 0, text_width, 0), Qt.TextFlag.TextWordWrap, instruction
        )
        instructions = QRect(24, 24, text_width + 32, text_bounds.height() + 16)
        if self.origin is not None and self.current is not None:
            if instructions.intersects(QRect(self.origin, self.current).normalized()):
                instructions.moveBottom(self.height() - 24)
        painter.fillRect(instructions, QColor(0, 0, 0, 190))
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(
            instructions.adjusted(16, 8, -16, -8),
            Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
            | Qt.TextFlag.TextWordWrap,
            instruction,
        )

    def _instruction_text(self) -> str:
        if self.save_error:
            return f"{self.save_error}. Enter reviews and retries; R starts again; Esc cancels."
        selected = self.origin is not None and self.current is not None
        return (
            "Select the whole dialogue box, including the name and continue indicator. "
            + DIALOG_REGION_GUIDANCE
            + " "
            + (
                "Enter reviews the area. "
                if selected
                else "Drag to select, or Enter suggests an area. "
            )
            + "Arrows move; Shift + arrows resize; F2 edits exact coordinates; R starts again; Esc cancels."
        )


def show_calibration_overlay(
    geometry: WindowGeometry | None = None,
    *,
    background: Image.Image | None = None,
    save_region: Callable[[DialogRegion], None] | None = None,
    capture_region: DialogRegion | None = None,
) -> DialogRegionOverlay:
    if background is None:
        background = capture_calibration_background(geometry)
    overlay = DialogRegionOverlay(
        background=background,
        save_region=save_region,
        capture_region=capture_region,
    )
    if geometry is None:
        overlay.showFullScreen()
    else:
        overlay.setGeometry(
            QRect(
                geometry.left,
                geometry.top,
                geometry.width,
                geometry.height,
            )
        )
        overlay.show()
        overlay.raise_()
    overlay.activateWindow()
    overlay.setFocus(Qt.FocusReason.OtherFocusReason)
    return overlay


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(value in {"-h", "--help"} for value in arguments):
        print("usage: vntts-calibrate")
        return 0
    if arguments:
        print("usage: vntts-calibrate", file=sys.stderr)
        return 2
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication(sys.argv)
    global _active_overlay
    _active_overlay = show_calibration_overlay()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
