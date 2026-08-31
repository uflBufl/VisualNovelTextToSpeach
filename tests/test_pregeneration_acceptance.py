import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts.pregeneration_acceptance import OfflineAcceptanceWorker
from vntts.pregeneration_generation import (
    OfflineGenerationCancelled,
    OfflineGenerationResult,
)
from vntts.pregeneration_queue import PregenerationInput


def inputs(root):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.mkdir()
    queue = directory / "queue.jsonl"
    queue.write_text("queue", encoding="utf-8")
    generation_input = PregenerationInput(
        identity,
        directory,
        directory / "story-index.jsonl",
        directory / "voice-manifest.json",
        queue,
        "b" * 64,
        2,
        2,
        (),
    )
    output = root / f"generation-output-{identity[:16]}"
    result = OfflineGenerationResult(
        output,
        output / "generation-state.json",
        output / "manifest.json",
        2,
        0,
        0,
    )
    return generation_input, result


class OfflineAcceptanceWorkerTest(unittest.TestCase):
    def test_accepts_all_pending_wavs_in_one_automatic_cohort(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first = inputs(Path(temporary_directory))
            final = OfflineGenerationResult(
                first.output, first.state, first.manifest, 2, 0, 0
            )
            generator = Mock()
            generator.inspect.return_value = final
            authorities = {"a": Mock(), "b": Mock()}
            state = {
                "items": {
                    "b": {"status": "generated", "review_status": "pending_review"},
                    "a": {"status": "generated", "review_status": "pending_review"},
                    "live": {
                        "status": "live_fallback",
                        "review_status": "live_fallback",
                    },
                }
            }

            with (
                patch(
                    "vntts.pregeneration_acceptance.load_generation_state",
                    return_value=state,
                ),
                patch(
                    "vntts.pregeneration_acceptance.generation_review_authorities",
                    return_value=authorities,
                ) as snapshot,
                patch(
                    "vntts.pregeneration_acceptance.review_generation_cohort"
                ) as commit,
            ):
                result = OfflineAcceptanceWorker(generator).accept(
                    generation_input,
                    first,
                )

        snapshot.assert_called_once_with(first.state, ("a", "b"))
        self.assertEqual(commit.call_args.args[3], "approved")
        self.assertEqual(commit.call_args.kwargs["provenance"]["human_reviewed"], False)
        self.assertEqual(result.approved, 2)
        self.assertIs(result.generation, final)

    def test_cancelled_acceptance_does_not_snapshot_or_commit(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, result = inputs(Path(temporary_directory))
            cancellation = Event()
            cancellation.set()

            with patch(
                "vntts.pregeneration_acceptance.generation_review_authorities"
            ) as snapshot:
                with self.assertRaises(OfflineGenerationCancelled):
                    OfflineAcceptanceWorker(Mock()).accept(
                        generation_input,
                        result,
                        cancellation,
                    )

        snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
