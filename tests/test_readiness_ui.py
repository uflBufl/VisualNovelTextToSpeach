import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from vntts.onboarding import DiagnosticResult  # noqa: E402
from vntts.readiness_ui import ReadinessDialog  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class ReadinessDialogTest(unittest.TestCase):
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
        pool.run_next()
        self.application.processEvents()

        self.assertIn("1 error", dialog.summary.text())
        self.assertEqual(dialog.table.rowCount(), 2)
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

    def test_selects_first_actionable_error_and_emits_only_its_remediation(self):
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

        self.assertFalse(dialog.remediation_button.isEnabled())
        self.assertIn("Wait", dialog.remediation_reason.text())
        pool.run_next()
        self.application.processEvents()

        self.assertEqual(dialog.table.currentRow(), 2)
        self.assertEqual(dialog.remediation_button.text(), "Open Settings")
        self.assertTrue(dialog.remediation_button.isEnabled())
        dialog.remediation_button.click()
        self.assertEqual(settings_requests, [True])
        self.assertEqual(voice_requests, [])

        dialog.table.setFocus()
        QTest.keyClick(dialog.table, Qt.Key.Key_Up)
        self.assertEqual(dialog.table.currentRow(), 1)
        self.assertEqual(dialog.remediation_button.text(), "Open Voice mappings")
        dialog.remediation_button.setFocus()
        QTest.keyClick(dialog.remediation_button, Qt.Key.Key_Return)
        self.assertEqual(voice_requests, [True])

        dialog.table.selectRow(0)
        self.assertFalse(dialog.remediation_button.isEnabled())
        self.assertIn("No in-app fix", dialog.remediation_reason.text())
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
        self.assertTrue(dialog.remediation_reason.isVisibleTo(dialog))
        self.assertLessEqual(
            dialog.remediation_button.geometry().right(),
            dialog.contentsRect().right(),
        )
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


class ManualThreadPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def run_next(self):
        self.tasks.pop(0).run()


if __name__ == "__main__":
    unittest.main()
