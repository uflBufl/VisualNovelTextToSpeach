import argparse
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import vntts.authoring.cli_terminal_conflicts as terminal_cli
from vntts.authoring.cli import create_parser, main
from vntts.authoring.cli_terminal_conflicts import (
    TerminalConflictResolutionError,
    handle,
)


class AuthoringCliTerminalConflictsTest(unittest.TestCase):
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

    def test_carry_io_failures_remain_raw_and_become_parser_errors(self):
        for target, arguments in (
            (
                "carry_terminal_conflict_decisions",
                ["terminal-conflict-carry", "source", "target"],
            ),
            (
                "carry_approved_cohort_terminal_conflict_decisions",
                ["terminal-conflict-cohort-carry", "review"],
            ),
        ):
            failure = PermissionError("terminal progress is unwritable")
            with (
                self.subTest(command=arguments[0]),
                patch.object(terminal_cli, target, side_effect=failure),
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "terminal progress is unwritable",
                ) as raised:
                    handle(create_parser().parse_args(arguments))
                self.assertIs(raised.exception, failure)
                output, errors = StringIO(), StringIO()
                with (
                    redirect_stdout(output),
                    redirect_stderr(errors),
                    self.assertRaises(SystemExit) as exited,
                ):
                    main(arguments)
                self.assertEqual(exited.exception.code, 2)
                self.assertIn("terminal progress is unwritable", errors.getvalue())
                self.assertEqual(output.getvalue(), "")

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


if __name__ == "__main__":
    unittest.main()
