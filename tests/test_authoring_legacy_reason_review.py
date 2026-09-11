import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_authoring_cohort_review import create_pending_cohort_workspace
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cohort_review import (
    build_cohort_review_decision,
    build_cohort_review_plan,
    write_cohort_review_plan,
)
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


def _legacy_bad_fixture(root):
    workspace, _state, queue_id = create_pending_cohort_workspace(root)
    reviews = workspace / "cohort-reviews"
    reviews.mkdir()
    plan = build_cohort_review_plan(workspace)
    write_cohort_review_plan(plan, reviews / f"plan-{plan.plan_id}.json")
    decision = build_cohort_review_decision(
        plan,
        plan.document["cohorts"][0]["cohort_id"],
        "rejected",
        reviewed_queue_ids=[queue_id],
        sample_assessments={queue_id: "bad"},
    )
    document = deepcopy(decision.document)
    document["schema_version"] = 1
    document.pop("item_review_statuses")
    document["sample_assessments"][0].pop("defect_reasons")
    document["decision_id"] = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "decision_id"}
    )
    decision_path = reviews / f"decision-{document['decision_id']}.json"
    decision_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    corpus = root / "corpus-v3"
    publish_speech_robustness_corpus([reviews], [], corpus)
    return workspace, queue_id, decision_path, corpus


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

            updated = root / "corpus-v4-input"
            publish_speech_robustness_corpus(
                [workspace / "cohort-reviews"], [], updated
            )
            sample = load_speech_robustness_corpus(updated).document["samples"][0]

        self.assertEqual(sample["human_defect_reasons"], ["pause_or_pacing"])
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


if __name__ == "__main__":
    unittest.main()
