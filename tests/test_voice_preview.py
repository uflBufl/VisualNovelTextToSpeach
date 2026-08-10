import os
import unittest
from concurrent.futures import Future
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.voice_preview_ui import VoicePreviewDialog  # noqa: E402


class VoicePreviewDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_plays_selected_voice_and_reports_completion(self):
        future = Future()
        preview_handler = Mock(return_value=future)
        dialog = VoicePreviewDialog(["Narrator", "Marcus"], preview_handler)
        dialog.character.setCurrentText("Marcus")
        dialog.text.setPlainText("Hello, Timekeeper.")

        dialog.preview()
        future.set_result(("Marcus", "Hello, Timekeeper."))
        self.application.processEvents()

        preview_handler.assert_called_once_with("Marcus", "Hello, Timekeeper.")
        self.assertTrue(dialog.preview_button.isEnabled())
        self.assertEqual(dialog.status.text(), "Played Marcus preview")
        dialog.deleteLater()

    def test_reports_preview_start_failure(self):
        dialog = VoicePreviewDialog(
            ["Narrator"],
            Mock(side_effect=RuntimeError("engine unavailable")),
        )

        dialog.preview()

        self.assertEqual(dialog.status.text(), "Preview failed: engine unavailable")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
