import json
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
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from vntts.main import recognize_screenshot_result  # noqa: E402
from vntts.ocr import OCRResult  # noqa: E402
from vntts.ocr_corrections import (  # noqa: E402
    OCRCorrectionDictionary,
    OCRCorrectionStore,
)
from vntts.ocr_corrections_ui import OCRCorrectionsDialog  # noqa: E402


class OCRCorrectionDictionaryTest(unittest.TestCase):
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
            warnings = []

            store = OCRCorrectionStore.load(path, warn=warnings.append)

        self.assertEqual(store.global_entries, {})
        self.assertEqual(store.profile_entries, {})
        self.assertIn("unsupported OCR corrections schema version", warnings[0])


class OCRCorrectionPipelineTest(unittest.TestCase):
    def test_recognized_result_is_corrected_before_use(self):
        result = OCRResult("Mareus", "Hello.", 95.0, "balanced", 1)
        dictionary = OCRCorrectionDictionary({"Mareus": "Marcus"})

        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=result
        ):
            corrected = recognize_screenshot_result(
                object(),
                correction_dictionary=dictionary,
            )

        self.assertEqual(corrected.character, "Marcus")
        self.assertEqual(corrected.corrections, ("Mareus -> Marcus",))


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
            dialog._append_row(dialog.global_table, "Mareus", "Marcus")
            dialog._append_row(dialog.profile_table, "Vertln", "Vertin")

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
        store.replace_entries.side_effect = OSError("temporary disk failure")
        dialog = OCRCorrectionsDialog("game", "Game", store)
        dialog._append_row(dialog.global_table, "Mareus", "Marcus")

        dialog.save()
        self.wait_for(lambda: not dialog._save_active)

        self.assertIn("Select Save to retry", dialog.status.text())
        self.assertTrue(dialog.buttons.isEnabled())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)


if __name__ == "__main__":
    unittest.main()
