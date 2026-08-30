import argparse
import ast
import hashlib
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import vntts.authoring.cli_cohort_reviews as cohort_cli
from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_cohort_reviews import COMMANDS, CohortReviewError, handle

PARSER_CONTRACT_SHA256 = (
    "ff2e19ec91a27e082babca922a6e808f93b237ea6f26bd7c0329de59ce9a8142"
)


def _subparser_action(parser):
    return next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )


def _parser_contract(parser):
    subparsers = _subparser_action(parser)
    help_by_command = {
        action.dest: action.help for action in subparsers._choices_actions
    }
    contract = {}
    for command in sorted(COMMANDS):
        actions = []
        for action in subparsers.choices[command]._actions:
            if action.dest == "help":
                continue
            default = action.default
            if isinstance(default, Path):
                default = str(default)
            actions.append(
                {
                    "options": action.option_strings,
                    "dest": action.dest,
                    "required": action.required,
                    "nargs": action.nargs,
                    "default": default,
                    "type": None if action.type is None else action.type.__name__,
                    "choices": (
                        None if action.choices is None else list(action.choices)
                    ),
                    "action": type(action).__name__,
                    "metavar": action.metavar,
                }
            )
        contract[command] = {
            "help": help_by_command[command],
            "actions": actions,
        }
    return contract


class AuthoringCliCohortReviewsTest(unittest.TestCase):
    def test_family_owns_the_exact_cohort_command_inventory(self):
        self.assertEqual(
            COMMANDS,
            {
                "cohort-review-plan",
                "cohort-review-bundle",
                "cohort-review-bundle-apply",
                "cohort-review-decision",
                "cohort-review-apply",
            },
        )
        owners = [family for family in COMMAND_FAMILIES if family.commands == COMMANDS]
        self.assertEqual(len(owners), 1)
        self.assertIs(owners[0].handler, handle)

    def test_parser_contract_and_command_order_match_the_captured_legacy_cli(self):
        parser = create_parser()
        contract = _parser_contract(parser)
        digest = hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.assertEqual(digest, PARSER_CONTRACT_SHA256)
        names = list(_subparser_action(parser).choices)
        expected_runs = (
            (
                "silence-comparison-session",
                "cohort-review-plan",
                "cohort-review-bundle",
                "cohort-review-bundle-apply",
                "pending-resolution-plan",
            ),
            (
                "failure-regeneration-command",
                "cohort-review-decision",
                "cohort-review-apply",
                "reference-report",
            ),
        )
        for expected in expected_runs:
            start = names.index(expected[0])
            self.assertEqual(tuple(names[start : start + len(expected)]), expected)

    def test_plan_preserves_build_write_and_json_contract(self):
        plan = Mock()
        plan.to_dict.return_value = {"plan": "exact"}
        output = StringIO()
        arguments = argparse.Namespace(
            command="cohort-review-plan",
            workspace=Path("workspace"),
            clean_samples_per_bucket=2,
            queue_ids=["queue-1"],
            output=Path("plan.json"),
        )
        with (
            patch.object(
                cohort_cli, "build_cohort_review_plan", return_value=plan
            ) as build,
            patch.object(cohort_cli, "write_cohort_review_plan") as write,
            redirect_stdout(output),
        ):
            self.assertEqual(handle(arguments), 0)

        build.assert_called_once_with(
            Path("workspace"), clean_samples_per_bucket=2, queue_ids=["queue-1"]
        )
        write.assert_called_once_with(plan, Path("plan.json"))
        self.assertEqual(json.loads(output.getvalue()), {"plan": "exact"})

    def test_bundle_preserves_exact_resolved_workspace_selections(self):
        first = Path("workspace-a")
        second = Path("workspace-b")
        bundle = Mock()
        bundle.to_dict.return_value = {"bundle": "exact"}
        output = StringIO()
        arguments = argparse.Namespace(
            command="cohort-review-bundle",
            workspace=[first, second],
            workspace_queue_id=[
                (str(first), "queue-a"),
                (str(second), "queue-b"),
            ],
            clean_samples_per_bucket=3,
            output=None,
        )
        with (
            patch.object(
                cohort_cli, "build_cohort_review_bundle", return_value=bundle
            ) as build,
            redirect_stdout(output),
        ):
            self.assertEqual(handle(arguments), 0)

        build.assert_called_once_with(
            [first, second],
            clean_samples_per_bucket=3,
            queue_ids_by_workspace={
                first.resolve(): ["queue-a"],
                second.resolve(): ["queue-b"],
            },
        )
        self.assertEqual(json.loads(output.getvalue()), {"bundle": "exact"})

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

    def test_family_handler_rejects_an_unowned_command(self):
        with self.assertRaisesRegex(ValueError, "Unsupported cohort-review command"):
            handle(argparse.Namespace(command="other"))

    def test_top_level_cli_imports_only_the_cohort_family_boundary(self):
        cli_path = Path(cohort_cli.__file__).with_name("cli.py")
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        self.assertIn("vntts.authoring.cli_cohort_reviews", imported_modules)
        self.assertFalse(
            {
                "vntts.authoring.cohort_bundle",
                "vntts.authoring.cohort_review",
            }
            & imported_modules
        )


if __name__ == "__main__":
    unittest.main()
