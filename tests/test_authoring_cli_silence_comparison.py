import argparse
import ast
import hashlib
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import vntts.authoring.cli_silence_comparison as silence_cli
from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_silence_comparison import (
    COMMANDS,
    SilenceComparisonError,
    handle,
)

PARSER_CONTRACT_SHA256 = (
    "0099becfa9fd34e355d879d373d98dcd3f0c8f61b1b7aa90880be40c9c8f3a89"
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


class AuthoringCliSilenceComparisonTest(unittest.TestCase):
    def test_family_owns_the_exact_silence_comparison_inventory(self):
        self.assertEqual(
            COMMANDS,
            {
                "silence-comparison-publish",
                "silence-comparison-check",
                "silence-comparison-session",
            },
        )
        owners = [family for family in COMMAND_FAMILIES if family.commands == COMMANDS]
        self.assertEqual(len(owners), 1)
        self.assertIs(owners[0].handler, handle)

    def test_parser_contract_and_order_match_the_captured_legacy_cli(self):
        parser = create_parser()
        contract = _parser_contract(parser)
        digest = hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.assertEqual(digest, PARSER_CONTRACT_SHA256)
        names = list(_subparser_action(parser).choices)
        expected = (
            "failure-repair-plan",
            "silence-comparison-publish",
            "silence-comparison-check",
            "silence-comparison-session",
            "cohort-review-plan",
        )
        start = names.index(expected[0])
        self.assertEqual(tuple(names[start : start + len(expected)]), expected)

    def test_publish_preserves_call_and_json_contract(self):
        plan = SimpleNamespace(
            samples=("sample",), path=Path("plan.json"), sha256="plan-sha"
        )
        result = SimpleNamespace(
            directory=Path("comparison"),
            sample_count=1,
            report_paths=(Path("report-a.json"), Path("report-b.json")),
        )
        output = StringIO()
        with (
            patch.object(
                silence_cli, "load_silence_comparison_input_plan", return_value=plan
            ) as load,
            patch.object(
                silence_cli, "publish_silence_comparison", return_value=result
            ) as publish,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="silence-comparison-publish",
                        plan=Path("plan.json"),
                        output=Path("comparison"),
                        target_seconds=0.6,
                    )
                ),
                0,
            )

        load.assert_called_once_with(Path("plan.json"))
        publish.assert_called_once_with(
            ("sample",),
            Path("comparison"),
            target_seconds=0.6,
            input_plan_sha256="plan-sha",
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "directory": "comparison",
                "input_plan": "plan.json",
                "input_plan_sha256": "plan-sha",
                "sample_count": 1,
                "reports": ["report-a.json", "report-b.json"],
            },
        )

    def test_check_and_session_preserve_special_json_contracts(self):
        comparison = Path("comparison")
        document = {
            "input_plan_sha256": "plan-sha",
            "policy": {
                "production_enabled": False,
                "requires_blind_review": True,
                "target_seconds": 0.6,
            },
            "samples": [{}, {}],
        }
        output = StringIO()
        with (
            patch.object(
                silence_cli, "load_silence_comparison", return_value=document
            ) as load,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="silence-comparison-check",
                        comparison=comparison,
                    )
                ),
                0,
            )
        load.assert_called_once_with(comparison)
        self.assertEqual(json.loads(output.getvalue())["sample_count"], 2)
        self.assertEqual(
            json.loads(output.getvalue())["comparison"], str(comparison.resolve())
        )

        output = StringIO()
        with (
            patch.object(
                silence_cli,
                "create_silence_comparison_session",
                return_value=Path("listening/session.json"),
            ) as create,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="silence-comparison-session",
                        comparison=comparison,
                        output=Path("listening"),
                        seed=17,
                    )
                ),
                0,
            )
        create.assert_called_once_with(comparison, Path("listening"), seed=17)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"comparison": "comparison", "session": "listening/session.json"},
        )

    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            patch.object(
                silence_cli,
                "load_silence_comparison",
                side_effect=SilenceComparisonError("comparison authority changed"),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["silence-comparison-check", "comparison"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("comparison authority changed", errors.getvalue())

    def test_family_handler_rejects_an_unowned_command(self):
        with self.assertRaisesRegex(
            ValueError, "Unsupported silence-comparison command"
        ):
            handle(argparse.Namespace(command="other"))

    def test_top_level_cli_imports_only_the_silence_family_boundary(self):
        cli_path = Path(silence_cli.__file__).with_name("cli.py")
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        self.assertIn("vntts.authoring.cli_silence_comparison", imported_modules)
        self.assertNotIn("vntts.authoring.silence_comparison", imported_modules)
        self.assertNotIn("vntts.authoring.failure_repair", imported_modules)


if __name__ == "__main__":
    unittest.main()
