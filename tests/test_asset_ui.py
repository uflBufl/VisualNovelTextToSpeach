import os
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThreadPool, QTimer  # noqa: E402
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

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.asset_ui import (  # noqa: E402
    AssetManagerDialog,
    VoiceImportDialog,
    default_model,
)
from vntts.assets import ModelDownloadCancelled, ModelIntegrityError  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402


class VoiceImportDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_audio_selection_can_be_replaced_before_adding_voice(self):
        dialog = VoiceImportDialog()
        self.assertFalse(dialog.add_button.isEnabled())
        dialog.character.setText("Believer IV")
        dialog.aliases.setText("Believer, Faithful")
        self.assertFalse(dialog.add_button.isEnabled())

        with patch.object(
            QFileDialog,
            "getOpenFileNames",
            side_effect=[
                (["/voices/first.wav", "/voices/second.wav"], ""),
                (["/voices/replacement.wav"], ""),
            ],
        ):
            dialog.choose_references()
            self.assertEqual(
                dialog.reference_files.toPlainText(), "first.wav\nsecond.wav"
            )
            self.assertTrue(dialog.add_button.isEnabled())
            dialog.choose_references()

        self.assertEqual(dialog.references, ["/voices/replacement.wav"])
        self.assertEqual(dialog.reference_files.toPlainText(), "replacement.wav")
        dialog.reference_files.setPlainText(
            "\n".join(f"very-long-reference-{index:02d}.wav" for index in range(20))
        )
        dialog.show()
        self.application.processEvents()
        self.assertGreater(dialog.reference_files.verticalScrollBar().maximum(), 0)
        self.assertEqual(
            dialog.values(),
            ("Believer IV", ["/voices/replacement.wav"], ["Believer", "Faithful"]),
        )
        dialog.validate_and_accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)


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

        dialog.download_model()
        self.wait_for(lambda: not dialog.model_runner.active)

        self.assertEqual(dialog.progress.value(), 100)
        self.assertTrue(dialog.progress.isHidden())
        self.assertTrue(dialog.cancel_button.isHidden())
        self.assertIn("Model ready", dialog.model_status.text())
        self.assertEqual(dialog.settings().tts_model, default_model)

    def test_editable_model_path_handles_invalid_names_and_recovers(self):
        model_manager = Mock()

        def model_path(name):
            if name == "..":
                raise ModelIntegrityError("Invalid model directory")
            return Path("managed") / name

        model_manager.model_path.side_effect = model_path
        dialog = AssetManagerDialog(
            AppSettings(speech_backend="coqui-xtts", tts_model=".."),
            model_manager=model_manager,
            voice_manager=Mock(),
        )
        self.assertIn("Invalid model directory", dialog.model_path.text())
        dialog.model.setCurrentText("")
        self.assertIn("Choose a model", dialog.model_path.text())
        dialog.model.setCurrentText("valid")
        self.assertEqual(dialog.model_path.text(), str(Path("managed") / "valid"))
        self.assertFalse(dialog.operation_running)
        dialog.reject()

    def test_empty_model_selection_does_not_start_verification(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = AssetManagerDialog(
            AppSettings(speech_backend="coqui-xtts"),
            model_manager=model_manager,
            voice_manager=Mock(),
        )
        dialog.model.setCurrentText(" ")

        with patch.object(QMessageBox, "warning") as warning:
            dialog.verify_model()

        warning.assert_called_once_with(dialog, "No model", "Choose a model to verify.")
        model_manager.validate.assert_not_called()
        self.assertFalse(dialog.operation_running)
        self.assertFalse(dialog.model_runner.active)

    def test_model_verification_discards_stale_completion(self):

        pool = ManualThreadPool()
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = AssetManagerDialog(
            AppSettings(speech_backend="coqui-xtts"),
            model_manager=model_manager,
            voice_manager=Mock(),
            thread_pool=pool,
        )
        dialog.set_operation_running(True, "verify")
        dialog.model_runner.start(lambda: Path("stale"))
        dialog.model_runner.start(lambda: Path("latest"))

        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertTrue(dialog.operation_running)
        self.assertNotIn("stale", dialog.model_status.text())

        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertFalse(dialog.operation_running)
        self.assertIn("latest", dialog.model_status.text())

    def test_model_selection_invalidates_verification_and_cannot_inherit_old_result(
        self,
    ):
        model_manager = Mock()
        model_manager.model_path.side_effect = lambda name: Path("managed") / name
        dialog = AssetManagerDialog(
            AppSettings(speech_backend="coqui-xtts"),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        dialog.set_operation_running(True, "verify")
        self.assertFalse(dialog.model.isEnabled())
        dialog.model_finished(True, "Model verified and ready at managed/first")
        dialog.model.setCurrentText("second")
        self.assertIn("not checked", dialog.model_status.text())
        self.assertEqual(dialog.model_path.text(), str(Path("managed") / "second"))

        dialog.set_operation_running(True, "verify")
        dialog.model.setCurrentText("third")
        dialog.model_finished(True, "Model verified and ready at managed/second")
        self.assertIn("Selected model changed", dialog.model_status.text())
        self.assertIn("Model not ready", dialog.model_status.text())
        self.assertNotEqual(dialog.settings().tts_model, "third")

    def test_model_verification_can_finish_after_close_without_updating_ui(self):
        started = Event()
        release = Event()
        pool = QThreadPool()
        self.addCleanup(pool.waitForDone, 3000)
        self.addCleanup(release.set)
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")

        def validate(_model_name):
            started.set()
            release.wait(3)
            return Path("managed/model")

        model_manager.validate.side_effect = validate
        dialog = AssetManagerDialog(
            AppSettings(speech_backend="coqui-xtts"),
            model_manager=model_manager,
            voice_manager=Mock(),
            thread_pool=pool,
        )
        dialog.verify_model()
        self.wait_for(started.is_set)
        close_event = QCloseEvent()
        dialog.closeEvent(close_event)

        self.assertTrue(close_event.isAccepted())
        self.assertFalse(dialog.model_runner.active)
        release.set()
        self.assertTrue(pool.waitForDone(3000))
        self.application.processEvents()
        self.assertEqual(dialog.model_status.text(), "Verifying model checksums...")

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
        self.assertTrue(dialog.tabs.tabBar().isHidden())
        self.assertTrue(dialog.voice_progress.isHidden())
        self.assertIn("No active voice manifest", dialog.voice_status.text())
        self.assertEqual(
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).text(),
            "Save selection",
        )
        dialog.download_model()
        model_manager.download.assert_not_called()
        dialog.accept_settings()
        self.assertIsNone(dialog.settings().tts_model)

    def test_cancelled_add_voice_does_not_start_import(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        voice_manager = Mock()
        dialog = AssetManagerDialog(
            AppSettings(), model_manager=model_manager, voice_manager=voice_manager
        )
        with patch.object(
            VoiceImportDialog,
            "exec",
            return_value=QDialog.DialogCode.Rejected,
        ):
            dialog.add_character_voice()

        voice_manager.import_voice.assert_not_called()
        self.assertFalse(dialog.operation_running)

    def test_failed_voice_import_reopens_with_editable_selection(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        voice_manager = Mock()
        voice_manager.import_voice.side_effect = OSError("bad audio file")
        dialog = AssetManagerDialog(
            AppSettings(), model_manager=model_manager, voice_manager=voice_manager
        )

        initial_dialog = VoiceImportDialog()
        initial_dialog.character.setText("Believer IV")
        initial_dialog.aliases.setText("Believer")
        initial_dialog.set_references(["/voices/bad.wav"])
        with (
            patch("vntts.asset_ui.VoiceImportDialog", return_value=initial_dialog),
            patch.object(
                initial_dialog, "exec", return_value=QDialog.DialogCode.Accepted
            ),
        ):
            dialog.add_character_voice()
        self.wait_for(lambda: not dialog.operation_running)
        self.assertIn("revise and retry", dialog.voice_status.text())

        retry_dialog = VoiceImportDialog()
        with (
            patch("vntts.asset_ui.VoiceImportDialog", return_value=retry_dialog),
            patch.object(
                retry_dialog, "exec", return_value=QDialog.DialogCode.Rejected
            ),
        ):
            dialog.add_character_voice()
        self.assertEqual(retry_dialog.character.text(), "Believer IV")
        self.assertEqual(retry_dialog.aliases.text(), "Believer")
        self.assertEqual(retry_dialog.references, ["/voices/bad.wav"])
        self.assertTrue(retry_dialog.add_button.isEnabled())
        retry_dialog.set_references(["/voices/better.wav"])
        voice_manager.import_voice.assert_called_once()

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

    def test_download_cancellation_waits_for_the_cooperative_worker(self):
        started = Event()
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")

        def download(_model_name, *, progress, cancel_event):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.005)
            raise ModelDownloadCancelled("Model download cancelled")

        model_manager.download.side_effect = download
        dialog = AssetManagerDialog(
            AppSettings(speech_backend="coqui-xtts", xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
        )
        dialog.download_model()
        self.wait_for(started.is_set)
        self.assertFalse(dialog.download_button.isEnabled())
        self.assertFalse(dialog.verify_button.isEnabled())
        self.assertTrue(dialog.cancel_button.isEnabled())
        self.assertFalse(
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save).isEnabled()
        )
        dialog.cancel_download()
        self.assertFalse(dialog.cancel_button.isEnabled())
        self.wait_for(lambda: not dialog.operation_running)

        self.assertIn("Model download cancelled", dialog.model_status.text())

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
        dialog._voice_import_draft = ("Believer IV", ["/voices/bad.wav"], [])

        dialog._start_voice_import(
            voice_manager.import_pack,
            "voices.json",
            message="Voice pack imported",
        )
        self.wait_for(lambda: not dialog.operation_running)

        self.assertIn("Choose the source again to retry", dialog.voice_status.text())
        self.assertIsNotNone(dialog._voice_import_draft)
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

    def test_selected_manifest_starts_unchecked_and_controls_scale_together(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = AssetManagerDialog(
            AppSettings(voice_manifest="voices.json"),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        self.assertIn("Files not checked", dialog.voice_status.text())
        font = dialog.font()
        font.setPointSize(font.pointSize() + 4)
        dialog.setFont(font)
        self.assertEqual(
            dialog.validate_manifest_button.font().pointSize(), font.pointSize()
        )
        self.assertEqual(
            dialog.buttons.button(QDialogButtonBox.StandardButton.Save)
            .font()
            .pointSize(),
            dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
            .font()
            .pointSize(),
        )

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
        dialog._set_manifest_validation_pending(True)
        self.assertFalse(dialog.import_pack_button.isEnabled())
        self.assertFalse(dialog.add_voice_button.isEnabled())
        self.assertEqual(dialog.validate_manifest_button.text(), "Validating...")
        self.assertFalse(dialog.voice_progress.isHidden())
        dialog._set_manifest_validation_pending(False)
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
            pool = QThreadPool()
            self.addCleanup(pool.waitForDone, 3000)
            self.addCleanup(release.set)
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
                thread_pool=pool,
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
            self.assertTrue(pool.waitForDone(3000))
            self.application.processEvents()

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

    @contextmanager
    def _manifest_fifo_swap(self, manifest, swap_at, expected_opens):
        native_open, native_path_open = os.open, Path.open
        descriptors = []
        hash_opens = 0

        def swap_and_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal hash_opens
            if Path(path) == manifest:
                hash_opens += 1
                if hash_opens == swap_at:
                    self.assertTrue(flags & os.O_NONBLOCK)
                    manifest.unlink()
                    os.mkfifo(manifest)
            descriptor = native_open(path, flags, mode, dir_fd=dir_fd)
            if Path(path) == manifest:
                descriptors.append(descriptor)
            return descriptor

        def reject_plain_open(path, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(path) == manifest and "b" in mode:
                self.fail("manifest hashing must not use blocking Path.open")
            return native_path_open(path, *args, **kwargs)

        with (
            patch("vntts.path_safety.os.open", side_effect=swap_and_open),
            patch.object(Path, "open", reject_plain_open),
        ):
            yield
        self.assertEqual(hash_opens, expected_opens)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_manifest_hash_phases_reject_fifo_swap_and_recover(self):
        for phase in ("before validation", "after validation", "completion", "save"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                manifest = Path(directory).resolve() / "manifest.json"
                manifest.write_text("{}", encoding="utf-8")
                model_manager = Mock()
                model_manager.model_path.return_value = Path("managed/model")
                voice_manager = Mock()
                voice_manager.validate.return_value = manifest
                pool = ManualThreadPool()
                dialog = AssetManagerDialog(
                    AppSettings(voice_manifest=str(manifest)),
                    model_manager=model_manager,
                    voice_manager=voice_manager,
                    thread_pool=pool,
                )
                result = dialog._validate_manifest_snapshot(str(manifest))
                dialog._validated_manifest_identity = result[:2]
                voice_manager.validate.reset_mock()
                swap_at = 2 if phase == "after validation" else 1
                with self._manifest_fifo_swap(
                    manifest, swap_at, swap_at + (phase == "save")
                ):
                    if phase == "save":
                        dialog.accept_settings()
                        self.assertTrue(dialog.manifest_runner.active)
                        self.assertNotEqual(
                            dialog.result(), QDialog.DialogCode.Accepted
                        )
                        pool.run_next()
                    else:
                        dialog._set_manifest_validation_pending(True)
                        dialog._accept_after_manifest_validation = True
                        if phase == "completion":
                            dialog._manifest_validation_finished(result, None)
                        else:
                            with self.assertRaisesRegex(
                                OSError, "regular file"
                            ) as raised:
                                dialog._validate_manifest_snapshot(str(manifest))
                            self.assertIsInstance(raised.exception.__cause__, OSError)
                            dialog._manifest_validation_finished(None, raised.exception)

                if phase == "after validation":
                    voice_manager.validate.assert_called_once_with(manifest)
                else:
                    voice_manager.validate.assert_not_called()
                expected_status = "unavailable" if phase == "completion" else "invalid"
                self.assertIn(expected_status, dialog.voice_status.text())
                self.assertIsNone(dialog._validated_manifest_identity)
                self.assertFalse(dialog._accept_after_manifest_validation)
                self.assertFalse(dialog.manifest_runner.active)
                self.assertEqual(dialog.validate_manifest_button.text(), "Verify files")
                self.assertTrue(dialog.validate_manifest_button.isEnabled())
                self.assertTrue(
                    dialog.buttons.button(
                        QDialogButtonBox.StandardButton.Save
                    ).isEnabled()
                )
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

                manifest.unlink()
                manifest.write_text("{}", encoding="utf-8")
                dialog.accept_settings()
                pool.run_next()
                self.assertEqual(dialog._validated_manifest_identity, result[:2])
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.settings().voice_manifest, str(manifest))

    def test_manifest_stale_after_validation_requires_explicit_reverify(self):
        scenarios = (
            ("changed", "changed after validation", False),
            ("missing", "unavailable", True),
        )
        for scenario, expected_status, restore_before_reverify in scenarios:
            with self.subTest(scenario=scenario), TemporaryDirectory() as directory:
                manifest = Path(directory) / "manifest.json"
                manifest.write_text("{}", encoding="utf-8")
                model_manager = Mock()
                model_manager.model_path.return_value = Path("managed/model")
                voice_manager = Mock()
                voice_manager.validate.return_value = manifest
                dialog = AssetManagerDialog(
                    AppSettings(voice_manifest=str(manifest)),
                    model_manager=model_manager,
                    voice_manager=voice_manager,
                )

                dialog._set_manifest_validation_pending(True)
                result = dialog._validate_manifest_snapshot(str(manifest))
                dialog._validated_manifest_identity = ("old", "digest")
                dialog._accept_after_manifest_validation = True
                if scenario == "changed":
                    manifest.write_text('{"changed": true}', encoding="utf-8")
                else:
                    manifest.unlink()

                dialog._manifest_validation_finished(result, None)

                self.assertIn(expected_status, dialog.voice_status.text())
                self.assertIn("Verify files", dialog.voice_status.text())
                self.assertIsNone(dialog._validated_manifest_identity)
                self.assertFalse(dialog._accept_after_manifest_validation)
                self.assertTrue(dialog.validate_manifest_button.isEnabled())
                self.assertTrue(
                    dialog.buttons.button(
                        QDialogButtonBox.StandardButton.Save
                    ).isEnabled()
                )
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

                if restore_before_reverify:
                    manifest.write_text("{}", encoding="utf-8")
                dialog.validate_voice_manifest()
                self.wait_for(lambda: not dialog.manifest_runner.active)
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
