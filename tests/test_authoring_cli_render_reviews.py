import argparse
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import vntts.authoring.cli_render_reviews as render_review_cli
from vntts.authoring.cli import main
from vntts.authoring.cli_render_reviews import (
    RenderHypothesisReviewError,
    handle,
)


class AuthoringCliRenderReviewsTest(unittest.TestCase):
    def test_reference_render_commands_preserve_special_json_contracts(self):
        plan = object()
        comparison_result = SimpleNamespace(
            directory=Path("comparison"),
            comparison_id="comparison-1",
            arm_count=3,
            sample_count=4,
            complete_pair_count=2,
        )
        output = StringIO()
        with (
            patch.object(
                render_review_cli, "load_reference_render_plan", return_value=plan
            ) as load,
            patch.object(
                render_review_cli,
                "publish_reference_render_comparison",
                return_value=comparison_result,
            ) as publish,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="failure-reference-render-comparison",
                        plan=Path("plan.json"),
                        output=Path("comparison"),
                    )
                ),
                0,
            )
        load.assert_called_once_with(Path("plan.json"))
        publish.assert_called_once_with(plan, Path("comparison"))
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "directory": "comparison",
                "comparison_id": "comparison-1",
                "arm_count": 3,
                "sample_count": 4,
                "complete_pair_count": 2,
            },
        )

        output = StringIO()
        with (
            patch.object(
                render_review_cli,
                "create_reference_render_listening",
                return_value=Path("session/session.json"),
            ) as create,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="failure-reference-render-session",
                        comparison=Path("comparison"),
                        output=Path("session"),
                        seed=7,
                        arm_id=["arm-a", "arm-b"],
                    )
                ),
                0,
            )
        create.assert_called_once_with(
            Path("comparison"),
            Path("session"),
            seed=7,
            arm_ids=["arm-a", "arm-b"],
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {"comparison": "comparison", "session": "session/session.json"},
        )

    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            patch.object(
                render_review_cli,
                "load_render_hypothesis_review",
                side_effect=RenderHypothesisReviewError("review authority changed"),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["render-hypothesis-review-status", "review"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("review authority changed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
