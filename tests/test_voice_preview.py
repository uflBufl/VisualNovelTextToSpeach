import os
import unittest
from concurrent.futures import Future
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.voice_preview_ui import VoicePreviewDialog  # noqa: E402
from vntts.voices import VoiceChoice  # noqa: E402


class VoicePreviewDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def create_dialog(self, preview_handler=None, assignment_handler=None):
        return VoicePreviewDialog(
            ["Narrator", "Marcus"],
            [
                VoiceChoice("preset:alba", "Alba", "Pocket TTS built-in voice"),
                VoiceChoice("preset:marius", "Marius", "Pocket TTS built-in voice"),
            ],
            preview_handler or Mock(),
            assignment_handler or Mock(),
            Mock(return_value="preset:alba"),
        )

    def test_plays_selected_candidate_and_reports_completion(self):
        future = Future()
        preview_handler = Mock(return_value=future)
        dialog = self.create_dialog(preview_handler=preview_handler)
        dialog.voice.setCurrentIndex(1)
        dialog.text.setPlainText("Hello, Timekeeper.")

        dialog.preview()
        future.set_result(("Marius", "Hello, Timekeeper."))
        self.application.processEvents()

        preview_handler.assert_called_once_with("preset:marius", "Hello, Timekeeper.")
        self.assertTrue(dialog.preview_button.isEnabled())
        self.assertEqual(dialog.status.text(), "Played Marius preview")
        dialog.deleteLater()

    def test_assigns_selected_candidate_to_an_editable_character(self):
        assignment_handler = Mock()
        dialog = self.create_dialog(assignment_handler=assignment_handler)
        dialog.character.setCurrentText("Selone")
        dialog.voice.setCurrentIndex(1)

        dialog.assign()

        assignment_handler.assert_called_once_with("Selone", "preset:marius")
        self.assertEqual(dialog.status.text(), "Saved Marius for Selone")
        dialog.deleteLater()

    def test_reports_preview_start_failure(self):
        dialog = self.create_dialog(
            preview_handler=Mock(side_effect=RuntimeError("engine unavailable"))
        )

        dialog.preview()

        self.assertEqual(dialog.status.text(), "Preview failed: engine unavailable")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
