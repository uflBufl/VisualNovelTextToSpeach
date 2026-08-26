import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    import tests.test_authoring_terminal_conflict_review as conflict_tests
    from vntts.authoring.terminal_conflict_review import (
        TerminalConflictReviewError,
        load_terminal_conflict_review_progress,
        publish_terminal_conflict_review,
        record_terminal_conflict_decision,
    )
    from vntts.authoring.terminal_conflict_review_ui import (
        TerminalConflictReviewDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    TerminalConflictReviewDialog = None


@unittest.skipIf(QApplication is None, "PySide6 is optional")
class TerminalConflictReviewUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for terminal conflict review UI")

    def finish_playback(self, dialog):
        self.wait_for(lambda: dialog._playing_candidate is not None)
        dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        self.application.processEvents()

    def create_review(self, root):
        _primary, _secondary, _queue_id, report = (
            conflict_tests.TerminalConflictReviewTest().create_fixture(root)
        )
        directory = root / "conflict-review"
        publish_terminal_conflict_review(report, directory)
        return directory

    def test_requires_both_candidates_and_saves_neither_in_background(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = TerminalConflictReviewDialog(review)
            dialog.show()
            self.application.processEvents()

            self.assertFalse(dialog.neither.isEnabled())
            dialog.play_buttons[0].click()
            self.assertFalse(dialog.neither.isEnabled())
            self.finish_playback(dialog)
            self.assertFalse(dialog.neither.isEnabled())
            dialog.play_buttons[1].click()
            self.assertFalse(dialog.neither.isEnabled())
            self.finish_playback(dialog)
            self.assertTrue(dialog.neither.isEnabled())
            self.assertIn("was approved", dialog.evidence.text().casefold())
            self.assertIn("was rejected", dialog.evidence.text().casefold())
            dialog.neither.click()
            self.assertTrue(dialog._active)
            self.assertIn("Saving in background", dialog.status.text())

            self.wait_for(lambda: not dialog._active)

            progress = load_terminal_conflict_review_progress(review)
            self.assertEqual(progress["decisions"][0]["decision"], "neither_acceptable")
            self.assertIn("All terminal conflicts", dialog.identity.text())
            dialog.close()

    def test_save_keeps_event_loop_responsive_and_defers_close(self):
        started = Event()
        release = Event()

        def slow_recorder(*args):
            started.set()
            release.wait(3)
            return record_terminal_conflict_decision(*args)

        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = TerminalConflictReviewDialog(
                review, decision_recorder=slow_recorder
            )
            dialog.show()
            dialog.play_buttons[0].click()
            self.finish_playback(dialog)
            dialog.play_buttons[1].click()
            self.finish_playback(dialog)
            dialog.choose_buttons[0].click()
            heartbeat = []
            self.application.processEvents()
            self.assertTrue(started.wait(1))
            heartbeat.append(True)
            event = QCloseEvent()
            dialog.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertTrue(dialog._close_pending)
            self.assertTrue(heartbeat)
            release.set()
            self.wait_for(lambda: not dialog._active)

    def test_copied_wav_tamper_blocks_playback_without_enabling_decision(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            document = json.loads((review / "review.json").read_text(encoding="utf-8"))
            audio = review / document["cases"][0]["candidates"][0]["audio"]
            audio.write_bytes(b"changed")
            with self.assertRaises(TerminalConflictReviewError):
                TerminalConflictReviewDialog(review)


if __name__ == "__main__":
    unittest.main()
