import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from tests import test_authoring_cohort_review
    from vntts.authoring.cohort_bundle import (
        build_cohort_review_bundle,
        load_cohort_review_bundle_samples,
    )
    from vntts.authoring.cohort_bundle_ui import CohortReviewBundleDialog
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    CohortReviewBundleDialog = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class AuthoringCohortBundleUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, CohortReviewBundleDialog):
                widget._load_active = False
                widget._playback_prepare_active = False
                widget._decision_active = False
                widget.close()
                widget.deleteLater()
        self.application.processEvents()

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for Qt bundle work")

    def create_bundle(self, root):
        fixture = test_authoring_cohort_review.AuthoringCohortReviewTest()
        first = fixture.create_pending_workspace(root / "first")[0]
        second = fixture.create_pending_workspace(root / "second")[0]
        return build_cohort_review_bundle((first, second))

    def test_replay_keeps_controls_and_marks_only_finished_audio_heard(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            self.assertTrue(dialog.replay.isEnabled())
            self.assertFalse(dialog.accept.isEnabled())

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

            self.assertTrue(dialog.replay.isEnabled())
            self.assertTrue(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertTrue(dialog.mark_bad.isEnabled())
            self.assertEqual(dialog.table.item(0, 0).text(), "yes")

    def test_bad_marker_blocks_accept_but_keeps_reject(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._show_current_cohort()

            dialog.toggle_bad()

            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertFalse(dialog.need_another.isEnabled())
            self.assertEqual(dialog.table.item(0, 1).text(), "bad")

    def test_decision_is_source_local_and_navigation_remains_live_while_saving(self):
        calls = []

        def execute(*arguments):
            calls.append(arguments)
            return SimpleNamespace(next_bundle=arguments[0])

        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(
                bundle,
                decision_executor=execute,
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            sample = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(sample.item.queue_id)
            dialog._update_actions()

            dialog.apply_decision("accepted")
            self.assertTrue(dialog.replay.isEnabled())
            self.wait_for(lambda: bool(calls) and not dialog._decision_active)

        self.assertEqual(calls[0][1], sample.workspace_id)
        self.assertEqual(calls[0][2], sample.cohort_id)
        self.assertEqual(calls[0][4], [sample.item.queue_id])

    def test_close_is_deferred_during_authority_work(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle)
            dialog._load_active = True
            event = QCloseEvent()

            dialog.closeEvent(event)

        self.assertFalse(event.isAccepted())
        self.assertIn("Close deferred", dialog.status.text())

    def test_retry_recovers_a_transient_load_error(self):
        calls = []

        def load(bundle):
            calls.append(None)
            if len(calls) == 1:
                raise RuntimeError("transient")
            return load_cohort_review_bundle_samples(bundle)

        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, sample_loader=load)
            dialog.show()
            self.wait_for(lambda: dialog.retry_load.isEnabled())
            self.assertIn("BLOCKED", dialog.status.text())

            dialog.retry_load.click()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

        self.assertEqual(len(calls), 2)
        self.assertIn("READY", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
