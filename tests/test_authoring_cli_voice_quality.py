import argparse
import ast
import hashlib
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import vntts.authoring.cli_voice_quality as voice_cli
from vntts.authoring.cli import COMMAND_FAMILIES, create_parser, main
from vntts.authoring.cli_voice_quality import COMMANDS, VoiceQualityGateError, handle

PARSER_CONTRACT_SHA256 = (
    "0d80e497c7db029b094a5c3d4e77720a9df8cb86db982c98d145a61f29ecf2e3"
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


class AuthoringCliVoiceQualityTest(unittest.TestCase):
    def test_family_owns_the_exact_voice_quality_inventory(self):
        self.assertEqual(
            COMMANDS,
            {
                "voice-quality-gate",
                "voice-quality-check",
                "voice-repair-comparison-plan",
                "voice-repair-candidate-workspace",
                "voice-repair-candidate-command",
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
            "failure-reference-binding",
            "voice-quality-gate",
            "voice-quality-check",
            "voice-repair-comparison-plan",
            "voice-repair-candidate-workspace",
            "voice-repair-candidate-command",
            "missing-voice-reuse-plan",
        )
        start = names.index(expected[0])
        self.assertEqual(tuple(names[start : start + len(expected)]), expected)

    def test_gate_preserves_load_build_write_and_json_contract(self):
        plan = object()
        decision = object()
        gate = Mock()
        gate.to_dict.return_value = {"gate": "exact"}
        output = StringIO()
        with (
            patch.object(
                voice_cli, "load_cohort_review_plan", return_value=plan
            ) as load_plan,
            patch.object(
                voice_cli, "load_cohort_review_decision", return_value=decision
            ) as load_decision,
            patch.object(
                voice_cli, "build_voice_quality_gate", return_value=gate
            ) as build,
            patch.object(voice_cli, "write_voice_quality_gate") as write,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="voice-quality-gate",
                        workspace=Path("workspace"),
                        plan=Path("plan.json"),
                        decision=Path("decision.json"),
                        output=Path("gate.json"),
                    )
                ),
                0,
            )
        load_plan.assert_called_once_with(Path("plan.json"))
        load_decision.assert_called_once_with(Path("decision.json"))
        build.assert_called_once_with(Path("workspace"), plan, decision)
        write.assert_called_once_with(gate, Path("gate.json"))
        self.assertEqual(json.loads(output.getvalue()), {"gate": "exact"})

    def test_repair_plan_preserves_implicit_and_explicit_profiles(self):
        for configured, expected in (
            (None, ("stable", "natural")),
            (["natural", "stable"], ("natural", "stable")),
        ):
            with self.subTest(configured=configured):
                plan = Mock()
                plan.to_dict.return_value = {"profiles": list(expected)}
                output = StringIO()
                with (
                    patch.object(
                        voice_cli,
                        "build_voice_repair_comparison_plan",
                        return_value=plan,
                    ) as build,
                    patch.object(
                        voice_cli, "write_voice_repair_comparison_plan"
                    ) as write,
                    redirect_stdout(output),
                ):
                    self.assertEqual(
                        handle(
                            argparse.Namespace(
                                command="voice-repair-comparison-plan",
                                workspace=Path("workspace"),
                                character="Rhiannon",
                                generation_profiles=configured,
                                output=Path("plan.json"),
                            )
                        ),
                        0,
                    )
                build.assert_called_once_with(
                    Path("workspace"),
                    "Rhiannon",
                    generation_profiles=expected,
                )
                write.assert_called_once_with(plan, Path("plan.json"))

    def test_candidate_command_preserves_argv_json_contract(self):
        plan = object()
        output = StringIO()
        with (
            patch.object(
                voice_cli, "load_voice_repair_comparison_plan", return_value=plan
            ) as load,
            patch.object(
                voice_cli,
                "build_voice_repair_candidate_command",
                return_value=("uv", "run", "vntts-pregenerate"),
            ) as build,
            redirect_stdout(output),
        ):
            self.assertEqual(
                handle(
                    argparse.Namespace(
                        command="voice-repair-candidate-command",
                        plan=Path("plan.json"),
                        candidate_id="candidate-1",
                        workspace=Path("workspace"),
                    )
                ),
                0,
            )
        load.assert_called_once_with(Path("plan.json"))
        build.assert_called_once_with(plan, "candidate-1", Path("workspace"))
        self.assertEqual(
            json.loads(output.getvalue()),
            {"command": ["uv", "run", "vntts-pregenerate"]},
        )

    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            patch.object(
                voice_cli,
                "load_voice_quality_gate",
                side_effect=VoiceQualityGateError("gate authority changed"),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["voice-quality-check", "gate.json", "workspace", "queue-1"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("gate authority changed", errors.getvalue())

    def test_family_handler_rejects_an_unowned_command(self):
        with self.assertRaisesRegex(ValueError, "Unsupported voice-quality command"):
            handle(argparse.Namespace(command="other"))

    def test_top_level_cli_imports_only_the_voice_quality_family_boundary(self):
        cli_path = Path(voice_cli.__file__).with_name("cli.py")
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }

        self.assertIn("vntts.authoring.cli_voice_quality", imported_modules)
        self.assertFalse(
            {
                "vntts.authoring.cohort_review",
                "vntts.authoring.voice_quality_gate",
                "vntts.authoring.voice_repair_comparison",
            }
            & imported_modules
        )


if __name__ == "__main__":
    unittest.main()
