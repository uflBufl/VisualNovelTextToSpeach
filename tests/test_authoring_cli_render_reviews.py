import argparse
import ast
import hashlib
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import vntts.authoring.cli_render_reviews as render_review_cli
from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_render_reviews import (
    COMMANDS,
    RenderHypothesisReviewError,
    handle,
)

PARSER_CONTRACT_SHA256 = (
    "06cce9ca7c8933ca73f12f1ded9eb2050e4dd7c40a007faac2929018a0621f93"
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
                }
            )
        contract[command] = {
            "help": help_by_command[command],
            "actions": actions,
        }
    return contract


class AuthoringCliRenderReviewsTest(unittest.TestCase):
    def test_family_owns_the_exact_render_review_command_inventory(self):
        self.assertEqual(
            COMMANDS,
            {
                "render-hypothesis-review-publish",
                "render-hypothesis-review-decide",
                "render-hypothesis-review-status",
                "render-hypothesis-review-import",
                "failure-reference-render-comparison",
                "failure-reference-render-session",
                "failure-reference-import-listening",
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
                "audio-event-composition-workspace",
                "render-hypothesis-review-publish",
                "render-hypothesis-review-decide",
                "render-hypothesis-review-status",
                "render-hypothesis-review-import",
                "failure-report",
            ),
            (
                "failure-reference-audit",
                "failure-reference-render-comparison",
                "failure-reference-render-session",
                "failure-reference-import-listening",
                "failure-reference-binding",
            ),
        )
        for expected in expected_runs:
            start = names.index(expected[0])
            self.assertEqual(tuple(names[start : start + len(expected)]), expected)

    def test_hypothesis_commands_preserve_call_and_json_contracts(self):
        cases = (
            (
                "render-hypothesis-review-publish",
                "publish_render_hypothesis_review",
                argparse.Namespace(
                    command="render-hypothesis-review-publish",
                    comparison=Path("comparison"),
                    queue_id="queue-1",
                    arm_id="arm-1",
                    output=Path("review"),
                ),
                (Path("comparison"), "queue-1", "arm-1", Path("review")),
            ),
            (
                "render-hypothesis-review-decide",
                "record_render_hypothesis_decision",
                argparse.Namespace(
                    command="render-hypothesis-review-decide",
                    directory=Path("review"),
                    decision="accept_hypothesis",
                ),
                (Path("review"), "accept_hypothesis"),
            ),
            (
                "render-hypothesis-review-status",
                "load_render_hypothesis_review",
                argparse.Namespace(
                    command="render-hypothesis-review-status",
                    directory=Path("review"),
                ),
                (Path("review"),),
            ),
            (
                "render-hypothesis-review-import",
                "import_accepted_render_hypothesis",
                argparse.Namespace(
                    command="render-hypothesis-review-import",
                    audit=Path("audit"),
                    comparison=Path("comparison"),
                    review=Path("review"),
                    queue_id="queue-1",
                ),
                (Path("audit"), Path("comparison"), Path("review"), "queue-1"),
            ),
        )
        for command, target, arguments, expected_call in cases:
            with self.subTest(command=command):
                result = Mock()
                result.to_dict.return_value = {"command": command}
                output = StringIO()
                with (
                    patch.object(
                        render_review_cli, target, return_value=result
                    ) as call,
                    redirect_stdout(output),
                ):
                    self.assertEqual(handle(arguments), 0)
                call.assert_called_once_with(*expected_call)
                self.assertEqual(json.loads(output.getvalue()), {"command": command})

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

    def test_family_handler_rejects_an_unowned_command(self):
        with self.assertRaisesRegex(ValueError, "Unsupported render-review command"):
            handle(argparse.Namespace(command="other"))

    def test_top_level_cli_imports_only_the_render_review_family_boundary(self):
        cli_path = Path(render_review_cli.__file__).with_name("cli.py")
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        self.assertIn("vntts.authoring.cli_render_reviews", imported_modules)
        self.assertFalse(
            {
                "vntts.authoring.reference_render_comparison",
                "vntts.authoring.render_hypothesis_review",
            }
            & imported_modules
        )


if __name__ == "__main__":
    unittest.main()
