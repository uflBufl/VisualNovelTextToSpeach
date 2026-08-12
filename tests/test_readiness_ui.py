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

        dialog = ReadinessDialog(AppSettings(), diagnostics)

        self.assertIn("1 error", dialog.summary.text())
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
