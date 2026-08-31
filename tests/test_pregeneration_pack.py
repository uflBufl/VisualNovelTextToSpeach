import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue
from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.test_authoring_bulk_generation import SyntheticRenderer
from vntts.authoring.bulk_generation import (
    authorize_live_fallback,
    review_generation_item,
    run_bulk_generation,
)
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_generation import OfflineGenerationResult
from vntts.pregeneration_pack import OfflinePackPublisher
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import (
    PregenerationJob,
    PreparationEstimate,
)
from vntts.synthesis import SynthesisCompletion


def item(name, sequence):
    text = f"Prepared line {name}."
    text_sha256 = hashlib.sha256(text.encode()).hexdigest()
    return {
        "record_type": "generation_item",
        "queue_id": f"pack:{name}:{text_sha256[:16]}",
        "line_id": f"pack:{name}",
        "text_sha256": text_sha256,
        "text": text,
        "speaker": "Narrator",
        "voice_character": "Narrator",
        "action": "generate",
        "prompt_adapters": {},
        "sequence": sequence,
    }


def fixture(root):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.mkdir()
    items = (item("generated", 1), item("fallback", 2))
    story = directory / "story-index.jsonl"
    write_story_index_document(
        story,
        {"game": "Synthetic Game", "language": "en"},
        [
            {
                "record_type": "line",
                "line_id": value["line_id"],
                "chapter": "1",
                "sequence": value["sequence"],
                "speaker": value["speaker"],
                "voice_character": value["voice_character"],
                "text": value["text"],
                "kind": "dialogue",
                "source_audio_status": "absent",
                "speakable": True,
            }
            for value in items
        ],
    )
    voices = directory / "voice-manifest.json"
    write_voice_manifest(
        voices,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Narrator",
                    "speaker": "alba",
                    "aliases": [],
                    "references": [],
                }
            ],
        },
    )
    queue = write_voice_generation_queue(
        directory / "queue.jsonl",
        {"game": "Synthetic Game", "language": "en"},
        items,
    )
    generation_input = PregenerationInput(
        identity,
        directory,
        story,
        voices,
        queue,
        sha256_file(queue),
        2,
        2,
        (),
    )
    output = root / f"generation-output-{identity[:16]}"
    renderer = SyntheticRenderer(
        [SynthesisCompletion.COMPLETE, SynthesisCompletion.LIMITED]
    )
    renderer.name = "pocket-tts"
    renderer.model_name = "pocket-tts"
    generated = run_bulk_generation(
        queue,
        output,
        renderer,
        provider="pocket-tts",
        model="pocket-tts",
        generation_profile="default",
        retries=0,
    )
    review_generation_item(generated.state, items[0]["queue_id"], "approved")
    authorize_live_fallback(
        generated.state,
        queue,
        items[1]["queue_id"],
        reason="automatic_recovery_exhausted",
        model="pocket-tts",
    )
    result = OfflineGenerationResult(
        output,
        generated.state,
        generated.manifest,
        1,
        0,
        1,
    )
    job = PregenerationJob(
        job_id="b" * 24,
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T00:00:00+00:00",
        status="planned",
        provider_id="synthetic",
        game="Synthetic Game",
        game_version="1.0",
        story_index=str(story),
        story_index_sha256=sha256_file(story),
        selected_story_ids=("chapter-1",),
        selected_line_ids=tuple(value["line_id"] for value in items),
        estimate=PreparationEstimate(2, 0, 2, 1, 1, 1000),
    )
    return job, generation_input, result, items


class OfflinePackPublisherTest(unittest.TestCase):
    def test_publishes_and_reuses_portable_generated_and_live_routes(self):
        with TemporaryDirectory() as temporary_directory:
            job, generation_input, generation_result, items = fixture(
                Path(temporary_directory)
            )
            publisher = OfflinePackPublisher()

            first = publisher.publish(job, generation_input, generation_result)
            second = publisher.publish(job, generation_input, generation_result)
            library = GeneratedAudioLibrary.load_optional(
                first.imported.generated_audio_manifest
            )
            self.assertEqual(first, second)
            self.assertEqual(first.approved, 1)
            self.assertEqual(first.live_fallbacks, 1)
            self.assertIsNotNone(
                library.find(items[0]["line_id"], items[0]["text_sha256"])
            )
            self.assertIn(
                (items[1]["line_id"], items[1]["text_sha256"]),
                library.live_fallbacks,
            )


if __name__ == "__main__":
    unittest.main()
