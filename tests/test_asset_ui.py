import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

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

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for asset operation")

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

    def test_voice_pack_import_is_nonblocking_and_close_safe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "voices.json"
            source.write_text("{}", encoding="utf-8")
            manifest = root / "managed" / "manifest.json"
            started = Event()
            release = Event()
            voice_manager = Mock()

            def import_pack(path):
                self.assertEqual(Path(path), source)
                started.set()
                release.wait(3)
                return manifest

            voice_manager.import_pack.side_effect = import_pack
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            dialog = AssetManagerDialog(
                AppSettings(),
                model_manager=model_manager,
                voice_manager=voice_manager,
            )
            heartbeat = []
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))

            with patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(str(source), "JSON files (*.json)"),
            ):
                before = time.monotonic()
                dialog.import_voice_pack()
                elapsed = time.monotonic() - before
            self.wait_for(lambda: started.is_set() and bool(heartbeat))

            self.assertLess(elapsed, 0.1)
            self.assertTrue(dialog.operation_running)
            self.assertEqual(dialog.operation_kind, "voice-import")
            self.assertFalse(dialog.import_pack_button.isEnabled())
            self.assertFalse(dialog.buttons.isEnabled())
            self.assertIn("background", dialog.voice_status.text())
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            self.assertIn("Close is deferred", dialog.voice_status.text())

            release.set()
            self.wait_for(lambda: not dialog.operation_running)
            self.assertEqual(dialog.voice_manifest.text(), str(manifest))
            self.assertEqual(dialog.voice_progress.value(), 100)

    def test_voice_import_failure_restores_controls_for_retry(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        voice_manager = Mock()
        voice_manager.import_pack.side_effect = OSError("temporary copy failure")
        dialog = AssetManagerDialog(
            AppSettings(),
            model_manager=model_manager,
            voice_manager=voice_manager,
        )

        dialog._start_voice_import(
            voice_manager.import_pack,
            "voices.json",
            message="Voice pack imported",
        )
        self.wait_for(lambda: not dialog.operation_running)

        self.assertIn("Choose the source again to retry", dialog.voice_status.text())
        self.assertTrue(dialog.import_pack_button.isEnabled())
        self.assertTrue(dialog.buttons.isEnabled())


if __name__ == "__main__":
    unittest.main()
