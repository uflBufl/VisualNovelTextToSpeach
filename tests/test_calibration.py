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
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QScrollArea,
    QTextEdit,
)
from pytesseract import pytesseract as pytesseract_runtime  # noqa: E402

from tests.qt_task_fixtures import release_and_wait_for_pool  # noqa: E402
from vntts.calibration import (  # noqa: E402
    CalibrationReviewDialog,
    DialogRegionOverlay,
    show_calibration_overlay,
)
from vntts.ocr import DialogRegion, OCRResult  # noqa: E402
from vntts.window_capture import WindowGeometry  # noqa: E402


class _ReviewDecision(QDialog):
    def __init__(self, decision):
        super().__init__()
        self.decision = decision

    def open(self):
        super().open()
        self.done(self.decision)


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

    def test_exact_bounds_editor_clamps_and_reviews_frozen_pixels(self):
        saved = []
        reviewer = Mock(return_value=_ReviewDecision(QDialog.DialogCode.Accepted))
        background = Image.new("RGB", (800, 450), "navy")
        overlay = DialogRegionOverlay(
            background=background, reviewer=reviewer, save_region=saved.append
        )
        self.addCleanup(overlay.deleteLater)
        self.addCleanup(overlay.close)
        overlay.resize(800, 450)
        overlay.show()
        self.assertTrue(overlay.bounds_editor.isVisible())
        for name, value in (
            ("Left", 80),
            ("Top", 270),
            ("Width", 640),
            ("Height", 144),
        ):
            control = overlay.bounds_inputs[name]
            self.assertIn("pixels", control.accessibleName())
            control.setValue(value)
        self.assertEqual(
            QRect(overlay.origin, overlay.current), QRect(80, 270, 640, 144)
        )
        overlay._review_bounds()
        self.assertEqual(reviewer.call_args.args[0].size, (640, 144))
        self.assertEqual(saved, [DialogRegion(0.1, 0.6, 0.8, 0.32)])
        self.assertFalse(overlay.bounds_editor.isVisible())

    def test_exact_bounds_editor_enforces_minimum_and_cancel_keeps_saved_region(self):
        save = Mock()
        overlay = DialogRegionOverlay(save_region=save)
        self.addCleanup(overlay.deleteLater)
        self.addCleanup(overlay.close)
        overlay.resize(800, 450)
        overlay.show()
        for name in ("Left", "Top", "Width", "Height"):
            overlay.bounds_inputs[name].setValue(10000)
        self.assertEqual(
            QRect(overlay.origin, overlay.current), QRect(780, 430, 20, 20)
        )
        QTest.keyClick(overlay, Qt.Key.Key_Left)
        self.assertEqual(overlay.bounds_inputs["Left"].value(), 778)
        QTest.keyClick(overlay, Qt.Key.Key_Escape)
        self.assertFalse(overlay.bounds_editor.isVisible())
        save.assert_not_called()

    def test_one_selection_reviews_and_saves_the_whole_capture(self):
        save = Mock()
        reviewer = Mock(return_value=_ReviewDecision(QDialog.DialogCode.Accepted))
        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450)),
            reviewer=reviewer,
            save_region=save,
        )
        self.addCleanup(overlay.deleteLater)
        self.addCleanup(overlay.close)
        overlay.resize(800, 450)
        overlay.show()
        overlay._review_rectangle(QRect(80, 270, 640, 144))
        reviewer.assert_called_once()
        self.assertEqual(reviewer.call_args.args[0].size, (640, 144))
        save.assert_called_once_with(DialogRegion(0.1, 0.6, 0.8, 0.32))
        self.assertFalse(overlay.isVisible())

    def test_review_failure_keeps_selection_visible_and_retryable(self):
        for stage in ("construction", "execution"):
            with self.subTest(stage=stage):
                reviewer = Mock(return_value=QDialog())
                if stage == "construction":
                    reviewer.side_effect = RuntimeError("review failed")
                else:
                    reviewer.return_value.open = Mock(
                        side_effect=RuntimeError("review failed")
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
                    return_value=_ReviewDecision(QDialog.DialogCode.Accepted)
                )
                overlay._review_rectangle(QRect(80, 270, 640, 144))
                save.assert_called_once()
                self.assertFalse(overlay.isVisible())

    def test_selection_clipped_below_minimum_reports_error_and_can_be_retried(self):
        for frozen_preview in (False, True):
            with self.subTest(frozen_preview=frozen_preview):
                reviewer = Mock(
                    return_value=_ReviewDecision(QDialog.DialogCode.Accepted)
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
            review.accept()

        review = QDialog()
        review.open = Mock(side_effect=finish_after_close)
        overlay.reviewer = Mock(return_value=review)
        overlay._review_rectangle(QRect(80, 270, 640, 144))

        save.assert_not_called()
        self.assertFalse(overlay.isVisible())

    def test_old_review_completion_cannot_save_or_retire_current_review(self):
        saved = []
        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450)),
            reviewer=lambda _image: QDialog(),
            save_region=saved.append,
        )
        self.addCleanup(overlay.deleteLater)
        self.addCleanup(overlay.close)
        overlay.resize(800, 450)
        overlay._review_rectangle(QRect(80, 270, 640, 144))
        old = overlay._review
        old.done(CalibrationReviewDialog.DrawAgain)
        overlay._review_rectangle(QRect(160, 180, 400, 200))
        current = overlay._review
        old.accept()
        self.assertIs(overlay._review, current)
        self.assertTrue(current.isVisible())
        self.assertFalse(overlay._closed)
        self.assertEqual(saved, [])
        current.accept()
        self.assertEqual(saved, [DialogRegion(0.2, 0.4, 0.5, 200 / 450)])
        self.assertTrue(overlay._closed)

    def test_async_review_returns_immediately_and_keeps_owner_on_save_error(self):
        save = Mock(side_effect=OSError("disk full"))
        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450)),
            reviewer=lambda _image: QDialog(),
            save_region=save,
        )
        self.addCleanup(overlay.deleteLater)
        self.addCleanup(overlay.close)
        overlay.resize(800, 450)
        overlay.origin, overlay.current = QPoint(80, 270), QPoint(719, 413)
        overlay.show()
        overlay._review_bounds()
        review = overlay._review
        self.assertIsNotNone(review)
        self.assertIs(review.parent(), overlay)
        self.assertTrue(review.isVisible())
        self.assertFalse(overlay.isVisible())
        save.assert_not_called()
        review.accept()
        self.assertTrue(overlay.isVisible())
        self.assertIn("disk full", overlay.save_error)
        self.assertIsNone(overlay._review)
        save.side_effect = None
        overlay._review_bounds()
        overlay._review.accept()
        self.assertFalse(overlay.isVisible())
        self.assertEqual(save.call_count, 2)

    def test_closing_overlay_rejects_its_async_review_and_cancels_ocr(self):
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
        overlay._review_rectangle(QRect(80, 270, 640, 144))
        self.assertIs(overlay._review, reviews[0])
        self.assertTrue(reviews[0].isVisible())
        save.assert_not_called()
        overlay.close()

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
        pool = QThreadPool()
        with release_and_wait_for_pool(pool, release):
            dialog = CalibrationReviewDialog(
                Image.new("RGB", (640, 180), "black"),
                recognizer=slow_recognizer,
                thread_pool=pool,
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

                pool = QThreadPool()
                with release_and_wait_for_pool(pool, release):
                    dialog = CalibrationReviewDialog(
                        Image.new("RGB", (640, 180), "black"),
                        recognizer=slow_recognizer,
                        thread_pool=pool,
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

                pool = QThreadPool()
                with release_and_wait_for_pool(pool, release):
                    dialog = CalibrationReviewDialog(
                        Image.new("RGB", (640, 180)),
                        recognizer=slow_recognizer,
                        thread_pool=pool,
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
        self.assertEqual(dialog.save_button.text(), "Save without OCR")
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertTrue(dialog.progress.isHidden())
        dialog.deleteLater()

    def test_review_keeps_unreadable_name_warning_and_requires_explicit_empty_save(
        self,
    ):
        result = OCRResult(
            "Narrator",
            "",
            0.0,
            "gray",
            1,
            speaker_note="Visible name could not be read",
        )
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (1200, 240), "black"),
            recognizer=lambda _image: result,
        )
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        self.wait_for(lambda: not dialog.runner.active)
        self.assertIn(result.speaker_note, dialog.result_text.toPlainText())
        self.assertNotIn("Speaker: Narrator", dialog.result_text.toPlainText())
        self.assertIn("No dialogue recognized", dialog.result_text.toPlainText())
        self.assertEqual(dialog.save_button.text(), "Save without OCR")
        font = dialog.font()
        font.setPointSize(20)
        dialog.setFont(font)
        dialog.resize(620, 430)
        dialog.show()
        self.application.processEvents()
        self.application.processEvents()
        self.assertEqual(dialog.size().toTuple(), (620, 430))
        self.assertLessEqual(dialog.preview.pixmap().width(), dialog.preview.width())
        self.assertLessEqual(dialog.preview.pixmap().height(), dialog.preview.height())
        for button in (dialog.save_button, dialog.retry_button, dialog.cancel_button):
            rect = button.rect().translated(
                button.mapTo(dialog, button.rect().topLeft())
            )
            self.assertTrue(dialog.rect().contains(rect))
        dialog.save_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_full_size_capture_preserves_pixels_and_closes_with_review(self):
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (1200, 900), "black"),
            recognizer=lambda _image: OCRResult("Selone", "A line.", 94.5, "gray", 1),
        )
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        dialog.show()
        dialog.enlarge_button.click()
        self.application.processEvents()
        enlarged = dialog.capture_preview
        self.assertTrue(enlarged.isVisible())
        scroll = enlarged.findChild(QScrollArea)
        self.assertEqual(scroll.widget().pixmap().size().toTuple(), (1200, 900))
        self.assertGreater(scroll.horizontalScrollBar().maximum(), 0)
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        QTest.keyClick(enlarged, Qt.Key.Key_Escape)
        self.assertFalse(enlarged.isVisible())
        self.assertTrue(dialog.isVisible())
        dialog.enlarge_button.click()
        self.assertIs(dialog.capture_preview, enlarged)
        dialog.reject()
        self.assertFalse(enlarged.isVisible())
        self.assertFalse(dialog.runner.active)

    def test_long_review_can_scroll_to_last_line_with_actions_visible(self):
        text = "\n".join(f"Dialogue line {index}." for index in range(50))
        result = OCRResult("Selone", text + "\nFinal dialogue line.", 94.5, "gray", 1)
        dialog = CalibrationReviewDialog(
            Image.new("RGB", (1200, 720), "black"), recognizer=lambda _image: result
        )
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        font = dialog.font()
        font.setPointSize(20)
        dialog.setFont(font)
        dialog.resize(620, 430)
        dialog.show()
        self.wait_for(lambda: not dialog.runner.active)
        dialog.result_text.setFocus()
        self.wait_for(lambda: dialog.result_text.verticalScrollBar().maximum() > 0)
        QTest.keyClick(dialog.result_text, Qt.Key.Key_End)
        self.application.processEvents()
        scroll = dialog.result_text.verticalScrollBar()
        self.assertGreater(scroll.maximum(), 0)
        self.assertEqual(scroll.value(), scroll.maximum())
        cursor = dialog.result_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.assertTrue(
            dialog.result_text.viewport()
            .rect()
            .intersects(dialog.result_text.cursorRect(cursor))
        )
        for button in (dialog.save_button, dialog.retry_button, dialog.cancel_button):
            self.assertTrue(
                dialog.rect().contains(
                    button.rect().translated(button.mapTo(dialog, QPoint()))
                )
            )

    def test_keyboard_suggestion_uses_current_capture_and_reviews_once(self):
        region = DialogRegion(0.1, 0.5, 0.8, 0.4)
        save = Mock()
        overlay = DialogRegionOverlay(
            background=Image.new("RGB", (800, 450)),
            save_region=save,
            capture_region=region,
            reviewer=Mock(
                return_value=_ReviewDecision(CalibrationReviewDialog.DrawAgain)
            ),
        )
        self.addCleanup(overlay.deleteLater)
        self.addCleanup(overlay.close)
        overlay.resize(800, 450)
        overlay.show()
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        self.assertEqual(
            QRect(overlay.origin, overlay.current), QRect(80, 225, 640, 180)
        )
        self.assertIn("Enter reviews", overlay._instruction_text())
        overlay.save_error = "disk full"
        QTest.keyClick(overlay, Qt.Key.Key_Return)
        overlay.reviewer.assert_called_once()
        save.assert_not_called()
        self.assertIsNone(overlay.save_error)
        self.assertIsNone(overlay.origin)
        self.assertTrue(overlay.isVisible())

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

        class Reviewer(_ReviewDecision):
            def __init__(self, image):
                super().__init__(next(decisions))
                crop_sizes.append(image.size)

        with TemporaryDirectory() as temporary_directory:
            selected = []
            overlay = DialogRegionOverlay(
                Path(temporary_directory) / "region.json",
                background=Image.new("RGB", (1600, 900), "black"),
                reviewer=Reviewer,
                capture_region=DialogRegion(0.08, 0.62, 0.84, 0.28),
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
        class Reviewer(_ReviewDecision):
            def __init__(self, _image):
                super().__init__(QDialog.DialogCode.Rejected)

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
        class Reviewer(_ReviewDecision):
            def __init__(self, _image):
                super().__init__(QDialog.DialogCode.Accepted)

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
