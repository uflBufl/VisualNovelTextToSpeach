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

import vntts.authoring.cli_terminal_conflicts as terminal_cli
from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_terminal_conflicts import (
    COMMANDS,
    TerminalConflictResolutionError,
    handle,
)

PARSER_CONTRACT_SHA256 = (
    "690143304daaaf71069cab2d9dbf152cef5bdd3a90c80a591ac4d3b0a7ccf2f8"
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
            if action.dest == "workspaces_root" and isinstance(default, Path):
                default = "<default-workspaces-root>"
            elif isinstance(default, Path):
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


class AuthoringCliTerminalConflictsTest(unittest.TestCase):
    def test_family_owns_the_exact_terminal_conflict_inventory(self):
        self.assertEqual(
            COMMANDS,
            {
                "terminal-conflict-resolution",
                "terminal-conflict-carry",
                "terminal-conflict-cohort-carry",
                "terminal-conflict-successor",
                "terminal-conflict-merge",
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
            "failed-prompt-hypothesis-selection",
            "terminal-conflict-resolution",
            "terminal-conflict-carry",
            "terminal-conflict-cohort-carry",
            "terminal-conflict-successor",
            "terminal-conflict-merge",
            "create-failure-reference-workspace",
        )
        start = names.index(expected[0])
        self.assertEqual(tuple(names[start : start + len(expected)]), expected)

    def test_document_commands_preserve_calls_and_json(self):
        cases = (
            (
                "terminal-conflict-resolution",
                "publish_terminal_conflict_resolution",
                argparse.Namespace(
                    command="terminal-conflict-resolution",
                    review_directory=Path("review"),
                    output=Path("resolution"),
                ),
                (Path("review"), Path("resolution")),
            ),
            (
                "terminal-conflict-successor",
                "publish_terminal_conflict_successor",
                argparse.Namespace(
                    command="terminal-conflict-successor",
                    reconciliation=Path("reconciliation.json"),
                    resolution_directory=Path("resolution"),
                    output=Path("successor"),
                ),
                (
                    Path("reconciliation.json"),
                    Path("resolution"),
                    Path("successor"),
                ),
            ),
        )
        for command, target, arguments, expected_call in cases:
            with self.subTest(command=command):
                result = Mock()
                result.to_dict.return_value = {"command": command}
                output = StringIO()
                with (
                    patch.object(terminal_cli, target, return_value=result) as call,
                    redirect_stdout(output),
                ):
                    self.assertEqual(handle(arguments), 0)
                call.assert_called_once_with(*expected_call)
                self.assertEqual(json.loads(output.getvalue()), {"command": command})

    def test_carry_commands_preserve_raw_progress_json(self):
        cases = (
            (
                "terminal-conflict-carry",
                "carry_terminal_conflict_decisions",
                argparse.Namespace(
                    command="terminal-conflict-carry",
                    source_review_directory=Path("source"),
                    target_review_directory=Path("target"),
                ),
                (Path("source"), Path("target")),
            ),
            (
                "terminal-conflict-cohort-carry",
                "carry_approved_cohort_terminal_conflict_decisions",
                argparse.Namespace(
                    command="terminal-conflict-cohort-carry",
                    review_directory=Path("review"),
                ),
                (Path("review"),),
            ),
        )
        for command, target, arguments, expected_call in cases:
            with self.subTest(command=command):
                progress = {"command": command, "carried": 2}
                output = StringIO()
                with (
                    patch.object(terminal_cli, target, return_value=progress) as call,
                    redirect_stdout(output),
                ):
                    self.assertEqual(handle(arguments), 0)
                call.assert_called_once_with(*expected_call)
                self.assertEqual(json.loads(output.getvalue()), progress)

    def test_merge_preserves_workspace_json_contract(self):
        result = SimpleNamespace(directory=Path("workspace"), created=False)
        output = StringIO()
        with (
            patch.object(
                terminal_cli,
                "merge_terminal_conflict_resolution",
                return_value=result,
            ) as merge,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="terminal-conflict-merge",
                        base_workspace=Path("base"),
                        successor_directory=Path("successor"),
                        workspaces_root=Path("workspaces"),
                    )
                ),
                0,
            )
        merge.assert_called_once_with(
            Path("base"), Path("successor"), Path("workspaces")
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {"directory": "workspace", "created": False},
        )

    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            patch.object(
                terminal_cli,
                "publish_terminal_conflict_resolution",
                side_effect=TerminalConflictResolutionError("authority changed"),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["terminal-conflict-resolution", "review", "resolution"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("authority changed", errors.getvalue())

    def test_family_handler_rejects_an_unowned_command(self):
        with self.assertRaisesRegex(
            ValueError, "Unsupported terminal-conflict command"
        ):
            handle(argparse.Namespace(command="other"))

    def test_top_level_cli_imports_only_the_terminal_conflict_family_boundary(self):
        cli_path = Path(terminal_cli.__file__).with_name("cli.py")
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        self.assertIn("vntts.authoring.cli_terminal_conflicts", imported_modules)
        self.assertFalse(
            {
                "vntts.authoring.terminal_conflict_resolution",
                "vntts.authoring.terminal_conflict_review",
                "vntts.authoring.terminal_conflict_successor",
                "vntts.authoring.terminal_conflict_workspace",
            }
            & imported_modules
        )


if __name__ == "__main__":
    unittest.main()
