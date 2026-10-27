import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from tests.qt_task_fixtures import ManualThreadPool  # noqa: E402
from vntts.pregeneration_failures import PreparationFailure  # noqa: E402
from vntts.pregeneration_failures_ui import PreparationFailuresPanel  # noqa: E402


class Player(QObject):
    errorOccurred = Signal(object, str)
    playbackStateChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.played = []
        self.playing = False

    def play_bytes(self, payload, source):
        self.played.append(payload)
        self.playing = True
        self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PlayingState)
        return object()

    def stop(self):
        if self.playing:
            self.playing = False
            self.playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)


class PreparationFailuresPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.pool = ManualThreadPool()
        self.player = Player()
        self.owner = QWidget()
        self.panel = PreparationFailuresPanel(
            self.owner, thread_pool=self.pool, player=self.player
        )
        self.failure = PreparationFailure(
            "queue",
            "line",
            "Cornerstone",
            "<Literal> & a long phrase. " * 40,
            "Internal pause: 1.6 s (limit 1.2 s)",
            "Provider detail",
            "available",
            {"created": True},
        )
        self.inspector = patch(
            "vntts.pregeneration_failures_ui.inspect_preparation_failures",
            return_value=(self.failure,),
        ).start()
        self.reader = patch(
            "vntts.pregeneration_failures_ui.load_preparation_failure_audio",
            return_value=b"validated WAV",
        ).start()
        self.addCleanup(patch.stopall)
        self.panel.set_input(SimpleNamespace(identity="first"))
        self.owner.show()
        self.panel.refresh()
        self.drain()
        self.panel.toggle.click()
        self.drain()

    def tearDown(self):
        self.panel.stop()
        self.owner.close()
        self.owner.deleteLater()
        self.app.processEvents()

    def drain(self):
        while self.pool.tasks:
            self.pool.run_next()
            self.app.processEvents()

    def test_full_phrase_and_distinct_missing_audio_explanations(self):
        self.assertEqual(self.panel.phrase.toPlainText(), self.failure.text)
        self.assertEqual(self.panel.character.text(), "Cornerstone")
        font = self.panel.font()
        font.setPointSize(20)
        self.panel.setFont(font)
        self.app.processEvents()
        self.assertGreaterEqual(
            self.panel.phrase.viewport().height(),
            self.panel.phrase.fontMetrics().lineSpacing() * 4,
        )
        self.assertEqual(self.panel.reason.text(), self.failure.reason)
        self.assertTrue(self.panel.phrase.isReadOnly())
        self.assertTrue(self.panel.play.isEnabled())
        self.assertFalse(self.panel.stop_button.isEnabled())
        self.panel.details.click()
        self.assertEqual(self.panel.technical.toPlainText(), "Provider detail")
        for state, explanation in (
            ("not_created", "No audio was created"),
            ("unavailable", "not retained"),
        ):
            self.inspector.return_value = (replace(self.failure, audio_status=state),)
            self.panel.refresh()
            self.drain()
            self.assertIn(explanation, self.panel.audio_status.text())
            self.assertFalse(self.panel.play.isEnabled())

    def test_playback_uses_validated_bytes_and_collapse_stops_it(self):
        self.panel.play.click()
        self.assertTrue(self.panel.stop_button.isEnabled())
        self.assertFalse(self.panel.play.isEnabled())
        self.drain()
        self.assertEqual(self.player.played, [b"validated WAV"])
        self.panel.toggle.click()
        self.assertFalse(self.player.playing)

    def test_old_read_cannot_play_after_input_changes(self):
        self.panel.play.click()
        self.panel.set_input(SimpleNamespace(identity="second"))
        self.drain()
        self.assertEqual(self.player.played, [])
        self.assertTrue(self.panel.isHidden())

    def test_deferred_reading_stop_cannot_play_after_hide(self):
        self.panel.before_playback = Mock()
        self.panel.play.click()
        start = self.panel.before_playback.call_args.args[0]
        self.owner.hide()
        start()
        self.drain()
        self.assertEqual(self.player.played, [])

    def test_stale_audio_error_is_visible_and_original_phrase_preserved(self):
        self.reader.side_effect = ValueError("Failed audio checksum changed")
        self.panel.play.click()
        self.drain()
        self.assertEqual(self.player.played, [])
        self.assertIn("checksum changed", self.panel.audio_status.text())
        self.assertEqual(self.panel.phrase.toPlainText(), self.failure.text)

    def test_empty_older_snapshot_does_not_drop_final_refresh(self):
        self.inspector.side_effect = [(), (self.failure,)]
        self.panel.refresh()
        self.panel.refresh()
        self.drain()
        self.assertEqual(self.panel.failures, (self.failure,))
        self.assertFalse(self.panel.isHidden())

    def test_hidden_inspection_is_refreshed_on_return(self):
        self.panel.refresh()
        self.owner.hide()
        changed = replace(self.failure, reason="A new failure")
        self.inspector.return_value = (changed,)
        self.drain()
        self.owner.show()
        self.drain()
        self.assertEqual(self.panel.reason.text(), "A new failure")

    def test_unchanged_selected_failure_keeps_playing_when_other_rows_change(self):
        self.panel.play.click()
        self.drain()
        self.inspector.return_value = (
            self.failure,
            replace(self.failure, queue_id="other", text="Another phrase"),
        )
        self.panel.refresh()
        self.drain()
        self.assertTrue(self.player.playing)
        self.assertEqual(self.player.played, [b"validated WAV"])


if __name__ == "__main__":
    unittest.main()
