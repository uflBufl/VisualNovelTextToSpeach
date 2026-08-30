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

import vntts.authoring.cli_audio_events as audio_event_cli
from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_audio_events import (
    COMMANDS,
    AudioEventReviewError,
    handle,
)

PARSER_CONTRACT_SHA256 = (
    "cf01c40624b070eb68db543c361acacda27c4dd590b5b838bcb9f87bf4e18bc7"
)


def _subparser_action(parser):
    return next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )


def _audio_event_parser_contract(parser):
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
                }
            )
        contract[command] = {
            "help": help_by_command[command],
            "actions": actions,
        }
    return contract


class AuthoringCliAudioEventsTest(unittest.TestCase):
    def test_family_owns_the_exact_audio_event_command_inventory(self):
        self.assertEqual(
            COMMANDS,
            {
                "audio-event-omission",
                "audio-event-projection-fallback",
                "audio-event-review-publish",
                "audio-event-review-decide",
                "audio-event-review-status",
                "audio-event-composition-publish",
                "audio-event-composition-decide",
                "audio-event-composition-status",
                "audio-event-composition-workspace",
            },
        )
        owners = [family for family in COMMAND_FAMILIES if family.commands == COMMANDS]
        self.assertEqual(len(owners), 1)
        self.assertIs(owners[0].handler, handle)

    def test_parser_contract_and_command_order_match_the_captured_legacy_cli(self):
        parser = create_parser()
        contract = _audio_event_parser_contract(parser)
        digest = hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        self.assertEqual(digest, PARSER_CONTRACT_SHA256)
        help_text = parser.format_help()
        self.assertLess(
            help_text.index("known-role-live-fallback"),
            help_text.index("audio-event-omission"),
        )
        self.assertLess(
            help_text.index("audio-event-projection-fallback"),
            help_text.index("reviewed-waveform-publication"),
        )
        self.assertLess(
            help_text.index("status"), help_text.index("audio-event-review-publish")
        )
        self.assertLess(
            help_text.index("audio-event-composition-workspace"),
            help_text.index("render-hypothesis-review-publish"),
        )

    def test_workspace_commands_preserve_json_and_call_contracts(self):
        result = SimpleNamespace(directory=Path("workspace"), created=True)
        cases = (
            (
                "audio-event-omission",
                "create_audio_event_omission_workspace",
            ),
            (
                "audio-event-projection-fallback",
                "create_audio_event_projection_fallback_workspace",
            ),
        )
        for command, target in cases:
            with self.subTest(command=command):
                output = StringIO()
                arguments = argparse.Namespace(
                    command=command,
                    base_workspace=Path("base"),
                    queue_ids=["queue-1"],
                    workspaces_root=Path("workspaces"),
                )
                with (
                    patch.object(audio_event_cli, target, return_value=result) as call,
                    redirect_stdout(output),
                ):
                    self.assertEqual(handle(arguments), 0)

                call.assert_called_once_with(
                    Path("base"), ["queue-1"], Path("workspaces")
                )
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"directory": "workspace", "created": True},
                )

    def test_review_and_composition_workspace_outputs_remain_compatible(self):
        review = Mock()
        review.to_dict.return_value = {"review": "valid"}
        review_output = StringIO()
        with (
            patch.object(
                audio_event_cli, "load_audio_event_review", return_value=review
            ),
            redirect_stdout(review_output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="audio-event-review-status",
                        directory=Path("review"),
                    )
                ),
                0,
            )
        self.assertEqual(json.loads(review_output.getvalue()), {"review": "valid"})

        workspace = SimpleNamespace(directory=Path("successor"), created=False)
        workspace_output = StringIO()
        with (
            patch.object(
                audio_event_cli,
                "create_audio_event_composition_workspace",
                return_value=workspace,
            ) as call,
            redirect_stdout(workspace_output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="audio-event-composition-workspace",
                        base_workspace=Path("base"),
                        composition=Path("composition"),
                        workspaces_root=None,
                    )
                ),
                0,
            )
        call.assert_called_once_with(Path("base"), Path("composition"), None)
        self.assertEqual(
            json.loads(workspace_output.getvalue()),
            {"created": False, "workspace": "successor"},
        )

    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            patch.object(
                audio_event_cli,
                "load_audio_event_review",
                side_effect=AudioEventReviewError("review authority changed"),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["audio-event-review-status", "review"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("review authority changed", errors.getvalue())

    def test_family_handler_rejects_an_unowned_command(self):
        with self.assertRaisesRegex(ValueError, "Unsupported audio-event command"):
            handle(argparse.Namespace(command="other"))

    def test_top_level_cli_imports_only_the_audio_event_family_boundary(self):
        cli_path = Path(audio_event_cli.__file__).with_name("cli.py")
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        self.assertIn("vntts.authoring.cli_audio_events", imported_modules)
        self.assertFalse(
            {
                "vntts.authoring.audio_event_composition",
                "vntts.authoring.audio_event_omission",
                "vntts.authoring.audio_event_projection_fallback",
                "vntts.authoring.audio_event_review",
            }
            & imported_modules
        )


if __name__ == "__main__":
    unittest.main()
