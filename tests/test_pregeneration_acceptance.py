import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from vntts.pregeneration_acceptance import (
    OfflineAcceptanceError,
    OfflineAcceptanceWorker,
)
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
        "c" * 64,
        "d" * 64,
        None,
    )
    output = root / f"generation-output-{identity[:16]}"
    result = OfflineGenerationResult(
        output,
        output / "generation-state.json",
        output / "manifest.json",
        2,
        0,
        0,
        2,
    )
    return generation_input, result


class OfflineAcceptanceWorkerTest(unittest.TestCase):
    def test_no_pending_wavs_reuses_validated_generation_result(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, generation = inputs(Path(temporary_directory))
            state = {
                "items": {
                    "a": {"status": "approved", "review_status": "approved"},
                    "b": {
                        "status": "live_fallback",
                        "review_status": "live_fallback",
                    },
                }
            }
            generator = Mock()

            with patch(
                "vntts.pregeneration_acceptance.load_generation_state",
                return_value=state,
            ):
                result = OfflineAcceptanceWorker(generator).accept(
                    generation_input,
                    generation,
                )

        generator.inspect.assert_not_called()
        self.assertIs(result.generation, generation)
        self.assertEqual(result.approved, 1)

    def test_pending_audio_is_rejected_without_a_review_decision(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, first = inputs(Path(temporary_directory))
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
                self.assertRaisesRegex(OfflineAcceptanceError, "unfinished items"),
            ):
                OfflineAcceptanceWorker().accept(generation_input, first)

    def test_cancelled_validation_does_not_load_state(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, result = inputs(Path(temporary_directory))
            cancellation = Event()
            cancellation.set()

            with patch("vntts.pregeneration_acceptance.load_generation_state") as load:
                with self.assertRaises(OfflineGenerationCancelled):
                    OfflineAcceptanceWorker(Mock()).accept(
                        generation_input,
                        result,
                        cancellation,
                    )

        load.assert_not_called()

    def test_rejects_generation_state_without_an_item_mapping(self):
        with TemporaryDirectory() as temporary_directory:
            generation_input, generation = inputs(Path(temporary_directory))
            with (
                patch(
                    "vntts.pregeneration_acceptance.load_generation_state",
                    return_value={"items": []},
                ),
                self.assertRaisesRegex(OfflineAcceptanceError, "state is invalid"),
            ):
                OfflineAcceptanceWorker().accept(generation_input, generation)


if __name__ == "__main__":
    unittest.main()
