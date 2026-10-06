import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.authoring.cli import create_parser
from vntts.authoring.cli_generation import _run_bulk_generation


class AuthoringGenerationCleanupTest(unittest.TestCase):
    def test_generation_cleanup_keeps_primary_failure_and_rejects_failed_release(self):
        arguments = create_parser().parse_args(
            [
                "generate",
                "--queue",
                "unused-queue.jsonl",
                "--output",
                "unused-generated",
                "--voice-manifest",
                "unused-voices.json",
                "--backend",
                "pocket-tts",
            ]
        )
        for primary in (None, ValueError("generation failed"), KeyboardInterrupt()):
            with self.subTest(primary=primary):
                cleanup_error = RuntimeError("shutdown failed")
                backend = SimpleNamespace(
                    generation_profile="stable",
                    model_name="fake",
                    shutdown=Mock(side_effect=cleanup_error),
                )
                expected = primary if primary is not None else cleanup_error
                policy = Mock()
                with (
                    patch(
                        "vntts.authoring.cli_generation.run_bulk_generation",
                        side_effect=primary,
                    ),
                    self.assertRaises(type(expected)) as raised,
                ):
                    _run_bulk_generation(
                        arguments,
                        Mock(return_value=backend),
                        None,
                        None,
                        Mock(),
                        SimpleNamespace(items=()),
                        None,
                        {},
                        None,
                        (),
                        {},
                        {},
                        policy,
                        policy,
                        None,
                    )
                self.assertIs(raised.exception, expected)
                backend.shutdown.assert_called_once_with()
