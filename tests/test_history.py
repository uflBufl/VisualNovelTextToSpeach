import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import numpy as np

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

from vntts.audio_output import PcmAdmissionRejected, resolve_audio_output  # noqa: E402
from vntts.history import DialogueHistory  # noqa: E402
from vntts.history_ui import DialogueHistoryDialog  # noqa: E402
from vntts.playback import ReplayAudio  # noqa: E402
from vntts.playback_observer import (  # noqa: E402
    playback_preparation_context,
    playback_started_context,
)
from vntts.support import PerformanceLog  # noqa: E402


class DialogueHistoryTest(unittest.TestCase):
    def test_export_groups_played_chunks_by_utterance_and_preserves_repetitions(self):
        history = DialogueHistory()
        first = history.add("Marcus", "Wait.", utterance=("run", 1), ordinal=1)
        history.add("Marcus", "Listen.", utterance=("run", 1), ordinal=2)
        history.add("Marcus", "Listen.", utterance=("run", 1), ordinal=2)
        history.add("Marcus", "Wait.", utterance=("run", 2), ordinal=1)

        with TemporaryDirectory() as directory:
            path = history.export(Path(directory) / "history.json")
            rows = json.loads(path.read_text())["entries"]
        self.assertEqual([row["text"] for row in rows], ["Wait. Listen.", "Wait."])
        self.assertEqual(rows[0]["id"], first.id)
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])

    def test_recordings_keep_order_upgrade_after_resume_and_evict_audio_separately(
        self,
    ):
        history = DialogueHistory(maximum_audio_bytes=12)
        first = history.add("Ada", "First.", utterance=("run", 1), ordinal=1)
        prefix = ReplayAudio(np.ones(1, np.float32), 24000, complete=False)
        full = ReplayAudio(np.ones(2, np.float32), 24000)
        history.retain_audio(first.id, 1, prefix)
        history.retain_audio(first.id, 1, full)
        history.retain_audio(first.id, 1, prefix)
        history.add("Ada", "Second.", utterance=("run", 1), ordinal=2)
        self.assertEqual(history.recording(first.id), ())
        history.retain_audio(first.id, 2, prefix)
        clips = history.recording(first.id)
        self.assertIs(clips[0], full)
        self.assertIs(clips[1], prefix)

        second = history.add("Ada", "Next.")
        history.retain_audio(second.id, None, full)
        self.assertEqual(history.recording(first.id), ())
        self.assertIs(history.recording(second.id)[0], full)
        self.assertEqual(
            [e.text for e in history.snapshot()], ["First. Second.", "Next."]
        )
        with TemporaryDirectory() as directory:
            export = json.loads(
                history.export(Path(directory) / "history.json").read_text()
            )
        self.assertEqual(
            set(export["entries"][1]), {"id", "recorded_at", "character", "text"}
        )

    def test_full_history_keeps_both_recent_lines_even_when_they_are_similar(self):
        history = DialogueHistory(maximum_entries=2)
        history.add("Lucy", "An older line.")
        first = history.add("Marcus", "The suitcase is ready for departure.")
        second = history.add("Marcus", "The suitcase is ready for inspection.")

        self.assertEqual(history.snapshot(), [first, second])
        self.assertNotEqual(first.id, second.id)


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

        self.assertEqual(dialog.search.accessibleName(), "Search history")
        self.assertEqual(dialog.search_label.buddy(), dialog.search)
        self.assertEqual(dialog.details.accessibleName(), "Selected dialogue details")
        self.assertEqual(dialog.details_label.buddy(), dialog.details)
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertTrue(dialog.replay_button.isHidden())
        self.assertEqual(dialog.replay_button.text(), "Original")
        self.assertEqual(dialog.replay_button.accessibleName(), "Play original")
        self.assertFalse(dialog.current_voice_button.isEnabled())
        self.assertFalse(dialog.current_voice_button.isHidden())
        self.assertEqual(dialog.current_voice_button.text(), "With current settings")
        self.assertEqual(
            dialog.current_voice_button.accessibleName(),
            "Speak with current settings",
        )
        self.assertFalse(hasattr(dialog, "stop_button"))
        self.assertEqual(dialog.export_button.text(), "Export...")
        self.assertEqual(dialog.export_button.accessibleName(), "Export history")
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertIn("No dialogue has played", dialog.empty_message.text())
        self.assertEqual(dialog.status.toPlainText(), "")

    def test_whitespace_search_preserves_empty_session_guidance(self):
        dialog = self.create_dialog(DialogueHistory(), Mock())

        dialog.search.setText("   ")

        self.assertIn("No dialogue has played", dialog.empty_message.text())
        self.assertEqual(dialog.status.toPlainText(), "")
        self.assertFalse(dialog.export_button.isEnabled())

    def test_searches_and_replays_selected_entry(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        history.add("Lucy", "Good morning.")
        replay = Mock()
        dialog = self.create_dialog(history, replay)

        dialog.search.setText("SUITCASE")
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.replay_runner.active)

        self.assertEqual(dialog.entries.count(), 1)
        replay.assert_called_once_with("Marcus", "The suitcase is ready.")

    def test_recorded_line_plays_without_synthesis_and_can_be_read_in_current_voice(
        self,
    ):
        history = DialogueHistory()
        entry = history.add("Marcus", "Hello.")
        clip = ReplayAudio(np.ones(3, np.float32), 24000, complete=False)
        history.retain_audio(entry.id, None, clip)
        started = Event()

        def play(clips, cancelled):
            self.assertIs(clips[0], clip)
            started.set()
            return not cancelled.wait(3)

        voice, stop_voice = Mock(), Mock()
        dialog = self.create_dialog(
            history, voice, stop_handler=stop_voice, play_recording=play
        )
        self.assertEqual(dialog.replay_button.text(), "Original (excerpt)")
        dialog.replay_button.click()
        self.wait_for(started.is_set)
        self.assertIn("original excerpt", dialog.status.toPlainText())
        dialog.replay_button.click()
        self.wait_for(lambda: not dialog.replay_runner.active)
        self.assertIn("stopped", dialog.status.toPlainText())
        stop_voice.assert_not_called()
        voice.assert_not_called()
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.replay_runner.active)
        voice.assert_called_once_with("Marcus", "Hello.")

    def test_line_without_recording_can_only_be_read_aloud(self):
        history = DialogueHistory()
        history.add("Marcus", "Hello.")
        voice = Mock()
        dialog = self.create_dialog(history, voice, play_recording=Mock())

        self.assertTrue(dialog.replay_button.isHidden())
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertFalse(dialog.current_voice_button.isHidden())
        self.assertTrue(dialog.current_voice_button.isEnabled())
        dialog.replay_button.click()
        self.application.processEvents()
        voice.assert_not_called()

        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.replay_runner.active)
        voice.assert_called_once_with("Marcus", "Hello.")

    def test_each_action_keeps_its_stop_control_when_search_hides_selection(self):
        for recorded in (False, True):
            with self.subTest(recorded=recorded):
                history = DialogueHistory()
                entry = history.add("Marcus", "Hello.")
                history.retain_audio(
                    entry.id, None, ReplayAudio(np.ones(3, np.float32), 24000)
                )
                started, release = Event(), Event()
                self.addCleanup(release.set)

                def replay(*_args):
                    started.set()
                    release.wait(3)

                def stop():
                    release.wait(3)
                    return True

                voice = Mock(side_effect=replay)
                recording = Mock(side_effect=replay)
                dialog = self.create_dialog(
                    history, voice, stop_handler=stop, play_recording=recording
                )
                origin = (
                    dialog.replay_button if recorded else dialog.current_voice_button
                )
                other = (
                    dialog.current_voice_button if recorded else dialog.replay_button
                )
                self.assertEqual(dialog.replay_button.text(), "Original")
                self.assertTrue(dialog.replay_button.isEnabled())
                self.assertTrue(dialog.current_voice_button.isEnabled())
                dialog.show()
                self.application.processEvents()
                idle_geometry = origin.geometry()
                origin.click()
                self.application.processEvents()
                self.assertGreaterEqual(origin.width(), idle_geometry.width())
                self.assertEqual(origin.x(), idle_geometry.x())
                self.assertEqual(origin.text(), "Stop")
                self.assertEqual(origin.property("playbackState"), "preparing")
                self.wait_for(started.is_set)
                self.assertTrue(origin.isEnabled())
                self.assertFalse(other.isEnabled())
                (voice if recorded else recording).assert_not_called()
                dialog.set_playing(True)
                self.assertEqual(origin.property("playbackState"), "playing")
                self.assertEqual(other.property("playbackState"), "idle")

                dialog.search.setText("missing")
                self.assertIsNone(dialog.current_entry())
                self.assertFalse(origin.isHidden())
                self.assertFalse(other.isHidden())
                self.assertTrue(origin.isEnabled())
                self.assertFalse(other.isEnabled())
                self.assertEqual(origin.text(), "Stop")
                origin.click()
                self.assertEqual(origin.text(), "Stopping...")
                self.assertEqual(origin.property("playbackState"), "preparing")
                self.assertFalse(origin.isEnabled())
                self.assertFalse(other.isEnabled())
                self.assertFalse(origin.isHidden())
                self.assertFalse(other.isHidden())
                release.set()
                self.wait_for(
                    lambda: (
                        not dialog.replay_runner.active
                        and not dialog.stop_runner.active
                    )
                )
                self.assertEqual(origin.property("playbackState"), "idle")
                self.assertTrue(dialog.replay_button.isHidden())
                self.assertFalse(dialog.current_voice_button.isHidden())
                self.assertFalse(dialog.current_voice_button.isEnabled())
                self.assertEqual(
                    dialog.current_voice_button.text(), "With current settings"
                )
                self.assertEqual(dialog.replay_button.text(), "Original")

    def test_current_voice_stop_failure_can_be_retried_on_the_same_button(self):
        history = DialogueHistory()
        history.add("Marcus", "Hello.")
        started, release = Event(), Event()
        self.addCleanup(release.set)

        def replay(*_args):
            started.set()
            release.wait(3)

        stop = Mock(side_effect=(OSError("stop failed"), True))
        dialog = self.create_dialog(history, replay, stop_handler=stop)
        dialog.current_voice_button.click()
        self.wait_for(started.is_set)
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.stop_runner.active)

        self.assertIn("stop failed", dialog.status.toPlainText())
        self.assertEqual(dialog.current_voice_button.text(), "Stop")
        self.assertTrue(dialog.current_voice_button.isEnabled())
        self.assertFalse(dialog.replay_button.isEnabled())
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.replay_runner.active)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(dialog.current_voice_button.text(), "With current settings")
        release.set()

    def test_current_voice_without_stop_handler_stays_disabled_until_finished(self):
        history = DialogueHistory()
        history.add("Marcus", "Hello.")
        release = Event()
        self.addCleanup(release.set)
        dialog = self.create_dialog(history, lambda *_args: release.wait(3))

        dialog.current_voice_button.click()
        self.assertFalse(dialog.current_voice_button.isEnabled())
        self.assertEqual(dialog.current_voice_button.text(), "Preparing...")
        dialog.set_playing(True)
        self.assertEqual(dialog.current_voice_button.text(), "Playing...")
        self.assertFalse(dialog.replay_button.isEnabled())
        release.set()
        self.wait_for(lambda: not dialog.replay_runner.active)
        self.assertTrue(dialog.current_voice_button.isEnabled())

    def test_slow_replay_keeps_qt_responsive_and_reports_completion(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        started, finished = Event(), Event()
        release = Event()
        self.addCleanup(release.set)

        def replay(_character, _text):
            started.set()
            release.wait(3)
            finished.set()

        stop = Mock(return_value=True)
        dialog = self.create_dialog(history, replay, stop_handler=stop)
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append("painted"))

        dialog.current_voice_button.click()
        self.wait_for(lambda: started.is_set() and bool(heartbeat))

        self.assertFalse(finished.is_set())
        self.assertTrue(dialog.replay_runner.active)
        self.assertTrue(dialog.current_voice_button.isEnabled())
        self.assertFalse(dialog.replay_button.isEnabled())
        self.assertIn("Preparing speech for Marcus", dialog.status.toPlainText())
        close_event = QCloseEvent()
        dialog.closeEvent(close_event)
        self.assertFalse(close_event.isAccepted())
        self.assertIn("Stopping", dialog.status.toPlainText())

        self.wait_for(lambda: not dialog.replay_runner.active)
        stop.assert_called_once_with()
        self.assertTrue(dialog.current_voice_button.isEnabled())
        self.assertEqual(dialog.status.toPlainText(), "Speech stopped for Marcus.")
        release.set()

    def test_cold_preview_reports_real_stages_until_audio_starts(self):
        history = DialogueHistory()
        history.add("Hedone", "Private story text.")
        release = Event()
        self.addCleanup(release.set)
        observers = []

        def replay(*_args):
            observers.append(
                (playback_preparation_context.get(), playback_started_context.get())
            )
            release.wait(3)

        performance = PerformanceLog()
        dialog = self.create_dialog(history, replay)
        with patch("vntts.support.performance_log", performance):
            dialog.current_voice_button.click()
            self.wait_for(lambda: bool(observers))
            prepare, onset = observers[0]
            for phase, label in (
                ("loading-model", "Loading Pocket TTS..."),
                ("worker-ready", "Preparing speech for Hedone..."),
                ("preparing-voice", "Preparing the voice for Hedone..."),
                ("generating-first-chunk", "Generating speech for Hedone..."),
                ("first-chunk-ready", "Starting audio for Hedone..."),
            ):
                prepare(phase)
                self.wait_for(lambda: dialog.status.toPlainText() == label)
            onset()
            self.wait_for(
                lambda: dialog.status.toPlainText() == "Speaking as Hedone..."
            )
            prepare("loading-model")
            self.application.processEvents()
            self.assertEqual(dialog.status.toPlainText(), "Speaking as Hedone...")
            release.set()
            self.wait_for(lambda: not dialog.replay_runner.active)

        events = performance.report()["events"]
        (replay_event,) = (
            e for e in events if e["operation"] == "history-current-voice"
        )
        offsets = [
            replay_event[key]
            for key in (
                "task_started_at_ms",
                "loading_model_at_ms",
                "worker_ready_at_ms",
                "preparing_voice_at_ms",
                "generating_first_chunk_at_ms",
                "first_chunk_ready_at_ms",
                "first_audio_ms",
                "elapsed_ms",
            )
        ]
        self.assertEqual(offsets, sorted(offsets))
        self.assertNotIn("waiting_for_worker_at_ms", replay_event)
        self.assertNotIn("Hedone", str(replay_event))
        self.assertNotIn("Private story text", str(replay_event))

    def test_current_voice_button_prepares_until_playback_and_stops_active_speech(self):
        history = DialogueHistory()
        history.add("Marcus", "Hello.")
        release = Event()
        self.addCleanup(release.set)
        dialog = self.create_dialog(
            history,
            lambda *_args: release.wait(3),
            stop_handler=Mock(return_value=True),
        )
        dialog.current_voice_button.click()
        self.assertEqual(
            dialog.current_voice_button.property("playbackState"), "preparing"
        )
        dialog.set_playing(True)
        self.assertEqual(
            dialog.current_voice_button.property("playbackState"), "playing"
        )
        self.assertEqual(dialog.current_voice_button.text(), "Stop")
        dialog.current_voice_button.click()
        self.assertEqual(
            dialog.current_voice_button.property("playbackState"), "preparing"
        )
        self.wait_for(lambda: not dialog.stop_runner.active)
        self.assertEqual(dialog.current_voice_button.property("playbackState"), "idle")
        dialog.set_playing(True)
        self.assertEqual(dialog.current_voice_button.property("playbackState"), "idle")
        self.assertIn("Speech stopped", dialog.status.toPlainText())

    def test_late_onset_from_stopped_replay_cannot_mark_retry_playing(self):
        history = DialogueHistory()
        history.add("Marcus", "Hello.")
        release = Event()
        self.addCleanup(release.set)
        onsets = []

        def replay(*_args):
            onsets.append(playback_started_context.get())
            release.wait(3)

        dialog = self.create_dialog(
            history, replay, stop_handler=Mock(return_value=True)
        )
        dialog.current_voice_button.click()
        self.wait_for(lambda: len(onsets) == 1)
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.stop_runner.active)
        dialog.current_voice_button.click()
        self.wait_for(lambda: len(onsets) == 2)
        onsets[0]()
        self.application.processEvents()
        self.assertEqual(
            dialog.current_voice_button.property("playbackState"), "preparing"
        )
        onsets[1]()
        self.wait_for(
            lambda: dialog.current_voice_button.property("playbackState") == "playing"
        )
        release.set()
        self.wait_for(lambda: not dialog.replay_runner.active)
        self.assertEqual(dialog.current_voice_button.property("playbackState"), "idle")

    def test_stopping_during_synthesis_prevents_late_first_pcm(self):
        history = DialogueHistory()
        history.add("Marcus", "Hello.")
        started, release, rejected = Event(), Event(), Event()
        self.addCleanup(release.set)
        output, native = Mock(), Mock()
        native.__enter__ = Mock(return_value=native)
        native.__exit__ = Mock(return_value=False)
        output.OutputStream.return_value = native

        def replay(*_args):
            started.set()
            release.wait(3)
            try:
                with resolve_audio_output(output).OutputStream(
                    samplerate=24000, channels=1, dtype="float32", latency="low"
                ) as stream:
                    stream.write(np.ones((4, 1)))
            except PcmAdmissionRejected:
                rejected.set()

        dialog = self.create_dialog(
            history, replay, stop_handler=Mock(return_value=True)
        )
        dialog.current_voice_button.click()
        self.wait_for(started.is_set)
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.stop_runner.active)
        release.set()
        self.assertTrue(self.thread_pool.waitForDone(3000))
        self.assertTrue(rejected.is_set(), "Stopped replay did not reject late audio")
        native.write.assert_not_called()
        self.assertEqual(dialog.current_voice_button.property("playbackState"), "idle")

    def test_speech_keeps_its_subject_when_selection_and_search_change(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                history = DialogueHistory()
                text = "The suitcase is ready."
                history.add("Marcus", text)
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
                dialog.current_voice_button.click()
                self.wait_for(started.is_set)
                dialog.entries.setCurrentRow(1)

                self.assertEqual(dialog.current_entry().character, "Lucy")
                self.assertIn(
                    "Preparing speech for Marcus", dialog.status.toPlainText()
                )
                self.assertIn(text, dialog.status.toolTip())
                dialog.search.setText("missing")

                self.assertIn("No matching lines", dialog.empty_message.text())
                self.assertIn(
                    "Preparing speech for Marcus", dialog.status.toPlainText()
                )
                self.assertTrue(dialog.current_voice_button.isEnabled())
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
                self.assertTrue(dialog.current_voice_button.isEnabled())
                self.assertFalse(dialog.replay_button.isEnabled())

    def test_stop_confirmation_closes_even_if_replay_future_is_unresponsive(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        started = Event()
        release = Event()
        self.addCleanup(release.set)

        def replay(_character, _text):
            started.set()
            release.wait(3)

        stop = Mock(return_value=True)
        dialog = self.create_dialog(history, replay, stop_handler=stop)
        dialog.show()
        dialog.current_voice_button.click()
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
                dialog.current_voice_button.click()
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
                    self.assertTrue(dialog.current_voice_button.isEnabled())
                    self.assertFalse(dialog.replay_button.isEnabled())
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
        dialog.current_voice_button.click()
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
        dialog.current_voice_button.click()
        self.wait_for(started.is_set)

        dialog.close()
        self.wait_for(lambda: not dialog.stop_runner.active)

        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.replay_runner.active)
        self.assertIn("cannot stop playback", dialog.status.toPlainText())
        self.assertFalse(dialog.current_voice_button.isEnabled())
        self.assertFalse(dialog.replay_button.isEnabled())
        dialog.close()
        self.assertEqual(stop.call_count, 1)
        release.set()
        self.wait_for(lambda: not dialog.isVisible())

    def test_keyboard_browses_copies_speaks_stops_and_closes_history(self):
        history = DialogueHistory()
        text = " ".join(["The suitcase is ready."] * 100)
        history.add("Marcus", text)
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

        self.tab_to(dialog.entries)
        QTest.keyClick(dialog.entries, Qt.Key.Key_Up)
        self.assertEqual(dialog.current_entry().character, "Marcus")
        self.tab_to(dialog.details)
        self.assert_details_copy_and_scroll(dialog)
        self.tab_to(dialog.current_voice_button)
        QTest.keyClick(dialog.current_voice_button, Qt.Key.Key_Space)
        self.wait_for(replay_started.is_set)
        replay_handler.assert_called_once_with("Marcus", text)

        self.tab_to(dialog.entries)
        QTest.keyClick(dialog.entries, Qt.Key.Key_Down)
        self.assertEqual(dialog.current_entry().character, "Lucy")
        self.assertIn("Preparing speech for Marcus", dialog.status.toPlainText())
        self.assertIn(text, dialog.status.toolTip())
        self.tab_to(dialog.current_voice_button)
        QTest.keyClick(dialog.current_voice_button, Qt.Key.Key_Space)
        self.wait_for(stop_started.is_set)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertTrue(dialog.isVisible())
        self.assertIn("Stopping", dialog.status.toPlainText())
        stop_release.set()
        self.wait_for(lambda: not dialog.isVisible())
        stop_handler.assert_called_once_with()
        self.assertEqual(dialog.status.toPlainText(), "Speech stopped for Marcus.")

    def tab_to(self, widget):
        for _ in range(12):
            if widget.hasFocus():
                return
            QTest.keyClick(self.application.focusWidget(), Qt.Key.Key_Tab)
        self.assertTrue(widget.hasFocus(), "Widget is unreachable using Tab")

    def assert_details_copy_and_scroll(self, dialog):
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

    def test_long_replay_failure_can_be_read_copied_and_retried(self):
        history = DialogueHistory()
        history.add("Marcus", "The suitcase is ready.")
        error = "Speech backend returned a detailed failure. " * 80
        replay = Mock(side_effect=(RuntimeError(error), None))
        dialog = self.create_dialog(history, replay)
        font = dialog.font()
        font.setPointSize(16)
        dialog.setFont(font)
        dialog.resize(520, 400)
        dialog.show()
        dialog.current_voice_button.click()
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

        self.assertIn("try again", dialog.status.toPlainText())
        dialog.current_voice_button.click()
        self.wait_for(lambda: not dialog.replay_runner.active)
        self.assertEqual(dialog.status.toPlainText(), "Speech finished for Marcus.")
        self.assertEqual(replay.call_count, 2)

    def test_export_keeps_the_whole_session_with_one_or_no_search_matches(self):
        history = DialogueHistory()
        marcus = history.add("Marcus", "The suitcase is ready.")
        lucy = history.add("Lucy", "Good morning.")
        dialog = self.create_dialog(history, Mock())

        with TemporaryDirectory() as directory:
            text_path = Path(directory) / "one-match"
            json_path = Path(directory) / "no-match"
            with patch(
                "vntts.history_ui.QFileDialog.getSaveFileName",
                side_effect=[
                    (str(text_path), "Text files (*.txt)"),
                    (str(json_path), "JSON files (*.json)"),
                ],
            ):
                dialog.search.setText("lucy")
                self.assertEqual(dialog.current_entry(), lucy)
                self.assertEqual(dialog.entries.count(), 1)
                dialog.export_button.click()
                dialog.search.setText("missing")
                self.assertEqual(dialog.entries.count(), 0)
                self.assertFalse(dialog.current_voice_button.isEnabled())
                self.assertIn("No matching lines", dialog.empty_message.text())
                self.assertEqual(dialog.status.toPlainText(), "")
                dialog.export_button.click()

            self.assertEqual(
                text_path.with_suffix(".txt").read_text(encoding="utf-8"),
                f"[{marcus.recorded_at}] Marcus\nThe suitcase is ready.\n\n"
                f"[{lucy.recorded_at}] Lucy\nGood morning.\n",
            )
            exported = json.loads(
                json_path.with_suffix(".json").read_text(encoding="utf-8")
            )
            self.assertEqual(exported["version"], 1)
            self.assertEqual(
                exported["entries"],
                [
                    {
                        "id": marcus.id,
                        "recorded_at": marcus.recorded_at,
                        "character": "Marcus",
                        "text": "The suitcase is ready.",
                    },
                    {
                        "id": lucy.id,
                        "recorded_at": lucy.recorded_at,
                        "character": "Lucy",
                        "text": "Good morning.",
                    },
                ],
            )

    def test_refresh_keeps_scroll_until_the_selected_line_expires(self):
        history = DialogueHistory(maximum_entries=40)
        for index in range(40):
            history.add(f"Speaker {index}", f"Dialogue line {index}")
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
        dialog.refresh()

        self.assertEqual(dialog.current_entry().id, selected_id)
        self.assertEqual(scroll_bar.value(), previous_scroll)
        self.assertIn("Speaker 8", dialog.details.toPlainText())

        dialog.entries.setCurrentRow(0)
        newest = history.add("Narrator", "Another line.")
        dialog.refresh()
        self.application.processEvents()

        self.assertEqual(dialog.current_entry(), newest)
        selected_rect = dialog.entries.visualItemRect(dialog.entries.currentItem())
        self.assertTrue(dialog.entries.viewport().rect().intersects(selected_rect))


if __name__ == "__main__":
    unittest.main()
