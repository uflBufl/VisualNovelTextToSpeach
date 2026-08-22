import sys

import mss
from PIL import Image
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from vntts.async_ui import LatestTaskRunner
from vntts.ocr import (
    DialogRegion,
    get_dialog_region_file,
    recognize_dialog_image_result,
    save_dialog_region,
)


def capture_calibration_background(geometry=None):
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


def pixmap_from_pil(image):
    image = image.convert("RGB")
    qimage = QImage(
        image.tobytes("raw", "RGB"),
        image.width,
        image.height,
        image.width * 3,
        QImage.Format.Format_RGB888,
    ).copy()
    return QPixmap.fromImage(qimage)


class CalibrationReviewDialog(QDialog):
    def __init__(
        self,
        image,
        parent=None,
        *,
        recognizer=recognize_dialog_image_result,
        thread_pool=None,
    ):
        super().__init__(parent)
        self.recognizer = recognizer
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._recognition_finished)
        self.setWindowTitle("Confirm dialogue capture")
        self.resize(760, 460)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName("Selected dialogue region preview")
        self.preview.setAccessibleDescription(
            "Frozen screenshot crop used for calibration review"
        )
        self.preview.setPixmap(
            pixmap_from_pil(image).scaled(
                720,
                260,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setAccessibleName("Calibration OCR result")
        self.result_text.setAccessibleDescription(
            "Recognized speaker, confidence and dialogue from the selected region"
        )
        self.result_text.setPlainText("Recognizing the selected dialogue region...")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Calibration OCR progress")
        self.progress.setAccessibleDescription(
            "Progress while recognizing the frozen calibration preview"
        )
        note = QLabel(
            "Confirm only when the speaker name and complete dialogue are inside "
            "the preview. Retry to draw the area again."
        )
        note.setWordWrap(True)
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
        buttons.accepted.connect(self.accept)
        self.retry_button.clicked.connect(self.reject)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.preview)
        layout.addWidget(self.result_text)
        layout.addWidget(self.progress)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.setTabOrder(self.result_text, self.save_button)
        self.setTabOrder(self.save_button, self.retry_button)
        self.setTabOrder(self.retry_button, self.cancel_button)
        self.runner.start(self.recognizer, image.copy())

    def _recognition_finished(self, result, error):
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        if error is not None:
            self.result_text.setPlainText(f"OCR preview failed: {error}")
            self.save_button.setText("Save region without OCR preview")
            self.save_button.setEnabled(True)
            return
        speaker = result.character or "Narrator"
        self.result_text.setPlainText(
            f"Speaker: {speaker}\nOCR confidence: {result.confidence:.1f}%\n\n"
            f"{result.text or '(No dialogue recognized)'}"
        )
        self.save_button.setText("Save region")
        self.save_button.setEnabled(True)

    def closeEvent(self, event):
        self.runner.cancel()
        super().closeEvent(event)


class DialogRegionOverlay(QWidget):
    selected = Signal(object)
    closed = Signal()

    def __init__(self, output=None, *, platform=None, background=None, reviewer=None):
        super().__init__()
        platform = sys.platform if platform is None else platform
        self.origin = None
        self.current = None
        self.output = output or get_dialog_region_file()
        self.background = background
        self.background_pixmap = (
            pixmap_from_pil(background) if background is not None else None
        )
        self.reviewer = reviewer or CalibrationReviewDialog
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
            "Select the speaker name and dialogue. Press Enter for a suggested "
            "region, use arrows to move it, Shift plus arrows to resize it, and "
            "press Enter again to review. Press Escape to cancel."
        )
        self.selected.connect(self.persist)

    def persist(self, region):
        save_dialog_region(region, self.output)
        print(f"Saved dialog region to {self.output}")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.position().toPoint()
            self.current = self.origin
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.origin is not None:
            self.current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
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

    def _region_from_rectangle(self, rectangle):
        rectangle = rectangle.normalized().intersected(self.rect())
        if rectangle.width() < 20 or rectangle.height() < 20:
            raise ValueError("Calibration region must be at least 20 by 20 pixels")
        return DialogRegion(
            rectangle.left() / self.width(),
            rectangle.top() / self.height(),
            rectangle.width() / self.width(),
            rectangle.height() / self.height(),
        )

    def _review_rectangle(self, rectangle):
        region = self._region_from_rectangle(rectangle)
        if self.background is None:
            # Keeps the overlay directly usable in tests and by callers that
            # deliberately opt out of the frozen preview workflow.
            self.selected.emit(region)
            self.close()
            return
        crop = region.crop(self.background)
        self.hide()
        review = self.reviewer(crop)
        if review.exec() == QDialog.DialogCode.Accepted:
            self.selected.emit(region)
            self.close()
            return
        self.show()
        self.raise_()
        self.activateWindow()
        self.origin = None
        self.current = None
        self.update()

    def _set_suggested_keyboard_region(self):
        left = round(self.width() * 0.08)
        top = round(self.height() * 0.62)
        right = max(left + 20, round(self.width() * 0.92))
        bottom = max(top + 20, round(self.height() * 0.94))
        rectangle = QRect(left, top, right - left, bottom - top).intersected(
            self.rect()
        )
        self.origin = rectangle.topLeft()
        self.current = rectangle.bottomRight()
        self.update()

    def _adjust_keyboard_region(self, key, modifiers):
        if self.origin is None or self.current is None:
            self._set_suggested_keyboard_region()
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
        self.update()

    def keyPressEvent(self, event: QKeyEvent):
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
            self._adjust_keyboard_region(event.key(), event.modifiers())
            return
        if event.key() == Qt.Key.Key_R:
            self.origin = None
            self.current = None
            self.update()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        if self.background_pixmap is not None:
            painter.drawPixmap(self.rect(), self.background_pixmap)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self.origin is not None and self.current is not None:
            rectangle = QRect(self.origin, self.current).normalized()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rectangle, Qt.GlobalColor.transparent)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )
            painter.setPen(QPen(QColor(0, 220, 255), 3))
            painter.drawRect(rectangle)

        instructions = QRect(24, 24, max(0, self.width() - 48), 64)
        painter.fillRect(instructions, QColor(0, 0, 0, 190))
        painter.setPen(Qt.GlobalColor.white)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        if self.origin is None or self.current is None:
            instruction = (
                "Drag around the speaker name and dialogue text. Keyboard: press "
                "Enter for a suggested region. Press Esc to cancel."
            )
        else:
            instruction = (
                "Press Enter to review. Arrow keys move; Shift plus arrows resize; "
                "R clears the selection; Esc cancels."
            )
        painter.drawText(
            instructions.adjusted(16, 8, -16, -8),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            instruction,
        )


def show_calibration_overlay(geometry=None, *, background=None):
    if background is None:
        background = capture_calibration_background(geometry)
    overlay = DialogRegionOverlay(background=background)
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


def main():
    application = QApplication.instance() or QApplication(sys.argv)
    application.calibration_overlay = show_calibration_overlay()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
