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

    def create_dialog(
        self,
        preview_handler=None,
        assignment_handler=None,
        clear_assignment_handler=None,
        force_live_handler=None,
        current_force_live_handler=None,
        preview_stop_handler=None,
    ):
        return VoicePreviewDialog(
            ["Narrator", "Marcus"],
            [
                VoiceChoice("preset:alba", "Alba", "Pocket TTS built-in voice"),
                VoiceChoice("preset:marius", "Marius", "Pocket TTS built-in voice"),
            ],
            preview_handler or Mock(),
            assignment_handler or Mock(),
            Mock(return_value="preset:alba"),
            clear_assignment_handler,
            force_live_handler=force_live_handler,
            current_force_live_handler=current_force_live_handler,
            preview_stop_handler=preview_stop_handler,
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
        self.assertIn("Narrator using Marius", dialog.preview_identity.text())
        dialog.deleteLater()

    def test_preview_freezes_exact_inputs_and_active_stop_is_call_bound(self):
        future = Future()
        future.set_running_or_notify_cancel()
        stop = Mock()
        dialog = self.create_dialog(
            preview_handler=Mock(return_value=future),
            preview_stop_handler=stop,
        )
        dialog.character.setCurrentText("Marcus")
        dialog.voice.setCurrentIndex(1)
        dialog.text.setPlainText("Exact preview text.")

        dialog.preview()

        self.assertFalse(dialog.character.isEnabled())
        self.assertFalse(dialog.voice.isEnabled())
        self.assertFalse(dialog.text.isEnabled())
        self.assertFalse(dialog.assign_button.isEnabled())
        self.assertTrue(dialog.stop_button.isEnabled())
        self.assertEqual(
            dialog.preview_identity.text(),
            "Marcus using Marius: Exact preview text.",
        )

        dialog.stop_preview()
        stop.assert_called_once_with()
        future.set_exception(RuntimeError("playback interrupted"))
        self.application.processEvents()

        self.assertEqual(dialog.status.text(), "Preview stopped.")
        self.assertTrue(dialog.character.isEnabled())
        self.assertTrue(dialog.assign_button.isEnabled())
        self.assertFalse(dialog.stop_button.isEnabled())
        dialog.deleteLater()

    def test_queued_preview_cancellation_allows_deferred_close(self):
        future = Future()
        dialog = self.create_dialog(preview_handler=Mock(return_value=future))
        dialog.preview()

        dialog.close()
        self.application.processEvents()

        self.assertTrue(future.cancelled())
        self.assertIsNone(dialog._preview_future)
        self.assertEqual(dialog.status.text(), "Preview stopped.")

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

    def test_narrator_controls_separate_fallback_voice_and_force_live(self):
        clear_assignment_handler = Mock()
        force_live_handler = Mock()
        dialog = self.create_dialog(
            clear_assignment_handler=clear_assignment_handler,
            force_live_handler=force_live_handler,
            current_force_live_handler=Mock(return_value=False),
        )

        self.assertIn("live fallback", dialog.routing_note.text())
        self.assertEqual(
            dialog.assign_button.text(),
            "Use selected Narrator fallback voice",
        )
        self.assertEqual(
            dialog.automatic_button.text(),
            "Use default Narrator voice",
        )
        self.assertFalse(dialog.force_live.isChecked())
        dialog.force_live.setChecked(True)
        dialog.assign()
        force_live_handler.assert_called_once_with(True)

        dialog.automatic_button.click()

        clear_assignment_handler.assert_called_once_with("Narrator")
        self.assertEqual(
            dialog.status.text(),
            "Default Narrator voice and generated-first routing restored",
        )
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
