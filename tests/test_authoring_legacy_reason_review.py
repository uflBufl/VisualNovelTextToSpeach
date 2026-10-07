import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.authoring_fixtures import _legacy_bad_fixture
from vntts.authoring.legacy_reason_review import (
    LegacyReasonReviewError,
    build_legacy_reason_review,
    load_reason_review_progress,
    publish_reason_review_decisions,
    write_reason_review_progress,
)
from vntts.authoring.robustness_corpus import (
    load_speech_robustness_corpus,
    publish_speech_robustness_corpus,
)


class LegacyReasonReviewTest(unittest.TestCase):
    def test_resumes_and_publishes_additive_reason_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id, decision_path, corpus = _legacy_bad_fixture(root)
            original = decision_path.read_bytes()
            review = build_legacy_reason_review(corpus, root)
            self.assertEqual(len(review.items), 1)
            self.assertEqual(review.items[0].queue_id, queue_id)

            progress = root / "reason-progress.json"
            selections = {review.items[0].item_id: ("pause_or_pacing",)}
            write_reason_review_progress(review, progress, selections)
            self.assertEqual(load_reason_review_progress(review, progress), selections)

            published = publish_reason_review_decisions(review, selections)
            self.assertEqual(len(published), 1)
            self.assertNotEqual(published[0], decision_path)
            self.assertEqual(decision_path.read_bytes(), original)
            self.assertEqual(
                publish_reason_review_decisions(review, selections), published
            )

            updated = root / "corpus-v4-input"
            publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], updated
            )
            sample = load_speech_robustness_corpus(updated).document["samples"][0]

        self.assertEqual(sample["human_defect_reasons"], ["pause_or_pacing"])
        self.assertEqual(len(sample["decision_ids"]), 2)

    def test_reassesses_legacy_bad_sample_as_acceptable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id, _decision_path, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)

            publish_reason_review_decisions(review, {review.items[0].item_id: ()})
            updated = root / "corpus-v4-input"
            publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], updated
            )
            sample = load_speech_robustness_corpus(updated).document["samples"][0]

        self.assertEqual(sample["human_label"], "acceptable")
        self.assertEqual(sample["human_defect_reasons"], [])
        self.assertEqual(len(sample["decision_ids"]), 2)

    def test_progress_is_bound_to_exact_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            progress = root / "reason-progress.json"
            write_reason_review_progress(
                review,
                progress,
                {review.items[0].item_id: ("repetition",)},
            )
            document = json.loads(progress.read_text(encoding="utf-8"))
            document["items"][0]["audio_sha256"] = "0" * 64
            progress.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                LegacyReasonReviewError, "changed audio evidence"
            ):
                load_reason_review_progress(review, progress)

    def test_progress_rejects_unhashable_item_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            progress = root / "reason-progress.json"
            write_reason_review_progress(review, progress, {})
            document = json.loads(progress.read_text(encoding="utf-8"))
            document["items"] = [
                {
                    "item_id": [],
                    "audio_sha256": review.items[0].audio_sha256,
                    "defect_reasons": [],
                }
            ]
            progress.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(LegacyReasonReviewError, "item is malformed"):
                load_reason_review_progress(review, progress)

    def test_progress_rejects_unhashable_defect_reason(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _workspace, _queue_id, _decision, corpus = _legacy_bad_fixture(root)
            review = build_legacy_reason_review(corpus, root)
            progress = root / "reason-progress.json"
            write_reason_review_progress(review, progress, {})
            document = json.loads(progress.read_text(encoding="utf-8"))
            document["items"] = [
                {
                    "item_id": review.items[0].item_id,
                    "audio_sha256": review.items[0].audio_sha256,
                    "defect_reasons": [[]],
                }
            ]
            progress.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(
                LegacyReasonReviewError, "only supported defect reasons"
            ):
                load_reason_review_progress(review, progress)


if __name__ == "__main__":
    unittest.main()
