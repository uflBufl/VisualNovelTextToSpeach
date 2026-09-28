import unittest

import vntts.authoring.cohort_bundle as cohort_bundle
import vntts.authoring.cohort_review as cohort_review
import vntts.authoring.config_rebase as config_rebase
import vntts.authoring.robustness_corpus as robustness_corpus
import vntts.authoring.terminal_conflict_review as terminal_conflict_review
import vntts.authoring.workspace_authority as workspace_authority
from vntts.authoring.workbench import AuthoringWorkbenchError


class AuthoringListSchemaValidationTest(unittest.TestCase):
    def test_malformed_list_elements_raise_domain_errors(self):
        carry = {
            "schema": "vntts.authoring-carry-forward",
            "schema_version": 1,
            "source_workspace_id": f"resume-{'a' * 24}-{'b' * 16}",
            "source_state_sha256": "c" * 64,
            "characters": [{}],
            "items": [],
        }
        observation = {
            "workspace_id": "workspace",
            "cohort_id": "a" * 64,
            "queue_id": "queue",
            "audio_sha256": "b" * 64,
            "assessment": "bad",
            "defect_reasons": [{}],
        }
        carry_progress = {
            "carry_forward": {
                "source_review": "/tmp/source-review",
                "source_review_sha256": "a" * 64,
                "source_progress": "/tmp/source-progress",
                "source_progress_sha256": "b" * 64,
                "source_review_id": "c" * 64,
                "case_ids": [{}],
            }
        }
        cases = (
            (
                "workspace authority",
                AuthoringWorkbenchError,
                lambda: workspace_authority._validate_carry_forward_header({}, carry),
            ),
            (
                "robustness corpus",
                robustness_corpus.SpeechRobustnessCorpusError,
                lambda: robustness_corpus._validate_sample_reasons(
                    {"human_defect_reasons": [{}], "human_label": "bad"}, 3
                ),
            ),
            (
                "config rebase",
                AuthoringWorkbenchError,
                lambda: config_rebase._prior_config_rebase_target_route(
                    {
                        "config_rebase": {
                            "target_effective_character": "Narrator",
                            "target_reference_sha256s": [{}],
                        }
                    }
                ),
            ),
            (
                "cohort bundle",
                cohort_bundle.CohortReviewError,
                lambda: cohort_bundle._validated_observation_entry(
                    observation, 2, set()
                ),
            ),
            (
                "cohort review",
                cohort_review.CohortReviewError,
                lambda: cohort_review._validated_document_assessments(
                    [
                        {
                            "queue_id": "queue",
                            "assessment": "bad",
                            "defect_reasons": [{}],
                        }
                    ],
                    2,
                ),
            ),
            (
                "cohort review sample IDs",
                cohort_review.CohortReviewError,
                lambda: cohort_review._validate_decision_reviewed(
                    "rejected", {"sample_queue_ids": [{}]}, []
                ),
            ),
            (
                "terminal conflict review",
                terminal_conflict_review.TerminalConflictReviewError,
                lambda: terminal_conflict_review._validate_progress_carry(
                    carry_progress,
                    terminal_conflict_review.TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION,
                    set(),
                ),
            ),
        )

        for name, error_type, parser in cases:
            with self.subTest(parser=name):
                with self.assertRaises(error_type):
                    parser()


if __name__ == "__main__":
    unittest.main()
