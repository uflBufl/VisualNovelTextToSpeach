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

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import QCloseEvent, QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from shiboken6 import isValid  # noqa: E402

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

    def setUp(self):
        self.thread_pool = QThreadPool()
        self.dialogs = []
        self.addCleanup(self._finish_dialogs)

    def create_dialog(self, *args, **kwargs):
        dialog = DialogueHistoryDialog(*args, thread_pool=self.thread_pool, **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def _finish_dialogs(self):
        self.assertTrue(
            self.thread_pool.waitForDone(3000), "History workers did not finish"
        )
        self.application.processEvents()
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_failed_assertion_releases_workers_before_deleting_dialogs(self):
        case = DialogueHistoryDialogTest(
            "test_slow_replay_keeps_qt_responsive_and_reports_completion"
        )
        wait_for = case.wait_for

        def fail_after_worker_starts(predicate, *args, **kwargs):
            wait_for(predicate, *args, **kwargs)
            raise AssertionError("injected failure after worker acquisition")

        case.wait_for = fail_after_worker_starts
        result = unittest.TestResult()
        case.run(result)

        self.assertEqual(len(result.failures), 1)
        self.assertIn("injected failure", result.failures[0][1])
        self.assertEqual(result.errors, [])
        self.assertEqual(case.thread_pool.activeThreadCount(), 0)
        self.assertTrue(case.thread_pool.waitForDone(0))
        self.assertTrue(case.dialogs)
        self.assertTrue(all(not isValid(dialog) for dialog in case.dialogs))

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail("Timed out waiting for dialogue replay")

    def test_search_and_details_expose_accessible_names_and_label_buddies(self):
        dialog = self.create_dialog(DialogueHistory(), Mock())

        self.assertEqual(dialog.search.accessibleName(), "Search dialogue history")
        self.assertEqual(dialog.search_label.buddy(), dialog.search)
        self.assertEqual(dialog.details.accessibleName(), "Selected dialogue details")
        self.assertEqual(dialog.details_label.buddy(), dialog.details)
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertEqual(dialog.replay_button.text(), "Speak with current voice")
        self.assertEqual(dialog.stop_button.text(), "Stop speaking")
        self.assertEqual(dialog.stop_button.accessibleName(), "Stop current speech")
        self.assertEqual(dialog.export_button.text(), "Export history...")
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertIn("No dialogue has been captured", dialog.empty_message.text())
        self.assertEqual(dialog.status.toPlainText(), "")

    def test_whitespace_search_preserves_empty_session_guidance(self):
        dialog = self.create_dialog(DialogueHistory(), Mock())

        dialog.search.setText("   ")

        self.assertIn("No dialogue has been captured", dialog.empty_message.text())
        self.assertEqual(dialog.status.toPlainText(), "")
        self.assertFalse(dialog.export_button.isEnabled())

    def test_searches_and_replays_selected_entry(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        history.add("Lucy", "Good morning.")
        replay = Mock()
        dialog = self.create_dialog(history, replay)

        dialog.search.setText("suitcase")
        dialog.refresh()
        dialog.replay_selected()
        self.wait_for(lambda: not dialog.replay_runner.active)

        self.assertEqual(dialog.entries.count(), 1)
        replay.assert_called_once_with("Marcus", "The suitcase is ready.")

    def test_exports_with_extension_selected_by_user(self):
        history = Mock()
        history.search.return_value = []
        dialog = self.create_dialog(history, Mock())

        with patch(
            "vntts.history_ui.QFileDialog.getSaveFileName",
            return_value=("session", "JSON files (*.json)"),
        ):
            dialog.export_history()

        history.export.assert_called_once_with("session.json")

    def test_slow_replay_keeps_qt_responsive_and_reports_completion(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        started = Event()
        release = Event()
        self.addCleanup(release.set)

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=True)
        dialog = self.create_dialog(history, replay, stop_handler=stop)
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
        self.assertIn("Speaking as Marcus", dialog.status.toPlainText())
        close_event = QCloseEvent()
        dialog.closeEvent(close_event)
        self.assertFalse(close_event.isAccepted())
        self.assertIn("Stopping", dialog.status.toPlainText())

        self.wait_for(lambda: not dialog.replay_runner.active)
        stop.assert_called_once_with()
        self.assertTrue(dialog.replay_button.isEnabled())
        self.assertEqual(dialog.status.toPlainText(), "Speech stopped for Marcus.")
        release.set()

    def test_speech_keeps_its_subject_when_selection_and_search_change(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                history = DialogueHistory()
                text = "The suitcase is ready."
                history.add("Marcus", text)
                history.finish_current()
                history.add("Lucy", "Good morning.")
                started = Event()
                release = Event()
                self.addCleanup(release.set)

                def replay(_character, _text):
                    started.set()
                    release.wait(3)
                    if fails:
                        raise RuntimeError("backend unavailable")

                dialog = self.create_dialog(history, replay, stop_handler=Mock())
                dialog.entries.setCurrentRow(0)
                dialog.replay_selected()
                self.wait_for(started.is_set)
                dialog.entries.setCurrentRow(1)

                self.assertEqual(dialog.current_entry().character, "Lucy")
                self.assertIn("Speaking as Marcus", dialog.status.toPlainText())
                self.assertIn(text, dialog.status.toolTip())
                dialog.search.setText("missing")

                self.assertIn("No matching lines", dialog.empty_message.text())
                self.assertIn("Speaking as Marcus", dialog.status.toPlainText())
                self.assertTrue(dialog.stop_button.isEnabled())
                release.set()
                self.wait_for(lambda: not dialog.replay_runner.active)

                self.assertIn("No matching lines", dialog.empty_message.text())
                self.assertIn(text, dialog.status.toolTip())
                if fails:
                    self.assertIn(
                        "Could not speak as Marcus:", dialog.status.toPlainText()
                    )
                    self.assertIn("backend unavailable", dialog.status.toPlainText())
                else:
                    self.assertEqual(
                        dialog.status.toPlainText(), "Speech finished for Marcus."
                    )
                speech_status = dialog.status.toPlainText()
                dialog.search.clear()
                self.assertEqual(dialog.status.toPlainText(), speech_status)
                self.assertTrue(dialog.replay_button.isEnabled())
                self.assertFalse(dialog.stop_button.isEnabled())

    def test_stop_confirmation_closes_even_if_replay_future_is_unresponsive(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        started = Event()
        release = Event()
        self.addCleanup(release.set)

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=True)
        dialog = self.create_dialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(started.is_set)

        dialog.close()
        self.wait_for(lambda: not dialog.isVisible())

        stop.assert_called_once_with()
        self.assertFalse(dialog.replay_runner.active)
        release.set()

    def test_close_waits_for_stop_even_when_replay_finishes_first(self):
        for outcome in (
            "early-close",
            "late-close",
            "failed-stop",
            "unsupported-after-finish",
            "unsupported-after-failure",
        ):
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
                    return not outcome.startswith("unsupported-after-")

                def replay(*_args):
                    replay_release.wait(3)
                    if outcome == "unsupported-after-failure":
                        raise RuntimeError("voice file missing")

                self.addCleanup(stop_release.set)
                self.addCleanup(replay_release.set)
                dialog = self.create_dialog(history, replay, stop_handler=stop)
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
                self.assertIn("Stopping", dialog.status.toPlainText())
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
                    self.assertIn("stop failed", dialog.status.toPlainText())
                    self.assertTrue(dialog.replay_button.isEnabled())
                    self.assertFalse(dialog.stop_button.isEnabled())
                    dialog.close()
                elif outcome == "unsupported-after-finish":
                    self.assertEqual(
                        dialog.status.toPlainText(), "Speech finished for Marcus."
                    )
                elif outcome == "unsupported-after-failure":
                    self.assertIn(
                        "Could not speak as Marcus", dialog.status.toPlainText()
                    )
                    self.assertIn("voice file missing", dialog.status.toPlainText())
                self.assertFalse(dialog.isVisible())

    def test_escape_stops_active_replay_before_closing(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        started = Event()
        release = Event()
        self.addCleanup(release.set)
        finished = Event()

        def replay(_character, _text):
            started.set()
            release.wait(3)
            finished.set()

        stop = Mock(return_value=True)
        dialog = self.create_dialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(started.is_set)

        QTest.keyClick(dialog, Qt.Key.Key_Escape)

        self.wait_for(lambda: not dialog.isVisible())
        stop.assert_called_once_with()
        release.set()
        self.wait_for(finished.is_set)

    def test_unsupported_stop_keeps_dialog_open_until_speech_finishes(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        started = Event()
        release = Event()
        self.addCleanup(release.set)

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=False)
        dialog = self.create_dialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(started.is_set)

        dialog.close()
        self.wait_for(lambda: not dialog.stop_runner.active)

        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.replay_runner.active)
        self.assertIn("cannot stop playback", dialog.status.toPlainText())
        dialog.close()
        self.assertEqual(stop.call_count, 1)
        release.set()
        self.wait_for(lambda: not dialog.isVisible())

    def test_keyboard_browses_copies_speaks_stops_and_closes_history(self):
        history = DialogueHistory()
        text = " ".join(["The suitcase is ready."] * 100)
        history.add("Marcus", text)
        history.finish_current()
        history.add("Lucy", "Good morning.")
        replay_started = Event()
        replay_release = Event()
        stop_started = Event()
        stop_release = Event()
        self.addCleanup(replay_release.set)
        self.addCleanup(stop_release.set)

        def replay(_character, _text):
            replay_started.set()
            replay_release.wait(3)

        def stop():
            stop_started.set()
            stop_release.wait(3)
            replay_release.set()
            return True

        replay_handler = Mock(side_effect=replay)
        stop_handler = Mock(side_effect=stop)
        dialog = self.create_dialog(history, replay_handler, stop_handler=stop_handler)
        dialog.show()
        dialog.search.setFocus()
        self.application.processEvents()

        def tab_to(widget):
            for _ in range(12):
                if widget.hasFocus():
                    return
                QTest.keyClick(self.application.focusWidget(), Qt.Key.Key_Tab)
            self.assertTrue(widget.hasFocus(), "Widget is unreachable using Tab")

        tab_to(dialog.entries)
        QTest.keyClick(dialog.entries, Qt.Key.Key_Up)
        self.assertEqual(dialog.current_entry().character, "Marcus")
        tab_to(dialog.details)
        QTest.keySequence(dialog.details, QKeySequence.StandardKey.SelectAll)
        QTest.keySequence(dialog.details, QKeySequence.StandardKey.Copy)
        self.assertEqual(
            self.application.clipboard().text(), dialog.details.toPlainText()
        )
        QTest.keySequence(
            dialog.details, QKeySequence.StandardKey.MoveToStartOfDocument
        )
        QTest.keyClick(dialog.details, Qt.Key.Key_PageDown)
        self.assertGreater(dialog.details.verticalScrollBar().value(), 0)
        tab_to(dialog.replay_button)
        QTest.keyClick(dialog.replay_button, Qt.Key.Key_Space)
        self.wait_for(replay_started.is_set)
        replay_handler.assert_called_once_with("Marcus", text)

        tab_to(dialog.entries)
        QTest.keyClick(dialog.entries, Qt.Key.Key_Down)
        self.assertEqual(dialog.current_entry().character, "Lucy")
        self.assertIn("Speaking as Marcus", dialog.status.toPlainText())
        self.assertIn(text, dialog.status.toolTip())
        tab_to(dialog.stop_button)
        QTest.keyClick(dialog.stop_button, Qt.Key.Key_Space)
        self.wait_for(stop_started.is_set)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertTrue(dialog.isVisible())
        self.assertIn("Stopping", dialog.status.toPlainText())
        stop_release.set()
        self.wait_for(lambda: not dialog.isVisible())
        stop_handler.assert_called_once_with()
        self.assertEqual(dialog.status.toPlainText(), "Speech stopped for Marcus.")

    def test_replay_failure_is_retryable_in_dialog(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        replay = Mock(side_effect=RuntimeError("backend unavailable"))
        dialog = self.create_dialog(history, replay)

        dialog.replay_selected()
        self.wait_for(lambda: not dialog.replay_runner.active)

        self.assertIn("try again", dialog.status.toPlainText())
        self.assertTrue(dialog.replay_button.isEnabled())

    def test_long_replay_failure_can_be_scrolled_and_copied(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        error = "Speech backend returned a detailed failure. " * 80
        dialog = self.create_dialog(history, Mock(side_effect=RuntimeError(error)))
        font = dialog.font()
        font.setPointSize(16)
        dialog.setFont(font)
        dialog.resize(520, 400)
        dialog.show()
        dialog.replay_selected()
        self.wait_for(lambda: not dialog.replay_runner.active)

        self.assertTrue(dialog.status.isReadOnly())
        self.assertIn(error, dialog.status.toPlainText())
        dialog.status.setFocus()
        QTest.keySequence(dialog.status, QKeySequence.StandardKey.MoveToStartOfDocument)
        QTest.keyClick(dialog.status, Qt.Key.Key_PageDown)
        self.assertGreater(dialog.status.verticalScrollBar().value(), 0)
        QTest.keySequence(dialog.status, QKeySequence.StandardKey.SelectAll)
        QTest.keySequence(dialog.status, QKeySequence.StandardKey.Copy)
        self.assertEqual(
            self.application.clipboard().text(), dialog.status.toPlainText()
        )

    def test_no_search_results_keep_session_export_available(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        dialog = self.create_dialog(history, Mock())

        dialog.search.setText("missing")

        self.assertEqual(dialog.entries.count(), 0)
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("No matching lines", dialog.empty_message.text())
        self.assertEqual(dialog.status.toPlainText(), "")

    def test_export_includes_whole_session_while_history_is_filtered(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.finish_current()
        history.add("Lucy", "Good morning.")
        dialog = self.create_dialog(history, Mock())
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "history.json"
            for query in ("Lucy", "missing"):
                with self.subTest(query=query):
                    dialog.search.setText(query)
                    with patch(
                        "vntts.history_ui.QFileDialog.getSaveFileName",
                        return_value=(str(path), "JSON files (*.json)"),
                    ):
                        dialog.export_history()
                    exported = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(len(exported["entries"]), 2)

    def test_refresh_preserves_older_selection_and_scroll_position(self):
        history = DialogueHistory()
        for index in range(40):
            history.add(f"Speaker {index}", f"Dialogue line {index}")
            history.finish_current()
        dialog = self.create_dialog(history, Mock())
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


if __name__ == "__main__":
    unittest.main()
