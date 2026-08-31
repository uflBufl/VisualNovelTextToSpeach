import argparse
import json
import unittest
from pathlib import Path
from unittest.mock import Mock

from vntts.authoring.cli import COMMAND_FAMILIES, create_parser
from vntts.authoring.cli_contract import parser_contract, parser_contract_sha256
from vntts.authoring.cli_dispatch import CommandFamily, dispatch_command


class AuthoringCliDispatchTest(unittest.TestCase):
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
