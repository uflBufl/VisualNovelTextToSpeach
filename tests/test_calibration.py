import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QTextEdit  # noqa: E402

from vntts.calibration import CalibrationReviewDialog, DialogRegionOverlay  # noqa: E402
from vntts.ocr import OCRResult  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
