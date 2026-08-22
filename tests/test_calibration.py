import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QRect, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QTextEdit  # noqa: E402

from vntts.calibration import (  # noqa: E402
    CalibrationReviewDialog,
    DialogRegionOverlay,
    show_calibration_overlay,
)
from vntts.ocr import OCRResult  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


class DialogRegionOverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for calibration OCR")

    def test_macos_overlay_remains_visible_when_application_loses_focus(self):
        with TemporaryDirectory() as temporary_directory:
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                platform="darwin",
            )

            self.assertTrue(
                overlay.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
            )
            overlay.deleteLater()

    def test_other_platforms_do_not_enable_macos_window_behavior(self):
        with TemporaryDirectory() as temporary_directory:
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                platform="win32",
            )

            self.assertFalse(
                overlay.testAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
            )
            overlay.deleteLater()

    def test_calibration_review_previews_ocr_without_speaking(self):
        result = OCRResult("Selone", "I have returned.", 94.5, "gray", 1)
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (640, 180), "black"),
            recognizer=lambda _image: result,
        )
        self.wait_for(lambda: not dialog.runner.active)

        rendered = dialog.findChild(QTextEdit).toPlainText()

        self.assertIn("Selone", rendered)
        self.assertIn("94.5%", rendered)
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.deleteLater()

    def test_slow_calibration_ocr_keeps_qt_responsive(self):
        started = Event()
        release = Event()
        result = OCRResult("Selone", "I have returned.", 94.5, "gray", 1)

        def slow_recognizer(_image):
            started.set()
            release.wait(3)
            return result

        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append("painted"))
        before = time.monotonic()
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (640, 180), "black"),
            recognizer=slow_recognizer,
        )
        elapsed = time.monotonic() - before
        self.wait_for(lambda: started.is_set() and bool(heartbeat))

        self.assertLess(elapsed, 0.1)
        self.assertTrue(dialog.runner.active)
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertIn("Recognizing", dialog.result_text.toPlainText())

        release.set()
        self.wait_for(lambda: not dialog.runner.active)
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertIn("Selone", dialog.result_text.toPlainText())
        dialog.deleteLater()

    def test_failed_ocr_requires_explicit_capture_only_save(self):
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (640, 180), "black"),
            recognizer=lambda _image: (_ for _ in ()).throw(OSError("OCR unavailable")),
        )
        self.wait_for(lambda: not dialog.runner.active)

        self.assertIn("OCR preview failed", dialog.result_text.toPlainText())
        self.assertEqual(dialog.save_button.text(), "Save region without OCR preview")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.deleteLater()

    def test_review_actions_have_keyboard_and_accessibility_contract(self):
        result = OCRResult("Selone", "I have returned.", 94.5, "gray", 1)
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (640, 180), "black"),
            recognizer=lambda _image: result,
        )
        self.wait_for(lambda: not dialog.runner.active)

        self.assertEqual(dialog.save_button.shortcut().toString(), "Ctrl+Return")
        self.assertEqual(dialog.retry_button.shortcut().toString(), "Ctrl+R")
        for widget in (
            dialog.preview,
            dialog.result_text,
            dialog.progress,
            dialog.save_button,
            dialog.retry_button,
            dialog.cancel_button,
        ):
            self.assertTrue(widget.accessibleName())
        rejected = []
        dialog.rejected.connect(lambda: rejected.append(True))
        dialog.show()
        dialog.retry_button.setFocus()
        QTest.keyClick(dialog.retry_button, Qt.Key.Key_Return)
        self.application.processEvents()

        self.assertEqual(rejected, [True])
        dialog.deleteLater()

    def test_overlay_can_select_adjust_retry_and_accept_with_keyboard(self):
        decisions = iter([QDialog.DialogCode.Rejected, QDialog.DialogCode.Accepted])
        crop_sizes = []

        class Reviewer:
            def __init__(self, image):
                crop_sizes.append(image.size)

            def exec(self):
                return next(decisions)

        with TemporaryDirectory() as temporary_directory:
            selected = []
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                background=Image.new("RGB", (1600, 900), "black"),
                reviewer=Reviewer,
            )
            overlay.selected.connect(selected.append)
            overlay.resize(800, 450)
            overlay.show()
            overlay.activateWindow()
            overlay.setFocus()
            self.application.processEvents()

            QTest.keyClick(overlay, Qt.Key.Key_Return)
            self.assertIsNotNone(overlay.origin)
            QTest.keyClick(
                overlay,
                Qt.Key.Key_Right,
                Qt.KeyboardModifier.ControlModifier,
            )
            QTest.keyClick(
                overlay,
                Qt.Key.Key_Down,
                Qt.KeyboardModifier.ShiftModifier,
            )
            QTest.keyClick(overlay, Qt.Key.Key_Return)
            self.assertIsNone(overlay.origin)

            QTest.keyClick(overlay, Qt.Key.Key_Return)
            QTest.keyClick(overlay, Qt.Key.Key_Return)
            self.application.processEvents()

            self.assertEqual(len(crop_sizes), 2)
            self.assertEqual(len(selected), 1)
            self.assertAlmostEqual(selected[0].left, 0.08, places=2)
            self.assertAlmostEqual(selected[0].top, 0.62, places=2)
            self.assertTrue(Path(temporary_directory, "region.json").is_file())
            overlay.deleteLater()

    def test_negative_monitor_and_scaled_pixels_keep_normalized_geometry(self):
        background = Image.new("RGB", (1600, 900), "black")
        geometry = WindowGeometry(-1600, -100, 800, 450)
        overlay = show_calibration_overlay(geometry, background=background)
        self.application.processEvents()

        region = overlay._region_from_rectangle(QRect(80, 270, 640, 144))
        crop = region.crop(background)

        self.assertEqual(overlay.geometry().size().width(), 800)
        self.assertEqual(overlay.geometry().size().height(), 450)
        self.assertEqual(overlay.geometry().left(), -1600)
        self.assertEqual(overlay.geometry().top(), -100)
        self.assertAlmostEqual(region.left, 0.1)
        self.assertAlmostEqual(region.top, 0.6)
        self.assertAlmostEqual(region.width, 0.8)
        self.assertAlmostEqual(region.height, 0.32)
        self.assertEqual(crop.size, (1280, 288))
        self.assertTrue(overlay.accessibleName())
        self.assertIn("Shift plus arrows", overlay.accessibleDescription())
        overlay.close()
        overlay.deleteLater()


if __name__ == "__main__":
    unittest.main()
