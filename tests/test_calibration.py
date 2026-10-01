import os
import shlex
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import QPoint, QRect, Qt, QThreadPool, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QTextEdit  # noqa: E402
from pytesseract import pytesseract as pytesseract_runtime  # noqa: E402

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

    def test_review_failure_keeps_selection_visible_and_retryable(self):
        for stage in ("construction", "execution"):
            with self.subTest(stage=stage):
                reviewer = Mock()
                if stage == "construction":
                    reviewer.side_effect = RuntimeError("review failed")
                else:
                    reviewer.return_value.exec.side_effect = RuntimeError(
                        "review failed"
                    )
                save = Mock()
                overlay = DialogRegionOverlay(
                    background=Image.new("RGB", (800, 450), "black"),
                    reviewer=reviewer,
                    save_region=save,
                )
                self.addCleanup(overlay.deleteLater)
                self.addCleanup(overlay.close)
                overlay.resize(800, 450)
                overlay.origin = QPoint(80, 270)
                overlay.current = QPoint(720, 414)
                overlay.show()

                overlay._review_rectangle(QRect(80, 270, 640, 144))

                self.assertTrue(overlay.isVisible())
                self.assertIn("review failed", overlay.save_error)
                self.assertEqual(overlay.origin, QPoint(80, 270))
                save.assert_not_called()
                overlay.reviewer = Mock(
                    return_value=Mock(
                        exec=Mock(return_value=QDialog.DialogCode.Accepted)
                    )
                )
                overlay._review_rectangle(QRect(80, 270, 640, 144))
                save.assert_called_once()
                self.assertFalse(overlay.isVisible())

    def test_selection_clipped_below_minimum_reports_error_and_can_be_retried(self):
        for frozen_preview in (False, True):
            with self.subTest(frozen_preview=frozen_preview):
                reviewer = Mock(
                    return_value=Mock(
                        exec=Mock(return_value=QDialog.DialogCode.Accepted)
                    )
                )
                save = Mock()
                overlay = DialogRegionOverlay(
                    background=Image.new("RGB", (800, 450)) if frozen_preview else None,
                    reviewer=reviewer,
                    save_region=save,
                )
                self.addCleanup(overlay.deleteLater)
                self.addCleanup(overlay.close)
                failures = []
                overlay.save_failed.connect(failures.append)
                overlay.resize(800, 450)
                overlay.origin = QPoint(795, 200)
                overlay.current = QPoint(850, 250)
                overlay.show()

                overlay._review_rectangle(QRect(overlay.origin, overlay.current))

                self.assertTrue(overlay.isVisible())
                self.assertIn("at least 20 by 20 pixels", failures[0])
                reviewer.assert_not_called()
                save.assert_not_called()
                overlay._review_rectangle(QRect(80, 270, 640, 144))
                save.assert_called_once()
                self.assertFalse(overlay.isVisible())

    def test_closing_overlay_during_review_prevents_late_save(self):
        save = Mock()
        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450), "black"),
            save_region=save,
        )
        self.addCleanup(overlay.deleteLater)
        overlay.resize(800, 450)
        overlay.show()

        def finish_after_close():
            overlay.close()
            return QDialog.DialogCode.Accepted

        overlay.reviewer = Mock(return_value=Mock(exec=finish_after_close))
        overlay._review_rectangle(QRect(80, 270, 640, 144))

        save.assert_not_called()
        self.assertFalse(overlay.isVisible())

    def test_closing_overlay_rejects_its_modal_review_and_cancels_ocr(self):
        reviews = []

        def reviewer(image):
            review = CalibrationReviewDialog(
                image, recognizer=lambda _image: OCRResult("", "", 0, "gray", 1)
            )
            reviews.append(review)
            self.addCleanup(review.deleteLater)
            return review

        save = Mock()
        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450), "black"),
            reviewer=reviewer,
            save_region=save,
        )
        self.addCleanup(overlay.deleteLater)
        overlay.resize(800, 450)
        overlay.show()
        QTimer.singleShot(0, overlay.close)
        # Bound the regression run if the modal is not closed by its owner.
        timed_out = []
        timeout = QTimer()
        timeout.setSingleShot(True)

        def reject_after_timeout():
            timed_out.append(True)
            reviews[0].reject()

        timeout.timeout.connect(reject_after_timeout)
        self.addCleanup(timeout.stop)
        timeout.start(500)

        overlay._review_rectangle(QRect(80, 270, 640, 144))
        timeout.stop()

        self.assertFalse(timed_out)
        self.assertTrue(reviews[0]._ocr_cancelled.is_set())
        self.assertFalse(reviews[0].runner.active)
        self.assertEqual(reviews[0].result(), QDialog.DialogCode.Rejected)
        save.assert_not_called()

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
        self.assertTrue(dialog.progress.isHidden())
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

    def test_cancel_and_draw_again_invalidate_pending_ocr(self):
        for outcome in ("cancel", "draw-again"):
            with self.subTest(outcome=outcome):
                started = Event()
                release = Event()

                def slow_recognizer(_image):
                    started.set()
                    release.wait(3)
                    return OCRResult("Selone", "Late text", 94.5, "gray", 1)

                dialog = CalibrationReviewDialog(
                    Image.new("RGB", (640, 180), "black"),
                    recognizer=slow_recognizer,
                )
                self.wait_for(started.is_set)
                if outcome == "cancel":
                    dialog.reject()
                else:
                    dialog.done(CalibrationReviewDialog.DrawAgain)
                self.assertFalse(dialog.runner.active)
                release.set()
                self.assertTrue(dialog.runner.thread_pool.waitForDone(3_000))
                self.application.processEvents()
                self.assertNotIn("Late text", dialog.result_text.toPlainText())
                dialog.deleteLater()

    @unittest.skipIf(os.name == "nt", "Requires a POSIX OCR executable fixture")
    def test_cancelled_review_releases_stalled_ocr_subprocess(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            started = root / "started"
            executable = root / "stall-ocr"
            executable.write_text(
                f"#!/bin/sh\ntouch {shlex.quote(str(started))}\nexec sleep 5\n"
            )
            executable.chmod(0o755)
            pool = QThreadPool()
            pool.setMaxThreadCount(1)
            try:
                with (
                    patch.object(pytesseract_runtime, "tesseract_cmd", str(executable)),
                    patch("vntts.calibration.CALIBRATION_OCR_TIMEOUT_SECONDS", 1.5),
                    patch(
                        "vntts.ocr.preprocess_dialog_image",
                        side_effect=lambda image, _profile: image,
                    ),
                ):
                    dialog = CalibrationReviewDialog(
                        Image.new("RGB", (320, 100), "black"), thread_pool=pool
                    )
                    self.wait_for(started.exists)
                    dialog.reject()
                    self.assertFalse(dialog.runner.active)
                    self.assertTrue(pool.waitForDone(3_000))
                    self.assertEqual(pool.activeThreadCount(), 0)
                    dialog.deleteLater()
            finally:
                pool.waitForDone(6_000)

    def test_accept_close_and_escape_cancel_pending_recognition(self):
        for outcome in ("accept", "close", "escape"):
            with self.subTest(outcome=outcome):
                started = Event()
                release = Event()

                def slow_recognizer(_image):
                    started.set()
                    release.wait(3)
                    return OCRResult("Selone", "Late text", 94.5, "gray", 1)

                dialog = CalibrationReviewDialog(
                    Image.new("RGB", (640, 180)), recognizer=slow_recognizer
                )
                self.addCleanup(dialog.deleteLater)
                self.addCleanup(dialog.close)
                self.addCleanup(release.set)
                dialog.show()
                self.wait_for(started.is_set)
                if outcome == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    getattr(dialog, outcome)()

                self.assertTrue(dialog._ocr_cancelled.is_set())
                self.assertFalse(dialog.runner.active)
                release.set()
                self.assertTrue(dialog.runner.thread_pool.waitForDone(3_000))
                self.application.processEvents()
                self.assertNotIn("Late text", dialog.result_text.toPlainText())

    def test_failed_ocr_requires_explicit_capture_only_save(self):
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (640, 180), "black"),
            recognizer=lambda _image: (_ for _ in ()).throw(OSError("OCR unavailable")),
        )
        self.wait_for(lambda: not dialog.runner.active)

        self.assertIn("OCR preview failed", dialog.result_text.toPlainText())
        self.assertEqual(dialog.save_button.text(), "Save region without OCR preview")
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertTrue(dialog.progress.isHidden())
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
        finished = []
        dialog.finished.connect(finished.append)
        dialog.show()
        dialog.retry_button.click()
        self.application.processEvents()

        self.assertEqual(finished, [CalibrationReviewDialog.DrawAgain])
        dialog.deleteLater()

    def test_overlay_can_select_adjust_retry_and_accept_with_keyboard(self):
        decisions = iter(
            [CalibrationReviewDialog.DrawAgain, QDialog.DialogCode.Accepted]
        )
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

    def test_review_cancel_closes_the_calibration_flow(self):
        class Reviewer:
            def __init__(self, _image):
                pass

            def exec(self):
                return QDialog.DialogCode.Rejected

        with TemporaryDirectory() as temporary_directory:
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                background=Image.new("RGB", (1600, 900), "black"),
                reviewer=Reviewer,
            )
            selected = []
            closed = []
            overlay.selected.connect(selected.append)
            overlay.closed.connect(lambda: closed.append(True))
            overlay.resize(800, 450)
            overlay.show()

            overlay._review_rectangle(QRect(80, 270, 640, 144))
            self.application.processEvents()

            self.assertEqual(selected, [])
            self.assertEqual(closed, [True])
            self.assertFalse(overlay.isVisible())
            overlay.deleteLater()

    def test_region_save_failure_keeps_selection_visible_and_retryable(self):
        class Reviewer:
            def __init__(self, _image):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450), "black"),
            reviewer=Reviewer,
            save_region=lambda _region: (_ for _ in ()).throw(OSError("disk full")),
        )
        selected = []
        failures = []
        overlay.selected.connect(selected.append)
        overlay.save_failed.connect(failures.append)
        overlay.resize(800, 450)
        overlay.origin = QPoint(80, 270)
        overlay.current = QPoint(720, 414)
        overlay.show()
        overlay._review_rectangle(QRect(80, 270, 640, 144))
        self.application.processEvents()

        self.assertTrue(overlay.isVisible())
        self.assertEqual(selected, [])
        self.assertIn("disk full", overlay.save_error)
        self.assertIn("disk full", failures[0])
        self.assertIsNotNone(overlay.origin)
        overlay.save_region = lambda _region: None
        overlay._review_rectangle(QRect(80, 270, 640, 144))
        self.application.processEvents()
        self.assertEqual(len(selected), 1)
        self.assertFalse(overlay.isVisible())
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
