import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTextEdit  # noqa: E402

from vntts.calibration import CalibrationReviewDialog, DialogRegionOverlay  # noqa: E402
from vntts.ocr import OCRResult  # noqa: E402


class DialogRegionOverlayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

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

        rendered = dialog.findChild(QTextEdit).toPlainText()

        self.assertIn("Selone", rendered)
        self.assertIn("94.5%", rendered)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
