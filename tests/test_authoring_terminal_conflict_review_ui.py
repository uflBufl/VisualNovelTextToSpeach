import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from tests.symlink_support import symlink_or_skip
    from tests.terminal_conflict_fixtures import create_terminal_conflict_review
    from vntts.authoring.terminal_conflict_review import (
        TerminalConflictReviewError,
        load_terminal_conflict_review_progress,
        record_terminal_conflict_decision,
    )
    from vntts.authoring.terminal_conflict_review_ui import (
        TerminalConflictReviewDialog,
    )
    from vntts.authoring.terminal_conflict_review_ui import (
        main as terminal_conflict_main,
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
        cls.media_player_patcher = patch(
            "vntts.authoring.terminal_conflict_review_ui.QMediaPlayer"
        )
        media_player = cls.media_player_patcher.start()
        media_player.MediaStatus = QMediaPlayer.MediaStatus
        cls.application = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.media_player_patcher.stop()

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
        return create_terminal_conflict_review(root)

    def create_dialog(self, directory, **options):
        dialog = TerminalConflictReviewDialog(directory, **options)
        self.wait_for(lambda: not dialog.load_runner.active)
        if dialog.load_error is not None:
            dialog.close()
            raise dialog.load_error
        return dialog

    def _run_cli_in_subprocess(self):
        # QApplication.exec/exit owns process lifetime; keep it out of the
        # application's shared Qt test instance used by later modal dialogs.
        child_key = "VNTTS_TERMINAL_CONFLICT_CLI_TEST"
        if os.environ.get(child_key) == self.id():
            return False
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", self.id()],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", child_key: self.id()},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            completed.returncode, 0, f"{completed.stdout}\n{completed.stderr}"
        )
        return True

    def test_open_and_refresh_keep_event_loop_responsive_and_cancel_stale_load(self):
        from vntts.authoring.terminal_conflict_review import (
            load_terminal_conflict_review_session,
        )

        started = Event()
        release = Event()
        finished = Event()

        def slow_loader(directory):
            started.set()
            release.wait(3)
            result = load_terminal_conflict_review_session(directory)
            finished.set()
            return result

        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            with patch(
                "vntts.authoring.terminal_conflict_review_ui.load_terminal_conflict_review_session",
                side_effect=slow_loader,
            ):
                dialog = TerminalConflictReviewDialog(review)
                dialog.show()
                try:
                    for refresh in (False, True):
                        if refresh:
                            started.clear()
                            release.clear()
                            finished.clear()
                            dialog._load_next()
                        self.assertTrue(started.wait(1))
                        heartbeat = []
                        QTimer.singleShot(0, lambda: heartbeat.append(True))
                        self.wait_for(lambda: bool(heartbeat))
                        self.assertFalse(finished.is_set())
                        self.assertTrue(dialog.load_runner.active)
                        self.assertTrue(
                            all(
                                not button.isEnabled()
                                for button in dialog.choose_buttons
                            )
                        )
                        if refresh:
                            dialog.close()
                        release.set()
                        self.wait_for(finished.is_set)
                        self.wait_for(lambda: not dialog.load_runner.active)
                        self.application.processEvents()
                        if refresh:
                            self.assertIsNone(dialog._current)
                        else:
                            self.assertIsNotNone(dialog._current)
                finally:
                    release.set()
                    dialog.close()

    def test_primary_async_load_failure_keeps_native_error_and_exit_status(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            symlink_or_skip(review / "progress.json", "missing-progress.json")
            if self._run_cli_in_subprocess():
                return
            with patch(
                "vntts.authoring.terminal_conflict_review_ui.QMessageBox.critical"
            ) as critical:
                self.assertEqual(terminal_conflict_main([str(review)]), 2)
            self.assertEqual(critical.call_count, 1)
            self.assertIn("Terminal conflict progress", critical.call_args.args[2])

    def test_initial_display_validation_failure_keeps_native_error_and_exit_status(
        self,
    ):
        if self._run_cli_in_subprocess():
            return
        from dataclasses import replace

        from vntts.authoring.terminal_conflict_review import (
            load_terminal_conflict_review_session,
        )

        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            session = load_terminal_conflict_review_session(review)
            case = session.review["cases"][0]
            unsupported = {
                **case,
                "candidates": case["candidates"] + case["candidates"][:1],
            }
            wrong_session = replace(
                session, review={**session.review, "cases": [unsupported]}
            )
            with (
                patch(
                    "vntts.authoring.terminal_conflict_review_ui.load_terminal_conflict_review_session",
                    return_value=wrong_session,
                ),
                patch(
                    "vntts.authoring.terminal_conflict_review_ui.QMessageBox.critical"
                ) as critical,
            ):
                self.assertEqual(terminal_conflict_main([str(review)]), 2)
            self.assertEqual(critical.call_count, 1)
            self.assertIn("exactly two distinct candidates", critical.call_args.args[2])

    def test_requires_both_candidates_and_saves_neither_in_background(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = self.create_dialog(review, confirmer=lambda _decision: True)
            dialog.show()
            self.application.processEvents()
            self.assertTrue(dialog.text.isVisibleTo(dialog))
            self.assertTrue(
                all(button.isVisibleTo(dialog) for button in dialog.play_buttons)
            )
            self.assertTrue(
                all(button.isVisibleTo(dialog) for button in dialog.choose_buttons)
            )

            self.assertFalse(dialog.neither.isEnabled())
            self.assertFalse(dialog.stop.isEnabled())
            self.assertTrue(dialog.stop.accessibleName())
            self.assertEqual(
                dialog.decision_context.values["game_speaker"],
                dialog._current["speaker"],
            )
            self.assertIn(
                "compared blind",
                dialog.decision_context.values["reference"],
            )
            self.assertIn(
                "require repair",
                dialog.decision_context.values["effect"],
            )
            dialog.play_buttons[0].click()
            self.assertFalse(dialog.neither.isEnabled())
            self.finish_playback(dialog)
            self.assertFalse(dialog.stop.isEnabled())
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

            self.wait_for(lambda: not dialog._active and not dialog.load_runner.active)

            progress = load_terminal_conflict_review_progress(review)
            self.assertEqual(progress["decisions"][0]["decision"], "neither_acceptable")
            self.assertIn("All terminal conflicts", dialog.identity.text())
            self.assertTrue(dialog.decision_context.isHidden())
            self.assertTrue(all(button.isHidden() for button in dialog.play_buttons))
            self.assertTrue(all(button.isHidden() for button in dialog.choose_buttons))
            self.assertTrue(dialog.close_button.hasFocus())
            dialog.close()

    def test_stopped_audio_does_not_unlock_conflict_decision(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = self.create_dialog(review)
            dialog._playing_candidate = dialog._display_candidates[0]["candidate_id"]

            dialog._stop()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

            self.assertEqual(dialog._heard, set())
            self.assertFalse(dialog.neither.isEnabled())
            dialog.close()

    def test_dangling_progress_link_blocks_review_on_open(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            symlink_or_skip(review / "progress.json", "missing-progress.json")

            with self.assertRaises(TerminalConflictReviewError):
                self.create_dialog(review)

    def test_enlarged_text_keeps_line_playback_and_decisions_visible(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = self.create_dialog(review)
            font = dialog.font()
            font.setPointSize(16)
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
            dialog.show()
            self.application.processEvents()

            viewport = dialog.review_scroll.viewport()
            for widget in (dialog.text, *dialog.play_buttons):
                bottom = widget.mapTo(viewport, QPoint(0, 0)).y() + widget.height()
                self.assertLessEqual(bottom, viewport.height())
            self.assertGreaterEqual(
                dialog.neither.height(), dialog.neither.sizeHint().height()
            )
            dialog.text.setText("Long affected dialogue. " * 50)
            self.application.processEvents()
            self.assertGreater(dialog.review_scroll.verticalScrollBar().maximum(), 0)
            dialog.context_toggle.setFocus()
            self.application.processEvents()
            context_bottom = dialog.context_toggle.mapTo(viewport, QPoint(0, 0)).y()
            context_bottom += dialog.context_toggle.height()
            self.assertLessEqual(context_bottom, viewport.height())
            dialog.close()

    def test_scaled_font_keeps_keyboard_journey_scroll_reachable(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = self.create_dialog(review)
            base_point_size = dialog.font().pointSizeF()
            for scale in (1.5, 2.0, None):
                font = dialog.font()
                if scale is None:
                    font.setPixelSize(48)  # Exercise wide metrics on every host.
                    font.setStretch(115)
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
            self.assertIs(
                dialog.decision_context.technical_toggle.nextInFocusChain(),
                dialog.choose_buttons[0],
            )
            self.assertIs(dialog.neither.nextInFocusChain(), dialog.close_button)
            for button in (
                *dialog.play_buttons,
                dialog.stop,
                *dialog.choose_buttons,
                dialog.neither,
                dialog.close_button,
            ):
                self.assertTrue(button.accessibleName(), button.text())
                self.assertTrue(button.accessibleDescription(), button.text())
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
            dialog = self.create_dialog(
                review,
                decision_recorder=slow_recorder,
                confirmer=lambda _decision: True,
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
            dialog.reject()
            self.assertTrue(dialog._close_pending)
            self.assertTrue(heartbeat)
            release.set()
            self.wait_for(lambda: not dialog._active and not dialog.load_runner.active)

    def test_irreversible_decision_can_be_cancelled(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            dialog = self.create_dialog(review, confirmer=lambda _decision: False)
            dialog._heard = {"candidate-a", "candidate-b"}

            dialog._save("neither_acceptable")

            self.assertFalse(dialog._active)
            self.assertIn("cancelled", dialog.status.text())

    def test_copied_wav_tamper_blocks_playback_without_enabling_decision(self):
        with TemporaryDirectory() as directory:
            review = self.create_review(Path(directory))
            document = json.loads((review / "review.json").read_text(encoding="utf-8"))
            audio = review / document["cases"][0]["candidates"][0]["audio"]
            audio.write_bytes(b"changed")
            with self.assertRaises(TerminalConflictReviewError):
                self.create_dialog(review)

    def test_main_reports_open_failure_in_a_native_dialog(self):
        with (
            patch(
                "vntts.authoring.terminal_conflict_review_ui.launch_terminal_conflict_review",
                side_effect=TerminalConflictReviewError("authority changed"),
            ),
            patch(
                "vntts.authoring.terminal_conflict_review_ui.QMessageBox.critical"
            ) as critical,
        ):
            self.assertEqual(terminal_conflict_main(["broken-review"]), 2)

        message = critical.call_args.args[2]
        self.assertIn("broken-review", message)
        self.assertIn("authority changed", message)


if __name__ == "__main__":
    unittest.main()
