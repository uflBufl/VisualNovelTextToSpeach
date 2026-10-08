import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel  # noqa: E402

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.onboarding import DiagnosticResult  # noqa: E402
from vntts.readiness_ui import ReadinessDialog  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class ReadinessDialogTest(unittest.TestCase):
    def test_copy_selected_check_keeps_full_error_and_path(self):
        pool = ManualThreadPool()
        message = "Missing voice reference: C:/voices/" + "a" * 100 + "/centurion.wav"
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult("Narrator", "error", message),
                )
            },
        )()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
        palette = dialog.table.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor("#222222"))
        dialog.table.setPalette(palette)
        pool.run_next()
        self.application.processEvents()
        dialog.show()
        dialog.table.setFocus()
        self.application.processEvents()
        dialog.copy_selected.click()
        expected = f"ERROR | Narrator\n{message}"
        self.assertEqual(self.application.clipboard().text(), expected)
        self.application.clipboard().clear()
        QTest.keyClick(dialog.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.application.clipboard().text(), expected)
        dialog.copy_all.click()
        self.assertIn(expected, self.application.clipboard().text())
        self.assertEqual(dialog.selected_details.toPlainText(), expected)
        self.assertNotIn("a" * 100, dialog.table.item(0, 2).text())
        self.assertGreater(
            dialog.table.item(0, 0).foreground().color().lightness(), 128
        )
        dialog.close()
        dialog.deleteLater()

    def test_long_details_can_scroll_to_end_at_compact_size(self):
        pool = ManualThreadPool()
        message = "Missing OCR path: " + "/Example Folder" * 50 + "/tesseract"
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult("OCR", "error", message),
                )
            },
        )()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
        pool.run_next()
        self.application.processEvents()
        dialog.resize(620, 440)
        font = dialog.font()
        font.setPointSize(font.pointSize() + 4)
        dialog.setFont(font)
        dialog.show()
        self.application.processEvents()
        dialog.selected_details.moveCursor(QTextCursor.MoveOperation.End)
        self.application.processEvents()
        scrollbar = dialog.selected_details.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertGreaterEqual(scrollbar.value(), scrollbar.maximum() - 1)
        self.assertTrue(dialog.selected_details.toPlainText().endswith("/tesseract"))
        dialog.copy_selected.click()
        self.assertIn(message, self.application.clipboard().text())
        dialog.close()
        dialog.deleteLater()

    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_intro_describes_live_reading_not_starting_the_game(self):
        dialog = ReadinessDialog(
            AppSettings(),
            type("Diagnostics", (), {"run": lambda _self, _settings: ()})(),
            thread_pool=ManualThreadPool(),
        )
        labels = [label.text() for label in dialog.findChildren(QLabel)]

        self.assertEqual(dialog.windowTitle(), "Check readiness")
        self.assertTrue(any("before starting live reading" in text for text in labels))
        self.assertFalse(any("before starting the game" in text for text in labels))
        dialog.close()
        dialog.deleteLater()

    def test_summarizes_errors_and_warnings(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult("Capture", "error", "Select a window"),
                    DiagnosticResult("Voices", "warning", "Narrator fallback"),
                )
            },
        )()

        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)

        self.assertEqual(dialog.summary.text(), "Running readiness checks...")
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertTrue(dialog.copy_selected.isHidden())
        self.assertTrue(dialog.copy_all.isHidden())
        self.assertTrue(dialog.refresh_button.isHidden())
        pool.run_next()
        self.application.processEvents()

        self.assertIn("1 error", dialog.summary.text())
        self.assertFalse(dialog.copy_all.isHidden())
        self.assertFalse(dialog.refresh_button.isHidden())
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertEqual(
            dialog.table.horizontalHeader().sectionResizeMode(1),
            QHeaderView.ResizeMode.ResizeToContents,
        )
        dialog.deleteLater()

    def test_changed_settings_discard_stale_probe_results(self):
        pool = ManualThreadPool()

        class Diagnostics:
            def run(self, settings):
                return (
                    DiagnosticResult(
                        "Backend", "ok", f"Using {settings.speech_backend}"
                    ),
                )

        dialog = ReadinessDialog(
            AppSettings(speech_backend="pocket-tts"),
            Diagnostics(),
            thread_pool=pool,
        )
        dialog.update_settings(AppSettings(speech_backend="moss-tts"))

        pool.run_next()
        self.application.processEvents()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertEqual(dialog.summary.text(), "Running readiness checks...")

        pool.run_next()
        self.application.processEvents()
        self.assertEqual(dialog.table.item(0, 2).text(), "Using moss-tts")
        dialog.deleteLater()

    def test_cancelled_probe_cannot_restore_stale_readiness(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult("Capture", "ok", "Ready"),
                )
            },
        )()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)

        dialog.cancel_checks()
        pool.run_next()
        self.application.processEvents()

        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertIn("No readiness result", dialog.summary.text())
        self.assertFalse(dialog.remediation_button.isEnabled())
        dialog.deleteLater()

    def test_closed_probe_cannot_publish_late_results(self):
        for action in ("close", "reject", "accept", "escape"):
            with self.subTest(action=action):
                pool = ManualThreadPool()
                diagnostics = Mock()
                diagnostics.run.return_value = (DiagnosticResult("OCR", "ok", "Ready"),)
                dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
                dialog.show()
                if action == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    getattr(dialog, action)()
                self.assertFalse(dialog.runner.active)
                pool.run_next()
                self.application.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertEqual(dialog.table.rowCount(), 0)
                self.assertFalse(dialog.reading_button.isVisible())
                dialog.deleteLater()

    def test_malformed_probe_result_is_a_visible_retryable_failure(self):
        for result in (None, [], (object(),)):
            with self.subTest(result=result):
                pool = ManualThreadPool()
                diagnostics = Mock()
                diagnostics.run.return_value = result
                dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
                pool.run_next()
                self.application.processEvents()
                self.assertIn("Checks failed", dialog.summary.text())
                self.assertIn("malformed", dialog.summary.text())
                self.assertEqual(dialog.table.rowCount(), 0)
                self.assertTrue(dialog.refresh_button.isEnabled())
                self.assertFalse(dialog.remediation_button.isEnabled())
                self.assertTrue(dialog.reading_button.isHidden())
                diagnostics.run.return_value = (DiagnosticResult("OCR", "ok", "Ready"),)
                dialog.refresh_button.click()
                self.assertFalse(dialog.refresh_button.isEnabled())
                self.assertFalse(dialog.progress.isHidden())
                pool.run_next()
                self.application.processEvents()
                self.assertEqual(dialog.table.rowCount(), 1)
                self.assertFalse(dialog.reading_button.isHidden())
                self.assertTrue(dialog.progress.isHidden())
                dialog.close()
                dialog.deleteLater()

    def test_selects_first_blocking_error_and_emits_only_its_remediation(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult("Tesseract OCR", "error", "Install it"),
                    DiagnosticResult(
                        "Character voices",
                        "warning",
                        "Missing reference",
                        "voices",
                    ),
                    DiagnosticResult(
                        "Capture source",
                        "error",
                        "Select a window",
                        "settings",
                    ),
                )
            },
        )()
        settings_requests = []
        voice_requests = []
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
        dialog.settings_requested.connect(lambda: settings_requests.append(True))
        dialog.voices_requested.connect(lambda: voice_requests.append(True))
        dialog.show()

        self.assertFalse(dialog.remediation_button.isEnabled())
        self.assertIn("Wait", dialog.remediation_reason.text())
        pool.run_next()
        self.application.processEvents()

        self.assertEqual(dialog.table.currentRow(), 0)
        self.assertIn("Start with Tesseract OCR", dialog.summary.text())
        self.assertFalse(dialog.remediation_button.isEnabled())
        dialog.table.selectRow(2)
        self.assertEqual(dialog.remediation_button.text(), "Open Settings")
        self.assertTrue(dialog.remediation_button.isEnabled())
        dialog.remediation_button.click()
        self.assertEqual(settings_requests, [True])
        self.assertEqual(voice_requests, [])
        self.assertFalse(dialog.isVisible())

        dialog.show()
        dialog.table.setFocus()
        QTest.keyClick(dialog.table, Qt.Key.Key_Up)
        self.assertEqual(dialog.table.currentRow(), 1)
        self.assertEqual(dialog.remediation_button.text(), "Open Voices")
        dialog.remediation_button.setFocus()
        QTest.keyClick(dialog.remediation_button, Qt.Key.Key_Return)
        self.assertEqual(voice_requests, [True])
        self.assertFalse(dialog.isVisible())
        self.assertIn("Fix the errors above first", dialog.remediation_reason.text())

        dialog.table.selectRow(0)
        self.assertFalse(dialog.remediation_button.isEnabled())
        self.assertIn("No automatic fix", dialog.remediation_reason.text())
        dialog.refresh()
        dialog.remediation_button.click()
        self.assertEqual(settings_requests, [True])
        self.assertEqual(voice_requests, [True])
        self.assertTrue(dialog.remediation_button.isHidden())
        dialog.deleteLater()

    def test_warning_before_error_still_selects_first_error(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult(
                        "Character voices", "warning", "Fallback", "voices"
                    ),
                    DiagnosticResult(
                        "Audio output", "error", "No device", "external-audio"
                    ),
                )
            },
        )()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
        pool.run_next()
        self.application.processEvents()
        self.assertEqual(dialog.table.currentRow(), 1)
        self.assertIn("Start with Audio output", dialog.summary.text())
        dialog.deleteLater()

    def test_external_ocr_and_audio_errors_name_next_action_without_false_button(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult(
                        "Tesseract OCR", "error", "not found", "external-ocr"
                    ),
                    DiagnosticResult(
                        "Audio output", "error", "no device", "external-audio"
                    ),
                )
            },
        )()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
        pool.run_next()
        self.application.processEvents()

        self.assertEqual(dialog.table.currentRow(), 0)
        self.assertIn(
            "system's app search path (PATH)", dialog.remediation_reason.text()
        )
        self.assertEqual(dialog.remediation_button.text(), "Tesseract install guide")
        with patch(
            "vntts.readiness_ui.QDesktopServices.openUrl", return_value=True
        ) as open_url:
            dialog.remediation_button.click()
        self.assertEqual(
            open_url.call_args.args[0].toString(),
            "https://tesseract-ocr.github.io/tessdoc/Installation.html",
        )
        with patch("vntts.readiness_ui.QDesktopServices.openUrl", return_value=False):
            dialog.remediation_button.click()
        self.assertIn("Could not open", dialog.remediation_reason.text())
        dialog.table.selectRow(1)
        self.assertIn("system sound settings", dialog.remediation_reason.text())
        self.assertTrue(dialog.remediation_button.isHidden())
        dialog.show()
        self.application.processEvents()
        self.assertTrue(dialog.selected_details.isVisibleTo(dialog))
        dialog.deleteLater()

    def test_ocr_recovery_uses_configured_language(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult(
                        "Tesseract OCR", "error", "not found", "external-ocr"
                    ),
                )
            },
        )()
        dialog = ReadinessDialog(
            AppSettings(ocr_language="jpn"), diagnostics, thread_pool=pool
        )
        pool.run_next()
        self.application.processEvents()
        self.assertIn("Japanese (jpn)", dialog.remediation_reason.text())
        self.assertNotIn("English", dialog.remediation_reason.text())
        dialog.update_settings(AppSettings(ocr_language="eng+jpn"))
        pool.run_next()
        self.application.processEvents()
        self.assertIn("English (eng), Japanese (jpn)", dialog.remediation_reason.text())
        dialog.deleteLater()

    def test_ready_row_explains_that_no_action_is_needed(self):
        pool = ManualThreadPool()
        diagnostics = type(
            "Diagnostics",
            (),
            {
                "run": lambda _self, _settings: (
                    DiagnosticResult("Audio output", "ok", "Speakers"),
                )
            },
        )()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)

        pool.run_next()
        self.application.processEvents()

        self.assertEqual(dialog.table.currentRow(), 0)
        self.assertFalse(dialog.remediation_button.isEnabled())
        self.assertIn("no remediation is needed", dialog.remediation_reason.text())
        self.assertTrue(dialog.table.accessibleName())
        self.assertTrue(dialog.remediation_reason.accessibleName())
        self.assertTrue(dialog.remediation_button.accessibleName())
        dialog.resize(520, 360)
        dialog.show()
        self.application.processEvents()
        self.assertTrue(dialog.table.isVisibleTo(dialog))
        self.assertFalse(dialog.remediation_reason.isVisibleTo(dialog))
        self.assertFalse(dialog.selected_details.isVisibleTo(dialog))
        self.assertTrue(dialog.reading_button.isVisibleTo(dialog))
        self.assertLessEqual(
            dialog.reading_button.mapTo(
                dialog, dialog.reading_button.rect().topRight()
            ).x(),
            dialog.contentsRect().right(),
        )
        dialog.deleteLater()

    def test_success_opens_reading_but_errors_hide_that_route(self):
        pool = ManualThreadPool()

        class Diagnostics:
            results = (DiagnosticResult("Audio output", "ok", "Speakers"),)

            def run(self, _settings):
                return self.results

        diagnostics = Diagnostics()
        dialog = ReadinessDialog(AppSettings(), diagnostics, thread_pool=pool)
        requested = []
        dialog.reading_requested.connect(lambda: requested.append(True))
        pool.run_next()
        self.application.processEvents()
        dialog.show()
        self.assertTrue(dialog.reading_button.isVisibleTo(dialog))
        dialog.reading_button.click()
        self.assertEqual(requested, [True])
        self.assertFalse(dialog.isVisible())

        diagnostics.results = (
            DiagnosticResult(
                "Character voices", "warning", "Narrator fallback", "voices"
            ),
        )
        dialog.refresh()
        pool.run_next()
        self.application.processEvents()
        dialog.show()
        self.assertTrue(dialog.reading_button.isVisibleTo(dialog))
        self.assertIn("fallback", dialog.summary.text())

        diagnostics.results = (DiagnosticResult("Tesseract OCR", "error", "Missing"),)
        dialog.refresh()
        pool.run_next()
        self.application.processEvents()
        dialog.show()
        self.assertFalse(dialog.reading_button.isVisibleTo(dialog))
        dialog.close()
        dialog.deleteLater()

    def test_failed_probe_has_recovery_without_stale_action(self):
        pool = ManualThreadPool()

        class Diagnostics:
            def run(self, _settings):
                raise RuntimeError("device probe failed")

        dialog = ReadinessDialog(AppSettings(), Diagnostics(), thread_pool=pool)
        pool.run_next()
        self.application.processEvents()

        self.assertIn("device probe failed", dialog.summary.text())
        self.assertTrue(dialog.refresh_button.isEnabled())
        self.assertFalse(dialog.remediation_button.isEnabled())
        self.assertIn("Select a warning or error", dialog.remediation_reason.text())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
