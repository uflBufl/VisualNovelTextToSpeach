import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from tests.test_authoring_legacy_reason_review import (  # noqa: E402
    _legacy_bad_fixture,
)
from vntts.authoring.legacy_reason_review import (  # noqa: E402
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
            self.assertIn("acceptable now", dialog.context.text())
            dialog.play.click()
            player.setSource.assert_called_once()
            player.play.assert_called_once()
            dialog.acceptable.click()
            self.assertTrue(dialog.finish.isEnabled())
            dialog.finish.click()

        self.assertEqual(publisher.call_args.args[1], {review.items[0].item_id: ()})
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)


if __name__ == "__main__":
    unittest.main()
