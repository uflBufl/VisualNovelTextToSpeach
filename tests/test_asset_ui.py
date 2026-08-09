import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.asset_ui import AssetManagerDialog, default_model  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class ImmediateThread:
    def __init__(self, *, target, args=(), daemon=True):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class AssetManagerDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_download_updates_progress_and_selected_model(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")

        def download(model_name, *, progress, cancel_event):
            progress(50, "Downloading model.pth")
            progress(100, "Checksums passed")
            return Path("managed/model")

        model_manager.download.side_effect = download
        dialog = AssetManagerDialog(
            AppSettings(xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        with patch("vntts.asset_ui.Thread", ImmediateThread):
            dialog.download_model()

        self.assertEqual(dialog.progress.value(), 100)
        self.assertIn("Model ready", dialog.model_status.text())
        self.assertEqual(dialog.settings().tts_model, default_model)

    def test_cancel_button_sets_download_event(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = AssetManagerDialog(
            AppSettings(xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        dialog.cancel_download()

        self.assertTrue(dialog.cancel_event.is_set())


if __name__ == "__main__":
    unittest.main()
