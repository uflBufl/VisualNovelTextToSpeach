import os
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from tests import test_authoring_cohort_review
    from vntts.authoring.cohort_bundle import (
        build_cohort_review_bundle,
        execute_cohort_bundle_decision,
        load_cohort_review_bundle_samples,
        write_cohort_review_bundle,
    )
    from vntts.authoring.cohort_bundle_ui import CohortReviewBundleDialog
    from vntts.authoring.cohort_bundle_ui import main as review_bundle_main
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    QTest = None
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

            self.assertTrue(dialog.cohort_choice.itemText(0).startswith("Required 1/2"))
            self.assertIn("Review every listed cohort", dialog.operation.text())
            self.assertEqual(
                dialog.overall_progress.format(),
                "0 of 2 cohorts completed in this review session",
            )
            self.assertIn("Play 1 remaining sample", dialog.decision_help.text())
            self.assertTrue(dialog.table.isColumnHidden(5))
            self.assertTrue(dialog.table.isColumnHidden(6))
            self.assertIn("Generated role", dialog.sample_identity.text())
            self.assertIn("listening decides", dialog.sample_identity.text())
            self.assertIn("not a rejection verdict", dialog.guide.text())
            self.assertNotIn("technical-attention", dialog.table.item(0, 4).text())
            self.assertEqual(
                dialog.sample_text.text(), dialog._selected_sample().item.text
            )

            dialog.technical_details.setChecked(True)
            self.assertFalse(dialog.table.isColumnHidden(5))
            self.assertFalse(dialog.table.isColumnHidden(6))
            self.assertTrue(dialog.cohort_audit.isVisible())
            self.assertIn("Provider:", dialog.cohort_audit.text())

            dialog.play_selected()
            self.wait_for(lambda: dialog._playback_target is not None)
            self.assertTrue(dialog.replay.isEnabled())
            self.assertFalse(dialog.accept.isEnabled())

            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

            self.assertTrue(dialog.replay.isEnabled())
            self.assertTrue(dialog.accept.isEnabled())
            self.assertTrue(dialog.reject.isEnabled())
            self.assertTrue(dialog.mark_bad.isEnabled())
            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertIn("All 1 required samples", dialog.decision_help.text())

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
            self.assertEqual(dialog.table.item(0, 1).text(), "Sounds bad")
            self.assertIn("marked bad", dialog.decision_help.text())

    def test_space_and_bad_shortcuts_work_from_table_at_compact_size(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.resize(900, 820)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            dialog.table.setFocus()

            QTest.keyClick(dialog.table, Qt.Key.Key_Space)
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            QTest.keyClick(dialog.table, Qt.Key.Key_B)
            dialog.table.doubleClicked.emit(dialog.table.model().index(0, 0))
            self.wait_for(lambda: dialog._playback_target is not None)
            dialog.stop_playback()

            self.assertEqual(dialog.table.item(0, 1).text(), "Sounds bad")
            self.assertTrue(dialog.heading.isVisible())
            self.assertTrue(dialog.sample_text.isVisible())
            self.assertTrue(dialog.reject.isVisible())
            self.assertIn("press Space to play/replay", dialog.shortcuts_help.text())
            table_bottom = dialog.table.mapTo(
                dialog, QPoint(0, dialog.table.height())
            ).y()
            decision_top = dialog.decision_help.mapTo(dialog, QPoint(0, 0)).y()
            self.assertLessEqual(table_bottom, decision_top)

    def test_decision_is_source_local_and_navigation_remains_live_while_saving(self):
        calls = []
        started = threading.Event()
        release = threading.Event()

        def execute(*arguments):
            calls.append(arguments)
            started.set()
            release.wait(2)
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

            dialog.table.setFocus()
            QTest.keyClick(
                dialog.table,
                Qt.Key.Key_Return,
                Qt.KeyboardModifier.ControlModifier,
            )
            self.wait_for(started.is_set)
            self.assertTrue(dialog._decision_active)
            self.assertTrue(dialog.replay.isEnabled())
            self.assertFalse(dialog.accept.isEnabled())
            self.assertTrue(dialog.progress.isVisible())
            self.assertIn("Saving in background", dialog.operation.text())
            release.set()
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
        self.assertFalse(dialog.progress.isHidden())
        self.assertIn("Refreshing checksum authority", dialog.operation.text())

    def test_real_decision_removes_completed_cohort_and_selects_next(self):
        with TemporaryDirectory() as directory:
            bundle = self.create_bundle(Path(directory))
            dialog = CohortReviewBundleDialog(bundle, confirmer=lambda *_args: True)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(first.item.queue_id)
            dialog._update_actions()

            dialog.apply_decision("accepted")

            self.wait_for(
                lambda: (
                    not dialog._decision_active
                    and not dialog._load_active
                    and dialog.cohort_choice.count() == 1
                ),
                timeout=5,
            )
            self.assertEqual(dialog.cohort_choice.currentIndex(), 0)
            self.assertTrue(
                dialog.cohort_choice.currentText().startswith("Required 1/1")
            )
            self.assertNotEqual(
                dialog._selected_sample().workspace_id, first.workspace_id
            )
            self.assertIn("Current required cohort: 1/1", dialog.operation.text())
            self.assertEqual(
                dialog.overall_progress.format(),
                "1 of 2 cohorts completed in this review session",
            )

    def test_published_bundle_reopens_from_persisted_successor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication,
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(first.item.queue_id)
            dialog._update_actions()

            dialog.apply_decision("accepted")
            self.wait_for(
                lambda: (
                    not dialog._decision_active
                    and not dialog._load_active
                    and dialog.cohort_choice.count() == 1
                ),
                timeout=5,
            )
            dialog.close()
            self.application.processEvents()

            reopened = CohortReviewBundleDialog(publication)
            reopened.show()
            self.wait_for(lambda: reopened.table.rowCount() == 1)
            self.assertNotEqual(
                reopened._selected_sample().workspace_id,
                first.workspace_id,
            )
            self.assertEqual(reopened.bundle.document["cohort_count"], 1)
            self.assertTrue((root / "bundle.progress.json").is_file())

    def test_published_expand_reopens_with_exact_prior_assessments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            selected = bundle.document["cohorts"][0]
            queue_id = selected["samples"][0]["queue_id"]
            execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "expand",
                reviewed_queue_ids=[queue_id],
                sample_assessments={queue_id: "bad"},
                next_clean_samples_per_bucket=2,
            )

            dialog = CohortReviewBundleDialog(publication)
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)

            self.assertEqual(dialog.table.item(0, 0).text(), "Heard")
            self.assertEqual(dialog.table.item(0, 1).text(), "Sounds bad")
            self.assertIn("1 heard", dialog.sample_position.text())

    def test_published_decision_preloads_next_cohort_without_second_reload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            dialog = CohortReviewBundleDialog(
                publication,
                confirmer=lambda *_args: True,
            )
            dialog.show()
            self.wait_for(lambda: dialog.table.rowCount() == 1)
            first = dialog._selected_sample()
            key = dialog._current_key()
            dialog.heard[key].add(first.item.queue_id)
            dialog._update_actions()
            reloads = []
            dialog.reload_bundle = lambda: reloads.append(None)

            dialog.apply_decision("accepted")
            self.wait_for(
                lambda: not dialog._decision_active,
                timeout=5,
            )

            self.assertEqual(reloads, [])
            self.assertFalse(dialog._load_active)
            self.assertEqual(dialog.cohort_choice.count(), 1)
            self.assertIn("commit", dialog.status.text())
            self.assertIn("checkpoint", dialog.status.text())
            self.assertIn("next cohort", dialog.status.text())

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

    def test_status_reports_progress_without_opening_a_window(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = self.create_bundle(root)
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = review_bundle_main([str(publication), "--status"])

        self.assertEqual(exit_code, 0)
        self.assertIn('"remaining_cohorts": 2', stdout.getvalue())
        self.assertEqual(
            [
                widget
                for widget in self.application.topLevelWidgets()
                if isinstance(widget, CohortReviewBundleDialog) and widget.isVisible()
            ],
            [],
        )


if __name__ == "__main__":
    unittest.main()
