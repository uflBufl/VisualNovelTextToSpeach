import argparse
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_contract import (
    _normalized,
    parser_contract,
    parser_contract_sha256,
)
from vntts.authoring.cli_dispatch import CommandFamily, dispatch_command
from vntts.authoring.publication import AtomicPublicationError


class AuthoringCliDispatchTest(unittest.TestCase):
    def test_model_status_preserves_ascii_escaping_sorted_keys_and_newline(self):
        output = StringIO()
        with (
            patch(
                "vntts.authoring.cli_speaker_identity.managed_speaker_identity_status",
                return_value={"z": ["é"], "name": "точный"},
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["speaker-identity-model-status"]), 0)
        self.assertEqual(
            output.getvalue(),
            '{\n  "name": "\\u0442\\u043e\\u0447\\u043d\\u044b\\u0439",\n  "z": [\n    "\\u00e9"\n  ]\n}\n',
        )

    def test_contract_normalizes_absolute_and_relative_path_defaults(self):
        self.assertEqual(
            _normalized(Path.cwd() / "different-user" / "authoring" / "workspaces"),
            {"kind": "absolute-path", "name": "workspaces"},
        )
        self.assertEqual(_normalized(Path("relative/path")), "relative/path")

    def test_parser_matches_captured_semantic_contract(self):
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures" / "authoring-cli-contract-v1.json"
            ).read_text(encoding="utf-8")
        )
        parser = create_parser()
        contract = parser_contract(parser)

        self.assertEqual(list(contract), fixture["commands"])
        self.assertEqual(len(contract), fixture["command_count"])
        self.assertEqual(parser_contract_sha256(parser), fixture["sha256"])

    def test_whole_parser_snapshot_detects_help_metavar_changes(self):
        parser = create_parser()
        before = parser_contract_sha256(parser)
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        command_parser = subparsers.choices["terminal-conflict-resolution"]
        argument = next(
            action for action in command_parser._actions if action.dest != "help"
        )
        argument.metavar = "CHANGED_ARGUMENT_LABEL"
        self.assertNotEqual(parser_contract_sha256(parser), before)

    def test_migrated_parser_defaults_and_order_remain_stable(self):
        parser = create_parser()
        help_text = parser.format_help()

        self.assertLess(
            help_text.index("discover-legacy"), help_text.index("build-queue")
        )
        self.assertLess(
            help_text.index("publish-pack"), help_text.index("annotate-delivery")
        )
        legacy = parser.parse_args(["discover-legacy"])
        listening = parser.parse_args(["inspect-listening", "session"])
        delivery = parser.parse_args(["annotate-delivery", "--text", "Hello"])
        self.assertIsInstance(legacy.jobs_root, Path)
        self.assertEqual(listening.session_directory, Path("session"))
        self.assertEqual(delivery.speaker, "Narrator")
        self.assertEqual(delivery.kind, "dialogue")

    def test_every_migrated_command_has_one_family_owner(self):
        parser_commands = set(parser_contract(create_parser()))
        commands = [
            command for family in COMMAND_FAMILIES for command in family.commands
        ]

        self.assertEqual(len(commands), len(set(commands)))
        self.assertEqual(parser_commands, set(commands))

    def test_dispatch_routes_once_and_returns_none_for_unmigrated_command(self):
        handler = Mock(return_value=7)
        family = CommandFamily(frozenset({"owned"}), handler)
        owned = argparse.Namespace(command="owned")

        self.assertEqual(dispatch_command(owned, (family,)), 7)
        handler.assert_called_once_with(owned)
        self.assertIsNone(
            dispatch_command(argparse.Namespace(command="other"), (family,))
        )

    def test_operational_failures_from_registered_families_reach_the_parser(self):
        for argv in (
            ["discover-legacy"],
            ["audio-event-review-status", "review"],
        ):
            for error in (
                PermissionError("authoring storage is read-only"),
                AtomicPublicationError("atomic no-replace publication is unavailable"),
            ):
                with self.subTest(command=argv[0], error=type(error).__name__):
                    handler = Mock(side_effect=error)
                    families = tuple(
                        CommandFamily(family.commands, handler)
                        if argv[0] in family.commands
                        else family
                        for family in COMMAND_FAMILIES
                    )
                    output, errors = StringIO(), StringIO()
                    with (
                        patch("vntts.authoring.cli.COMMAND_FAMILIES", families),
                        redirect_stdout(output),
                        redirect_stderr(errors),
                        self.assertRaises(SystemExit) as exited,
                    ):
                        main(argv)
                    handler.assert_called_once()
                    self.assertEqual(exited.exception.code, 2)
                    self.assertIn(str(error), errors.getvalue())
                    self.assertNotIn("Traceback", errors.getvalue())
                    self.assertEqual(output.getvalue(), "")

    def test_programming_errors_and_interruptions_propagate_from_registered_family(
        self,
    ):
        for error in (
            RuntimeError("unexpected state"),
            ValueError("unexpected value"),
            TypeError("unexpected type"),
            KeyboardInterrupt(),
        ):
            with self.subTest(error=type(error).__name__):
                handler = Mock(side_effect=error)
                families = tuple(
                    CommandFamily(family.commands, handler)
                    if "discover-legacy" in family.commands
                    else family
                    for family in COMMAND_FAMILIES
                )
                output, errors = StringIO(), StringIO()
                with (
                    patch("vntts.authoring.cli.COMMAND_FAMILIES", families),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                    self.assertRaises(type(error)) as raised,
                ):
                    main(["discover-legacy"])
                handler.assert_called_once()
                self.assertIs(raised.exception, error)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), "")

    def test_dispatch_rejects_duplicate_family_ownership(self):
        arguments = argparse.Namespace(command="duplicate")
        families = (
            CommandFamily(frozenset({"duplicate"}), Mock()),
            CommandFamily(frozenset({"duplicate"}), Mock()),
        )

        with self.assertRaisesRegex(ValueError, "multiple families"):
            dispatch_command(arguments, families)


if __name__ == "__main__":
    unittest.main()
