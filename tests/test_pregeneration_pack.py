import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.game_pack import GamePackError, write_game_pack
from vntts_artifacts.generated_audio import load_generated_audio_document
from vntts_artifacts.live_sequence import (
    load_live_sequence_plan,
    write_live_sequence_plan,
)
from vntts_artifacts.story_index import (
    load_story_index_document,
    write_story_index_document,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)
from vntts_artifacts.voice_manifest import load_voice_manifest, write_voice_manifest

from tests.authoring_fixtures import tree_hashes
from tests.pregeneration_fixtures import fixture
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.story_fixtures import write_source_audio_semantic_evidence
from vntts import pregeneration_pack as pack_module
from vntts.authoring.bulk_generation import BulkGenerationError
from vntts.authoring.generation_state import load_generation_state_from_snapshot
from vntts.game_pack import import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_contract import OfflineGenerationCancelled
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    StoryAudioInspector,
    _copy_file,
    _copy_prepared_file,
    _ensure_pack_disk_space,
    _link_verified_file,
    _load_incremental_base,
    _load_terminal_generation,
    _optional_sequence_snapshot,
    _portable_generated_record,
    _portable_voice_entries,
    _stage_live_sequence,
    _story_audio_pack,
    _validate_inputs,
    _write_cumulative_routes,
    inspect_story_audio,
    load_saved_pack,
)
from vntts.pregeneration_setup import (
    PregenerationJobStore,
    _cached_story_index_document,
    inspect_story_index,
)
from vntts.source_audio_semantics import (
    SEMANTIC_EVIDENCE_METHOD,
    SourceAudioSemanticEvidenceError,
)
from vntts.story_index_snapshot import load_story_index_snapshot
from vntts.versioned_json import write_versioned_json


def game_fixture(
    root,
    *,
    game_id=None,
    provider_id="reverse1999",
    chapter="1",
    names=("generated", "fallback"),
):
    job, prepared, generated, items = fixture(root, names=names)
    story = load_story_index_document(prepared.story_index)
    metadata = story.metadata | {"game": "Reverse: 1999"}
    if game_id is not None:
        metadata["game_id"] = game_id
    write_story_index_document(
        prepared.story_index,
        metadata,
        [record.to_record() | {"chapter": chapter} for record in story.records],
    )
    checksum = sha256_file(prepared.story_index)
    return (
        replace(
            job,
            provider_id=provider_id,
            game="Reverse: 1999",
            story_index_sha256=checksum,
        ),
        replace(prepared, story_index_sha256=checksum),
        generated,
        items,
    )


def linear_plan(story_path, game_id):
    story = load_story_index_document(story_path)
    chapters = []
    for chapter in dict.fromkeys(record.chapter for record in story.records):
        records = [record for record in story.records if record.chapter == chapter]
        events = [
            {
                "event_id": f"{chapter}:{index}",
                "sequence": index,
                "kind": "speech",
                "control": "terminal" if index == len(records) else "automatic",
                "successors": []
                if index == len(records)
                else [f"{chapter}:{index + 1}"],
                "line_id": record.line_id,
            }
            for index, record in enumerate(records, 1)
        ]
        chapters.append(
            {"chapter": chapter, "entry_event_ids": [f"{chapter}:1"], "events": events}
        )
    return write_live_sequence_plan(
        story_path.with_name("live-sequence.json"),
        {
            "schema": "vntts.live-sequence-plan",
            "schema_version": 1,
            "game_id": game_id,
            "producer": {"name": "test", "version": "1"},
            "story_index_sha256": sha256_file(story_path),
            "source_extract_sha256": "a" * 64,
            "chapters": chapters,
        },
        story_path,
    ).path


class SavedStoryPackSelectionTest(unittest.TestCase):
    def test_one_shot_selection_filters_every_saved_job(self):
        matching = SimpleNamespace(selected_line_ids=("selected",))
        other = SimpleNamespace(selected_line_ids=("other",))
        store = Mock()
        store.jobs_for_content.return_value = (matching, other)
        store.published_packs.return_value = ()
        result = _story_audio_pack(
            object(),
            iter(("selected",)),
            store,
            manifest=None,
            imported_pack=None,
            pack_cache={},
        )
        store.published_packs.assert_called_once_with(matching)
        self.assertIsNone(result.manifest)
        self.assertFalse(result.explicit)


class OfflinePackPublisherTest(unittest.TestCase):
    def test_legacy_adapter_and_explicit_imported_identity_publish_and_import(self):
        for provider, explicit_id, expected in (
            ("reverse1999", None, "reverse1999"),
            ("local-story-index", "custom-game", "custom-game"),
        ):
            with self.subTest(provider=provider), TemporaryDirectory() as directory:
                root = Path(directory)
                job, prepared, generated, _ = game_fixture(
                    root, provider_id=provider, game_id=explicit_id
                )
                plan = linear_plan(prepared.story_index, expected)
                before = tree_hashes(generated.output)
                published = OfflinePackPublisher().publish(
                    job, prepared, generated, live_sequence_plan=plan
                )
                imported = import_game_pack(published.manifest)
                self.assertEqual(imported.pack.game_id, expected)
                self.assertEqual(
                    load_story_index_document(imported.story_index).game,
                    "Reverse: 1999",
                )
                self.assertIsNotNone(imported.live_sequence_plan)
                self.assertEqual(
                    load_live_sequence_plan(
                        imported.live_sequence_plan, imported.story_index
                    ).game_id,
                    expected,
                )
                self.assertEqual(tree_hashes(generated.output), before)
                self.assertEqual(
                    OfflinePackPublisher().publish(
                        job, prepared, generated, live_sequence_plan=plan
                    ),
                    published,
                )

    def test_foreign_graph_is_rejected_without_inference_from_the_display_title(self):
        for explicit_id in (None, "foreign-game"):
            with (
                self.subTest(explicit_id=explicit_id),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                job, prepared, generated, _ = game_fixture(
                    root, provider_id="local-story-index", game_id=explicit_id
                )
                plan = linear_plan(prepared.story_index, "reverse1999")
                before = tree_hashes(root)
                with self.assertRaisesRegex(OfflinePackError, "different game"):
                    OfflinePackPublisher().publish(
                        job, prepared, generated, live_sequence_plan=plan
                    )
                self.assertEqual(tree_hashes(root), before)

    def test_publication_cache_identity_includes_the_game_id_and_version(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, prepared, generated, _ = fixture(root)
            before = tree_hashes(generated.output)
            first = OfflinePackPublisher().publish(job, prepared, generated)
            different_game = OfflinePackPublisher().publish(
                replace(job, game_id="explicit-import-id"), prepared, generated
            )
            different_version = OfflinePackPublisher().publish(
                replace(job, game_version="2.0"), prepared, generated
            )
            self.assertEqual(
                len(
                    {
                        first.identity,
                        different_game.identity,
                        different_version.identity,
                    }
                ),
                3,
            )
            self.assertEqual(different_game.imported.pack.game_id, "explicit-import-id")
            self.assertEqual(different_version.imported.pack.game_version, "2.0")
            self.assertEqual(tree_hashes(generated.output), before)

    def test_reused_destination_rejects_changed_pack_game_metadata(self):
        for field in ("id", "version"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                job, prepared, generated, _ = fixture(Path(directory))
                published = OfflinePackPublisher().publish(job, prepared, generated)
                document = json.loads(published.manifest.read_bytes())
                document["game"][field] = "foreign"
                atomic_write_json(published.manifest, document)
                with self.assertRaisesRegex(OfflinePackError, "game identity changed"):
                    OfflinePackPublisher().publish(job, prepared, generated)

    def test_canonical_identity_survives_incremental_successor_reuse(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base_job, base_input, base_result, _ = game_fixture(root / "base")
            base_plan = linear_plan(base_input.story_index, "reverse1999")
            base = OfflinePackPublisher().publish(
                base_job, base_input, base_result, live_sequence_plan=base_plan
            )
            base_hashes = tree_hashes(base.directory)
            job, prepared, generated, _ = game_fixture(
                root / "current", chapter="2", names=("next", "last")
            )
            current_plan = linear_plan(prepared.story_index, "reverse1999")
            base_story = load_story_index_document(base_input.story_index)
            current_story = load_story_index_document(prepared.story_index)
            source = root / "source.jsonl"
            write_story_index_document(
                source,
                current_story.metadata,
                [
                    record.to_record()
                    for record in (*base_story.records, *current_story.records)
                ],
            )
            job = replace(
                job, story_index=str(source), story_index_sha256=sha256_file(source)
            )
            successor = OfflinePackPublisher(base_pack=base.manifest).publish(
                job, prepared, generated, live_sequence_plan=current_plan
            )
            self.assertEqual(successor.imported.pack.game_id, "reverse1999")
            self.assertEqual(successor.story_lines, 4)
            self.assertEqual(successor.approved, 2)
            self.assertEqual(
                len(
                    load_live_sequence_plan(
                        successor.imported.live_sequence_plan,
                        successor.imported.story_index,
                    ).chapters
                ),
                2,
            )
            self.assertEqual(tree_hashes(base.directory), base_hashes)
            base_wav = next(iter(base.imported.pack.generated_wavs))
            reused = successor.directory / base_wav.path.relative_to(
                base.directory.resolve()
            )
            self.assertEqual(sha256_file(reused), base_wav.sha256)
            self.assertTrue(reused.samefile(base_wav.path))

    def test_story_batch_reuses_each_pack_parse_but_new_batch_imports_again(self):
        with TemporaryDirectory() as directory:
            job, inputs, result, _items = fixture(Path(directory) / "source")
            published = OfflinePackPublisher().publish(job, inputs, result)
            content = inspect_story_index(inputs.story_index)
            selection = content.selections[0]
            content = replace(
                content,
                selections=tuple(
                    replace(
                        selection, selection_id=f"stage-{index}", line_ids=(line_id,)
                    )
                    for index, line_id in enumerate(selection.line_ids)
                ),
            )
            store = Mock()
            store.jobs_for_content.return_value = (job,)
            store.published_packs.return_value = (published.manifest,)
            with (
                patch(
                    "vntts.pregeneration_pack.import_game_pack", wraps=import_game_pack
                ) as imported,
                patch(
                    "vntts.pregeneration_pack.load_generated_audio_document",
                    wraps=load_generated_audio_document,
                ) as parsed,
                patch(
                    "vntts.pregeneration_pack.load_verified_story_index_document",
                    wraps=pack_module.load_verified_story_index_document,
                ) as source,
            ):
                inspector = StoryAudioInspector(content, store)
                first = inspector.inspect("stage-0", manifest=published.manifest)
                second = inspector.inspect("stage-1")
                self.assertEqual((first.generated, second.live), (1, 1))
                self.assertEqual(imported.call_count, 1)
                self.assertEqual(parsed.call_count, 1)
                self.assertEqual(source.call_count, 2)  # Catalog and bound pack story.
                StoryAudioInspector(content, store).inspect(
                    "stage-0", manifest=published.manifest
                )
                self.assertEqual(imported.call_count, 2)
                self.assertEqual(source.call_count, 4)

    def test_story_coverage_rejects_a_missing_selected_line(self):
        with TemporaryDirectory() as directory:
            _job, inputs, _result, _items = fixture(Path(directory) / "source")
            content = inspect_story_index(inputs.story_index)
            selection = replace(content.selections[0], line_ids=("missing-line",))
            content = replace(content, selections=(selection,))
            with self.assertRaisesRegex(
                OfflinePackError, "Selected story lines changed"
            ):
                inspect_story_audio(
                    content,
                    selection.selection_id,
                    PregenerationJobStore(Path(directory) / "jobs"),
                )

    def test_story_batch_rechecks_current_source_when_a_new_batch_starts(self):
        with TemporaryDirectory() as directory:
            _job, inputs, _result, _items = fixture(Path(directory) / "source")
            content = inspect_story_index(inputs.story_index)
            store = PregenerationJobStore(Path(directory) / "jobs")
            selection_id = content.selections[0].selection_id
            inspector = StoryAudioInspector(content, store)
            coverage = inspector.inspect(selection_id)
            inputs.story_index.write_bytes(b"changed source")
            self.assertEqual(inspector.inspect(selection_id), coverage)
            with self.assertRaises(OfflinePackError):
                StoryAudioInspector(content, store).inspect(selection_id)
            with self.assertRaises(OfflinePackError):
                inspect_story_audio(content, selection_id, store)

    def test_story_batch_revalidates_wav_bytes_and_new_batch_rejects_changed_pack(self):
        for changed in ("wav", "manifest"):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                job, inputs, result, _items = fixture(Path(directory) / "source")
                published = OfflinePackPublisher().publish(job, inputs, result)
                content = inspect_story_index(inputs.story_index)
                store = PregenerationJobStore(Path(directory) / "jobs")
                inspector = StoryAudioInspector(content, store)
                selection_id = content.selections[0].selection_id
                inspector.inspect(selection_id, manifest=published.manifest)
                if changed == "wav":
                    document = load_generated_audio_document(
                        published.imported.generated_audio_manifest
                    )
                    document.records[0].audio.write_bytes(b"damaged")
                    with self.assertRaises(OfflinePackError):
                        inspector.inspect(selection_id, manifest=published.manifest)
                else:
                    published.manifest.write_text("{}")
                with self.assertRaises(GamePackError):
                    StoryAudioInspector(content, store).inspect(
                        selection_id, manifest=published.manifest
                    )

    def test_story_batch_reuses_a_pack_import_failure_only_within_that_batch(self):
        with TemporaryDirectory() as directory:
            _job, inputs, _result, _items = fixture(Path(directory) / "source")
            content = inspect_story_index(inputs.story_index)
            store = PregenerationJobStore(Path(directory) / "jobs")
            manifest = Path(directory) / "missing.json"
            selection_id = content.selections[0].selection_id
            with patch(
                "vntts.pregeneration_pack.import_game_pack",
                side_effect=GamePackError("invalid pack"),
            ) as imported:
                inspector = StoryAudioInspector(content, store)
                for _ in range(2):
                    with self.assertRaises(GamePackError):
                        inspector.inspect(selection_id, manifest=manifest)
                self.assertEqual(imported.call_count, 1)
                with self.assertRaises(GamePackError):
                    StoryAudioInspector(content, store).inspect(
                        selection_id, manifest=manifest
                    )
                self.assertEqual(imported.call_count, 2)

    def test_story_coverage_cancels_before_loading_content(self):
        cancellation = Event()
        cancellation.set()
        with patch(
            "vntts.pregeneration_pack.load_verified_story_index_document"
        ) as load:
            with self.assertRaises(OfflineGenerationCancelled):
                inspect_story_audio(
                    object(), "story", object(), cancel_event=cancellation
                )
        load.assert_not_called()

    def test_story_coverage_cancels_between_line_checks(self):
        with TemporaryDirectory() as directory:
            store = PregenerationJobStore(Path(directory) / "jobs")
            _job, inputs, _result, _items = fixture(Path(directory) / "source")
            content = inspect_story_index(inputs.story_index)
            cancellation = Event()

            def checked_line(*_arguments):
                cancellation.set()
                return "missing"

            with patch(
                "vntts.pregeneration_pack._story_audio_route", side_effect=checked_line
            ) as route:
                with self.assertRaises(OfflineGenerationCancelled):
                    inspect_story_audio(
                        content,
                        content.selections[0].selection_id,
                        store,
                        cancel_event=cancellation,
                    )
            route.assert_called_once()

    def test_legacy_original_base_requires_repreparing_preserved_stories(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base_job, base_input, base_result, _ = fixture(root / "base")
            base = OfflinePackPublisher().publish(base_job, base_input, base_result)
            old = load_story_index_document(base.imported.story_index)
            original = old.records[0].to_record() | {
                "line_id": "pack:original",
                "text_sha256": old.records[0].text_sha256,
                "sequence": 3,
                "source_audio_status": "available",
                "source_audio_duration_seconds": 1.0,
                "source_audio_duration_media_id": 7,
                "source_audio_duration_media_sha256": "a" * 64,
                "source_audio_duration_sample_rate": 24000,
                "source_audio_duration_sample_count": 24000,
                "source_audio_duration_decoder": "synthetic",
                "source_audio_completeness": "full",
                "source_audio_completeness_reason": "exact-normalized-asr-transcript",
            }
            sidecar = old.path.parent / "source-audio-semantic-evidence.json"
            evidence = write_source_audio_semantic_evidence(
                sidecar,
                line_id=original["line_id"],
                text=original["text"],
                media_id=7,
                preprocessing=None,
            )
            original.update(
                source_audio_semantic_evidence_id=evidence.evidence_id,
                source_audio_semantic_evidence_entry_id=evidence.entry_id,
            )
            base_records = [r.to_record() for r in old.records] + [original]
            write_story_index_document(
                old.path,
                old.metadata
                | {
                    "source_audio_completion": "verified-media-duration-seconds",
                    "source_audio_semantics": {
                        "evidence_id": evidence.evidence_id,
                        "evidence_sha256": evidence.sha256,
                        "method": SEMANTIC_EVIDENCE_METHOD,
                        "applied_count": 1,
                    },
                },
                base_records,
            )
            write_game_pack(
                base.manifest,
                {
                    "game": {"id": old.game, "version": base_job.game_version},
                    "producers": [{"name": "test", "version": "1"}],
                    "created_at": "2026-10-15T00:00:00+00:00",
                    "vntts.self-service": {"identity": base.identity},
                    "vntts.authoring": {
                        "source_audio_semantic_evidence": {
                            "path": "story/source-audio-semantic-evidence.json",
                            "sha256": evidence.sha256,
                            "evidence_id": evidence.evidence_id,
                            "entry_count": 1,
                        }
                    },
                },
                {
                    "story_index": old.path,
                    "voice_manifest": base.imported.voice_manifest,
                    "generated_audio": base.imported.generated_audio_manifest,
                },
            )
            job, prepared, generated, _ = fixture(
                root / "current", names=("new", "next")
            )
            current = load_story_index_document(prepared.story_index)
            catalog = root / "catalog.jsonl"
            write_story_index_document(
                catalog,
                current.metadata,
                base_records + [r.to_record() for r in current.records],
            )
            job = replace(
                job, story_index=str(catalog), story_index_sha256=sha256_file(catalog)
            )
            before = tree_hashes(root)
            publisher = OfflinePackPublisher(base_pack=base.manifest)
            for operation in (
                lambda: publisher.inspect_changes(job, prepared),
                lambda: publisher.publish(job, prepared, generated),
            ):
                with self.assertRaisesRegex(
                    OfflinePackError, "Select the previously prepared stories"
                ):
                    operation()
                self.assertEqual(tree_hashes(root), before)
            # Selecting all old lines allows corrected current owners to replace them.
            all_selected = write_story_index_document(
                root / "all-selected.jsonl",
                current.metadata,
                base_records + [r.to_record() for r in current.records],
            )
            imported, _ = _load_incremental_base(base.manifest, job, all_selected)
            self.assertEqual(imported.pack.manifest_path, base.manifest)

    def test_publication_reuses_the_generation_result_identity_contract(self):
        with TemporaryDirectory() as directory:
            job, prepared, generated, _items = fixture(Path(directory))
            expected = (
                prepared.directory.parent
                / f"generation-output-{prepared.identity[:16]}"
            )
            self.assertEqual(prepared.output_directory, expected)
            self.assertEqual(generated.output, expected)
            _validate_inputs(job, prepared, generated)
            cases = (
                ((None, prepared, generated), "Offline preparation job is invalid"),
                ((job, None, generated), "Offline generation input is invalid"),
                ((job, prepared, None), "Offline generation result is invalid"),
                (
                    (
                        job,
                        prepared,
                        replace(generated, output=expected.parent / "foreign-output"),
                    ),
                    "Offline pack output identity changed",
                ),
            )
            for values, message in cases:
                with (
                    self.subTest(message=message),
                    self.assertRaisesRegex(OfflinePackError, message),
                ):
                    _validate_inputs(*values)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_optional_sequence_capture_refuses_fifo_without_publishing_and_recovers(
        self,
    ):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sequence.json"
            payload = b"captured optional evidence"
            path.write_bytes(payload)
            path.chmod(0o444)
            mode = path.stat().st_mode
            gate: RegularFileGate = {"path": path, "armed": True}
            with swap_at_acquisition(self, gate):
                self.assertEqual(_optional_sequence_snapshot(path), (None, None))
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(path.stat().st_mode, mode)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertEqual(
                _optional_sequence_snapshot(path), (sha256_file(path), payload)
            )

    def _publication_copy_fixture(self, root, phase):
        job, prepared, generated, items = fixture(root / "current")
        reference = prepared.voice_manifest.parent / "references" / "voice.wav"
        write_pcm16_wav(reference, [0.1, -0.1] * 400, 16_000)
        voice_document, _voices = load_voice_manifest(
            prepared.voice_manifest, allow_legacy=False
        )
        voice_document["voices"][0]["references"] = ["references/voice.wav"]
        write_voice_manifest(prepared.voice_manifest, voice_document)
        prepared = replace(
            prepared, voice_manifest_sha256=sha256_file(prepared.voice_manifest)
        )
        state = json.loads(generated.state.read_text())
        source = {
            "story": prepared.story_index,
            "voice-manifest": prepared.voice_manifest,
            "reference": reference,
            "generated": generated.output
            / state["items"][items[0]["queue_id"]]["path"],
        }.get(phase)
        publisher = OfflinePackPublisher()
        if phase.startswith("reused-"):
            base_job, base_input, base_result, _base_items = fixture(
                root / "base", names=("preserved", "old-fallback")
            )
            base_reference = (
                base_input.voice_manifest.parent / "references" / "voice.wav"
            )
            write_pcm16_wav(base_reference, [0.2, -0.2] * 400, 16_000)
            write_voice_manifest(base_input.voice_manifest, voice_document)
            base_input = replace(
                base_input,
                voice_manifest_sha256=sha256_file(base_input.voice_manifest),
            )
            base = OfflinePackPublisher().publish(base_job, base_input, base_result)
            base_story = load_story_index_document(base_input.story_index)
            current_story = load_story_index_document(prepared.story_index)
            source_story = root / "source-story.jsonl"
            write_story_index_document(
                source_story,
                current_story.metadata,
                [
                    record.to_record() | {"chapter": str(chapter)}
                    for chapter, selected in enumerate((base_story, current_story), 1)
                    for record in selected.records
                ],
            )
            job = replace(
                job,
                story_index=str(source_story),
                story_index_sha256=sha256_file(source_story),
            )
            publisher = OfflinePackPublisher(base_pack=base.manifest)
            if phase == "reused-reference":
                _document, voices = load_voice_manifest(
                    base.imported.voice_manifest, allow_legacy=False
                )
                source = base.imported.voice_manifest.parent / voices[0].references[0]
            else:
                source = (
                    load_generated_audio_document(
                        base.imported.generated_audio_manifest
                    )
                    .records[0]
                    .audio
                )
        return publisher, job, prepared, generated, source.resolve()

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_public_copy_phases_refuse_fifo_clean_up_and_recover(self):
        for phase in (
            "story",
            "voice-manifest",
            "reference",
            "generated",
            "reused-reference",
            "reused-audio",
        ):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                publisher, job, prepared, generated, source = (
                    self._publication_copy_fixture(root, phase)
                )
                source_bytes = source.read_bytes()
                before = tree_hashes(root)
                original_copy = pack_module.copy_regular_file
                refused = []

                def copy(original, target):
                    if Path(original) != source:
                        return original_copy(original, target)
                    gate: RegularFileGate = {"path": source, "armed": True}
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original_copy(original, target)
                    self.assertFalse(Path(target).exists())
                    refused.append(caught.exception)
                    raise caught.exception

                def link_fallback(original, target, digest):
                    if phase != "reused-audio":
                        return _link_verified_file(original, target, digest)
                    with patch.object(
                        pack_module.os, "link", side_effect=OSError("no links")
                    ) as link:
                        try:
                            return _link_verified_file(original, target, digest)
                        finally:
                            link.assert_called_once_with(
                                Path(original).resolve(), target
                            )

                output = prepared.directory.parent / "game-packs"
                with patch.object(pack_module, "_link_verified_file", link_fallback):
                    with (
                        patch.object(pack_module, "copy_regular_file", copy),
                        self.assertRaisesRegex(
                            OfflinePackError, "regular file"
                        ) as caught,
                    ):
                        publisher.publish(job, prepared, generated)
                    self.assertEqual(len(refused), 1)
                    self.assertIs(caught.exception.__cause__, refused[0])
                    self.assertEqual(tree_hashes(root), before)
                    self.assertEqual(list(output.iterdir()), [])
                    recovered = publisher.publish(job, prepared, generated)
                self.assertEqual(source.read_bytes(), source_bytes)
                recovered_tree = tree_hashes(root)
                self.assertEqual(publisher.publish(job, prepared, generated), recovered)
                self.assertEqual(tree_hashes(root), recovered_tree)
                self.assertEqual(load_saved_pack(recovered.manifest), recovered)
                if phase == "reused-audio":
                    records = load_generated_audio_document(
                        recovered.imported.generated_audio_manifest
                    ).records
                    reused = next(
                        record.audio
                        for record in records
                        if record.line_id == "pack:preserved"
                    )
                    self.assertEqual(reused.read_bytes(), source_bytes)
                    self.assertFalse(source.samefile(reused))

    def test_saved_and_reused_packs_reject_noninteger_counts(self):
        with TemporaryDirectory() as directory:
            job, prepared, generated, _items = fixture(Path(directory))
            publisher = OfflinePackPublisher()
            published = publisher.publish(job, prepared, generated)
            manifest_before = published.manifest.read_bytes()
            document = json.loads(manifest_before)
            extension = document["vntts.self-service"]
            state_before = generated.state.read_bytes()
            loaders = (
                ("saved", lambda: load_saved_pack(published.manifest)),
                ("reuse", lambda: publisher.publish(job, prepared, generated)),
            )
            for field in (
                "approved_count",
                "live_fallback_count",
                "omission_count",
                "story_line_count",
            ):
                original = extension[field]
                for malformed in (bool(original), float(original)):
                    extension[field] = malformed
                    atomic_write_json(published.manifest, document)
                    for route, loader in loaders:
                        with (
                            self.subTest(field=field, value=malformed, route=route),
                            self.assertRaisesRegex(
                                OfflinePackError, "(coverage|route counts) changed"
                            ),
                        ):
                            loader()
                extension[field] = original
            published.manifest.write_bytes(manifest_before)
            loaded = load_saved_pack(published.manifest)
            self.assertEqual(loaded.identity, published.identity)
            self.assertEqual(loaded.directory, published.directory.resolve())
            self.assertEqual(
                (
                    loaded.approved,
                    loaded.live_fallbacks,
                    loaded.story_lines,
                    loaded.omissions,
                ),
                (
                    published.approved,
                    published.live_fallbacks,
                    published.story_lines,
                    published.omissions,
                ),
            )
            self.assertEqual(generated.state.read_bytes(), state_before)

    def test_saved_pack_preserves_legacy_optional_count_defaults(self):
        with TemporaryDirectory() as directory:
            job, prepared, generated, _items = fixture(Path(directory))
            published = OfflinePackPublisher().publish(job, prepared, generated)
            document = json.loads(published.manifest.read_text())
            extension = document["vntts.self-service"]
            del extension["story_line_count"]
            del extension["omission_count"]
            atomic_write_json(published.manifest, document)
            loaded = load_saved_pack(published.manifest)
            self.assertEqual(loaded.identity, published.identity)
            self.assertEqual(loaded.directory, published.directory.resolve())
            self.assertEqual(
                (
                    loaded.approved,
                    loaded.live_fallbacks,
                    loaded.story_lines,
                    loaded.omissions,
                ),
                (
                    published.approved,
                    published.live_fallbacks,
                    published.story_lines,
                    published.omissions,
                ),
            )
            for count in (
                loaded.approved,
                loaded.live_fallbacks,
                loaded.story_lines,
                loaded.omissions,
            ):
                self.assertIs(type(count), int)
            for required in ("approved_count", "live_fallback_count"):
                value = extension.pop(required)
                atomic_write_json(published.manifest, document)
                with self.subTest(field=required), self.assertRaises(OfflinePackError):
                    load_saved_pack(published.manifest)
                extension[required] = value

    def test_branch_sequence_is_left_out_of_the_staged_pack(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story-index.jsonl"
            write_story_index_document(
                story,
                {"game": "Reverse: 1999", "language": "en"},
                [
                    {
                        "record_type": "line",
                        "line_id": "reverse1999:1:1",
                        "chapter": "1",
                        "sequence": 1,
                        "speaker": "A",
                        "text": "One.",
                        "kind": "dialogue",
                    },
                    {
                        "record_type": "line",
                        "line_id": "reverse1999:1:2",
                        "chapter": "1",
                        "sequence": 2,
                        "speaker": "B",
                        "text": "Two.",
                        "kind": "dialogue",
                    },
                ],
            )
            plan_path = root / "branch.json"
            write_live_sequence_plan(
                plan_path,
                {
                    "schema": "vntts.live-sequence-plan",
                    "schema_version": 1,
                    "game_id": "reverse1999",
                    "producer": {"name": "test", "version": "1"},
                    "story_index_sha256": sha256_file(story),
                    "source_extract_sha256": "a" * 64,
                    "chapters": [
                        {
                            "chapter": "1",
                            "entry_event_ids": ["one"],
                            "events": [
                                {
                                    "event_id": "one",
                                    "sequence": 1,
                                    "kind": "speech",
                                    "control": "manual",
                                    "successors": ["two", "other"],
                                    "line_id": "reverse1999:1:1",
                                },
                                {
                                    "event_id": "two",
                                    "sequence": 2,
                                    "kind": "speech",
                                    "control": "terminal",
                                    "successors": [],
                                    "line_id": "reverse1999:1:2",
                                },
                                {
                                    "event_id": "other",
                                    "sequence": 3,
                                    "kind": "transition",
                                    "control": "terminal",
                                    "successors": [],
                                },
                            ],
                        }
                    ],
                },
                story,
            )

            staged = _stage_live_sequence(
                None,
                plan_path,
                story,
                story,
                load_story_index_document(story),
                root / "staged.json",
            )

        self.assertIsNone(staged)

    def test_cumulative_sequence_is_rebound_to_final_story_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base_story = root / "base.jsonl"
            current_story = root / "current.jsonl"
            final_story = root / "final.jsonl"
            for path, chapter in ((base_story, "1"), (current_story, "2")):
                write_story_index_document(
                    path,
                    {"game": "Reverse: 1999", "language": "en"},
                    [
                        {
                            "record_type": "line",
                            "line_id": f"reverse1999:{chapter}:1",
                            "chapter": chapter,
                            "sequence": 1,
                            "speaker": "A",
                            "text": f"Chapter {chapter}.",
                            "kind": "dialogue",
                        }
                    ],
                )
            write_story_index_document(
                final_story,
                {"game": "Reverse: 1999", "language": "en"},
                [
                    *(
                        record.to_record()
                        for record in load_story_index_document(base_story).records
                    ),
                    *(
                        record.to_record()
                        for record in load_story_index_document(current_story).records
                    ),
                ],
            )
            plans = []
            for story, chapter in ((base_story, "1"), (current_story, "2")):
                path = root / f"{chapter}.json"
                write_live_sequence_plan(
                    path,
                    {
                        "schema": "vntts.live-sequence-plan",
                        "schema_version": 1,
                        "game_id": "reverse1999",
                        "producer": {"name": "test", "version": "1"},
                        "story_index_sha256": sha256_file(story),
                        "source_extract_sha256": "a" * 64,
                        "chapters": [
                            {
                                "chapter": chapter,
                                "entry_event_ids": [f"{chapter}-one"],
                                "events": [
                                    {
                                        "event_id": f"{chapter}-one",
                                        "sequence": 1,
                                        "kind": "speech",
                                        "control": "terminal",
                                        "successors": [],
                                        "line_id": f"reverse1999:{chapter}:1",
                                    }
                                ],
                            }
                        ],
                    },
                    story,
                )
                plans.append(path)
            base = type(
                "Base", (), {"live_sequence_plan": plans[0], "story_index": base_story}
            )()

            staged = _stage_live_sequence(
                base,
                plans[1],
                current_story,
                final_story,
                load_story_index_document(final_story),
                root / "staged.json",
            )

            self.assertIsNotNone(staged)
            plan = load_live_sequence_plan(staged, final_story)
            self.assertEqual(
                tuple(chapter.chapter for chapter in plan.chapters), ("1", "2")
            )

            source_digest, source_payload = _optional_sequence_snapshot(plans[1])
            self.assertEqual(source_digest, sha256_file(plans[1]))
            replacement_document = json.loads(plans[1].read_text(encoding="utf-8"))
            replacement_document["source_extract_sha256"] = "b" * 64
            replacement_chapter = replacement_document["chapters"][0]
            replacement_chapter["entry_event_ids"] = ["2-other"]
            replacement_chapter["events"][0]["event_id"] = "2-other"
            replacement = root / "replacement.json"
            write_live_sequence_plan(replacement, replacement_document, current_story)

            from vntts.live_sequence import load_live_sequence_snapshot

            def replace_source_after_validation(
                path, story_index, plan_payload, story_payload
            ):
                validated = load_live_sequence_snapshot(
                    path, story_index, plan_payload, story_payload
                )
                if story_index == current_story and replacement.exists():
                    replacement.replace(plans[1])
                return validated

            with patch(
                "vntts.pregeneration_pack.load_live_sequence_snapshot",
                side_effect=replace_source_after_validation,
            ):
                staged = _stage_live_sequence(
                    base,
                    plans[1],
                    current_story,
                    final_story,
                    load_story_index_document(final_story),
                    root / "staged-race.json",
                    current_payload=source_payload,
                )

            self.assertIsNotNone(staged)
            self.assertEqual(
                load_live_sequence_plan(staged, final_story)
                .chapters[1]
                .entry_event_ids,
                ("2-one",),
            )
            self.assertFalse(replacement.exists())
            self.assertEqual(
                load_live_sequence_plan(plans[1], current_story).source_extract_sha256,
                "b" * 64,
            )

    def test_copy_rejects_symlinked_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "source.wav"
            target.write_bytes(b"verified audio")
            alias = root / "alias.wav"
            try:
                alias.symlink_to(target)
            except OSError:
                self.skipTest("symlinks are unavailable on this host")
            with self.assertRaises(OfflinePackError):
                _copy_file(alias, root / "pack.wav")

    def test_current_route_cannot_overwrite_reused_base_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "published.wav"
            source.write_bytes(b"published audio")
            changed = root / "current.wav"
            changed.write_bytes(b"changed after terminal validation")
            manifest = root / "staging" / "manifest.json"
            digest = sha256_file(source)
            record = {"audio_sha256": digest}
            _portable_generated_record(record, source, manifest, reuse=True)
            destination = manifest.parent / f"audio/{digest}.wav"

            with self.assertRaisesRegex(OfflinePackError, "destination conflicts"):
                _portable_generated_record(record, changed, manifest)

            self.assertEqual(source.read_bytes(), b"published audio")
            self.assertTrue(source.samefile(destination))

    def test_duplicate_copy_reuses_verified_destination_without_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(b"same audio")
            destination = root / "staging" / "audio.wav"
            digest = _copy_file(source, destination)
            with patch(
                "vntts.pregeneration_pack.copy_regular_file",
                side_effect=AssertionError("verified staged file must be reused"),
            ):
                self.assertEqual(_copy_file(source, destination), digest)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_prepared_copy_reuses_digest_and_preserves_race_checks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            destination = root / "staged.json"
            source.write_bytes(b"prepared document")
            digest = sha256_file(source)
            with patch(
                "vntts.pregeneration_pack.sha256_file", wraps=sha256_file
            ) as hashes:
                _copy_prepared_file(source, destination, digest, "story index")
            self.assertEqual(
                [call.args[0] for call in hashes.call_args_list],
                [source.resolve(), source.resolve(), destination],
            )
            destination.unlink()
            with self.assertRaisesRegex(
                OfflinePackError, "Prepared staged story index changed"
            ):
                _copy_prepared_file(source, destination, "0" * 64, "story index")
            destination.unlink()
            copyfile = pack_module.copy_regular_file

            for target in (source, destination):
                with self.subTest(changed=target):
                    source.write_bytes(b"prepared document")

                    def copy_then_change(original, staged):
                        copyfile(original, staged)
                        target.write_bytes(b"changed while copying")

                    with (
                        patch(
                            "vntts.pregeneration_pack.copy_regular_file",
                            side_effect=copy_then_change,
                        ),
                        self.assertRaisesRegex(OfflinePackError, "source changed"),
                    ):
                        _copy_prepared_file(source, destination, digest, "story index")
                    destination.unlink()

    def test_incremental_reuse_hard_links_verified_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            destination = root / "pack" / "audio.wav"
            source.write_bytes(b"verified audio")

            _link_verified_file(source, destination, sha256_file(source))

            self.assertTrue(source.samefile(destination))
            self.assertEqual(destination.read_bytes(), b"verified audio")

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
            for contract, fields, original, authoritative in (
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "full",
                    },
                    0,
                    False,
                ),
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "partial",
                    },
                    0,
                    False,
                ),
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "unknown",
                    },
                    0,
                    False,
                ),
                (
                    "duration-seconds",
                    {"source_audio_duration_seconds": 1.0},
                    0,
                    False,
                ),
                (
                    "duration-seconds",
                    {"source_audio_completeness": "full"},
                    0,
                    False,
                ),
                (
                    "duration-seconds",
                    {
                        "source_audio_duration_seconds": -1,
                        "source_audio_completeness": "full",
                    },
                    0,
                    False,
                ),
                (
                    None,
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "full",
                    },
                    0,
                    False,
                ),
                (
                    "verified-media-duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_completeness": "full",
                    },
                    0,
                    False,
                ),
                (
                    "verified-media-duration-seconds",
                    {
                        "source_audio_duration_seconds": 1.0,
                        "source_audio_duration_media_id": 7,
                        "source_audio_duration_media_sha256": "c" * 64,
                        "source_audio_duration_sample_rate": 24000,
                        "source_audio_duration_sample_count": 24000,
                        "source_audio_duration_decoder": "synthetic",
                        "source_media_ids": [7],
                        "available_media_ids": [7],
                        "source_audio_completeness": "full",
                        "source_audio_completeness_reason": (
                            "exact-normalized-asr-transcript"
                        ),
                    },
                    1,
                    True,
                ),
            ):
                with self.subTest(contract=contract, fields=fields):
                    metadata = {"game": "Synthetic Game", "language": "en"}
                    if contract is not None:
                        metadata["source_audio_completion"] = contract
                    write_story_index_document(path, metadata, [source | fields])
                    content = inspect_story_index(path)
                    with patch(
                        "vntts.pregeneration_pack._validated_source_audio_line_ids",
                        return_value=(
                            frozenset({"source-only"}) if authoritative else frozenset()
                        ),
                    ):
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
            inputs = replace(
                inputs,
                story_index_sha256=sha256_file(inputs.story_index),
            )
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
                selections=(
                    replace(
                        selection,
                        selection_id="stage-one",
                        line_ids=(selection.line_ids[0],),
                    ),
                ),
            )
            scoped = inspect_story_audio(single_line, "stage-one", store)
            self.assertEqual((scoped.generated, scoped.live, scoped.omitted), (1, 0, 0))
            active = inspect_story_audio(
                single_line,
                "stage-one",
                PregenerationJobStore(Path(directory) / "no-saved-jobs"),
                manifest=pack.manifest,
            )
            self.assertEqual((active.generated, active.live, active.missing), (1, 0, 0))
            with patch(
                "vntts.pregeneration_pack.import_game_pack",
                wraps=import_game_pack,
            ) as imported:
                loaded = load_saved_pack(pack.manifest)
            self.assertEqual(imported.call_count, 1)
            self.assertEqual(loaded.identity, pack.identity)
            with patch(
                "vntts.pregeneration_pack.import_game_pack",
                side_effect=AssertionError("validated pack was loaded again"),
            ):
                reused = inspect_story_audio(
                    content,
                    selection.selection_id,
                    store,
                    imported_pack=pack.imported,
                )
            self.assertEqual(reused.missing, 0)
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

    def test_change_summary_uses_one_queue_snapshot(self):
        with TemporaryDirectory() as directory:
            job, generation_input, _result, items = fixture(Path(directory))
            original_load = VoiceGenerationQueue.load

            def replace_queue_after_parse(path):
                queue = original_load(path)
                write_voice_generation_queue(
                    generation_input.queue,
                    {"game": "Synthetic Game", "language": "en"},
                    [{**items[0], "speaker": "Other"}, items[1]],
                )
                return queue

            with patch.object(
                VoiceGenerationQueue, "load", side_effect=replace_queue_after_parse
            ):
                summary = OfflinePackPublisher().inspect_changes(job, generation_input)

            self.assertEqual(summary.new, 0)
            self.assertNotEqual(
                sha256_file(generation_input.queue), generation_input.queue_sha256
            )

    def test_terminal_generation_uses_the_validated_queue_snapshot(self):
        with TemporaryDirectory() as directory:
            job, generation_input, result, items = fixture(Path(directory))
            original_load = VoiceGenerationQueue.load

            def replace_queue_after_parse(path):
                queue = original_load(path)
                write_voice_generation_queue(
                    generation_input.queue,
                    {"game": "Synthetic Game", "language": "en"},
                    [{**items[0], "speaker": "Other"}, items[1]],
                )
                return queue

            with (
                patch.object(
                    VoiceGenerationQueue, "load", side_effect=replace_queue_after_parse
                ),
                patch(
                    "vntts.pregeneration_pack.load_generation_state_from_snapshot",
                    wraps=load_generation_state_from_snapshot,
                ) as state_loader,
            ):
                _load_terminal_generation(
                    job,
                    generation_input,
                    result,
                    sha256_file(result.state),
                    state_document=json.loads(result.state.read_text()),
                )

            validated_queue = state_loader.call_args.args[1]
            self.assertEqual(validated_queue.items[0].speaker, items[0]["speaker"])

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
            _cached_story_index_document.cache_clear()
            self.addCleanup(_cached_story_index_document.cache_clear)
            with patch(
                "vntts.pregeneration_setup.load_story_index_snapshot",
                wraps=load_story_index_snapshot,
            ) as story_load:
                unchanged = publisher.inspect_changes(job, inputs)
            self.assertEqual(
                sum(
                    Path(call.args[0]).resolve() == Path(job.story_index).resolve()
                    for call in story_load.call_args_list
                ),
                1,
            )
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

            with patch(
                "vntts.pregeneration_pack.record_background_operation"
            ) as record:
                first = publisher.publish(job, generation_input, generation_result)
                with patch(
                    "vntts.pregeneration_pack.load_generation_state_from_snapshot",
                    side_effect=AssertionError("published pack must be reused"),
                ):
                    second = publisher.publish(job, generation_input, generation_result)
            self.assertEqual(
                [call.args[0] for call in record.call_args_list],
                [
                    "pregeneration-publication-identity",
                    "pregeneration-publication-terminal-load",
                    "pregeneration-publication-disk-preflight",
                    "pregeneration-publication-story-and-voices",
                    "pregeneration-publication-audio-routes",
                    "pregeneration-publication-staged-validation",
                    "pregeneration-publication-atomic-publish",
                    "pregeneration-publication-published-validation",
                    "pregeneration-publication-identity",
                    "pregeneration-publication-reuse",
                ],
            )
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

    def test_published_pack_uses_the_state_bytes_hashed_for_its_identity(self):
        with TemporaryDirectory() as temporary_directory:
            job, generation_input, generation_result, _items = fixture(
                Path(temporary_directory)
            )
            original_state = json.loads(generation_result.state.read_text())
            original_sha256 = sha256_file(generation_result.state)

            def replace_state_after_snapshot(*args, **kwargs):
                replacement = json.loads(generation_result.state.read_text())
                replacement["items"][next(iter(replacement["items"]))]["updated_at"] = (
                    "2030-01-01T00:00:00+00:00"
                )
                atomic_write_json(generation_result.state, replacement, sort_keys=True)
                self.assertEqual(kwargs["state_document"], original_state)
                return _load_terminal_generation(*args, **kwargs)

            with patch(
                "vntts.pregeneration_pack._load_terminal_generation",
                side_effect=replace_state_after_snapshot,
            ):
                published = OfflinePackPublisher().publish(
                    job, generation_input, generation_result
                )

            self.assertNotEqual(sha256_file(generation_result.state), original_sha256)
            self.assertEqual(
                published.imported.pack.extensions["vntts.self-service"][
                    "source_state_sha256"
                ],
                original_sha256,
            )

    def test_cancellation_stops_voice_copy_and_cleans_staging(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, generation_input, generation_result, _items = fixture(root)
            references = generation_input.voice_manifest.parent / "references"
            references.mkdir()
            for name in ("first.wav", "second.wav"):
                (references / name).write_bytes(name.encode())
            write_voice_manifest(
                generation_input.voice_manifest,
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Narrator",
                            "speaker": "alba",
                            "aliases": [],
                            "references": [
                                "references/first.wav",
                                "references/second.wav",
                            ],
                        }
                    ],
                },
            )
            generation_input = replace(
                generation_input,
                voice_manifest_sha256=sha256_file(generation_input.voice_manifest),
            )
            cancellation = Event()
            copied_references = []

            def copy_then_cancel(source, destination):
                digest = _copy_file(source, destination)
                if Path(source).suffix == ".wav":
                    copied_references.append(Path(source).name)
                    cancellation.set()
                return digest

            with (
                patch(
                    "vntts.pregeneration_pack._copy_file", side_effect=copy_then_cancel
                ),
                self.assertRaises(OfflineGenerationCancelled),
            ):
                OfflinePackPublisher().publish(
                    job, generation_input, generation_result, cancel_event=cancellation
                )

            self.assertEqual(copied_references, ["first.wav"])
            self.assertEqual(list((root / "game-packs").iterdir()), [])

    def test_cancellation_stops_before_the_next_generated_route(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _job, generation_input, generation_result, _items = fixture(root)
            story = load_story_index_document(generation_input.story_index)
            cancellation = Event()
            copied = []

            def copy_then_cancel(record, *_args, **_kwargs):
                copied.append(record["audio"])
                cancellation.set()
                return record

            with (
                patch(
                    "vntts.pregeneration_pack.approved_manifest_entries",
                    return_value=[{"audio": "first.wav"}, {"audio": "second.wav"}],
                ),
                patch(
                    "vntts.pregeneration_pack._portable_generated_record",
                    side_effect=copy_then_cancel,
                ),
                self.assertRaises(OfflineGenerationCancelled),
            ):
                _write_cumulative_routes(
                    None,
                    story,
                    {},
                    generation_result,
                    root / "generated" / "manifest.json",
                    [],
                    cancel_event=cancellation,
                )

            self.assertEqual(copied, ["first.wav"])

    def test_cancellation_stops_cumulative_voice_copy(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            references = root / "references"
            references.mkdir()
            for name in ("first.wav", "second.wav"):
                (references / name).write_bytes(name.encode())
            manifest = root / "manifest.json"
            write_voice_manifest(
                manifest,
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Narrator",
                            "speaker": "alba",
                            "aliases": [],
                            "references": [
                                "references/first.wav",
                                "references/second.wav",
                            ],
                        }
                    ],
                },
            )
            document, voices = load_voice_manifest(manifest, allow_legacy=False)
            cancellation = Event()
            copied = []

            def copy_then_cancel(source, destination):
                digest = _copy_file(source, destination)
                copied.append(Path(source).name)
                cancellation.set()
                return digest

            with (
                patch(
                    "vntts.pregeneration_pack._copy_file", side_effect=copy_then_cancel
                ),
                self.assertRaises(OfflineGenerationCancelled),
            ):
                _portable_voice_entries(
                    manifest,
                    root / "staging" / "voice-manifest.json",
                    document,
                    voices,
                    cancel_event=cancellation,
                )

            self.assertEqual(copied, ["first.wav"])

    def test_rejects_generated_wav_changed_after_terminal_validation(self):
        with TemporaryDirectory() as temporary_directory:
            job, generation_input, generation_result, _items = fixture(
                Path(temporary_directory)
            )
            state = json.loads(generation_result.state.read_text())
            approved = next(
                item for item in state["items"].values() if item["status"] == "approved"
            )
            audio = generation_result.output / approved["path"]

            def change_after_preflight(*args):
                _ensure_pack_disk_space(*args)
                audio.write_bytes(b"changed after validation")

            with (
                patch(
                    "vntts.pregeneration_pack._ensure_pack_disk_space",
                    side_effect=change_after_preflight,
                ),
                self.assertRaisesRegex(OfflinePackError, "hash does not match"),
            ):
                OfflinePackPublisher().publish(job, generation_input, generation_result)

    def test_rejects_changed_prepared_story_before_publication(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, generation_input, generation_result, _items = fixture(root)
            story = load_story_index_document(generation_input.story_index)
            records = [record.to_record() for record in story.records]
            records[0]["speaker"] = "Someone Else"
            write_story_index_document(
                generation_input.story_index,
                story.metadata,
                records,
            )

            with self.assertRaisesRegex(
                OfflinePackError, "Prepared story index changed"
            ):
                OfflinePackPublisher().inspect_changes(job, generation_input)

            with self.assertRaisesRegex(
                OfflinePackError, "Prepared story index changed"
            ):
                OfflinePackPublisher().publish(job, generation_input, generation_result)

            self.assertFalse((root / "game-packs").exists())

    def test_rejects_changed_prepared_voice_manifest_and_queue(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for path_name, label in (
                ("voice_manifest", "voice manifest"),
                ("queue", "generation queue"),
            ):
                with self.subTest(path=path_name):
                    job, generation_input, generation_result, _items = fixture(
                        root / path_name
                    )
                    path = getattr(generation_input, path_name)
                    path.write_bytes(path.read_bytes() + b"\n")

                    with self.assertRaisesRegex(
                        OfflinePackError, f"Prepared {label} changed"
                    ):
                        OfflinePackPublisher().publish(
                            job, generation_input, generation_result
                        )

                    self.assertFalse((root / path_name / "game-packs").exists())

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
                root / "base",
                include_omission=True,
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
                | {"chapter": "1", "collection_id": "story"}
                for record in base_story.records
            }
            for record in current_story.records:
                records.setdefault(
                    record.line_id,
                    record.to_record() | {"chapter": "2", "collection_id": "story"},
                )
            source_story = root / "source" / "story-index.jsonl"
            source_story.parent.mkdir(parents=True)
            write_story_index_document(
                source_story,
                current_story.metadata
                | {
                    "collections": [
                        {
                            "collection_id": "story",
                            "title": "A Story",
                            "kind": "character_story",
                            "order": 1,
                        }
                    ]
                },
                records.values(),
            )
            base_job = replace(base_job, selected_story_ids=("story:stage:1",))
            current_job = replace(
                current_job,
                story_index=str(source_story),
                story_index_sha256=sha256_file(source_story),
                selected_story_ids=("story:stage:2",),
            )

            with (
                patch(
                    "vntts.pregeneration_pack.merge_source_audio_semantics",
                    side_effect=SourceAudioSemanticEvidenceError("corrupt sidecar"),
                ),
                self.assertRaisesRegex(OfflinePackError, "corrupt sidecar"),
            ):
                OfflinePackPublisher(base_pack=base.manifest).publish(
                    current_job,
                    current_input,
                    current_result,
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
            stage_one = inspect_story_audio(
                inspect_story_index(source_story),
                "story:stage:1",
                PregenerationJobStore(root / "no-saved-jobs"),
                manifest=successor.manifest,
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
        self.assertEqual(
            (stage_one.generated, stage_one.live, stage_one.missing), (1, 1, 0)
        )
        self.assertTrue(base_unchanged)


if __name__ == "__main__":
    unittest.main()
