import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QScrollArea  # noqa: E402

from tests.authoring_fixtures import (  # noqa: E402
    _legacy_bad_fixture,
)
from vntts.authoring.legacy_reason_review import (  # noqa: E402
    LegacyReasonReviewError,
    build_legacy_reason_review,
)
from vntts.authoring.legacy_reason_review_ui import (  # noqa: E402
    LegacyReasonReviewDialog,
)


class LegacyReasonReviewDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_plays_only_legacy_bad_wav_and_accepts_current_reassessment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            player = Mock()
            publisher = Mock(return_value=(root / "decision.json",))
            dialog = LegacyReasonReviewDialog(
                review,
                root / "progress.json",
                player=player,
                progress_writer=Mock(),
                publisher=publisher,
            )

            self.assertFalse(dialog.finish.isEnabled())
            self.assertIn("Choices save automatically", dialog.context.text())
            self.assertFalse(dialog.acceptable.isEnabled())
            dialog.play.click()
            player.setSource.assert_called_once()
            player.play.assert_called_once()
            dialog.stop.click()
            self.assertFalse(dialog.acceptable.isEnabled())
            dialog.play.click()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            self.assertTrue(dialog.acceptable.isEnabled())
            dialog.acceptable.click()
            self.assertTrue(dialog.finish.isEnabled())
            self.assertIn("1 assessed", dialog.progress.text())
            self.assertIn("Publish assessments", dialog.status.text())
            dialog.finish.click()

        self.assertEqual(publisher.call_args.args[1], {review.items[0].item_id: ()})
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_failed_autosave_reverts_visible_assessment_and_blocks_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            dialog = LegacyReasonReviewDialog(
                review,
                root / "progress.json",
                player=Mock(),
                progress_writer=Mock(side_effect=OSError("read-only storage")),
            )
            dialog._heard_items.add(review.items[0].item_id)
            dialog._update_actions()
            dialog.acceptable.click()

            self.assertFalse(dialog.acceptable.isChecked())
            self.assertFalse(dialog.finish.isEnabled())
            self.assertIn("read-only storage", dialog.status.text())
            self.assertEqual(dialog.selections, {})
            self.assertIn("0 assessed", dialog.progress.text())

    def test_two_recordings_keep_independent_listening_and_assessments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            first = review.items[0]
            review = replace(
                review,
                items=(first, replace(first, item_id="second-recording", line_id="2")),
            )
            dialog = LegacyReasonReviewDialog(
                review, root / "progress.json", player=Mock(), progress_writer=Mock()
            )
            dialog.play.click()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            dialog.reason_controls["pause_or_pacing"].click()
            dialog.reason_controls["repetition"].click()
            self.assertEqual(
                dialog.selections[first.item_id], ("pause_or_pacing", "repetition")
            )
            dialog.next.click()
            self.assertFalse(dialog.acceptable.isEnabled())
            self.assertFalse(dialog.finish.isEnabled())
            dialog.play.click()
            dialog.stop.click()
            self.assertFalse(dialog.acceptable.isEnabled())
            dialog.play.click()
            dialog._media_status_changed(QMediaPlayer.MediaStatus.EndOfMedia)
            dialog.acceptable.click()
            self.assertTrue(dialog.finish.isEnabled())
            dialog.previous.click()
            self.assertTrue(dialog.reason_controls["pause_or_pacing"].isChecked())
            self.assertTrue(dialog.reason_controls["repetition"].isChecked())
            dialog.acceptable.click()
            self.assertEqual(dialog.selections[first.item_id], ())
            self.assertFalse(dialog.reason_controls["pause_or_pacing"].isChecked())
            self.assertFalse(dialog.reason_controls["repetition"].isChecked())

    def test_failed_update_restores_last_saved_reasons(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            writer = Mock(side_effect=[None, OSError("storage full")])
            dialog = LegacyReasonReviewDialog(
                review, root / "progress.json", player=Mock(), progress_writer=writer
            )
            dialog._heard_items.add(review.items[0].item_id)
            dialog._update_actions()
            dialog.reason_controls["pause_or_pacing"].click()
            dialog.reason_controls["repetition"].click()

            self.assertEqual(
                dialog.selections[review.items[0].item_id], ("pause_or_pacing",)
            )
            self.assertTrue(dialog.reason_controls["pause_or_pacing"].isChecked())
            self.assertFalse(dialog.reason_controls["repetition"].isChecked())
            self.assertTrue(dialog.finish.isEnabled())
            self.assertIn("Assessment not saved", dialog.status.text())

    def test_publication_failure_keeps_saved_assessment_available_for_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            dialog = LegacyReasonReviewDialog(
                review,
                root / "progress.json",
                player=Mock(),
                progress_writer=Mock(),
                publisher=Mock(side_effect=LegacyReasonReviewError("stale decision")),
            )
            dialog._heard_items.add(review.items[0].item_id)
            dialog._update_actions()
            dialog.acceptable.click()
            dialog.finish.click()

            self.assertEqual(dialog.selections[review.items[0].item_id], ())
            self.assertTrue(dialog.finish.isEnabled())
            self.assertIn("Assessments not published", dialog.status.text())
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_compact_assessment_remains_reachable_by_keyboard(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            dialog = LegacyReasonReviewDialog(
                review, root / "progress.json", player=Mock()
            )
            font = dialog.font()
            font.setPointSize(16)
            dialog.setFont(font)
            dialog.resize(dialog.minimumSize())
            dialog.show()
            self.application.processEvents()
            self.assertGreaterEqual(dialog.heading.font().pointSize(), 16)
            self.assertGreaterEqual(dialog.speaker.font().pointSize(), 16)
            self.assertGreaterEqual(dialog.acceptable.font().pointSize(), 16)
            self.assertGreaterEqual(
                dialog.reason_controls["other_or_unclear"].font().pointSize(), 16
            )
            scroll = dialog.findChild(QScrollArea)
            self.assertIsNotNone(scroll)
            assert scroll is not None
            dialog._heard_items.add(review.items[0].item_id)
            dialog._update_actions()
            last_reason = dialog.reason_controls["other_or_unclear"]
            last_reason.setFocus()
            self.application.processEvents()
            self.assertGreater(scroll.verticalScrollBar().value(), 0)
            self.assertTrue(dialog.finish.isVisible())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
