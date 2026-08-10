import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vntts.history import DialogueHistory  # noqa: E402
from vntts.history_ui import DialogueHistoryDialog  # noqa: E402


class DialogueHistoryTest(unittest.TestCase):
    def create_history(self):
        timestamps = iter(
            (
                datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 8, 10, 10, 2, tzinfo=timezone.utc),
            )
        )
        return DialogueHistory(clock=timestamps.__next__)

    def test_typewriter_updates_coalesce_until_dialog_finishes(self):
        history = self.create_history()

        first = history.add("Marcus", "Hello")
        updated = history.add("Marcus", "Hello, Timekeeper.")
        history.finish_current()
        repeated = history.add("Marcus", "Hello, Timekeeper.")

        self.assertEqual(len(history.snapshot()), 2)
        self.assertEqual(updated.id, first.id)
        self.assertEqual(updated.text, "Hello, Timekeeper.")
        self.assertNotEqual(repeated.id, updated.id)

    def test_search_matches_speaker_and_dialog_case_insensitively(self):
        history = self.create_history()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        history.add("Lucy", "Good morning.")

        self.assertEqual(history.search("SUITCASE")[0].character, "Marcus")
        self.assertEqual(history.search("lucy")[0].text, "Good morning.")
        self.assertEqual(history.search("missing"), [])

    def test_exports_text_and_machine_readable_json(self):
        history = self.create_history()
        history.add("Marcus", "Hello, Timekeeper.")
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)

            text_path = history.export(directory / "history.txt")
            json_path = history.export(directory / "history.json")
            text = text_path.read_text(encoding="utf-8")
            payload = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertIn("Marcus\nHello, Timekeeper.", text)
        self.assertEqual(payload["entries"][0]["character"], "Marcus")
        self.assertEqual(payload["entries"][0]["text"], "Hello, Timekeeper.")


class DialogueHistoryDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_searches_and_replays_selected_entry(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        history.add("Lucy", "Good morning.")
        replay = Mock()
        dialog = DialogueHistoryDialog(history, replay)

        dialog.search.setText("suitcase")
        dialog.refresh()
        dialog.replay_selected()

        self.assertEqual(dialog.entries.count(), 1)
        replay.assert_called_once_with("Marcus", "The suitcase is ready.")
        dialog.deleteLater()

    def test_exports_with_extension_selected_by_user(self):
        history = Mock()
        history.search.return_value = []
        dialog = DialogueHistoryDialog(history, Mock())

        with patch(
            "vntts.history_ui.QFileDialog.getSaveFileName",
            return_value=("session", "JSON files (*.json)"),
        ):
            dialog.export_history()

        history.export.assert_called_once_with("session.json")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
