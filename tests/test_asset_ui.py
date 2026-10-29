import os
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import QCloseEvent, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QScrollArea,
)
from shiboken6 import isValid  # noqa: E402
from vntts_artifacts.file_integrity import sha256_file  # noqa: E402

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.asset_integrity import ModelIntegrityError  # noqa: E402
from vntts.asset_ui import (  # noqa: E402
    AssetManagerDialog,
    VoiceImportDialog,
    build_voice_replacement_prompt,
    default_model,
)
from vntts.model_assets import ModelDownloadCancelled  # noqa: E402
from vntts.settings import AppSettings  # noqa: E402
from vntts.voice_packs import VoiceReplacementRequired  # noqa: E402
from vntts.voices import VoiceManifestError  # noqa: E402


def delete_dialog(dialog):
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(dialog, QEvent.Type.DeferredDelete)


class VoiceImportDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_audio_selection_can_be_replaced_before_adding_voice(self):
        dialog = VoiceImportDialog()
        self.addCleanup(delete_dialog, dialog)
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
                dialog.reference_files.toPlainText(),
                "/voices/first.wav\n/voices/second.wav",
            )
            self.assertTrue(dialog.add_button.isEnabled())
            dialog.choose_references()

        self.assertEqual(dialog.references, ["/voices/replacement.wav"])
        self.assertEqual(
            dialog.reference_files.toPlainText(), "/voices/replacement.wav"
        )
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

    def test_invalid_names_stay_in_form_and_focus_the_problem(self):
        dialog = VoiceImportDialog()
        self.addCleanup(delete_dialog, dialog)
        dialog.set_references(["/voices/line.wav"])
        dialog.character.setText("???")
        dialog.show()
        self.application.processEvents()
        self.assertFalse(dialog.add_button.isEnabled())
        self.assertIn("letter or number", dialog.validation_error.text())
        dialog.validate_and_accept()
        self.assertIs(self.application.focusWidget(), dialog.character)
        dialog.character.setText("Маркус")
        dialog.aliases.setText("Marcus, !!!")
        self.assertFalse(dialog.add_button.isEnabled())
        dialog.validate_and_accept()
        self.assertIs(self.application.focusWidget(), dialog.aliases)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.references, ["/voices/line.wav"])
        dialog.aliases.setText("Marcus, М")
        self.assertTrue(dialog.add_button.isEnabled())
        self.assertEqual(dialog.validation_error.text(), "")

    def test_file_paths_are_distinct_copyable_and_do_not_trap_tab(self):
        dialog = VoiceImportDialog()
        self.addCleanup(delete_dialog, dialog)
        paths = ["/recordings/first/line.wav", "/recordings/second/line.wav"]
        dialog.set_references(paths)
        dialog.character.setText("Marcus")
        dialog.show()
        self.application.processEvents()
        dialog.character.setFocus()
        for control in (
            dialog.character,
            dialog.aliases,
            dialog.choose_button,
            dialog.reference_files,
        ):
            self.assertIs(self.application.focusWidget(), control)
            QTest.keyClick(control, Qt.Key.Key_Tab)
        self.assertIs(self.application.focusWidget(), dialog.add_button)
        QTest.keyClick(dialog.add_button, Qt.Key.Key_Backtab)
        self.assertIs(self.application.focusWidget(), dialog.reference_files)
        QTest.keySequence(dialog.reference_files, QKeySequence.StandardKey.SelectAll)
        QTest.keySequence(dialog.reference_files, QKeySequence.StandardKey.Copy)
        self.assertEqual(self.application.clipboard().text(), "\n".join(paths))
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([], "")):
            dialog.choose_references()
        self.assertEqual(dialog.references, paths)
        with patch.object(
            QFileDialog, "getOpenFileNames", return_value=([], "")
        ) as picker:
            dialog.choose_button.setFocus()
            QTest.keyClick(dialog.choose_button, Qt.Key.Key_Space)
        picker.assert_called_once()
        font = dialog.font()
        font.setPointSize(20)
        dialog.setFont(font)
        self.application.processEvents()
        self.assertGreaterEqual(
            dialog.reference_files.height(),
            dialog.reference_files.fontMetrics().lineSpacing() * 3,
        )
        dialog.resize(520, 250)
        dialog.character.setText("A very long character name " * 8)
        dialog.set_references(
            [
                f"/recordings/a-very-long-folder-name/a-very-long-recording-name-{index:02d}.wav"
                for index in range(20)
            ]
        )
        self.application.processEvents()
        dialog.character.setFocus()
        QTest.keyClick(dialog.character, Qt.Key.Key_End)
        self.assertEqual(
            dialog.character.cursorPosition(), len(dialog.character.text())
        )
        dialog.reference_files.setFocus()
        for _ in range(20):
            QTest.keyClick(dialog.reference_files, Qt.Key.Key_PageDown)
        self.assertGreater(dialog.reference_files.verticalScrollBar().value(), 0)
        self.assertEqual(
            dialog.reference_files.verticalScrollBar().value(),
            dialog.reference_files.verticalScrollBar().maximum(),
        )

    def test_replacement_prompt_keeps_cancel_as_keyboard_default(self):
        for key in (Qt.Key.Key_Escape, Qt.Key.Key_Return):
            with self.subTest(key=key):
                dialog = build_voice_replacement_prompt("<Marcus & 37>")
                self.addCleanup(delete_dialog, dialog)
                self.assertEqual(dialog.textFormat(), Qt.TextFormat.PlainText)
                self.assertIn("<Marcus & 37>", dialog.text())
                self.assertIn(
                    "all recordings and other names", dialog.informativeText()
                )
                self.assertIs(
                    dialog.defaultButton(),
                    dialog.button(QMessageBox.StandardButton.Cancel),
                )
                QTimer.singleShot(0, lambda key=key: QTest.keyClick(dialog, key))
                self.assertEqual(dialog.exec(), QMessageBox.StandardButton.Cancel)
                self.assertFalse(dialog.isVisible())
                self.assertIs(
                    dialog.escapeButton(),
                    dialog.button(QMessageBox.StandardButton.Cancel),
                )


class AssetManagerDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.thread_pool = QThreadPool()
        self.dialogs = []
        self.addCleanup(self._finish_dialogs)

    def create_dialog(self, *args, **kwargs):
        kwargs.setdefault("thread_pool", self.thread_pool)
        dialog = AssetManagerDialog(*args, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def _finish_dialogs(self):
        self.assertTrue(self.thread_pool.waitForDone(3000))
        self.application.processEvents()
        for dialog in self.dialogs:
            delete_dialog(dialog)

    def _owned_pool(self) -> QThreadPool:
        pool = QThreadPool()
        self.addCleanup(lambda: self.assertTrue(pool.waitForDone(3000)))
        return pool

    def test_failed_assertions_release_and_drain_asset_workers(self):
        for method in (
            "test_download_cancellation_waits_for_the_cooperative_worker",
            "test_voice_pack_import_is_nonblocking_and_close_safe",
            "test_save_waits_for_exact_manifest_validation_then_accepts",
        ):
            with self.subTest(method=method):
                case = AssetManagerDialogTest(method)
                wait_for = case.wait_for
                owned_pool = case._owned_pool
                pools = []

                def record_pool():
                    pool = owned_pool()
                    pools.append(pool)
                    return pool

                def fail_after_worker_starts(predicate, *args, **kwargs):
                    wait_for(predicate, *args, **kwargs)
                    raise AssertionError(
                        "injected failure after asset worker acquisition"
                    )

                case._owned_pool = record_pool
                case.wait_for = fail_after_worker_starts
                result = unittest.TestResult()
                case.run(result)

                self.assertEqual(len(result.failures), 1)
                self.assertIn("injected failure", result.failures[0][1])
                self.assertEqual(result.errors, [])
                self.assertEqual(len(pools), 1)
                self.assertEqual(pools[0].activeThreadCount(), 0)
                self.assertTrue(pools[0].waitForDone(0))
                self.assertTrue(case.dialogs)
                self.assertTrue(all(not isValid(dialog) for dialog in case.dialogs))
                self.assertTrue(
                    all(not isValid(dialog.models_tab) for dialog in case.dialogs)
                )

    def test_completed_cases_delete_owned_dialogs_and_hidden_model_pages(self):
        for method in (
            "test_default_pocket_backend_offers_only_character_voice_assets",
            "test_manifest_browse_validates_inline_and_supports_keyboard",
        ):
            with self.subTest(method=method):
                case = AssetManagerDialogTest(method)
                result = unittest.TestResult()
                case.run(result)
                self.assertEqual(result.errors, [])
                self.assertEqual(result.failures, [])
                self.assertTrue(case.dialogs)
                self.assertTrue(all(not isValid(dialog) for dialog in case.dialogs))
                self.assertTrue(
                    all(not isValid(dialog.models_tab) for dialog in case.dialogs)
                )

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
        dialog = self.create_dialog(
            AppSettings(speech_backend="coqui-xtts", xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        dialog.download_model()
        self.wait_for(lambda: not dialog.model_runner.active)

        self.assertEqual(dialog.progress.value(), 100)
        self.assertTrue(dialog.progress.isHidden())
        self.assertTrue(dialog.cancel_button.isHidden())
        self.assertIn("Model ready", dialog.model_status.toPlainText())
        self.assertEqual(dialog.settings().tts_model, default_model)

    def test_editable_model_path_handles_invalid_names_and_recovers(self):
        model_manager = Mock()

        def model_path(name):
            if name == "..":
                raise ModelIntegrityError("Invalid model directory")
            return Path("managed") / name

        model_manager.model_path.side_effect = model_path
        dialog = self.create_dialog(
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
        dialog = self.create_dialog(
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
        dialog = self.create_dialog(
            AppSettings(speech_backend="coqui-xtts"),
            model_manager=model_manager,
            voice_manager=Mock(),
            thread_pool=pool,
        )
        dialog.set_operation_running(True, "verify")
        pending_status = dialog.model_status.toPlainText()
        dialog.model_runner.start(lambda: Path("stale"))
        dialog.model_runner.start(lambda: Path("latest"))

        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertTrue(dialog.operation_running)
        self.assertEqual(dialog.model_status.toPlainText(), pending_status)

        pool.tasks.pop(0).run()
        self.application.processEvents()
        self.assertFalse(dialog.operation_running)
        self.assertEqual(
            dialog.model_status.toPlainText(),
            "Model verified and ready. Save selection to use it.",
        )

    def test_model_selection_invalidates_verification_and_cannot_inherit_old_result(
        self,
    ):
        model_manager = Mock()
        model_manager.model_path.side_effect = lambda name: Path("managed") / name
        dialog = self.create_dialog(
            AppSettings(speech_backend="coqui-xtts"),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        dialog.set_operation_running(True, "verify")
        self.assertFalse(dialog.model.isEnabled())
        dialog.model_finished(True, "Model verified and ready at managed/first")
        dialog.model.setCurrentText("second")
        self.assertIn("not checked", dialog.model_status.toPlainText())
        self.assertEqual(dialog.model_path.text(), str(Path("managed") / "second"))

        dialog.set_operation_running(True, "verify")
        dialog.model.setCurrentText("third")
        dialog.model_finished(True, "Model verified and ready at managed/second")
        self.assertIn("Selected model changed", dialog.model_status.toPlainText())
        self.assertIn("Model not ready", dialog.model_status.toPlainText())
        self.assertNotEqual(dialog.settings().tts_model, "third")

    def test_model_verification_can_finish_after_close_without_updating_ui(self):
        started = Event()
        release = Event()
        pool = self._owned_pool()
        self.addCleanup(release.set)
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")

        def validate(_model_name):
            started.set()
            release.wait(3)
            return Path("managed/model")

        model_manager.validate.side_effect = validate
        dialog = self.create_dialog(
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
        self.assertEqual(
            dialog.model_status.toPlainText(), "Verifying model checksums..."
        )

    def test_default_pocket_backend_offers_only_character_voice_assets(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = self.create_dialog(
            AppSettings(),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        self.assertEqual(dialog.windowTitle(), "Voice libraries")
        self.assertEqual(dialog.tabs.count(), 1)
        self.assertEqual(dialog.tabs.tabText(0), "Voice libraries")
        self.assertTrue(dialog.tabs.tabBar().isHidden())
        self.assertTrue(dialog.voice_progress.isHidden())
        self.assertIn("No voice library", dialog.voice_status.toPlainText())
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
        dialog = self.create_dialog(
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
        dialog = self.create_dialog(
            AppSettings(), model_manager=model_manager, voice_manager=voice_manager
        )

        initial_dialog = VoiceImportDialog(dialog)
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
        self.assertIn("revise and retry", dialog.voice_status.toPlainText())

        retry_dialog = VoiceImportDialog(dialog)
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

    def test_existing_voice_requires_explicit_confirmation_before_retry(self):
        for consent in (
            QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        ):
            with self.subTest(consent=consent):
                pool = ManualThreadPool()
                manager = Mock()
                manager.import_voice.side_effect = [
                    VoiceReplacementRequired("Marcus", "confirmed-revision"),
                    Path("managed/custom/manifest.json"),
                ]
                dialog = self.create_dialog(
                    AppSettings(voice_manifest="existing.json"),
                    voice_manager=manager,
                    thread_pool=pool,
                )
                draft = ("Marcus", ["/voices/new.wav"], ["M"])
                dialog._voice_import_draft = draft
                dialog._start_drafted_voice_import()
                with patch.object(QMessageBox, "exec", return_value=consent) as prompt:
                    pool.run_next()
                prompt.assert_called_once()
                self.assertEqual(manager.import_voice.call_count, 1)
                self.assertEqual(dialog.voice_manifest.text(), "existing.json")
                if consent == QMessageBox.StandardButton.Yes:
                    self.assertTrue(dialog.operation_running)
                    self.assertFalse(dialog.add_voice_button.isEnabled())
                    self.assertFalse(dialog.voice_progress.isHidden())
                    pool.run_next()
                    self.assertEqual(
                        manager.import_voice.call_args.kwargs[
                            "expected_manifest_sha256"
                        ],
                        "confirmed-revision",
                    )
                    self.assertEqual(
                        dialog.voice_manifest.text(), "managed/custom/manifest.json"
                    )
                    self.assertIsNone(dialog._voice_import_draft)
                else:
                    self.assertEqual(dialog._voice_import_draft, draft)
                    self.assertIn(
                        "Existing voice kept", dialog.voice_status.toPlainText()
                    )
                self.assertFalse(dialog.operation_running)
                self.assertTrue(dialog.add_voice_button.isEnabled())
                self.assertTrue(dialog.voice_progress.isHidden())

    def test_stale_confirmation_preserves_draft_and_does_not_retry_automatically(self):
        pool = ManualThreadPool()
        manager = Mock()
        manager.import_voice.side_effect = [
            VoiceReplacementRequired("Marcus", "old-revision"),
            VoiceManifestError("Voice library changed. Review it and retry."),
        ]
        dialog = self.create_dialog(
            AppSettings(), voice_manager=manager, thread_pool=pool
        )
        draft = ("Marcus", ["/voices/new.wav"], ["M"])
        dialog._voice_import_draft = draft
        dialog._start_drafted_voice_import()
        with patch.object(
            QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes
        ):
            pool.run_next()
        pool.run_next()
        self.assertFalse(dialog.operation_running)
        self.assertEqual(manager.import_voice.call_count, 2)
        self.assertEqual(dialog._voice_import_draft, draft)
        self.assertIn("Voice library changed", dialog.voice_status.toPlainText())
        self.assertEqual(dialog.voice_manifest.text(), "")
        self.assertEqual(pool.tasks, [])

    def test_close_pending_never_prompts_for_voice_replacement(self):
        pool = ManualThreadPool()
        manager = Mock()
        manager.import_voice.side_effect = VoiceReplacementRequired(
            "Marcus", "revision"
        )
        dialog = self.create_dialog(
            AppSettings(), voice_manager=manager, thread_pool=pool
        )
        dialog._voice_import_draft = ("Marcus", ["/voices/new.wav"], [])
        dialog.show()
        dialog._start_drafted_voice_import()
        dialog.close()
        self.assertTrue(dialog._close_pending)
        with patch.object(QMessageBox, "exec") as prompt:
            pool.run_next()
        prompt.assert_not_called()
        manager.import_voice.assert_called_once()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(dialog.operation_running)

    def test_cancel_button_sets_download_event(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = self.create_dialog(
            AppSettings(xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        dialog.cancel_download()

        self.assertTrue(dialog.cancel_event.is_set())

    def test_download_cancellation_waits_for_the_cooperative_worker(self):
        pool = self._owned_pool()
        started = Event()
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")

        def download(_model_name, *, progress, cancel_event):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.005)
            raise ModelDownloadCancelled("Model download cancelled")

        model_manager.download.side_effect = download
        dialog = self.create_dialog(
            AppSettings(speech_backend="coqui-xtts", xtts_terms_accepted=True),
            model_manager=model_manager,
            voice_manager=Mock(),
            thread_pool=pool,
        )
        self.addCleanup(dialog.cancel_download)
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

        self.assertIn("Model download cancelled", dialog.model_status.toPlainText())

    def test_voice_pack_import_is_nonblocking_and_close_safe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "voices.json"
            source.write_text("{}", encoding="utf-8")
            manifest = root / "managed" / "manifest.json"
            pool = self._owned_pool()
            started = Event()
            release = Event()
            self.addCleanup(release.set)
            voice_manager = Mock()

            def import_pack(path):
                self.assertEqual(Path(path), source)
                started.set()
                release.wait(3)
                return manifest

            voice_manager.import_pack.side_effect = import_pack
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            dialog = self.create_dialog(
                AppSettings(),
                model_manager=model_manager,
                voice_manager=voice_manager,
                thread_pool=pool,
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
            self.assertIn("Importing voice files", dialog.voice_status.toPlainText())
            close_event = QCloseEvent()
            dialog.closeEvent(close_event)
            self.assertFalse(close_event.isAccepted())
            self.assertIn(
                "Finishing the voice import", dialog.voice_status.toPlainText()
            )

            release.set()
            self.wait_for(lambda: not dialog.operation_running)
            self.assertEqual(dialog.voice_manifest.text(), str(manifest))
            self.assertEqual(dialog.voice_progress.value(), 100)

    def test_voice_import_failure_restores_controls_for_retry(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        voice_manager = Mock()
        voice_manager.import_pack.side_effect = OSError("temporary copy failure")
        dialog = self.create_dialog(
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

        self.assertIn(
            "Choose the source again to retry", dialog.voice_status.toPlainText()
        )
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
            dialog = self.create_dialog(
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
            self.assertIn(
                "passed checksum validation", dialog.voice_status.toPlainText()
            )
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
            dialog = self.create_dialog(
                AppSettings(voice_manifest=str(manifest)),
                model_manager=model_manager,
                voice_manager=voice_manager,
            )

            with patch.object(QMessageBox, "warning") as warning:
                dialog.accept_settings()
                self.wait_for(lambda: not dialog.manifest_runner.active)

            warning.assert_not_called()
            self.assertIn("checksum mismatch", dialog.voice_status.toPlainText())
            self.assertEqual(dialog.voice_manifest.selectedText(), str(manifest))
            self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_save_rejects_initial_unavailable_manifest_and_recovers(self):
        scenarios = ("missing",)
        if hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"):
            scenarios += ("fifo",)
        for unavailable in scenarios:
            with (
                self.subTest(unavailable=unavailable),
                TemporaryDirectory() as directory,
            ):
                manifest = Path(directory).resolve() / "manifest.json"
                if unavailable == "fifo":
                    os.mkfifo(manifest)
                model_manager = Mock()
                model_manager.model_path.return_value = Path("managed/model")
                voice_manager = Mock()
                voice_manager.validate.return_value = manifest
                settings = AppSettings(voice_manifest=str(manifest))
                pool = ManualThreadPool()
                dialog = self.create_dialog(
                    settings,
                    model_manager=model_manager,
                    voice_manager=voice_manager,
                    thread_pool=pool,
                )
                native_path_open = Path.open

                def reject_plain_open(path, *args, **kwargs):
                    mode = args[0] if args else kwargs.get("mode", "r")
                    if Path(path) == manifest and "b" in mode:
                        self.fail("manifest hashing must not use blocking Path.open")
                    return native_path_open(path, *args, **kwargs)

                with patch.object(Path, "open", reject_plain_open):
                    dialog.accept_settings()
                    self.assertTrue(dialog.manifest_runner.active)
                    self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                    self.assertIs(dialog.settings(), settings)
                    pool.run_next()
                self.assertIn("invalid", dialog.voice_status.toPlainText())
                self.assertFalse(dialog.manifest_runner.active)
                self.assertFalse(dialog._accept_after_manifest_validation)
                self.assertIsNone(dialog._validated_manifest_identity)
                self.assertIs(dialog.settings(), settings)
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                voice_manager.validate.assert_not_called()

                manifest.unlink(missing_ok=True)
                manifest.write_text("{}", encoding="utf-8")
                dialog.accept_settings()
                self.assertTrue(dialog.manifest_runner.active)
                self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
                pool.run_next()
                self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
                self.assertEqual(dialog.settings().voice_manifest, str(manifest))
                voice_manager.validate.assert_called_once_with(manifest.resolve())

    def test_selected_manifest_starts_unchecked_and_controls_scale_together(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = self.create_dialog(
            AppSettings(voice_manifest="voices.json"),
            model_manager=model_manager,
            voice_manager=Mock(),
        )

        self.assertIn("Files not checked", dialog.voice_status.toPlainText())
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

    def test_long_statuses_are_plain_copyable_and_do_not_expand_the_window(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        dialog = self.create_dialog(
            AppSettings(speech_backend="coqui-xtts", voice_manifest="voices.json"),
            model_manager=model_manager,
            voice_manager=Mock(),
        )
        font = dialog.font()
        font.setPointSize(18)
        dialog.setFont(font)
        dialog.resize(680, 440)
        dialog.show()
        self.application.processEvents()
        message = "<Missing reference> " + "long-path/" * 80 + "voice.wav"
        for tab, status in ((0, dialog.model_status), (1, dialog.voice_status)):
            with self.subTest(tab=tab):
                dialog.tabs.setCurrentIndex(tab)
                status.setPlainText(message)
                self.application.processEvents()
                self.assertEqual(status.toPlainText(), message)
                self.assertEqual(status.font().pointSize(), 18)
                self.assertTrue(status.isReadOnly())
                self.assertGreater(status.verticalScrollBar().maximum(), 0)
                status.setFocus()
                QTest.keyClick(status, Qt.Key.Key_PageDown)
                self.assertGreater(status.verticalScrollBar().value(), 0)
                QTest.keySequence(status, QKeySequence.StandardKey.SelectAll)
                QTest.keySequence(status, QKeySequence.StandardKey.Copy)
                self.assertEqual(self.application.clipboard().text(), message)
                self.assertEqual(dialog.size().toTuple(), (680, 440))
                page = dialog.tabs.currentWidget()
                self.assertIsInstance(page, QScrollArea)
                self.assertTrue(dialog.buttons.isVisible())

        dialog.tabs.setCurrentIndex(0)
        dialog.resize(700, 320)
        self.application.processEvents()
        viewport = dialog.models_tab.viewport()
        for action in (dialog.verify_button, dialog.download_button):
            visible_rect = action.rect().translated(
                action.mapTo(viewport, action.rect().topLeft())
            )
            self.assertTrue(viewport.rect().contains(visible_rect))

    def test_manifest_controls_follow_operation_state_and_empty_is_valid(self):
        model_manager = Mock()
        model_manager.model_path.return_value = Path("managed/model")
        voice_manager = Mock()
        dialog = self.create_dialog(
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
            pool = self._owned_pool()
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
            dialog = self.create_dialog(
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
            self.assertEqual(dialog.voice_manifest.text(), str(second))
            self.assertIn(
                "passed checksum validation", dialog.voice_status.toPlainText()
            )
            self.assertNotIn(first.name, dialog.voice_status.toPlainText())

    def test_save_waits_for_exact_manifest_validation_then_accepts(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            pool = self._owned_pool()
            started = Event()
            release = Event()
            self.addCleanup(release.set)
            voice_manager = Mock()

            def validate(path):
                started.set()
                release.wait(3)
                return Path(path)

            voice_manager.validate.side_effect = validate
            model_manager = Mock()
            model_manager.model_path.return_value = Path("managed/model")
            dialog = self.create_dialog(
                AppSettings(voice_manifest=str(manifest)),
                model_manager=model_manager,
                voice_manager=voice_manager,
                thread_pool=pool,
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
                dialog = self.create_dialog(
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
                self.assertIn(expected_status, dialog.voice_status.toPlainText())
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
                dialog = self.create_dialog(
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

                self.assertIn(expected_status, dialog.voice_status.toPlainText())
                self.assertIn("Verify files", dialog.voice_status.toPlainText())
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
