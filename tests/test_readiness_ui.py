import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.onboarding import DiagnosticResult  # noqa: E402
from vntts.readiness_ui import ReadinessDialog  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class ReadinessDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

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
