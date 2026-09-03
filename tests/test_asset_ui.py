import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
)
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

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
            AppSettings(speech_backend="coqui-xtts", xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        with patch("vntts.asset_ui.Thread", ImmediateThread):
            dialog.download_model()

        self.assertEqual(dialog.progress.value(), 100)
        self.assertIn("Model ready", dialog.model_status.text())
        self.assertEqual(dialog.settings().tts_model, default_model)

    def test_default_pocket_backend_offers_only_character_voice_assets(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = AssetManagerDialog(
            AppSettings(),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        self.assertEqual(dialog.windowTitle(), "Character voices")
        self.assertEqual(dialog.tabs.count(), 1)
        self.assertEqual(dialog.tabs.tabText(0), "Character voices")
        dialog.download_model()
        model_manager.download.assert_not_called()
        dialog.accept_settings()
        self.assertIsNone(dialog.settings().tts_model)

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

    def test_manifest_browse_validates_inline_and_supports_keyboard(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            voice_manager = Mock()
            voice_manager.validate.return_value = manifest
            dialog = AssetManagerDialog(
                AppSettings(),
                model_manager=model_manager,
                voice_manager=voice_manager,
            )

            with patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(str(manifest), "JSON files (*.json)"),
            ):
                dialog.browse_manifest_button.click()
            self.wait_for(lambda: not dialog.manifest_runner.active)

            self.assertEqual(dialog.voice_manifest.text(), str(manifest))
            voice_manager.validate.assert_called_once_with(manifest.resolve())
            self.assertIn("passed checksum validation", dialog.voice_status.text())
            self.assertTrue(dialog.voice_manifest.accessibleName())
            self.assertTrue(dialog.browse_manifest_button.accessibleDescription())
            self.assertTrue(dialog.validate_manifest_button.accessibleDescription())
            self.assertIs(
                dialog.browse_manifest_button.nextInFocusChain(),
                dialog.validate_manifest_button,
            )

            voice_manager.validate.reset_mock()
            dialog.validate_manifest_button.setFocus()
            QTest.keyClick(dialog.validate_manifest_button, Qt.Key.Key_Return)
            self.wait_for(lambda: not dialog.manifest_runner.active)
            voice_manager.validate.assert_called_once_with(manifest.resolve())

    def test_invalid_manifest_stays_inline_and_focuses_field_on_save(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "broken.json"
            manifest.write_text("{}", encoding="utf-8")
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            voice_manager = Mock()
            voice_manager.validate.side_effect = ValueError("checksum mismatch")
            dialog = AssetManagerDialog(
                AppSettings(voice_manifest=str(manifest)),
                model_manager=model_manager,
                voice_manager=voice_manager,
            )

            with patch.object(QMessageBox, "warning") as warning:
                dialog.accept_settings()
                self.wait_for(lambda: not dialog.manifest_runner.active)

            warning.assert_not_called()
            self.assertIn("checksum mismatch", dialog.voice_status.text())
            self.assertEqual(dialog.voice_manifest.selectedText(), str(manifest))
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_manifest_controls_follow_operation_state_and_empty_is_valid(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        voice_manager = Mock()
        dialog = AssetManagerDialog(
            AppSettings(),
            model_manager=model_manager,
            voice_manager=voice_manager,
        )

        self.assertFalse(dialog.validate_manifest_button.isEnabled())
        self.assertTrue(dialog.validate_voice_manifest())
        voice_manager.validate.assert_not_called()
        dialog.voice_manifest.setText("voices.json")
        dialog.set_operation_running(True, "voice-import")
        self.assertFalse(dialog.voice_manifest.isEnabled())
        self.assertFalse(dialog.browse_manifest_button.isEnabled())
        self.assertFalse(dialog.validate_manifest_button.isEnabled())
        dialog.set_operation_running(False)
        self.assertTrue(dialog.voice_manifest.isEnabled())
        self.assertTrue(dialog.browse_manifest_button.isEnabled())
        self.assertTrue(dialog.validate_manifest_button.isEnabled())

        dialog.resize(680, 440)
        dialog.layout().activate()
        self.assertEqual(dialog.size().width(), 680)
        self.assertEqual(dialog.size().height(), 440)

    def test_manifest_validation_is_nonblocking_and_discards_stale_path_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")
            started = Event()
            release = Event()
            voice_manager = Mock()

            def validate(path):
                path = Path(path)
                if path == first.resolve():
                    started.set()
                    release.wait(3)
                return path

            voice_manager.validate.side_effect = validate
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            dialog = AssetManagerDialog(
                AppSettings(),
                model_manager=model_manager,
                voice_manager=voice_manager,
            )
            heartbeat = []
            dialog.voice_manifest.setText(str(first))
            QTimer.singleShot(0, lambda: heartbeat.append("painted"))

            dialog.validate_voice_manifest()
            self.wait_for(lambda: started.is_set() and bool(heartbeat))
            self.assertTrue(dialog.voice_manifest.isEnabled())
            self.assertTrue(
                dialog.buttons.button(
                    QDialogButtonBox.StandardButton.Cancel
                ).isEnabled()
            )
            self.assertFalse(
                dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
            )

            dialog.voice_manifest.setText(str(second))
            dialog.validate_voice_manifest()
            release.set()
            self.wait_for(lambda: not dialog.manifest_runner.active)

            self.assertEqual(
                dialog._validated_manifest_identity,
                (str(second.resolve()), sha256_file(second)),
            )
            self.assertIn(second.name, dialog.voice_status.text())
            self.assertNotIn(first.name, dialog.voice_status.text())

    def test_save_waits_for_exact_manifest_validation_then_accepts(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            started = Event()
            release = Event()
            voice_manager = Mock()

            def validate(path):
                started.set()
                release.wait(3)
                return Path(path)

            voice_manager.validate.side_effect = validate
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            dialog = AssetManagerDialog(
                AppSettings(voice_manifest=str(manifest)),
                model_manager=model_manager,
                voice_manager=voice_manager,
            )

            dialog.accept_settings()
            self.wait_for(started.is_set)
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
            release.set()
            self.wait_for(lambda: dialog.result() == QDialog.DialogCode.Accepted)

            self.assertEqual(dialog.settings().voice_manifest, str(manifest))


if __name__ == "__main__":
    unittest.main()
