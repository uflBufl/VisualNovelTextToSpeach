import argparse
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import vntts.authoring.cli_voice_quality as voice_cli
from vntts.authoring.cli import main
from vntts.authoring.cli_voice_quality import VoiceQualityGateError, handle


class AuthoringCliVoiceQualityTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
