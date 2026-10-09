import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
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

    def test_typewriter_rollback_does_not_erase_longer_captured_text(self):
        history = self.create_history()

        complete = history.add("Marcus", "Hello, Timekeeper.")
        regressed = history.add("Marcus", "Hello")

        self.assertIs(regressed, complete)
        self.assertEqual(history.snapshot()[0].text, "Hello, Timekeeper.")

    def test_similar_consecutive_lines_remain_separate(self):
        history = self.create_history()

        first = history.add("Marcus", "The suitcase is ready for departure.")
        second = history.add("Marcus", "The suitcase is ready for inspection.")

        self.assertEqual(len(history.snapshot()), 2)
        self.assertNotEqual(first.id, second.id)

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

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for dialogue replay")

    def test_search_and_details_expose_accessible_names_and_label_buddies(self):
        dialog = DialogueHistoryDialog(DialogueHistory(), Mock())

        self.assertEqual(dialog.search.accessibleName(), "Search dialogue history")
        self.assertEqual(dialog.search_label.buddy(), dialog.search)
        self.assertEqual(dialog.details.accessibleName(), "Selected dialogue details")
        self.assertEqual(dialog.details_label.buddy(), dialog.details)
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertEqual(dialog.replay_button.text(), "Speak with current voice")
        self.assertEqual(dialog.stop_button.text(), "Stop speaking")
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertIn("No dialogue has been captured", dialog.status.text())

        dialog.deleteLater()

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
        self.wait_for(lambda: not dialog.replay_runner.active)

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

    def test_slow_replay_keeps_qt_responsive_and_reports_completion(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        started = Event()
        release = Event()

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=True)
        dialog = DialogueHistoryDialog(history, replay, stop_handler=stop)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append("painted"))

        before = time.monotonic()
        dialog.replay_selected()
        elapsed = time.monotonic() - before
        self.wait_for(lambda: started.is_set() and bool(heartbeat))

        self.assertLess(elapsed, 0.1)
        self.assertTrue(dialog.replay_runner.active)
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertTrue(dialog.stop_button.isEnabled())
        self.assertIn("Speaking as Marcus", dialog.status.text())
        close_event = QCloseEvent()
        dialog.closeEvent(close_event)
        self.assertFalse(close_event.isAccepted())
        self.assertIn("Stopping", dialog.status.text())

        self.wait_for(lambda: not dialog.replay_runner.active)
        stop.assert_called_once_with()
        self.assertTrue(dialog.replay_button.isEnabled())
        self.assertEqual(dialog.status.text(), "Speech stopped.")
        release.set()

    def test_stop_confirmation_closes_even_if_replay_future_is_unresponsive(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        started = Event()
        release = Event()

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=True)
        dialog = DialogueHistoryDialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(started.is_set)

        dialog.close()
        self.wait_for(lambda: not dialog.isVisible())

        stop.assert_called_once_with()
        self.assertFalse(dialog.replay_runner.active)
        release.set()

    def test_close_waits_for_stop_even_when_replay_finishes_first(self):
        for outcome in ("early-close", "late-close", "failed-stop"):
            with self.subTest(outcome=outcome):
                history = DialogueHistory()
                history.add("Marcus", "Hello.")
                replay_release = Event()
                stop_started = Event()
                stop_release = Event()

                def stop():
                    stop_started.set()
                    stop_release.wait(3)
                    if outcome == "failed-stop":
                        raise OSError("stop failed")
                    return True

                dialog = DialogueHistoryDialog(
                    history, lambda *_args: replay_release.wait(3), stop_handler=stop
                )
                self.addCleanup(dialog.deleteLater)
                self.addCleanup(dialog.close)
                self.addCleanup(stop_release.set)
                self.addCleanup(replay_release.set)
                dialog.show()
                dialog.replay_selected()
                if outcome == "late-close":
                    dialog.stop_replay()
                else:
                    dialog.close()
                self.wait_for(stop_started.is_set)
                cursor = dialog.details.textCursor()
                cursor.select(cursor.SelectionType.Document)
                dialog.details.setTextCursor(cursor)
                selected_details = cursor.selectedText()
                replay_release.set()
                self.wait_for(lambda: not dialog.replay_runner.active)
                dialog.close()

                self.assertTrue(dialog.isVisible())
                self.assertTrue(dialog.stop_runner.active)
                stop_release.set()
                self.wait_for(lambda: not dialog.stop_runner.active)
                self.assertEqual(
                    dialog.details.textCursor().selectedText(), selected_details
                )
                if outcome == "failed-stop":
                    self.assertTrue(dialog.isVisible())
                    self.assertIn("stop failed", dialog.status.text())
                    self.assertTrue(dialog.replay_button.isEnabled())
                    self.assertFalse(dialog.stop_button.isEnabled())
                    dialog.close()
                self.assertFalse(dialog.isVisible())

    def test_escape_stops_active_replay_before_closing(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        started = Event()
        release = Event()
        finished = Event()

        def replay(_character, _text):
            started.set()
            release.wait(3)
            finished.set()

        stop = Mock(return_value=True)
        dialog = DialogueHistoryDialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(started.is_set)

        QTest.keyClick(dialog, Qt.Key.Key_Escape)

        self.wait_for(lambda: not dialog.isVisible())
        stop.assert_called_once_with()
        release.set()
        self.wait_for(finished.is_set)
        dialog.deleteLater()

    def test_unsupported_stop_keeps_dialog_open_until_speech_finishes(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        started = Event()
        release = Event()

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=False)
        dialog = DialogueHistoryDialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(started.is_set)

        dialog.close()
        self.wait_for(lambda: not dialog.stop_runner.active)

        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.replay_runner.active)
        self.assertIn("cannot stop playback", dialog.status.text())
        dialog.close()
        self.assertEqual(stop.call_count, 1)
        release.set()
        self.wait_for(lambda: not dialog.isVisible())

    def test_replay_failure_is_retryable_in_dialog(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        replay = Mock(side_effect=RuntimeError("backend unavailable"))
        dialog = DialogueHistoryDialog(history, replay)

        dialog.replay_selected()
        self.wait_for(lambda: not dialog.replay_runner.active)

        self.assertIn("try again", dialog.status.text())
        self.assertTrue(dialog.replay_button.isEnabled())

    def test_no_search_results_keep_session_export_available(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        dialog = DialogueHistoryDialog(history, Mock())

        dialog.search.setText("missing")

        self.assertEqual(dialog.entries.count(), 0)
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("No matching lines", dialog.status.text())
        dialog.deleteLater()

    def test_export_includes_whole_session_while_history_is_filtered(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        history.add("Lucy", "Good morning.")
        dialog = DialogueHistoryDialog(history, Mock())
        dialog.search.setText("Lucy")

        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "history.json"
            with patch(
                "vntts.history_ui.QFileDialog.getSaveFileName",
                return_value=(str(path), "JSON files (*.json)"),
            ):
                dialog.export_history()
            exported = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(exported["entries"]), 2)
        dialog.deleteLater()

    def test_refresh_preserves_older_selection_and_scroll_position(self):
        history = DialogueHistory()
        for index in range(40):
            history.add(f"Speaker {index}", f"Dialogue line {index}")
            history.finish_current()
        dialog = DialogueHistoryDialog(history, Mock())
        dialog.resize(520, 300)
        dialog.show()
        self.application.processEvents()
        dialog.entries.setCurrentRow(8)
        selected_id = dialog.current_entry().id
        scroll_bar = dialog.entries.verticalScrollBar()
        scroll_bar.setValue(max(1, scroll_bar.maximum() // 3))
        previous_scroll = scroll_bar.value()

        history.add("New speaker", "A newly captured line")
        history.finish_current()
        dialog.refresh()

        self.assertEqual(dialog.current_entry().id, selected_id)
        self.assertEqual(scroll_bar.value(), previous_scroll)
        self.assertIn("Speaker 8", dialog.details.toPlainText())
        dialog.close()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
