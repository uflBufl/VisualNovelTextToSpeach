import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue
from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.test_authoring_bulk_generation import SyntheticRenderer
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.bulk_generation import (
    authorize_live_fallback,
    is_spoken_queue_item,
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


def fixture(root, names=("generated", "fallback"), *, include_omission=False):
    identity = "a" * 64
    directory = root / f"generation-input-{identity[:16]}"
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    items = [item(name, sequence) for sequence, name in enumerate(names, 1)]
    if include_omission:
        text = "*chirp*"
        event = item("omission", len(items) + 1)
        event.update(
            text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
        event["queue_id"] = f"pack:omission:{event['text_sha256'][:16]}"
        event["vntts.authoring.audio_event_plan"] = audio_event_plan_for_record(event)
        items.append(event)
    items = tuple(items)
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
        audio_event_omission_queue_ids=(items[-1]["queue_id"],)
        if include_omission
        else (),
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
        item_filter=is_spoken_queue_item,
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
            with patch(
                "vntts.pregeneration_pack.load_generation_state",
                side_effect=AssertionError("published pack must be reused"),
            ):
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

    def test_publishes_exact_pure_event_omission_without_a_wav(self):
        with TemporaryDirectory() as temporary_directory:
            job, generation_input, generation_result, items = fixture(
                Path(temporary_directory),
                include_omission=True,
            )

            pack = OfflinePackPublisher().publish(
                job,
                generation_input,
                generation_result,
            )
            library = GeneratedAudioLibrary.load_optional(
                pack.imported.generated_audio_manifest
            )

        self.assertEqual(pack.omissions, 1)
        self.assertIsNotNone(
            library.find_audio_event_omission(
                items[-1]["line_id"], items[-1]["text_sha256"]
            )
        )

    def test_second_selection_publishes_an_immutable_cumulative_successor(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base_job, base_input, base_result, base_items = fixture(
                root / "base", include_omission=True
            )
            base = OfflinePackPublisher().publish(
                base_job,
                base_input,
                base_result,
            )
            base_hashes = (
                sha256_file(base.manifest),
                sha256_file(base.imported.generated_audio_manifest),
            )
            current_job, current_input, current_result, current_items = fixture(
                root / "current",
                names=("generated", "new"),
            )
            base_story = load_story_index_document(base_job.story_index)
            current_story = load_story_index_document(current_job.story_index)
            records = {
                record.line_id: record.to_record()
                for record in (*base_story.records, *current_story.records)
            }
            source_story = root / "source" / "story-index.jsonl"
            source_story.parent.mkdir(parents=True)
            write_story_index_document(
                source_story,
                current_story.metadata,
                records.values(),
            )
            current_job = replace(
                current_job,
                story_index=str(source_story),
                story_index_sha256=sha256_file(source_story),
            )

            successor = OfflinePackPublisher(base_pack=base.manifest).publish(
                current_job,
                current_input,
                current_result,
            )
            successor_story = load_story_index_document(successor.imported.story_index)
            library = GeneratedAudioLibrary.load_optional(
                successor.imported.generated_audio_manifest
            )
            current_found = (
                library.find(
                    current_items[0]["line_id"], current_items[0]["text_sha256"]
                )
                is not None
            )
            live_fallbacks = set(library.live_fallbacks)
            retained_omission = library.find_audio_event_omission(
                base_items[-1]["line_id"], base_items[-1]["text_sha256"]
            )
            base_unchanged = base_hashes == (
                sha256_file(base.manifest),
                sha256_file(base.imported.generated_audio_manifest),
            )

        self.assertNotEqual(successor.identity, base.identity)
        self.assertEqual(len(successor_story.records), 4)
        self.assertEqual(successor.story_lines, 4)
        self.assertEqual(successor.approved, 1)
        self.assertEqual(successor.live_fallbacks, 2)
        self.assertTrue(current_found)
        self.assertIsNotNone(retained_omission)
        self.assertEqual(successor.omissions, 1)
        self.assertIn(
            (base_items[1]["line_id"], base_items[1]["text_sha256"]),
            live_fallbacks,
        )
        self.assertIn(
            (current_items[1]["line_id"], current_items[1]["text_sha256"]),
            live_fallbacks,
        )
        self.assertTrue(base_unchanged)


if __name__ == "__main__":
    unittest.main()
