import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtMultimedia import QMediaPlayer
    from PySide6.QtWidgets import QApplication

    from tests import test_authoring_failure_reference_audit
    from vntts.authoring.failure_reference_audit import (
        load_failure_reference_decisions,
        prepare_failure_reference_audio,
        publish_failure_reference_audit,
    )
    from vntts.authoring.failure_reference_audit_ui import (
        FailureReferenceAuditDialog,
    )
except ModuleNotFoundError as error:
    if error.name != "PySide6":
        raise
    QApplication = None
    QCloseEvent = None
    QMediaPlayer = None
    FailureReferenceAuditDialog = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class FailureReferenceAuditUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def tearDown(self):
        for widget in self.application.topLevelWidgets():
            if isinstance(widget, FailureReferenceAuditDialog):
                widget._playback_active = False
                widget._save_active = False
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
        self.fail("Timed out waiting for failed-reference audit work")

    def create_audit(self, root):
        fixture = test_authoring_failure_reference_audit.FailureReferenceAuditTest()
        workspace, _queue_id = fixture.create_failed_workspace(root)
        output = root / "audit"
        publish_failure_reference_audit(workspace, output)
        return output

    def hear_all_candidates(self, dialog):
        group = dialog._current_group()
        for index, candidate in enumerate(group["candidates"]):
            dialog.candidate_choice.setCurrentIndex(index)
            dialog._playback_target = (
                group["group_id"],
                candidate["candidate_id"],
                candidate["sha256"],
            )
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)

    def test_playback_uses_immutable_bytes_and_unlocks_after_every_candidate(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()
            group = dialog._current_group()
            candidate_id = dialog.candidate_choice.currentData()
            prepared = prepare_failure_reference_audio(
                audit, group["group_id"], candidate_id
            )

            dialog._playback_serial = 1
            dialog._playback_active = True
            dialog._playback_finished(1, prepared, None)

            self.assertEqual(dialog._playback_buffer.data().data(), prepared.payload)
            self.assertFalse(dialog.choose.isEnabled())
            self.assertFalse(dialog.neither.isEnabled())
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.hear_all_candidates(dialog)
            self.assertTrue(dialog.choose.isEnabled())
            self.assertTrue(dialog.neither.isEnabled())
            total = len(group["candidates"])
            self.assertIn(f"{total}/{total}", dialog.candidate_heard.text())
            self.assertNotIn("source_reference", dialog.summary.text())

    def test_initial_layout_prioritizes_candidate_and_collapses_line_ids(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()

            self.assertFalse(dialog.cases.isVisibleTo(dialog))
            self.assertIn("Candidate 1 of", dialog.candidate_heading.text())
            self.assertIn(
                f"0/{len(dialog._current_group()['candidates'])}",
                dialog.candidate_heard.text(),
            )
            self.assertFalse(dialog.choose.isEnabled())
            self.assertFalse(dialog.neither.isEnabled())
            self.assertIn("listen through every candidate", dialog.action_reason.text())
            self.assertTrue(dialog.progress.accessibleName())
            self.assertTrue(dialog.action_reason.accessibleName())

            dialog.technical_details.setChecked(True)
            self.assertTrue(dialog.cases.isVisibleTo(dialog))

    def test_direct_decision_is_blocked_until_all_candidates_are_heard(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)

            dialog.save_decision(dialog.candidate_choice.currentData())

            self.assertFalse(dialog._save_active)
            self.assertIn("listen through every candidate", dialog.status.text())
            self.assertEqual(load_failure_reference_decisions(audit)["decisions"], [])

    def test_save_runs_in_background_and_advances_progress(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog.show()

            self.hear_all_candidates(dialog)
            dialog.choose_selected()
            self.assertTrue(dialog._save_active)
            self.assertTrue(dialog.play.isEnabled())
            self.wait_for(lambda: not dialog._save_active)

            decisions = load_failure_reference_decisions(audit)
            self.assertEqual(len(decisions["decisions"]), 1)
            self.assertIn("SAVED", dialog.status.text())

    def test_close_is_deferred_during_checksum_work(self):
        with TemporaryDirectory() as directory:
            audit = self.create_audit(Path(directory))
            dialog = FailureReferenceAuditDialog(audit)
            dialog._save_active = True
            event = QCloseEvent()

            dialog.closeEvent(event)

            self.assertFalse(event.isAccepted())
            self.assertIn("Close deferred", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
