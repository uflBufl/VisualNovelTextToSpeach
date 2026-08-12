import sys

import mss
from PIL import Image
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

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
    def __init__(self, image, parent=None, *, recognizer=recognize_dialog_image_result):
        super().__init__(parent)
        self.setWindowTitle("Confirm dialogue capture")
        self.resize(760, 460)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setPixmap(
            pixmap_from_pil(image).scaled(
                720,
                260,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        result_text = QTextEdit()
        result_text.setReadOnly(True)
        try:
            result = recognizer(image)
            speaker = result.character or "Narrator"
            result_text.setPlainText(
                f"Speaker: {speaker}\nOCR confidence: {result.confidence:.1f}%\n\n"
                f"{result.text or '(No dialogue recognized)'}"
            )
        except Exception as error:
            result_text.setPlainText(f"OCR preview failed: {error}")
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
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Save region")
        buttons.button(QDialogButtonBox.StandardButton.Retry).setText("Draw again")
        buttons.accepted.connect(self.accept)
        buttons.button(QDialogButtonBox.StandardButton.Retry).clicked.connect(
            self.reject
        )
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(preview)
        layout.addWidget(result_text)
        layout.addWidget(note)
        layout.addWidget(buttons)


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
        region = DialogRegion(
            rectangle.left() / self.width(),
            rectangle.top() / self.height(),
            rectangle.width() / self.width(),
            rectangle.height() / self.height(),
        )
        if self.background is None:
            # Keeps the overlay directly usable in tests and by callers that
            # deliberately opt out of the frozen preview workflow.
            self.selected.emit(region)
            self.close()
            return
        crop = self.background.crop(
            (
                round(region.x * self.background.width),
                round(region.y * self.background.height),
                round((region.x + region.width) * self.background.width),
                round((region.y + region.height) * self.background.height),
            )
        )
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

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

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
        painter.drawText(
            instructions.adjusted(16, 8, -16, -8),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            "Drag around the speaker name and dialogue text. Press Esc to cancel.",
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
