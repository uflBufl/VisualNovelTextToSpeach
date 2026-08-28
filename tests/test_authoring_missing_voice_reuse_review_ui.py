import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from tests.test_authoring_missing_voice_reuse_review import (
        AuthoringMissingVoiceReuseReviewTest,
    )
    from vntts.authoring.missing_voice_reuse_review import (
        build_missing_voice_reuse_review,
        load_missing_voice_reuse_review,
        record_missing_voice_reuse_decision,
    )
    from vntts.authoring.missing_voice_reuse_review_ui import (
        MissingVoiceReuseReviewDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QMediaPlayer = None
    MissingVoiceReuseReviewDialog = None


@unittest.skipIf(QApplication is None, "PySide6 is optional")
class AuthoringMissingVoiceReuseReviewUiTest(unittest.TestCase):
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
        self.fail("Timed out waiting for missing-voice review UI")

    def create_review(self, root):
        plan_path, evidence, snapshots, queue_id = (
            AuthoringMissingVoiceReuseReviewTest().fixture(root)
        )
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            side_effect=lambda _plan, _candidate, path: snapshots[
                Path(path).resolve()
            ],
        ):
            session = build_missing_voice_reuse_review(
                plan_path, evidence, root / "review", seed=7
            )
        return session, queue_id

    def finish_current_audio(self, dialog):
        dialog._playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
        dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        self.wait_for(
            lambda: not dialog.heard_runner.active and not dialog._pending_heard
        )

    def test_failed_arm_is_visible_and_replay_stays_available_during_save(self):
        release = Event()
        started = Event()

        def slow_decision(*arguments):
            started.set()
            release.wait(3)
            return record_missing_voice_reuse_decision(*arguments)

        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            dialog = MissingVoiceReuseReviewDialog(
                session_path, decision_recorder=slow_decision
            )
            dialog.player = Mock()
            bundle, _session = load_missing_voice_reuse_review(session_path)
            generated = next(
                candidate
                for candidate in bundle["candidates"]
                if candidate["samples"][0]["status"] == "generated"
            )
            failed = next(
                candidate
                for candidate in bundle["candidates"]
                if candidate["samples"][0]["status"] == "failed"
            )

            self.assertFalse(dialog.play_buttons[failed["label"]].isEnabled())
            self.assertIn("FAILED", dialog.arm_statuses[failed["label"]].text())
            self.assertFalse(dialog.decision_buttons[generated["label"]].isEnabled())
            dialog.play_buttons[generated["label"]].click()
            self.finish_current_audio(dialog)
            self.assertTrue(dialog.decision_buttons[generated["label"]].isEnabled())
            self.assertIn("Replay", dialog.play_buttons[generated["label"]].text())

            dialog.decision_buttons[generated["label"]].click()
            self.assertTrue(started.wait(1))
            self.application.processEvents()
            self.assertTrue(dialog.play_buttons[generated["label"]].isEnabled())
            self.assertIn("background", dialog.decision_reason.text())
            release.set()
            self.wait_for(lambda: not dialog.decision_runner.active)
            self.assertIn("Review complete", dialog.cohort_heading.text())
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
