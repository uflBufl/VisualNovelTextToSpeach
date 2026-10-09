import json
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
    QLabel,
    QLineEdit,
    QMessageBox,
)

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.main import recognize_screenshot_result  # noqa: E402
from vntts.ocr import OCRResult  # noqa: E402
from vntts.ocr_corrections import (  # noqa: E402
    OCRCorrectionDictionary,
    OCRCorrectionStore,
)
from vntts.ocr_corrections_ui import OCRCorrectionsDialog  # noqa: E402


class OCRCorrectionDictionaryTest(unittest.TestCase):
    def test_loaded_rules_are_reused_beyond_the_regex_cache_capacity(self):
        rules = {f"wrong{index}": f"right{index}" for index in range(600)}
        dictionary = OCRCorrectionDictionary(rules)
        rules["wrong1"] = "changed later"
        with patch(
            "vntts.ocr_corrections.re.compile",
            side_effect=AssertionError("rules recompiled"),
        ):
            for _ in range(3):
                corrected, changes = dictionary.correct_text("wrong1 wrong599 wrongly1")
                self.assertEqual(corrected, "right1 right599 wrongly1")
                self.assertEqual(changes, ("wrong599 -> right599", "wrong1 -> right1"))

        replacement = OCRCorrectionDictionary(rules)
        self.assertEqual(replacement.correct_text("wrong1")[0], "changed later")

    def test_equal_length_rules_keep_insertion_order_and_cascade(self):
        dictionary = OCRCorrectionDictionary({"cat": "dog", "dog": "fox"})

        corrected, changes = dictionary.correct_text("cat dog")

        self.assertEqual(corrected, "fox fox")
        self.assertEqual(changes, ("cat -> dog", "dog -> fox"))

    def test_corrects_speaker_and_dialog_and_reports_each_change(self):
        dictionary = OCRCorrectionDictionary(
            {"Mareus": "Marcus", "tiniekeeper": "timekeeper"}
        )
        result = OCRResult(
            "Mareus",
            "The tiniekeeper met another Mareus.",
            91.0,
            "balanced",
            1,
        )

        corrected = dictionary.correct_result(result)

        self.assertEqual(corrected.character, "Marcus")
        self.assertEqual(
            corrected.text,
            "The timekeeper met another Marcus.",
        )
        self.assertEqual(
            corrected.corrections,
            ("Mareus -> Marcus", "tiniekeeper -> timekeeper"),
        )

    def test_does_not_replace_text_inside_another_word(self):
        dictionary = OCRCorrectionDictionary({"son": "sun"})

        corrected, changes = dictionary.correct_text("A son speaks reasonably.")

        self.assertEqual(corrected, "A sun speaks reasonably.")
        self.assertEqual(changes, ("son -> sun",))

    def test_can_correct_letter_case(self):
        dictionary = OCRCorrectionDictionary({"marcus": "Marcus"})

        corrected, _changes = dictionary.correct_text("MARCUS")

        self.assertEqual(corrected, "Marcus")


class OCRCorrectionStoreTest(unittest.TestCase):
    def test_round_trips_global_and_profile_entries(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ocr-corrections.json"
            store = OCRCorrectionStore(path)
            store.replace_entries(
                {"Mareus": "Marcus"},
                "reverse-1999",
                {"Vertln": "Vertin"},
            )

            loaded = OCRCorrectionStore.load(path)

        self.assertEqual(loaded.global_entries, {"Mareus": "Marcus"})
        self.assertEqual(
            loaded.profile_entries,
            {"reverse-1999": {"Vertln": "Vertin"}},
        )

    def test_profile_entries_override_global_entries_case_insensitively(self):
        store = OCRCorrectionStore(
            global_entries={"Vertln": "Vertin"},
            profile_entries={"game": {"vertln": "Ms. Vertin"}},
        )

        corrected, _changes = store.dictionary_for("game").correct_text("Vertln")

        self.assertEqual(corrected, "Ms. Vertin")

    def test_profile_entries_can_be_copied_and_removed(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ocr-corrections.json"
            store = OCRCorrectionStore(
                path,
                profile_entries={"source": {"Vertln": "Vertin"}},
            )
            store.copy_profile("source", "copy")
            store.remove_profile("source")

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["profiles"], {"copy": {"Vertln": "Vertin"}})

    def test_entries_can_be_added_without_replacing_existing_rules(self):
        with TemporaryDirectory() as temporary_directory:
            store = OCRCorrectionStore(
                Path(temporary_directory) / "ocr-corrections.json",
                global_entries={"Mareus": "Marcus"},
            )

            store.upsert_entries({"Vertln": "Vertin"})
            store.upsert_entries({"mareus": "Ms. Marcus"})

        self.assertEqual(
            store.global_entries,
            {"Vertln": "Vertin", "mareus": "Ms. Marcus"},
        )

    def test_failed_save_preserves_in_memory_entries(self):
        with TemporaryDirectory() as directory:
            store = OCRCorrectionStore(
                Path(directory) / "ocr-corrections.json",
                global_entries={"Mareus": "Marcus"},
                profile_entries={"game": {"Vertln": "Vertin"}},
            )
            store.save()
            original = store.path.read_bytes()
            revision = store._revision
            for method, arguments in (
                ("replace_entries", ({"New": "Global"}, "game", {"New": "Profile"})),
                ("upsert_entries", ({"New": "Global"},)),
                ("upsert_entries", ({"New": "Profile"}, "game")),
                ("copy_profile", ("game", "copy")),
                ("remove_profile", ("game",)),
            ):
                with self.subTest(method=method, arguments=arguments):
                    with (
                        patch(
                            "vntts.versioned_json.write_versioned_json",
                            side_effect=OSError("disk full"),
                        ),
                        self.assertRaisesRegex(OSError, "disk full"),
                    ):
                        getattr(store, method)(*arguments)

                    self.assertEqual(store.global_entries, {"Mareus": "Marcus"})
                    self.assertEqual(
                        store.profile_entries, {"game": {"Vertln": "Vertin"}}
                    )
                    self.assertEqual(store._revision, revision)
                    self.assertEqual(store.path.read_bytes(), original)

    def test_stale_store_cannot_overwrite_another_store(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ocr-corrections.json"
            first = OCRCorrectionStore(path)
            first.upsert_entries({"Mareus": "Marcus"})
            stale = OCRCorrectionStore.load(path)
            first.upsert_entries({"Vertln": "Vertin"})

            with self.assertRaisesRegex(OSError, "changed on disk"):
                stale.upsert_entries({"Poaeher": "Poacher"})
            with self.assertRaisesRegex(OSError, "changed on disk"):
                stale.save()

            self.assertEqual(stale.global_entries, {"Mareus": "Marcus"})
            self.assertEqual(
                OCRCorrectionStore.load(path).global_entries,
                {"Mareus": "Marcus", "Vertln": "Vertin"},
            )

    def test_loaded_entries_use_the_revision_of_the_decoded_snapshot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ocr-corrections.json"
            initial = OCRCorrectionStore(path)
            initial.upsert_entries({"Mareus": "Marcus"})
            snapshot_a = path.read_bytes()
            external = OCRCorrectionStore.load(path)
            external.upsert_entries({"Vertln": "Vertin"})
            snapshot_b = path.read_bytes()
            path.write_bytes(snapshot_a)

            from vntts.ocr_corrections import load_versioned_json as original_loader

            def load_b_then_restore_a(*args, **kwargs):
                path.write_bytes(snapshot_b)
                document = original_loader(*args, **kwargs)
                path.write_bytes(snapshot_a)
                return document

            with patch(
                "vntts.ocr_corrections.load_versioned_json",
                side_effect=load_b_then_restore_a,
            ):
                loaded = OCRCorrectionStore.load(path)

            self.assertEqual(
                loaded.global_entries,
                {"Mareus": "Marcus", "Vertln": "Vertin"},
            )
            with self.assertRaisesRegex(OSError, "changed on disk"):
                loaded.upsert_entries({"Poaeher": "Poacher"})

            self.assertEqual(
                OCRCorrectionStore.load(path).global_entries,
                {"Mareus": "Marcus"},
            )

    def test_invalid_file_falls_back_to_empty_dictionary(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ocr-corrections.json"
            path.write_text("not json", encoding="utf-8")
            warnings = []

            store = OCRCorrectionStore.load(path, warn=warnings.append)

        self.assertEqual(store.global_entries, {})
        self.assertEqual(store.profile_entries, {})
        self.assertIn("Unable to load OCR corrections", warnings[0])

    def test_future_schema_falls_back_to_empty_dictionary(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ocr-corrections.json"
            path.write_text(
                json.dumps({"schema_version": 2, "global": {}, "profiles": {}}),
                encoding="utf-8",
            )
            original = path.read_bytes()
            warnings = []

            store = OCRCorrectionStore.load(path, warn=warnings.append)
            with self.assertRaisesRegex(OSError, "changed on disk"):
                store.save()
            self.assertEqual(path.read_bytes(), original)

        self.assertEqual(store.global_entries, {})
        self.assertEqual(store.profile_entries, {})
        self.assertIn("unsupported OCR corrections schema version", warnings[0])


class OCRCorrectionPipelineTest(unittest.TestCase):
    def test_recognized_result_is_corrected_before_use(self):
        result = OCRResult("Mareus", "Hello.", 95.0, "balanced", 1)
        dictionary = OCRCorrectionDictionary({"Mareus": "Marcus", "Hello": r"Line\1"})

        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=result
        ):
            corrected = recognize_screenshot_result(
                object(),
                correction_dictionary=dictionary,
            )

        self.assertEqual(corrected.character, "Marcus")
        self.assertEqual(corrected.text, r"Line\1.")
        self.assertEqual(
            corrected.corrections, ("Mareus -> Marcus", r"Hello -> Line\1")
        )


class OCRCorrectionsDialogTest(unittest.TestCase):
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
        self.fail("Timed out waiting for OCR correction save")

    def test_saves_global_and_current_profile_tables(self):
        with TemporaryDirectory() as temporary_directory:
            store = OCRCorrectionStore(
                Path(temporary_directory) / "ocr-corrections.json"
            )
            dialog = OCRCorrectionsDialog("game", "Game", store)
            self.assertEqual(dialog.tabs.currentIndex(), 1)
            self.assertFalse(dialog.save_button.isEnabled())
            dialog.show()
            self.application.processEvents()
            self.assertGreater(dialog.profile_table.columnWidth(0), 200)
            dialog._append_row(dialog.global_table, " Mareus ", " Marcus ")
            dialog._append_row(dialog.global_table, " ", " ")
            dialog._append_row(dialog.profile_table, " Vertln ", " Vertin ")

            dialog.save()
            self.wait_for(lambda: not dialog._save_active)

            loaded = OCRCorrectionStore.load(store.path)
        self.assertEqual(loaded.global_entries, {"Mareus": "Marcus"})
        self.assertEqual(loaded.profile_entries["game"], {"Vertln": "Vertin"})
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.deleteLater()

    def test_slow_save_keeps_qt_responsive_and_defers_close(self):
        started = Event()
        release = Event()
        store = Mock(global_entries={}, profile_entries={})

        def replace_entries(*_args):
            started.set()
            release.wait(3)

        store.replace_entries.side_effect = replace_entries
        dialog = OCRCorrectionsDialog("game", "Game", store)
        dialog._append_row(dialog.global_table, "Mareus", "Marcus")
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append("painted"))

        before = time.monotonic()
        dialog.save()
        elapsed = time.monotonic() - before
        self.wait_for(lambda: started.is_set() and bool(heartbeat))

        self.assertLess(elapsed, 0.1)
        self.assertTrue(dialog._save_active)
        self.assertFalse(dialog.buttons.isEnabled())
        close_event = QCloseEvent()
        dialog.closeEvent(close_event)
        self.assertFalse(close_event.isAccepted())
        self.assertIn("Close is deferred", dialog.status.text())

        release.set()
        self.wait_for(lambda: not dialog._save_active)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_save_failure_restores_controls_for_retry(self):
        store = Mock(global_entries={}, profile_entries={})
        store.replace_entries.side_effect = [OSError("temporary disk failure"), None]
        dialog = OCRCorrectionsDialog("game", "Game", store)
        dialog._append_row(dialog.global_table, "Mareus", "Marcus")
        dialog._append_row(dialog.profile_table, "Vertln", "Vertin")

        dialog.save()
        self.wait_for(lambda: not dialog._save_active)

        self.assertIn("select Save again", dialog.status.text())
        self.assertTrue(dialog.buttons.isEnabled())
        self.assertEqual(dialog.profile_table.item(0, 1).text(), "Vertin")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.save()
        self.wait_for(lambda: not dialog._save_active)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(store.replace_entries.call_count, 2)

    def test_submission_failure_preserves_rules_and_restores_save_for_retry(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ocr-corrections.json"
            store = OCRCorrectionStore(path, global_entries={"Mareus": "Marcus"})
            store.save()
            original = path.read_bytes()
            pool = ManualThreadPool()
            dialog = OCRCorrectionsDialog("game", "Game", store, thread_pool=pool)
            self.addCleanup(dialog.deleteLater)
            dialog._append_row(dialog.profile_table, "Vertln", "Vertin")

            with patch.object(
                store, "replace_entries", wraps=store.replace_entries
            ) as save:
                with patch.object(
                    pool, "start", side_effect=RuntimeError("pool unavailable")
                ):
                    dialog.save()
                self.application.processEvents()

                save.assert_not_called()
                self.assertFalse(dialog.save_runner.active)
                self.assertFalse(dialog._save_active)
                self.assertTrue(dialog.tabs.isEnabled())
                self.assertTrue(dialog.buttons.isEnabled())
                self.assertTrue(dialog.save_button.isEnabled())
                self.assertIn("pool unavailable", dialog.status.text())
                self.assertIn("select Save again", dialog.status.text())
                self.assertEqual(dialog.profile_table.item(0, 1).text(), "Vertin")
                self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
                self.assertEqual(store.global_entries, {"Mareus": "Marcus"})
                self.assertEqual(store.profile_entries, {})
                self.assertEqual(path.read_bytes(), original)

                dialog.save()
                self.assertTrue(dialog._save_active)
                self.assertFalse(dialog.buttons.isEnabled())
                pool.run_next()
                self.application.processEvents()
                save.assert_called_once_with(
                    {"Mareus": "Marcus"}, "game", {"Vertln": "Vertin"}
                )

            self.assertFalse(dialog._save_active)
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            loaded = OCRCorrectionStore.load(path)
            self.assertEqual(loaded.global_entries, {"Mareus": "Marcus"})
            self.assertEqual(loaded.profile_entries, {"game": {"Vertln": "Vertin"}})

    def test_stale_rules_request_reopen_instead_of_retry(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "ocr-corrections.json"
            current = OCRCorrectionStore(path)
            current.upsert_entries({"Mareus": "Marcus"})
            stale = OCRCorrectionStore.load(path)
            current.upsert_entries({"Vertln": "Vertin"})
            dialog = OCRCorrectionsDialog(store=stale)
            dialog._append_row(dialog.global_table, "Poaeher", "Poacher")

            dialog.save()
            self.wait_for(lambda: not dialog._save_active)

            self.assertIn("reopen OCR corrections", dialog.status.text())
            self.assertNotIn("select Save again", dialog.status.text())
            self.assertEqual(dialog.global_table.item(1, 1).text(), "Poacher")
            self.assertEqual(
                OCRCorrectionStore.load(path).global_entries,
                {"Mareus": "Marcus", "Vertln": "Vertin"},
            )
            dialog.deleteLater()

    def test_row_errors_are_inline_and_all_scopes_are_reported(self):
        store = Mock(global_entries={}, profile_entries={})
        dialog = OCRCorrectionsDialog("game", "Game", store)
        dialog._append_row(dialog.global_table, "Mareus", "")
        dialog._append_row(dialog.profile_table, "Vertln", "Vertin")
        dialog._append_row(dialog.profile_table, "vertln", "Ms. Vertin")

        dialog.save()

        self.assertFalse(dialog._save_active)
        self.assertIn("All games row 1", dialog.status.text())
        self.assertIn("Profile row 2", dialog.status.text())
        self.assertEqual(dialog.tabs.currentIndex(), 0)
        self.assertTrue(dialog.global_table.item(0, 1).toolTip())
        self.assertTrue(dialog.profile_table.item(1, 0).toolTip())
        store.replace_entries.assert_not_called()
        dialog.deleteLater()

    def test_insert_and_delete_shortcuts_edit_the_focused_table(self):
        dialog = OCRCorrectionsDialog("game", "Game", OCRCorrectionStore())
        dialog.show()
        dialog.global_table.setFocus()

        QTest.keyClick(dialog.global_table, Qt.Key.Key_Insert)
        self.application.processEvents()
        self.assertEqual(dialog.global_table.rowCount(), 1)
        with patch.object(
            QMessageBox,
            "exec",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            QTest.keyClick(dialog.global_table, Qt.Key.Key_Escape)
        dialog.global_table.selectRow(0)
        QTest.keyClick(
            dialog.global_table,
            Qt.Key.Key_Delete,
            Qt.KeyboardModifier.ControlModifier,
        )
        self.application.processEvents()

        self.assertEqual(dialog.global_table.rowCount(), 0)
        dialog.deleteLater()

    def test_unsaved_close_requires_explicit_discard(self):
        dialog = OCRCorrectionsDialog("game", "Game", OCRCorrectionStore())
        dialog._append_row(dialog.global_table, "Mareus", "Marcus")

        first_close = QCloseEvent()
        with patch.object(
            QMessageBox,
            "exec",
            return_value=QMessageBox.StandardButton.Cancel,
        ):
            dialog.closeEvent(first_close)

        self.assertFalse(first_close.isAccepted())
        self.assertEqual(dialog.cancel_button.text(), "Cancel")
        self.assertEqual(
            dialog._discard_dialog().button(QMessageBox.StandardButton.Cancel).text(),
            "Keep editing",
        )

        second_close = QCloseEvent()
        with patch.object(
            QMessageBox,
            "exec",
            return_value=QMessageBox.StandardButton.Discard,
        ):
            dialog.closeEvent(second_close)
        self.assertTrue(second_close.isAccepted())
        dialog.deleteLater()

    def test_empty_editor_and_full_value_inspection(self):
        dialog = OCRCorrectionsDialog("game", "Game", OCRCorrectionStore())
        self.assertFalse(dialog.save_button.isEnabled())
        self.assertTrue(
            any(
                "No rules in this scope" in label.text()
                for label in dialog.findChildren(QLabel)
            )
        )
        source = "The unknowable chronology of the distant island's memories"
        replacement = "The hidden chronology of the distant island's memories"
        dialog._append_row(dialog.profile_table, source, replacement)
        self.assertTrue(dialog.save_button.isEnabled())
        self.assertEqual(dialog.profile_table.item(0, 0).toolTip(), source)
        self.assertEqual(dialog.profile_table.item(0, 1).toolTip(), replacement)
        dialog.deleteLater()

    def test_blank_added_row_does_not_enable_save_or_discard_prompt(self):
        dialog = OCRCorrectionsDialog("game", "Game", OCRCorrectionStore())
        dialog._append_row(dialog.profile_table)

        self.assertFalse(dialog.save_button.isEnabled())
        self.assertFalse(dialog._has_unsaved_changes())
        close_event = QCloseEvent()
        with patch.object(QMessageBox, "exec") as confirmation:
            dialog.closeEvent(close_event)
        self.assertTrue(close_event.isAccepted())
        confirmation.assert_not_called()
        dialog.deleteLater()

    def test_uncommitted_cell_text_is_not_discarded_on_close(self):
        dialog = OCRCorrectionsDialog("game", "Game", OCRCorrectionStore())
        dialog.show()
        dialog._append_row(dialog.profile_table)
        editor = dialog.profile_table.findChild(QLineEdit)
        self.assertIsNotNone(editor)
        QTest.keyClicks(editor, "Mareus")
        self.assertEqual(editor.text(), "Mareus")
        self.assertEqual(dialog.profile_table.item(0, 0).text(), "")
        close_event = QCloseEvent()
        with patch.object(
            QMessageBox,
            "exec",
            return_value=QMessageBox.StandardButton.Cancel,
        ) as confirmation:
            dialog.closeEvent(close_event)
        self.assertFalse(close_event.isAccepted())
        confirmation.assert_called_once()
        dialog.deleteLater()

    def test_keyboard_can_inspect_long_value_without_changing_it(self):
        source = "The unknowable chronology of the distant island's memories"
        store = OCRCorrectionStore(profile_entries={"game": {source: "Known"}})
        dialog = OCRCorrectionsDialog("game", "Game", store)
        dialog.show()
        dialog.profile_table.setCurrentCell(0, 0)
        dialog.profile_table.editItem(dialog.profile_table.item(0, 0))
        self.application.processEvents()
        editor = dialog.profile_table.findChild(QLineEdit)
        self.assertIsNotNone(editor)
        self.assertEqual(editor.text(), source)
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.deleteLater()

    def test_unchanged_replacement_is_reported_inline(self):
        dialog = OCRCorrectionsDialog("game", "Game", OCRCorrectionStore())
        dialog._append_row(dialog.profile_table, "Mareus", "Mareus")
        dialog.save()

        self.assertFalse(dialog._save_active)
        self.assertIn("replacement must differ", dialog.status.text())
        self.assertTrue(dialog.profile_table.item(0, 0).toolTip())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
