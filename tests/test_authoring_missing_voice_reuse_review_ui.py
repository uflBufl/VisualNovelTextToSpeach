import os
import time
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from tests.missing_voice_reuse_fixtures import (
        create_missing_voice_reuse_review_fixture,
    )
    from vntts.authoring.missing_voice_reuse_review import (
        AUTOMATIC_UNRESOLVED_ORIGIN,
        build_missing_voice_reuse_review,
        load_missing_voice_reuse_review,
        record_missing_voice_reuse_decision,
    )
    from vntts.authoring.missing_voice_reuse_review_ui import (
        MissingVoiceReuseReviewDialog,
        launch_missing_voice_reuse_review,
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

    def create_review(self, root, *, statuses=("generated", "failed")):
        plan_path, evidence, snapshots, queue_id = (
            create_missing_voice_reuse_review_fixture(root, statuses=statuses)
        )
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            side_effect=lambda _plan, _candidate, path: snapshots[Path(path).resolve()],
        ):
            session = build_missing_voice_reuse_review(
                plan_path, evidence, root / "review", seed=7
            )
        return session, queue_id

    def test_zero_choice_cohort_needs_no_human_action(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(
                Path(directory), statuses=("failed", "failed")
            )
            dialog = MissingVoiceReuseReviewDialog(session_path)

            self.assertIn("Completed 1 of 1", dialog.progress.text())
            self.assertIn("Review complete", dialog.cohort_heading.text())
            self.assertIn("automatically", dialog.sample_text.text())
            self.assertIn("No listening", dialog.sample_text.text())
            self.assertFalse(dialog.neither.isEnabled())
            self.assertTrue(
                all(not button.isEnabled() for button in dialog.play_buttons.values())
            )
            dialog.show()
            self.application.processEvents()
            self.assertFalse(dialog.sample_box.isVisible())
            self.assertFalse(dialog.decision_box.isVisible())
            self.assertFalse(dialog.decision_context.isVisible())
            self.assertTrue(
                all(not button.isVisible() for button in dialog.play_buttons.values())
            )
            self.assertTrue(
                all("Failed" in label.text() for label in dialog.arm_statuses.values())
            )
            dialog.deleteLater()

    def test_candidate_controls_expose_accessible_task_names(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            dialog = MissingVoiceReuseReviewDialog(session_path)

            buttons = tuple(dialog.play_buttons.values())
            self.assertTrue(dialog.sample_selector.accessibleName())
            self.assertTrue(dialog.sample_text.accessibleName())
            self.assertTrue(all(button.accessibleName() for button in buttons))
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.decision_box.isVisible())
            self.assertEqual(buttons[0].width(), buttons[1].width())
            dialog.deleteLater()

    def test_terminal_summary_distinguishes_mixed_recorded_outcomes(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            bundle, session = load_missing_voice_reuse_review(session_path)
            first_cohort = bundle["cohorts"][0]
            second_cohort = deepcopy(first_cohort)
            second_cohort["cohort_id"] = "automatic-family"
            bundle["cohorts"].append(second_cohort)
            session["decisions"] = [
                {"cohort_id": first_cohort["cohort_id"], "decision": "A"},
                {
                    "cohort_id": "automatic-family",
                    "decision": "neither",
                    "decision_origin": AUTOMATIC_UNRESOLVED_ORIGIN,
                },
            ]
            with patch(
                "vntts.authoring.missing_voice_reuse_review_ui._review_loader",
                return_value=(bundle, session),
            ):
                dialog = MissingVoiceReuseReviewDialog(session_path)

            self.assertEqual(dialog.cohort_heading.text(), "Review complete")
            self.assertIn("1 family decision saved", dialog.instructions.text())
            self.assertIn("1 stayed unresolved", dialog.instructions.text())
            self.assertNotIn("Neither candidate completed", dialog.instructions.text())
            dialog.deleteLater()

    def test_scaled_font_keeps_keyboard_journey_scroll_reachable(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            dialog = MissingVoiceReuseReviewDialog(session_path)
            base_point_size = dialog.font().pointSizeF()
            for scale in (1.5, 2.0, None):
                font = dialog.font()
                if scale is None:
                    font.setPixelSize(48)  # Exercise wide metrics on every host.
                else:
                    font.setPointSizeF(base_point_size * scale)
                dialog.setFont(font)
                dialog.resize(dialog.minimumSize())
                dialog.show()
                self.application.processEvents()
                self.assertEqual(
                    dialog.review_scroll.horizontalScrollBar().maximum(),
                    0,
                    (
                        font.toString(),
                        dialog.review_scroll.viewport().size(),
                        sorted(
                            (
                                widget.minimumSizeHint().width(),
                                widget.metaObject().className(),
                                widget.accessibleName(),
                            )
                            for widget in dialog.findChildren(
                                type(dialog.review_scroll.widget())
                            )
                            if widget.isVisible()
                        )[-10:],
                    ),
                )

            self.assertGreater(dialog.review_scroll.verticalScrollBar().maximum(), 0)
            self.assertTrue(dialog.close_button.isVisible())
            self.assertIs(dialog.sample_label.buddy(), dialog.sample_selector)
            self.assertEqual(
                dialog.sample_selector.toolTip(), dialog.sample_selector.currentText()
            )
            self.assertIs(
                dialog.decision_context.technical_toggle.nextInFocusChain(),
                dialog.previous,
            )
            self.assertIs(dialog.neither.nextInFocusChain(), dialog.close_button)
            for button in (
                dialog.previous,
                dialog.next,
                dialog.stop,
                *dialog.play_buttons.values(),
                *dialog.decision_buttons.values(),
                dialog.neither,
                dialog.close_button,
            ):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())
            dialog.close()

    def test_irreversible_decision_can_be_cancelled(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            dialog = MissingVoiceReuseReviewDialog(
                session_path, confirmer=lambda _decision: False
            )

            dialog._save_decision("neither")

            self.assertFalse(dialog.decision_runner.active)
            self.assertIn("cancelled", dialog.status.text())
            dialog.deleteLater()

    def test_reject_defers_until_heard_evidence_is_saved(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            dialog = MissingVoiceReuseReviewDialog(session_path)
            dialog._pending_heard = [("cohort", "queue", "A")]

            dialog.reject()

            self.assertTrue(dialog._close_pending)
            self.assertIn("Close requested", dialog.status.text())
            dialog._pending_heard = []
            dialog.close()
            dialog.deleteLater()

    @patch("vntts.authoring.missing_voice_reuse_review_ui.QMessageBox.critical")
    @patch(
        "vntts.authoring.missing_voice_reuse_review_ui.MissingVoiceReuseReviewDialog"
    )
    def test_launch_failure_stays_visible_without_a_terminal(self, dialog, critical):
        dialog.side_effect = RuntimeError("wrong error type")
        with self.assertRaisesRegex(RuntimeError, "wrong error type"):
            launch_missing_voice_reuse_review("broken/session.json")
        critical.assert_not_called()

        from vntts.authoring.missing_voice_reuse_review import (
            MissingVoiceReuseReviewError,
        )

        dialog.side_effect = MissingVoiceReuseReviewError("authority changed")
        self.assertEqual(launch_missing_voice_reuse_review("broken/session.json"), 2)
        message = critical.call_args.args[2]
        self.assertIn(str(Path("broken/session.json")), message)
        self.assertIn("authority changed", message)

    def test_decision_context_explains_speaker_voice_and_synthesis_controls(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(Path(directory))
            dialog = MissingVoiceReuseReviewDialog(session_path)

            values = dialog.decision_context.values
            self.assertEqual(values["game_speaker"], "Aderyn")
            self.assertEqual(
                values["synthesis_voice"],
                "Hidden for this blind comparison",
            )
            self.assertEqual(
                values["reference"],
                "Hidden for this blind comparison",
            )
            self.assertEqual(values["backend"], "moss-tts")
            self.assertEqual(values["generation_profile"], "stable")
            self.assertIn("Seed: 0", values["controls"])
            self.assertIn("bind one complete candidate", values["effect"])
            self.assertIn("Plan:", dialog.decision_context.technical.text())
            dialog.deleteLater()

    def finish_current_audio(self, dialog):
        dialog._playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
        dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
        self.wait_for(
            lambda: not dialog.heard_runner.active and not dialog._pending_heard
        )

    def test_both_candidates_require_completed_playback_before_choice(self):
        with TemporaryDirectory() as directory:
            session_path, _queue_id = self.create_review(
                Path(directory), statuses=("generated", "generated")
            )
            dialog = MissingVoiceReuseReviewDialog(session_path)
            dialog.player = Mock()
            self.assertFalse(dialog.neither.isEnabled())

            dialog.play_buttons["A"].click()
            self.assertFalse(dialog.decision_buttons["A"].isEnabled())
            self.finish_current_audio(dialog)
            self.assertFalse(dialog.neither.isEnabled())
            self.assertIn("HEARD", dialog.arm_statuses["A"].text())

            dialog.play_buttons["B"].click()
            self.finish_current_audio(dialog)
            self.assertTrue(dialog.neither.isEnabled())
            self.assertTrue(dialog.decision_buttons["A"].isEnabled())
            self.assertTrue(dialog.decision_buttons["B"].isEnabled())

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
                session_path,
                decision_recorder=slow_decision,
                confirmer=lambda _decision: True,
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
