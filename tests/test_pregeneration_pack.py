import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError
from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue
from vntts_artifacts.voice_manifest import write_voice_manifest

from tests.test_authoring_bulk_generation import SyntheticRenderer
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    authorize_live_fallback,
    is_spoken_queue_item,
    review_generation_item,
    run_bulk_generation,
)
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_generation import OfflineGenerationResult
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    inspect_story_audio,
)
from vntts.pregeneration_queue import PregenerationInput
from vntts.pregeneration_setup import (
    PregenerationJob,
    PregenerationJobStore,
    PreparationEstimate,
    inspect_story_index,
)
from vntts.synthesis import SynthesisCompletion
from vntts.versioned_json import write_versioned_json


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


def fixture(
    root,
    names=("generated", "fallback"),
    *,
    include_omission=False,
    omission_source_audio_status="absent",
):
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
            source_audio_status=omission_source_audio_status,
            source_audio_reason=f"fixture_{omission_source_audio_status}",
        )
        event["action"] = {
            "absent": "generate",
            "unavailable": "prefer_source_audio",
            "unknown": "resolve_audio",
        }[omission_source_audio_status]
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
                "source_audio_status": value.get("source_audio_status", "absent"),
                "source_audio_reason": value.get(
                    "source_audio_reason", "fixture_absent"
                ),
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
    def test_original_readiness_requires_full_valid_declared_source_duration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "story.jsonl"
            store = PregenerationJobStore(Path(directory) / "jobs")
            source = {
                "record_type": "line",
                "line_id": "source-only",
                "chapter": "1",
                "sequence": 1,
                "speaker": "Narrator",
                "text": "A game line.",
                "kind": "dialogue",
                "source_audio_status": "available",
                "speakable": True,
            }
            for contract, fields, original in (
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "full",
                    },
                    1,
                ),
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "partial",
                    },
                    0,
                ),
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "unknown",
                    },
                    0,
                ),
                ("duration-seconds", {"source_audio_duration_seconds": 1.0}, 0),
                ("duration-seconds", {"source_audio_completeness": "full"}, 0),
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": -1,
                        "source_audio_completeness": "full",
                    },
                    0,
                ),
                (
                    None,
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "full",
                    },
                    0,
                ),
                (
                    "verified-media-duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "full",
                    },
                    0,
                ),
            ):
                with self.subTest(contract=contract, fields=fields):
                    metadata = {"game": "Synthetic Game", "language": "en"}
                    if contract is not None:
                        metadata["source_audio_completion"] = contract
                    write_story_index_document(path, metadata, [source | fields])
                    content = inspect_story_index(path)
                    coverage = inspect_story_audio(
                        content, content.selections[0].selection_id, store
                    )
                    self.assertEqual(
                        (coverage.original, coverage.missing), (original, 1 - original)
                    )

    def test_story_coverage_verifies_mixed_saved_routes_and_rejects_damaged_audio(self):
        with TemporaryDirectory() as directory:
            store = PregenerationJobStore(Path(directory) / "jobs")
            job, inputs, result, _items = fixture(
                store.root / ("b" * 24), include_omission=True
            )
            story = load_story_index_document(inputs.story_index)
            rows = [record.document for record in story.records]
            for row in rows[:2]:
                row.update(
                    source_audio_status="available",
                    source_audio_duration_seconds=1.0,
                    source_audio_completeness="partial",
                )
            write_story_index_document(
                inputs.story_index,
                story.metadata | {"source_audio_completion": "duration-seconds"},
                rows,
            )
            job = replace(job, story_index_sha256=sha256_file(inputs.story_index))
            content = inspect_story_index(inputs.story_index)
            selection = content.selections[0]
            job = replace(job, selected_story_ids=(selection.selection_id,))
            write_versioned_json(store.path_for(job.job_id), 1, job.to_document())
            absent = inspect_story_audio(content, selection.selection_id, store)
            self.assertIsNone(absent.manifest)
            self.assertEqual(absent.missing, 3)
            pack = OfflinePackPublisher().publish(job, inputs, result)
            coverage = inspect_story_audio(content, selection.selection_id, store)
            self.assertEqual(coverage.manifest.resolve(), pack.manifest)
            self.assertEqual(
                (coverage.generated, coverage.live, coverage.omitted, coverage.missing),
                (1, 1, 1, 0),
            )
            single_line = replace(
                content,
                selections=(replace(selection, line_ids=(selection.line_ids[0],)),),
            )
            scoped = inspect_story_audio(single_line, selection.selection_id, store)
            self.assertEqual((scoped.generated, scoped.live, scoped.omitted), (1, 0, 0))
            active = inspect_story_audio(
                single_line,
                selection.selection_id,
                PregenerationJobStore(Path(directory) / "no-saved-jobs"),
                manifest=pack.manifest,
            )
            self.assertEqual((active.generated, active.live, active.missing), (1, 0, 0))
            outside = Path(directory) / "outside-story.jsonl"
            write_story_index_document(
                outside,
                {"game": "Synthetic Game", "language": "en"},
                [
                    {
                        "record_type": "line",
                        "line_id": "outside-pack",
                        "chapter": "2",
                        "sequence": 1,
                        "speaker": "Narrator",
                        "text": "Original outside this pack.",
                        "kind": "dialogue",
                        "source_audio_status": "available",
                        "speakable": True,
                    }
                ],
            )
            outside_content = inspect_story_index(outside)
            uncovered = inspect_story_audio(
                outside_content,
                outside_content.selections[0].selection_id,
                store,
                manifest=pack.manifest,
            )
            self.assertEqual((uncovered.original, uncovered.missing), (0, 1))
            library = GeneratedAudioLibrary.load_optional(
                pack.imported.generated_audio_manifest
            )
            library.index.entries[0].audio.write_bytes(b"damaged")
            with self.assertRaises((OfflinePackError, GamePackError)):
                inspect_story_audio(content, selection.selection_id, store)

    def test_change_summary_uses_verified_resume_and_exact_replacement_candidates(self):
        with TemporaryDirectory() as directory:
            job, inputs, result, _items = fixture(Path(directory))
            publisher = OfflinePackPublisher()
            resumed = publisher.inspect_changes(job, inputs)
            self.assertEqual(
                (resumed.reused, resumed.new, resumed.live_fallbacks), (1, 0, 1)
            )
            base = publisher.publish(job, inputs, result)
            publisher = OfflinePackPublisher(base_pack=base.manifest)
            unchanged = publisher.inspect_changes(job, inputs)
            self.assertEqual(unchanged.replacement_candidates, 0)
            fresh = publisher.inspect_changes(job, replace(inputs, identity="c" * 64))
            self.assertEqual(
                (fresh.reused, fresh.new, fresh.replacement_candidates), (0, 2, 1)
            )
            self.assertEqual(fresh.live_fallbacks, 0)
            self.assertFalse(fresh.switches_pack)
            state = json.loads(result.state.read_text())
            saved = next(
                item for item in state["items"].values() if item["status"] == "approved"
            )
            (result.output / saved["path"]).write_bytes(b"broken")
            with self.assertRaises(BulkGenerationError):
                publisher.inspect_changes(job, inputs)

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

    def test_publishes_pure_event_omission_when_game_audio_is_unavailable(self):
        with TemporaryDirectory() as temporary_directory:
            job, generation_input, generation_result, items = fixture(
                Path(temporary_directory),
                include_omission=True,
                omission_source_audio_status="unavailable",
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

    def test_rejects_unresolved_pure_event_omission(self):
        with TemporaryDirectory() as temporary_directory:
            job, generation_input, generation_result, _items = fixture(
                Path(temporary_directory),
                include_omission=True,
                omission_source_audio_status="unknown",
            )

            with self.assertRaisesRegex(
                OfflinePackError,
                "Offline audio-event omission is invalid",
            ):
                OfflinePackPublisher().publish(
                    job,
                    generation_input,
                    generation_result,
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
