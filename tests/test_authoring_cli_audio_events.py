import argparse
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import vntts.authoring.cli_audio_events as audio_event_cli
from vntts.authoring.cli import create_parser, main
from vntts.authoring.cli_audio_events import (
    AudioEventReviewError,
    handle,
)


class AuthoringCliAudioEventsTest(unittest.TestCase):
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

    def test_publication_io_errors_remain_raw_and_reach_the_parser(self):
        cases = (
            (
                "publish_source_audio_event_review",
                [
                    "audio-event-review-publish",
                    "queue.jsonl",
                    "queue-1",
                    "story.json",
                    "source.wav",
                    "--output",
                    "review",
                    "--source-line-id",
                    "line-1",
                    "--source-speaker",
                    "speaker",
                    "--source-event",
                    "event",
                    "--source-bank",
                    "bank",
                    "--source-media-id",
                    "1",
                    "--source-audio-id",
                    "audio-1",
                ],
            ),
            (
                "publish_audio_event_composition",
                [
                    "audio-event-composition-publish",
                    "review",
                    "--output",
                    "composition",
                ],
            ),
        )
        for target, argv in cases:
            with self.subTest(target=target):
                error = PermissionError("audio-event storage is read-only")
                stdout, stderr = StringIO(), StringIO()
                with (
                    patch.object(audio_event_cli, target, side_effect=error),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    with self.assertRaises(PermissionError) as caught:
                        handle(create_parser().parse_args(argv))
                    self.assertIs(caught.exception, error)
                    with self.assertRaises(SystemExit) as exited:
                        main(argv)
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(str(error), stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
