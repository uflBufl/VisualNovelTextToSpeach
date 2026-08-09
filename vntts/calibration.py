import sys

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from vntts.ocr import DialogRegion, get_dialog_region_file, save_dialog_region


class DialogRegionOverlay(QWidget):
    selected = Signal(object)
    closed = Signal()

    def __init__(self, output=None):
        super().__init__()
        self.origin = None
        self.current = None
        self.output = output or get_dialog_region_file()
        self.setWindowTitle("Select the visual-novel dialog region")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
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
        self.selected.emit(
            DialogRegion(
                rectangle.left() / self.width(),
                rectangle.top() / self.height(),
                rectangle.width() / self.width(),
                rectangle.height() / self.height(),
            )
        )
        self.close()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self.origin is None or self.current is None:
            return
        rectangle = QRect(self.origin, self.current).normalized()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(rectangle, Qt.GlobalColor.transparent)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.setPen(QPen(QColor(0, 220, 255), 3))
        painter.drawRect(rectangle)


def show_calibration_overlay(geometry=None):
    overlay = DialogRegionOverlay()
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
    return overlay


def main():
    application = QApplication.instance() or QApplication(sys.argv)
    application.calibration_overlay = show_calibration_overlay()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
