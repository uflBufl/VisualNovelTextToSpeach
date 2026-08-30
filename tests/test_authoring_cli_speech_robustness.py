import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vntts.authoring.asr_model import ManagedAsrModelError
from vntts.authoring.cli import create_parser
from vntts.authoring.cli_speech_robustness import (
    COMMANDS,
    SpeechRobustnessCommandError,
    handle,
)


class SpeechRobustnessCliFamilyTest(unittest.TestCase):
    def test_parser_order_and_defaults_remain_compatible(self):
        parser = create_parser()
        help_text = parser.format_help()

        self.assertLess(
            help_text.index("failure-report"),
            help_text.index("speech-robustness-corpus"),
        )
        self.assertLess(
            help_text.index("asr-model-status"),
            help_text.index("specialist-failure-plan"),
        )
        arguments = parser.parse_args(
            [
                "speech-robustness-asr",
                "corpus",
                "--output",
                "report.json",
                "--offline",
            ]
        )
        self.assertEqual(arguments.corpus, Path("corpus"))
        self.assertIsNone(arguments.model)
        self.assertTrue(arguments.offline)

    def test_family_owns_every_speech_robustness_command(self):
        self.assertEqual(
            COMMANDS,
            {
                "speech-robustness-corpus",
                "speech-robustness-check",
                "speech-robustness-asr",
                "asr-model-install",
                "asr-model-status",
            },
        )

    @patch("vntts.authoring.cli_speech_robustness.managed_asr_status")
    def test_status_preserves_sorted_json_output(self, status):
        status.return_value = {"status": "installed", "model_id": "tiny"}
        output = io.StringIO()

        with redirect_stdout(output):
            result = handle(argparse.Namespace(command="asr-model-status"))

        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"model_id": "tiny", "status": "installed"},
        )

    @patch("vntts.authoring.cli_speech_robustness.resolve_managed_asr_model")
    @patch("vntts.authoring.cli_speech_robustness.write_speech_robustness_asr_report")
    @patch("vntts.authoring.cli_speech_robustness.build_speech_robustness_asr_report")
    def test_offline_asr_resolves_managed_model_and_preserves_report_contract(
        self,
        build,
        write,
        resolve,
    ):
        resolve.return_value = Path("/managed/model")
        build.return_value = SimpleNamespace(
            report_id="report-id",
            document={"summary": {"sample_count": 2}},
        )
        arguments = argparse.Namespace(
            command="speech-robustness-asr",
            corpus=Path("corpus"),
            model=None,
            output=Path("report.json"),
            device="cpu",
            offline=True,
            progress=None,
        )
        output = io.StringIO()

        with redirect_stdout(output):
            result = handle(arguments)

        self.assertEqual(result, 0)
        resolve.assert_called_once_with()
        build.assert_called_once_with(
            Path("corpus"),
            Path("/managed/model"),
            device="cpu",
            progress_path=Path("report.json.progress.json"),
        )
        write.assert_called_once_with(build.return_value, Path("report.json").resolve())
        self.assertEqual(json.loads(output.getvalue())["report_id"], "report-id")

    @patch("vntts.authoring.cli_speech_robustness.managed_asr_status")
    def test_family_translates_domain_error_for_top_level_parser(self, status):
        status.side_effect = ManagedAsrModelError("checksum changed")

        with self.assertRaisesRegex(SpeechRobustnessCommandError, "checksum changed"):
            handle(argparse.Namespace(command="asr-model-status"))


if __name__ == "__main__":
    unittest.main()
