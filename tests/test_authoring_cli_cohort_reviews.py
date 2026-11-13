import argparse
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import vntts.authoring.cli_cohort_reviews as cohort_cli
from vntts.authoring.cli import main
from vntts.authoring.cli_cohort_reviews import CohortReviewError, handle


class AuthoringCliCohortReviewsTest(unittest.TestCase):
    def test_bundle_rejects_partial_and_duplicate_exact_selections(self):
        base = {
            "command": "cohort-review-bundle",
            "workspace": [Path("workspace-a"), Path("workspace-b")],
            "clean_samples_per_bucket": 1,
            "output": None,
        }
        cases = (
            (
                [("workspace-a", "queue-a")],
                "Every review bundle workspace requires an exact queue selection",
            ),
            (
                [("workspace-a", "queue-a"), ("workspace-a", "queue-a")],
                "selected queue ID is duplicated",
            ),
        )
        for selections, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(CohortReviewError, message):
                    handle(argparse.Namespace(**base, workspace_queue_id=selections))

    def test_decision_preserves_bad_sample_projection_and_write_contract(self):
        plan = object()
        decision = Mock()
        decision.to_dict.return_value = {"decision": "split"}
        output = StringIO()
        arguments = argparse.Namespace(
            command="cohort-review-decision",
            plan=Path("plan.json"),
            cohort_id="cohort-1",
            decision="split",
            reviewed_queue_id=["good", "bad"],
            bad_queue_id=["bad"],
            next_clean_samples_per_bucket=4,
            output=Path("decision.json"),
        )
        with (
            patch.object(
                cohort_cli, "load_cohort_review_plan", return_value=plan
            ) as load,
            patch.object(
                cohort_cli, "build_cohort_review_decision", return_value=decision
            ) as build,
            patch.object(cohort_cli, "write_cohort_review_decision") as write,
            redirect_stdout(output),
        ):
            self.assertEqual(handle(arguments), 0)

        load.assert_called_once_with(Path("plan.json"))
        build.assert_called_once_with(
            plan,
            "cohort-1",
            "split",
            reviewed_queue_ids=["good", "bad"],
            sample_assessments={"good": "acceptable", "bad": "bad"},
            next_clean_samples_per_bucket=4,
        )
        write.assert_called_once_with(decision, Path("decision.json"))
        self.assertEqual(json.loads(output.getvalue()), {"decision": "split"})

    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(
                [
                    "cohort-review-bundle-apply",
                    "bundle.json",
                    "workspace-1",
                    "cohort-1",
                    "accepted",
                    "--bad-queue-id",
                    "not-reviewed",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Bad queue IDs were not reviewed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
