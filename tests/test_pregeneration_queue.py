import hashlib
import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from vntts_artifacts import write_story_index_document
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import load_story_index_document
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from tests.bulk_generation_fixtures import SyntheticRenderer, audio_samples
from tests.story_fixtures import semantic_evidence_document, semantic_evidence_entry
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.bulk_generation import BulkGenerationError, run_bulk_generation
from vntts.authoring.generation_manifest import validate_success_file_with_samples
from vntts.authoring.generation_state import (
    load_generation_state,
    load_generation_state_from_snapshot,
    load_stable_generation_queue,
)
from vntts.authoring.missing_voice_policy import NARRATOR_ROLES, MissingVoicePolicy
from vntts.generation_database import state_path as generation_state_path
from vntts.pregeneration_pack import (
    OfflinePackError,
    OfflinePackPublisher,
    StoryAudioInspector,
)
from vntts.pregeneration_queue import (
    PregenerationInputStore,
    PregenerationQueueError,
    _may_have_saved_recordings,
    _voice_content_identity,
    load_generation_input_metadata,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoicePlanStore
from vntts.settings import AppSettings
from vntts.source_audio_semantics import (
    SEMANTIC_EVIDENCE_METHOD,
    SourceAudioSemanticEvidenceError,
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
    project_source_audio_semantics,
    semantic_text_sha256,
    validate_source_audio_semantic_evidence,
    validate_story_semantic_evidence,
)
from vntts.voice_library import VoiceLibrary
from vntts.voices import CharacterVoiceRegistry, remember_voice_binding


class PocketRenderer(SyntheticRenderer):
    name = "pocket-tts"
    model_name = "pocket-tts"


def generate_prepared(prepared, renderer, **options):
    return run_bulk_generation(
        prepared.queue,
        prepared.output_directory,
        renderer,
        provider="pocket-tts",
        model="pocket-tts",
        generation_profile="default",
        retries=0,
        control_files={"voice_manifest": prepared.voice_manifest},
        narrator_character="Narrator",
        synthesis_character_overrides={
            role: "Narrator" for role in prepared.narrator_fallback_roles
        },
        missing_voice_policy=MissingVoicePolicy(
            NARRATOR_ROLES, prepared.narrator_fallback_roles
        ),
        **options,
    )


def write_content(root):
    root.mkdir(parents=True, exist_ok=True)
    path = root / "story-index.jsonl"
    write_story_index_document(
        path,
        {
            "game": "Reverse: 1999",
            "language": "en",
            "collections": [
                {
                    "collection_id": "selected",
                    "title": "Selected story",
                    "kind": "character-story",
                    "order": 1,
                },
                {
                    "collection_id": "later",
                    "title": "Later story",
                    "kind": "character-story",
                    "order": 2,
                },
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": "original",
                "chapter": "1",
                "sequence": 1,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "Original.",
                "kind": "dialogue",
                "collection_id": "selected",
                "source_audio_status": "available",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "rhiannon",
                "chapter": "1",
                "sequence": 2,
                "speaker": "Aderyn",
                "voice_character": "Rhiannon",
                "text": "Generate with Rhiannon.",
                "kind": "dialogue",
                "collection_id": "selected",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "hotelier",
                "chapter": "1",
                "sequence": 3,
                "speaker": "Hotelier",
                "voice_character": "Hotelier",
                "text": "Generate with narrator.",
                "kind": "dialogue",
                "collection_id": "selected",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "unknown",
                "chapter": "1",
                "sequence": 4,
                "speaker": "???",
                "voice_character": "Unknown",
                "text": "Narrator role.",
                "kind": "dialogue",
                "collection_id": "selected",
                "source_audio_status": "absent",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "not-selected",
                "chapter": "2",
                "sequence": 1,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "text": "Do not include me.",
                "kind": "dialogue",
                "collection_id": "later",
                "source_audio_status": "absent",
                "speakable": True,
            },
        ],
    )
    return path


def write_manifest(root):
    references = root / "references"
    references.mkdir(parents=True, exist_ok=True)
    write_pcm16_wav(references / "rhiannon.wav", [0.0, 0.2, -0.2, 0.0], 16_000)
    write_pcm16_wav(references / "centurion.wav", [0.0, 0.3, -0.3, 0.0], 24_000)
    path = root / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "rhiannon-v1",
                        "aliases": ["Aderyn"],
                        "references": ["references/rhiannon.wav"],
                    },
                    {
                        "character": "Centurion",
                        "speaker": "centurion-v1",
                        "aliases": [],
                        "references": ["references/centurion.wav"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def add_semantic_evidence(story_path):
    story = load_story_index_document(story_path)
    records = [record.to_record() for record in story.records]
    entries = []
    for index, line_id in enumerate(("original", "not-selected"), start=1):
        record = next(value for value in records if value["line_id"] == line_id)
        media_sha256 = hashlib.sha256(f"media-{index}".encode()).hexdigest()
        text_sha256 = hashlib.sha256(record["text"].encode()).hexdigest()
        record["text_sha256"] = text_sha256
        entry, entry_id = semantic_evidence_entry(
            line_id=line_id,
            text=record["text"],
            displayed_text_sha256=text_sha256,
            media_id=index,
            media_sha256=media_sha256,
            model_sha256="2" * 64,
        )
        record.update(
            source_audio_duration_media_sha256=media_sha256,
            source_audio_completeness="full",
            source_audio_completeness_reason="exact-normalized-asr-transcript",
            source_audio_semantic_evidence_entry_id=entry_id,
        )
        entries.append(entry)
    evidence, evidence_id = semantic_evidence_document(
        entries,
        model_sha256="2" * 64,
        source_story_index_sha256="3" * 64,
        generated_at="2026-08-31T00:00:00+00:00",
    )
    for record in records:
        if record.get("source_audio_semantic_evidence_entry_id") is not None:
            record["source_audio_semantic_evidence_id"] = evidence_id
    evidence_path = story_path.parent / "source-audio-semantic-evidence.json"
    atomic_write_json(evidence_path, evidence, sort_keys=True)
    metadata = dict(story.metadata)
    metadata["source_audio_semantics"] = {
        "evidence_id": evidence_id,
        "evidence_sha256": sha256_file(evidence_path),
        "method": SEMANTIC_EVIDENCE_METHOD,
        "selected_chapters": ["1", "2"],
        "applied_count": 2,
    }
    write_story_index_document(story_path, metadata, records)
    return story_path


class PregenerationInputStoreTest(unittest.TestCase):
    def test_semantic_verdict_matches_the_transcript_and_preserves_authority(self):
        with TemporaryDirectory() as directory:
            story = add_semantic_evidence(write_content(Path(directory) / "content"))
            path = story.parent / "source-audio-semantic-evidence.json"
            original = json.loads(path.read_bytes())
            for observed in (
                original["entries"][0]["observed_transcript"],
                "Other words",
            ):
                for verdict in ("full", "partial", [], {}):
                    with self.subTest(observed=observed, verdict=verdict):
                        document = json.loads(json.dumps(original))
                        entry = document["entries"][0]
                        entry.update(
                            observed_transcript=observed,
                            normalized_observed_text_sha256=semantic_text_sha256(
                                observed
                            ),
                            verdict=verdict,
                            reason=(
                                "exact-normalized-asr-transcript"
                                if verdict == "full"
                                else "asr-transcript-mismatch"
                            ),
                            producer_extension={"keep": True},
                        )
                        entry["entry_id"] = canonical_document_sha256(
                            {
                                key: value
                                for key, value in entry.items()
                                if key not in {"entry_id", "source_line_ids"}
                            }
                        )
                        document["evidence_id"] = canonical_document_sha256(
                            {
                                key: value
                                for key, value in document.items()
                                if key not in {"evidence_id", "generated_at"}
                            }
                        )
                        expected = (
                            "full"
                            if entry["normalized_observed_text_sha256"]
                            == entry["normalized_displayed_text_sha256"]
                            else "partial"
                        )
                        if verdict == expected:
                            self.assertIs(
                                validate_source_audio_semantic_evidence(document),
                                document,
                            )
                            self.assertEqual(
                                entry["producer_extension"], {"keep": True}
                            )
                        else:
                            with self.assertRaisesRegex(
                                SourceAudioSemanticEvidenceError, "[Vv]erdict"
                            ):
                                validate_source_audio_semantic_evidence(document)

    def test_story_semantic_binding_rejects_malformed_ids_and_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = add_semantic_evidence(write_content(root / "content"))
            story = load_story_index_document(path)
            evidence_path = path.parent / "source-audio-semantic-evidence.json"
            evidence = load_source_audio_semantic_evidence(evidence_path)
            evidence_sha256 = sha256_file(evidence_path)
            for entry_id in ([], {}, 1):
                with self.subTest(entry_id=entry_id):
                    records = [record.to_record() for record in story.records]
                    record = next(
                        record for record in records if record["line_id"] == "original"
                    )
                    record["source_audio_semantic_evidence_entry_id"] = entry_id
                    write_story_index_document(path, story.metadata, records)
                    with self.assertRaisesRegex(
                        SourceAudioSemanticEvidenceError,
                        "Story semantic evidence changed",
                    ):
                        validate_story_semantic_evidence(
                            path, evidence_sha256, evidence
                        )
                    with self.assertRaisesRegex(
                        SourceAudioSemanticEvidenceError,
                        "Selected dialogue semantic entry ID",
                    ):
                        project_source_audio_semantics(story, records, root / "staging")
                    self.assertFalse((root / "staging").exists())
            for applied_count in (True, 2.0):
                with self.subTest(applied_count=applied_count):
                    metadata = dict(story.metadata)
                    metadata["source_audio_semantics"] = dict(
                        metadata["source_audio_semantics"], applied_count=applied_count
                    )
                    records = [record.to_record() for record in story.records]
                    if applied_count is True:
                        for record in records:
                            if record["line_id"] == "not-selected":
                                record.pop("source_audio_semantic_evidence_entry_id")
                    write_story_index_document(path, metadata, records)
                    with self.assertRaisesRegex(
                        SourceAudioSemanticEvidenceError, "applied count changed"
                    ):
                        validate_story_semantic_evidence(
                            path, evidence_sha256, evidence
                        )

    def test_story_semantic_binding_requires_line_provenance(self):
        with TemporaryDirectory() as directory:
            path = add_semantic_evidence(write_content(Path(directory) / "content"))
            story = load_story_index_document(path)
            evidence_path = path.parent / "source-audio-semantic-evidence.json"
            evidence = json.loads(evidence_path.read_bytes())
            evidence["entries"][0]["source_line_ids"] = ["unrelated-line"]
            evidence["evidence_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"evidence_id", "generated_at"}
                }
            )
            atomic_write_json(evidence_path, evidence, sort_keys=True)
            metadata = dict(story.metadata)
            metadata["source_audio_semantics"] = dict(
                metadata["source_audio_semantics"],
                evidence_id=evidence["evidence_id"],
                evidence_sha256=sha256_file(evidence_path),
            )
            records = [record.to_record() for record in story.records]
            for record in records:
                if record.get("source_audio_semantic_evidence_entry_id") is not None:
                    record["source_audio_semantic_evidence_id"] = evidence[
                        "evidence_id"
                    ]
            write_story_index_document(path, metadata, records)
            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError, "Story semantic evidence changed"
            ):
                load_source_audio_semantic_evidence(evidence_path, path)

    def test_semantic_evidence_rejects_invalid_utf8(self):
        with TemporaryDirectory() as directory:
            evidence = Path(directory) / "source-audio-semantic-evidence.json"
            evidence.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError, "Unable to read"
            ):
                load_source_audio_semantic_evidence(evidence)

    def test_semantic_evidence_hash_matches_the_decoded_snapshot(self):
        with TemporaryDirectory() as directory:
            story = add_semantic_evidence(write_content(Path(directory) / "content"))
            evidence_path = story.parent / "source-audio-semantic-evidence.json"
            original = json.loads(evidence_path.read_text(encoding="utf-8"))
            replacement = story.parent / "replacement.json"
            changed = dict(original, generated_at="2026-09-01T00:00:00+00:00")
            atomic_write_json(replacement, changed, sort_keys=True)
            story_document = load_story_index_document(story)
            metadata = dict(story_document.metadata)
            metadata["source_audio_semantics"] = dict(
                metadata["source_audio_semantics"],
                evidence_sha256=sha256_file(replacement),
            )
            write_story_index_document(
                story,
                metadata,
                [record.to_record() for record in story_document.records],
            )
            original_json_loads = json.loads

            def replace_after_decode(value, *args, **kwargs):
                decoded = original_json_loads(value, *args, **kwargs)
                if replacement.exists():
                    replacement.replace(evidence_path)
                return decoded

            with (
                patch(
                    "vntts.source_audio_semantics.json.loads",
                    side_effect=replace_after_decode,
                ),
                self.assertRaisesRegex(
                    SourceAudioSemanticEvidenceError, "binding changed"
                ),
            ):
                load_source_audio_semantic_evidence(evidence_path, story)

            self.assertEqual(
                load_source_audio_semantic_evidence(evidence_path, story)[
                    "generated_at"
                ],
                changed["generated_at"],
            )

    def test_semantic_evidence_rejects_boolean_schema_version(self):
        with TemporaryDirectory() as temporary_directory:
            story = add_semantic_evidence(
                write_content(Path(temporary_directory) / "content")
            )
            evidence_path = story.parent / "source-audio-semantic-evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["schema_version"] = True
            evidence["evidence_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"evidence_id", "generated_at"}
                }
            )
            atomic_write_json(evidence_path, evidence, sort_keys=True)

            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError,
                "Unsupported.*schema",
            ):
                load_source_audio_semantic_evidence(evidence_path)

    def test_semantic_evidence_requires_generation_timestamp(self):
        with TemporaryDirectory() as temporary_directory:
            story = add_semantic_evidence(
                write_content(Path(temporary_directory) / "content")
            )
            evidence_path = story.parent / "source-audio-semantic-evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence.pop("generated_at")
            atomic_write_json(evidence_path, evidence, sort_keys=True)

            with self.assertRaisesRegex(
                SourceAudioSemanticEvidenceError,
                "generation timestamp",
            ):
                load_source_audio_semantic_evidence(evidence_path)

    def test_semantic_evidence_rejects_non_string_source_line_ids(self):
        with TemporaryDirectory() as temporary_directory:
            story = add_semantic_evidence(
                write_content(Path(temporary_directory) / "content")
            )
            evidence_path = story.parent / "source-audio-semantic-evidence.json"
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            for source_line_ids in (["original", 1], [{}]):
                with self.subTest(source_line_ids=source_line_ids):
                    evidence["entries"][0]["source_line_ids"] = source_line_ids
                    atomic_write_json(evidence_path, evidence, sort_keys=True)
                    with self.assertRaisesRegex(
                        SourceAudioSemanticEvidenceError,
                        "source line IDs are invalid",
                    ):
                        load_source_audio_semantic_evidence(evidence_path)

    def fixture(self, root, *, narrator=True, backend="pocket-tts"):
        content = inspect_story_index(write_content(root / "content"))
        jobs = PregenerationJobStore(root / "jobs")
        job = jobs.create_or_resume(content, ("selected",))
        manifest = write_manifest(root / "voices")
        settings = AppSettings(
            speech_backend=backend,
            pocket_gated_model_accepted=backend == "pocket-tts",
        )
        library = VoiceLibrary(root / "library")
        if narrator:
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Narrator",
                "character:centurion",
            )
        voice_plan = VoicePlanStore(jobs, voice_library=library).create(
            job, settings, manifest_path=manifest
        )
        return job, jobs, voice_plan, manifest

    def test_generation_input_inventory_uses_sql_without_scans_and_sees_new_rows(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"HOME": str(root / "home")}):
                job, jobs, plan, _ = self.fixture(root)
                original = PregenerationInputStore(jobs).materialize(job, plan)
                generate_prepared(original, PocketRenderer())
                indexed = PregenerationJobStore(jobs.root)
                glob = Path.glob
                scans = []

                def counted_glob(path, pattern, *args, **kwargs):
                    if "generation-input-" in pattern:
                        scans.append((path, pattern))
                    return glob(path, pattern, *args, **kwargs)

                with patch.object(Path, "glob", counted_glob):
                    self.assertEqual(
                        indexed.generation_input_directories(),
                        (original.directory.resolve(),),
                    )
                    revised = replace(
                        plan,
                        groups=tuple(
                            replace(group, control_sha256="e" * 64)
                            for group in plan.groups
                        ),
                    )
                    current = PregenerationInputStore(indexed).materialize(job, revised)
                    self.assertNotEqual(
                        current.directory.resolve(), original.directory.resolve()
                    )
                    self.assertFalse(
                        (current.directory.resolve() / "input.json").exists()
                    )
                    self.assertEqual(
                        set(indexed.generation_input_directories()),
                        {original.directory.resolve(), current.directory.resolve()},
                    )
                    self.assertEqual(
                        set(
                            indexed.generation_input_directories(
                                job.catalog_story_index_sha256
                            )
                        ),
                        {original.directory.resolve(), current.directory.resolve()},
                    )
                    content = inspect_story_index(job.catalog_story_index)
                    inspector = StoryAudioInspector(content, indexed)
                    self.assertEqual(inspector.inspect("selected").generated, 4)
                    self.assertEqual(inspector.inspect("selected").generated, 4)
                    self.assertEqual(len(scans), 0)
                before = load_generation_state(
                    generation_state_path(original.output_directory), original.queue
                )
                after = load_generation_state(
                    generation_state_path(current.output_directory), current.queue
                )
                self.assertEqual(len(after["items"]), 4)
                for key, value in after["items"].items():
                    self.assertTrue(
                        (current.output_directory / value["path"]).samefile(
                            original.output_directory / before["items"][key]["path"]
                        )
                    )

    def test_pending_sql_publication_recovers_before_or_after_directory_rename(self):
        for renamed in (False, True):
            with self.subTest(renamed=renamed), TemporaryDirectory() as temporary:
                root = Path(temporary)
                job, jobs, plan, _ = self.fixture(root)
                store = PregenerationInputStore(jobs)
                prepared = store.materialize(job, plan)
                document = load_generation_input_metadata(prepared.directory)
                staging = prepared.directory.with_name(".generation-input-interrupted")
                if not renamed:
                    prepared.directory.rename(staging)
                jobs.catalog.save_input(
                    job.job_id, document, prepared.directory, staging=staging
                )
                recovered = store.materialize(job, plan)
                self.assertEqual(recovered, prepared)
                self.assertIsNone(jobs.catalog.input_staging(prepared.directory))
                self.assertFalse(staging.exists())
                self.assertFalse((prepared.directory / "input.json").exists())
                self.assertEqual(
                    load_generation_input_metadata(prepared.directory), document
                )

    def test_pending_sql_publication_rejects_corrupt_or_missing_staged_assets(self):
        for missing in (False, True):
            with self.subTest(missing=missing), TemporaryDirectory() as temporary:
                root = Path(temporary)
                job, jobs, plan, _ = self.fixture(root)
                prepared = PregenerationInputStore(jobs).materialize(job, plan)
                document = load_generation_input_metadata(prepared.directory)
                staging = prepared.directory.with_name(".generation-input-interrupted")
                prepared.directory.rename(staging)
                if missing:
                    (staging / "queue.jsonl").unlink()
                else:
                    (staging / "queue.jsonl").write_bytes(b"changed queue")
                jobs.catalog.save_input(
                    job.job_id, document, prepared.directory, staging=staging
                )
                with self.assertRaises((OSError, ValueError)):
                    load_generation_input_metadata(prepared.directory)
                self.assertFalse(prepared.directory.exists())
                self.assertEqual(
                    jobs.catalog.input_staging(prepared.directory), staging.resolve()
                )
                self.assertTrue(staging.exists())

    def test_pending_future_input_schema_is_rejected_without_publishing_or_rewriting(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, jobs, plan, _ = self.fixture(root)
            prepared = PregenerationInputStore(jobs).materialize(job, plan)
            document = load_generation_input_metadata(prepared.directory)
            staging = prepared.directory.with_name(".generation-input-future")
            prepared.directory.rename(staging)
            jobs.catalog.save_input(
                job.job_id, document, prepared.directory, staging=staging
            )
            document["schema_version"] = 99
            with jobs.catalog._connection(write=True) as connection:
                connection.execute(
                    "UPDATE generation_inputs SET payload=? WHERE directory=?",
                    (json.dumps(document), str(prepared.directory.resolve())),
                )
            before = jobs.catalog.path.read_bytes()
            with self.assertRaisesRegex(
                PregenerationQueueError, "Unsupported offline generation input schema"
            ):
                PregenerationInputStore(jobs).materialize(job, plan)
            self.assertFalse(prepared.directory.exists())
            self.assertTrue(staging.is_dir())
            self.assertEqual(jobs.catalog.path.read_bytes(), before)
            self.assertEqual(
                jobs.catalog.input_staging(prepared.directory), staging.resolve()
            )

    def test_concurrent_same_input_publication_keeps_one_complete_sql_registration(
        self,
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            job, jobs, plan, _ = self.fixture(root)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        PregenerationInputStore(jobs).materialize, job, plan
                    )
                    for _ in range(2)
                ]
                first, second = (future.result() for future in futures)
            self.assertEqual(first, second)
            self.assertIsNone(jobs.catalog.input_staging(first.directory))
            self.assertEqual(
                jobs.generation_input_directories(), (first.directory.resolve(),)
            )
            self.assertEqual(
                load_generation_input_metadata(first.directory)["identity"],
                first.identity,
            )

    def test_unindexed_metadata_is_not_admitted_and_expected_input_fails_closed(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), TemporaryDirectory() as directory:
                root = Path(directory)
                with patch.dict(os.environ, {"HOME": str(root / "home")}):
                    job, jobs, plan, _ = self.fixture(root)
                    original = PregenerationInputStore(jobs).materialize(job, plan)
                    generate_prepared(original, PocketRenderer())
                    document = load_generation_input_metadata(original.directory)
                    legacy = original.directory / "input.json"
                    legacy.write_text(
                        "broken legacy metadata" if corrupt else json.dumps(document)
                    )
                    payload = legacy.read_bytes()
                    with jobs.catalog._connection(write=True) as connection:
                        connection.execute(
                            "DELETE FROM generation_inputs WHERE directory=?",
                            (str(original.directory.resolve()),),
                        )
                    restarted = PregenerationJobStore(jobs.root)
                    self.assertEqual(restarted.generation_input_directories(), ())
                    with self.assertRaisesRegex(
                        ValueError, "explicit legacy migration"
                    ):
                        load_generation_input_metadata(original.directory)
                    with self.assertRaisesRegex(
                        PregenerationQueueError, "explicit legacy migration"
                    ):
                        PregenerationInputStore(restarted).materialize(job, plan)
                    self.assertEqual(legacy.read_bytes(), payload)
                    self.assertIsNone(
                        restarted.catalog.input_document(original.directory)
                    )
                    self.assertEqual(
                        len(list(original.output_directory.rglob("*.wav"))), 4
                    )

    def test_indexed_inventory_reuses_unchanged_selected_lines_across_content_revisions(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"HOME": str(root / "home")}):
                job, jobs, plan, manifest = self.fixture(root)
                store = PregenerationInputStore(jobs)
                original = store.materialize(job, plan)
                generate_prepared(original, PocketRenderer())
                before = load_generation_state(
                    generation_state_path(original.output_directory), original.queue
                )
                story = load_story_index_document(job.story_index)
                records = [record.to_record() for record in story.records]
                for record in records:
                    if record["line_id"] == "not-selected":
                        record["text"] = "Revised unrelated chapter."
                        record.pop("text_sha256", None)
                write_story_index_document(job.story_index, story.metadata, records)
                content = inspect_story_index(job.story_index)
                updated_job = jobs.create_or_resume(content, ("selected",))
                self.assertNotEqual(
                    updated_job.catalog_story_index_sha256,
                    job.catalog_story_index_sha256,
                )
                updated_plan = VoicePlanStore(
                    jobs, voice_library=VoiceLibrary(root / "library")
                ).create(
                    updated_job,
                    AppSettings(
                        speech_backend="pocket-tts", pocket_gated_model_accepted=True
                    ),
                    manifest_path=manifest,
                )
                current = store.materialize(updated_job, updated_plan)
                after = load_generation_state(
                    generation_state_path(current.output_directory), current.queue
                )
                self.assertEqual(len(after["items"]), 4)
                self.assertNotIn(
                    original.directory.resolve(),
                    jobs.catalog.input_directories(
                        updated_job.catalog_story_index_sha256
                    ),
                )
                self.assertIn(
                    original.directory.resolve(), jobs.generation_input_directories()
                )
                for key, value in after["items"].items():
                    audio = current.output_directory / value["path"]
                    previous = original.output_directory / before["items"][key]["path"]
                    self.assertTrue(audio.samefile(previous))
                    self.assertEqual(
                        sha256_file(audio), before["items"][key]["file_sha256"]
                    )
                renderer = PocketRenderer()
                generate_prepared(current, renderer)
                self.assertEqual(renderer.requests, [])

    def test_materializes_selected_story_effective_voices_and_queue(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)

            result = PregenerationInputStore(jobs).materialize(job, voice_plan)

            self.assertEqual(result.queue_items, 4)
            self.assertEqual(result.ready_items, 4)
            self.assertEqual(result.narrator_fallback_roles, ("Hotelier",))
            queue = VoiceGenerationQueue.load(result.queue)
            self.assertEqual(
                tuple(item.line_id for item in queue.items),
                ("original", "rhiannon", "hotelier", "unknown"),
            )
            manifest = json.loads(result.voice_manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                [voice["character"] for voice in manifest["voices"]],
                ["Narrator", "Rhiannon"],
            )
            for voice in manifest["voices"]:
                for relative in voice["references"]:
                    probe_pcm16_mono_wav(result.directory / relative)
            self.assertNotIn("not-selected", result.story_index.read_text())

    def test_voice_revision_reuses_completed_other_characters_without_cache(self):
        for partial, cross_job in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            with (
                self.subTest(partial=partial, cross_job=cross_job),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                job, jobs, plan, manifest = self.fixture(root)
                store = PregenerationInputStore(jobs)
                original = store.materialize(job, plan)
                generate_prepared(
                    original,
                    PocketRenderer(),
                    include_characters=("Narrator", "Hotelier") if partial else None,
                )
                before = load_generation_state(
                    generation_state_path(original.output_directory), original.queue
                )
                old_files = {
                    key: original.output_directory / value["path"]
                    for key, value in before["items"].items()
                }
                for source in old_files.values():
                    detached = source.with_suffix(".detached")
                    detached.write_bytes(source.read_bytes())
                    detached.replace(source)
                centurion = CharacterVoiceRegistry.from_file(manifest).resolve(
                    "Centurion"
                )
                revised = replace(
                    plan,
                    job_id="b" * 24 if cross_job else plan.job_id,
                    groups=tuple(
                        replace(
                            group,
                            source_id="character:centurion",
                            source_character="Centurion",
                            source_speaker="centurion-v1",
                            reference_sha256s=tuple(
                                sha256_file(path) for path in centurion.references
                            ),
                            control_sha256="f" * 64,
                        )
                        if group.character == "Rhiannon"
                        else group
                        for group in plan.groups
                    ),
                )
                if cross_job:
                    job = replace(job, job_id=revised.job_id)
                current = store.materialize(job, revised)
                state = load_generation_state(
                    generation_state_path(current.output_directory), current.queue
                )
                self.assertEqual(
                    {value["line_id"] for value in state["items"].values()},
                    {"hotelier", "unknown"},
                )
                for key, value in state["items"].items():
                    target = current.output_directory / value["path"]
                    self.assertTrue(target.samefile(old_files[key]))
                    self.assertEqual(sha256_file(target), value["file_sha256"])
                changes = OfflinePackPublisher().inspect_changes(job, current)
                self.assertEqual((changes.reused, changes.new), (2, 2))
                self.assertEqual(
                    PregenerationInputStore(jobs).materialize(job, revised), current
                )
                renderer = PocketRenderer()
                generate_prepared(current, renderer)
                self.assertEqual(len(renderer.requests), 2)
                self.assertEqual(
                    {request.voice for request in renderer.requests}, {"Rhiannon"}
                )

    def test_voice_revision_counts_partial_audio_across_chapters_and_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_job, jobs, _plan, manifest = self.fixture(root)
            story = load_story_index_document(first_job.story_index)
            templates = {record.line_id: record.to_record() for record in story.records}
            records = []
            for index in range(320):
                changed = index % 160 < 10
                record = dict(templates["rhiannon" if changed else "hotelier"])
                record.pop("text_sha256", None)
                record.update(
                    line_id=f"line-{index}",
                    chapter=str(index // 160 + 1),
                    sequence=index % 160 + 1,
                    text=f"Chapter dialogue {index}.",
                )
                records.append(record)
            write_story_index_document(first_job.story_index, story.metadata, records)
            content = inspect_story_index(first_job.story_index)
            job = jobs.create_or_resume(
                content,
                tuple(selection.selection_id for selection in content.selections),
            )
            plans = VoicePlanStore(jobs, voice_library=VoiceLibrary(root / "library"))
            settings = AppSettings(
                speech_backend="pocket-tts", pocket_gated_model_accepted=True
            )
            store = PregenerationInputStore(jobs)
            for chapter in range(2):
                selected = tuple(
                    f"line-{index}"
                    for index in range(chapter * 160, (chapter + 1) * 160)
                )
                chapter_job = replace(
                    job, job_id=str(chapter + 1) * 24, selected_line_ids=selected
                )
                chapter_plan = plans.create(
                    chapter_job, settings, manifest_path=manifest
                )
                prepared = store.materialize(chapter_job, chapter_plan)
                queue = VoiceGenerationQueue.load(prepared.queue)
                generate_prepared(
                    prepared,
                    PocketRenderer(),
                    include_queue_ids=tuple(item.queue_id for item in queue.items[:60]),
                )
            plan = plans.create(job, settings, manifest_path=manifest)
            combined = store.materialize(job, plan)
            changes = OfflinePackPublisher().inspect_changes(job, combined)
            self.assertEqual((changes.reused, changes.new), (120, 200))
            centurion = CharacterVoiceRegistry.from_file(manifest).resolve("Centurion")
            revised = replace(
                plan,
                groups=tuple(
                    replace(
                        group,
                        source_id="character:centurion",
                        source_character="Centurion",
                        source_speaker="centurion-v1",
                        reference_sha256s=tuple(
                            sha256_file(path) for path in centurion.references
                        ),
                        control_sha256="f" * 64,
                    )
                    if group.character == "Rhiannon"
                    else group
                    for group in plan.groups
                ),
            )
            with patch(
                "vntts.pregeneration_queue._voice_content_identity",
                wraps=_voice_content_identity,
            ) as identity:
                current = store.materialize(job, revised)
            self.assertLessEqual(identity.call_count, 8)
            changes = OfflinePackPublisher().inspect_changes(job, current)
            self.assertEqual((changes.reused, changes.new), (100, 220))
            restarted = PregenerationInputStore(jobs).materialize(job, revised)
            changes = OfflinePackPublisher().inspect_changes(job, restarted)
            self.assertEqual((changes.reused, changes.new), (100, 220))
            renderer = PocketRenderer()
            generate_prepared(restarted, renderer)
            self.assertEqual(len(renderer.requests), 220)
            self.assertEqual(
                sum(request.voice == "Rhiannon" for request in renderer.requests), 20
            )
            changes = OfflinePackPublisher().inspect_changes(job, restarted)
            self.assertEqual((changes.reused, changes.new), (320, 0))

    def test_story_coverage_includes_verified_unpublished_partial_recordings(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _manifest = self.fixture(root)
            prepared = PregenerationInputStore(jobs).materialize(job, plan)
            generate_prepared(
                prepared,
                PocketRenderer(),
                include_characters=("Narrator", "Hotelier"),
            )
            content = inspect_story_index(job.story_index)
            selection = content.selections[0].selection_id
            inspector = StoryAudioInspector(content, jobs)
            with patch(
                "vntts.pregeneration_pack.load_generation_state_from_snapshot",
                wraps=load_generation_state_from_snapshot,
            ) as loaded:
                coverage = inspector.inspect(selection)
                self.assertEqual((coverage.generated, coverage.missing), (2, 2))
                self.assertIsNone(coverage.manifest)
                self.assertEqual(inspector.inspect(selection), coverage)
                self.assertEqual(loaded.call_count, 1)
            generate_prepared(prepared, PocketRenderer())
            coverage = inspector.inspect(selection)
            self.assertEqual((coverage.generated, coverage.missing), (4, 0))
            restarted = StoryAudioInspector(content, jobs)
            with (
                patch(
                    "vntts.pregeneration_pack.load_stable_generation_queue"
                ) as parsed,
                patch(
                    "vntts.pregeneration_pack.load_generation_state_from_snapshot"
                ) as validated,
            ):
                self.assertEqual(restarted.inspect(selection), coverage)
                parsed.assert_not_called()
                validated.assert_not_called()
            state = load_generation_state(
                generation_state_path(prepared.output_directory), prepared.queue
            )
            audio = (
                prepared.output_directory / next(iter(state["items"].values()))["path"]
            )
            before = audio.stat()
            payload = audio.read_bytes()
            audio.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
            os.utime(audio, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaises(OfflinePackError):
                inspector.inspect(selection)
            with self.assertRaises(OfflinePackError):
                restarted.inspect(selection)
            with self.assertRaises(OfflinePackError):
                StoryAudioInspector(content, jobs).inspect(selection)

    def test_recording_index_skips_unrelated_inputs_but_rechecks_changed_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _ = self.fixture(root)
            prepared = PregenerationInputStore(jobs).materialize(job, plan)
            generate_prepared(
                prepared,
                PocketRenderer(),
                include_characters=("Narrator",),
            )
            content = inspect_story_index(job.story_index)
            StoryAudioInspector(content, jobs).inspect(
                content.selections[0].selection_id
            )
            queue = VoiceGenerationQueue.load(prepared.queue)
            unrelated = replace(queue.items[0], line_id="unrelated-line")
            document = load_generation_input_metadata(prepared.directory)
            state_path = generation_state_path(prepared.output_directory)
            arguments = (
                jobs,
                prepared.directory,
                state_path,
                document,
                {unrelated.queue_id: unrelated},
                plan.synthesis_controls_sha256,
            )
            self.assertFalse(_may_have_saved_recordings(*arguments))
            original = state_path.read_bytes()
            try:
                state_path.write_bytes(original + b"changed")
                self.assertTrue(_may_have_saved_recordings(*arguments))
            finally:
                state_path.write_bytes(original)
            self.assertFalse(_may_have_saved_recordings(*arguments))

    def test_recording_index_keeps_sqlite_sidecar_admission_on_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _ = self.fixture(root)
            prepared = PregenerationInputStore(jobs).materialize(job, plan)
            generate_prepared(prepared, PocketRenderer())
            content = inspect_story_index(job.story_index)
            selection = content.selections[0].selection_id
            self.assertEqual(
                StoryAudioInspector(content, jobs).inspect(selection).generated, 4
            )
            queue = VoiceGenerationQueue.load(prepared.queue)
            unrelated = replace(queue.items[0], line_id="unrelated-line")
            state = generation_state_path(prepared.output_directory)
            target = root / "unrelated"
            target.write_bytes(b"keep this file")
            for suffix in ("-journal", "-wal", "-shm"):
                with self.subTest(suffix=suffix):
                    sidecar = state.with_name(state.name + suffix)
                    sidecar.symlink_to(target)
                    try:
                        with self.assertRaisesRegex(OfflinePackError, "regular file"):
                            StoryAudioInspector(content, jobs).inspect(selection)
                        with self.assertRaisesRegex(ValueError, "regular file"):
                            _may_have_saved_recordings(
                                jobs,
                                prepared.directory,
                                state,
                                load_generation_input_metadata(prepared.directory),
                                {unrelated.queue_id: unrelated},
                                plan.synthesis_controls_sha256,
                            )
                    finally:
                        sidecar.unlink()
            self.assertEqual(target.read_bytes(), b"keep this file")

    def test_sql_readiness_keeps_unselected_path_validation_on_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original, jobs, _plan, manifest = self.fixture(root)
            content = inspect_story_index(original.catalog_story_index)
            job = jobs.create_or_resume(content, ("selected", "later"))
            plan = VoicePlanStore(
                jobs, voice_library=VoiceLibrary(root / "library")
            ).create(
                job,
                AppSettings(
                    speech_backend="pocket-tts", pocket_gated_model_accepted=True
                ),
                manifest_path=manifest,
            )
            prepared = PregenerationInputStore(jobs).materialize(job, plan)
            generate_prepared(prepared, PocketRenderer())
            inspector = StoryAudioInspector(content, jobs)
            self.assertEqual(inspector.inspect("selected").generated, 4)
            self.assertTrue((jobs.root / "recordings.sqlite3").exists())
            state = load_generation_state(
                generation_state_path(prepared.output_directory), prepared.queue
            )
            saved = next(
                value
                for value in state["items"].values()
                if value["line_id"] == "not-selected"
            )
            audio = prepared.output_directory / saved["path"]
            target = root / "outside.wav"
            target.write_bytes(audio.read_bytes())
            audio.unlink()
            audio.symlink_to(target)
            with self.assertRaisesRegex(
                OfflinePackError, "stay within generation output"
            ):
                inspector.inspect("selected")
            with self.assertRaisesRegex(
                OfflinePackError, "stay within generation output"
            ):
                StoryAudioInspector(content, jobs).inspect("selected")

    def test_story_coverage_checks_only_selected_wavs_and_reuses_batch_validation(self):
        for shared_state in (False, True):
            with (
                self.subTest(shared_state=shared_state),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                job, jobs, plan, manifest = self.fixture(root)
                content = inspect_story_index(job.catalog_story_index)
                store = PregenerationInputStore(jobs)
                if not shared_state:
                    prepared = store.materialize(job, plan)
                    generate_prepared(prepared, PocketRenderer())
                other_job = jobs.create_or_resume(
                    content, ("selected", "later") if shared_state else ("later",)
                )
                other_plan = VoicePlanStore(
                    jobs, voice_library=VoiceLibrary(root / "library")
                ).create(
                    other_job,
                    AppSettings(
                        speech_backend="pocket-tts", pocket_gated_model_accepted=True
                    ),
                    manifest_path=manifest,
                )
                other = store.materialize(other_job, other_plan)
                renderer = PocketRenderer()
                renderer.inspect_state = lambda request: setattr(
                    renderer,
                    "pcm",
                    audio_samples()
                    * (0.5 if request.text == "Do not include me." else 1.0),
                )
                if shared_state:
                    generate_prepared(other, renderer)
                else:
                    run_bulk_generation(
                        other.queue,
                        other.output_directory,
                        renderer,
                        provider="pocket-tts",
                        model="pocket-tts",
                        generation_profile="default",
                        retries=0,
                        control_files={"voice_manifest": other.voice_manifest},
                    )
                inspector = StoryAudioInspector(content, jobs)
                with patch(
                    "vntts.authoring.generation_state.validate_success_file_with_samples",
                    wraps=validate_success_file_with_samples,
                ) as validated:
                    for _ in range(2):
                        self.assertEqual(inspector.inspect("selected").generated, 4)
                        self.assertEqual(inspector.inspect("later").generated, 1)
                    self.assertEqual(validated.call_count, 5)
                state = load_generation_state(
                    generation_state_path(other.output_directory), other.queue
                )
                saved = next(
                    value
                    for value in state["items"].values()
                    if value["line_id"] == "not-selected"
                )
                audio = other.output_directory / saved["path"]
                audio.write_bytes(b"damaged unselected chapter")
                self.assertEqual(inspector.inspect("selected").generated, 4)
                self.assertEqual(
                    StoryAudioInspector(content, jobs).inspect("selected").generated, 4
                )
                with self.assertRaises(OfflinePackError):
                    inspector.inspect("later")
                with self.assertRaises(OfflinePackError):
                    StoryAudioInspector(content, jobs).inspect("later")
                if shared_state:
                    revised = replace(
                        plan,
                        groups=tuple(
                            replace(group, control_sha256="e" * 64)
                            for group in plan.groups
                        ),
                    )
                    selected_input = store.materialize(job, revised)
                    changes = OfflinePackPublisher().inspect_changes(
                        job, selected_input
                    )
                    self.assertEqual((changes.reused, changes.new), (4, 0))
                queue, digest = load_stable_generation_queue(other.queue)
                for malformed in ("unknown_status", "unknown_queue"):
                    changed = json.loads(json.dumps(state))
                    if malformed == "unknown_status":
                        changed["items"][
                            next(
                                key
                                for key, value in changed["items"].items()
                                if value["line_id"] == "not-selected"
                            )
                        ]["status"] = "unknown"
                    else:
                        changed["items"]["unknown-queue"] = saved
                    with self.assertRaises(BulkGenerationError):
                        load_generation_state_from_snapshot(
                            generation_state_path(other.output_directory),
                            queue,
                            digest,
                            state_document=changed,
                            selected_line_ids=frozenset(job.selected_line_ids),
                        )

    def test_restart_imports_missing_results_without_changing_existing_decisions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _ = self.fixture(root)
            store = PregenerationInputStore(jobs)
            original = store.materialize(job, plan)
            revised = replace(
                plan,
                groups=tuple(
                    replace(group, control_sha256="e" * 64) for group in plan.groups
                ),
            )
            current = store.materialize(job, revised)
            generate_prepared(
                current,
                PocketRenderer(
                    [RuntimeError("recorded failure"), RuntimeError("recorded failure")]
                ),
                include_characters=("Rhiannon",),
            )
            before = load_generation_state(
                generation_state_path(current.output_directory), current.queue
            )
            generate_prepared(
                original, PocketRenderer(), include_characters=("Narrator", "Hotelier")
            )
            self.assertEqual(
                PregenerationInputStore(jobs).materialize(job, revised), current
            )
            after = load_generation_state(
                generation_state_path(current.output_directory), current.queue
            )
            self.assertEqual(len(after["items"]), 4)
            for queue_id, result in before["items"].items():
                self.assertEqual(after["items"][queue_id], result)

    def test_corrupt_completed_recording_is_not_reused_or_replaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _ = self.fixture(root)
            store = PregenerationInputStore(jobs)
            original = store.materialize(job, plan)
            generate_prepared(original, PocketRenderer())
            state = load_generation_state(
                generation_state_path(original.output_directory), original.queue
            )
            source = (
                original.output_directory / next(iter(state["items"].values()))["path"]
            )
            source.write_bytes(b"corrupt recording")
            revised = replace(
                plan,
                groups=tuple(
                    replace(group, control_sha256="e" * 64) for group in plan.groups
                ),
            )
            with self.assertRaisesRegex(
                PregenerationQueueError, "reuse saved offline audio"
            ):
                store.materialize(job, revised)
            self.assertEqual(source.read_bytes(), b"corrupt recording")

    def test_changed_synthesis_controls_do_not_reuse_completed_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _ = self.fixture(root)
            store = PregenerationInputStore(jobs)
            original = store.materialize(job, plan)
            generate_prepared(original, PocketRenderer())
            revised = replace(
                plan,
                synthesis_controls_sha256="d" * 64,
                groups=tuple(
                    replace(group, control_sha256="e" * 64) for group in plan.groups
                ),
            )
            current = store.materialize(job, revised)
            self.assertFalse(current.output_directory.exists())

    def test_unrelated_damaged_previous_job_does_not_block_preparation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            job, jobs, plan, _ = self.fixture(root)
            store = PregenerationInputStore(jobs)
            original = store.materialize(job, plan)
            generate_prepared(original, PocketRenderer())
            original.voice_manifest.write_bytes(b"damaged unrelated manifest")
            other_job = replace(
                job, job_id="b" * 24, selected_line_ids=("not-selected",)
            )
            current = store.materialize(
                other_job, replace(plan, job_id=other_job.job_id)
            )
            self.assertEqual(
                [
                    item.line_id
                    for item in VoiceGenerationQueue.load(current.queue).items
                ],
                ["not-selected"],
            )
            self.assertFalse(current.output_directory.exists())

    def test_materialization_rejects_duplicate_selected_line_ids(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)
            repeated = replace(job, selected_line_ids=(job.selected_line_ids[0],) * 2)

            with self.assertRaisesRegex(
                PregenerationQueueError, "duplicate identities"
            ):
                PregenerationInputStore(jobs).materialize(repeated, voice_plan)

    def test_materializes_narrator_by_source_id_not_friendly_label(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)
            voice_plan = replace(
                voice_plan,
                groups=tuple(
                    replace(group, source_character="Friendly narrator label")
                    if group.route == "narrator"
                    else group
                    for group in voice_plan.groups
                ),
            )

            result = PregenerationInputStore(jobs).materialize(job, voice_plan)

            self.assertEqual(result.narrator_fallback_roles, ("Hotelier",))

    def test_materialization_records_build_and_reuse_phase_timings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)
            store = PregenerationInputStore(jobs)
            with patch(
                "vntts.pregeneration_queue.record_background_operation"
            ) as record:
                store.materialize(job, voice_plan)
                store.materialize(job, voice_plan)

            operations = [call.args[0] for call in record.call_args_list]
            self.assertEqual(
                operations,
                [
                    "pregeneration-input-load",
                    "pregeneration-input-identity",
                    "pregeneration-input-story-projection",
                    "pregeneration-input-voice-copy",
                    "pregeneration-input-queue-build",
                    "pregeneration-input-publish",
                    "pregeneration-input-load",
                    "pregeneration-input-identity",
                    "pregeneration-input-reuse",
                ],
            )
            self.assertTrue(
                all("cpu_ms" in call.kwargs for call in record.call_args_list)
            )

    def test_classifies_mixed_and_pure_audio_events(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            story_path = write_content(root / "content")
            story = load_story_index_document(story_path)
            records = [record.to_record() for record in story.records]
            for line_id, sequence, text in (
                ("mixed-event", 5, "*cough* Who is there?"),
                ("pure-event", 6, "*chirp*"),
            ):
                records.append(
                    {
                        "record_type": "line",
                        "line_id": line_id,
                        "chapter": "1",
                        "sequence": sequence,
                        "speaker": "Narrator",
                        "voice_character": "Narrator",
                        "text": text,
                        "kind": "dialogue",
                        "collection_id": "selected",
                        "source_audio_status": "absent",
                        "speakable": True,
                    }
                )
            write_story_index_document(story_path, story.metadata, records)
            content = inspect_story_index(story_path)
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("selected",))
            manifest = write_manifest(root / "voices")
            library = VoiceLibrary(root / "library")
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Narrator",
                "character:centurion",
            )
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )

            with patch(
                "vntts.pregeneration_queue.audio_event_plan_for_record",
                wraps=audio_event_plan_for_record,
            ) as parse_events:
                result = PregenerationInputStore(jobs).materialize(job, plan)
            self.assertEqual(parse_events.call_count, 2 * result.queue_items)
            queue = VoiceGenerationQueue.load(result.queue)
            queue_by_line = {item.line_id: item.queue_id for item in queue.items}
            original = jobs.catalog.input_document(result.directory)
            self.assertIsNotNone(original)

            def replace_metadata(document):
                with (
                    closing(sqlite3.connect(jobs.catalog.path)) as connection,
                    connection,
                ):
                    updated = connection.execute(
                        "UPDATE generation_inputs SET payload = ? WHERE directory = ?",
                        (json.dumps(document), str(result.directory.resolve())),
                    )
                    self.assertEqual(updated.rowcount, 1)

            for field, value, error in (
                (
                    "audio_event_projection_queue_ids",
                    ["unknown"],
                    "projection queue ids changed",
                ),
                (
                    "audio_event_projection_queue_ids",
                    [queue_by_line["pure-event"]],
                    "projection queue ids changed",
                ),
                (
                    "audio_event_projection_queue_ids",
                    [],
                    "projection queue ids changed",
                ),
                (
                    "audio_event_omission_queue_ids",
                    [queue_by_line["mixed-event"]],
                    "omission queue ids changed",
                ),
                ("audio_event_omission_queue_ids", [], "omission queue ids changed"),
                (
                    "audio_event_projection_queue_ids",
                    [1],
                    "projection queue ids are invalid",
                ),
                ("queue_items", 5, "queue item counts changed"),
                ("queue_items", True, "queue items must be"),
                ("ready_items", 6, "queue item counts changed"),
                ("ready_items", 4, "queue item counts changed"),
                ("narrator_fallback_roles", [], "narrator fallback roles changed"),
                (
                    "narrator_fallback_roles",
                    ["Other"],
                    "narrator fallback roles changed",
                ),
                ("ready_items", -1, "ready items must be"),
            ):
                with self.subTest(field=field, value=value):
                    replace_metadata({**original, field: value})
                    with self.assertRaisesRegex(PregenerationQueueError, error):
                        PregenerationInputStore(jobs).materialize(job, plan)
            replace_metadata(original)
            self.assertEqual(
                PregenerationInputStore(jobs).materialize(job, plan), result
            )

        self.assertEqual(result.queue_items, 6)
        self.assertEqual(result.ready_items, 5)
        self.assertEqual(
            result.audio_event_projection_queue_ids,
            (queue_by_line["mixed-event"],),
        )
        self.assertEqual(
            result.audio_event_omission_queue_ids,
            (queue_by_line["pure-event"],),
        )

    def test_materializes_one_voice_despite_portrait_and_bank_variants(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            story_path = write_content(root / "content")
            story = load_story_index_document(story_path)
            records = [record.to_record() for record in story.records]
            next(
                record for record in records if record["line_id"] == "not-selected"
            ).update(portrait="young", source_bank="young.bnk")
            write_story_index_document(story_path, story.metadata, records)
            content = inspect_story_index(story_path)
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("selected", "later"))
            manifest = write_manifest(root / "voices")
            library = VoiceLibrary(root / "library")
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Narrator",
                "character:centurion",
            )
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                AppSettings(pocket_gated_model_accepted=True),
                manifest_path=manifest,
            )
            variants = [group for group in plan.groups if group.character == "Rhiannon"]
            self.assertEqual(len(variants), 1)
            self.assertEqual(
                {group.source_character for group in variants}, {"Rhiannon"}
            )

            result = PregenerationInputStore(jobs).materialize(job, plan)
            queue = VoiceGenerationQueue.load(result.queue)
            routes = {item.line_id: item for item in queue.items}

        self.assertEqual(routes["rhiannon"].speaker, "Aderyn")
        self.assertEqual(routes["rhiannon"].voice_character, "Rhiannon")
        self.assertEqual(routes["not-selected"].speaker, "Rhiannon")
        self.assertEqual(routes["not-selected"].voice_character, "Rhiannon")

    def test_projects_semantic_evidence_to_selected_lines(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(
                add_semantic_evidence(write_content(root / "content"))
            )
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("selected",))
            manifest = write_manifest(root / "voices")
            settings = AppSettings(
                speech_backend="pocket-tts",
                pocket_gated_model_accepted=True,
            )
            library = VoiceLibrary(root / "library")
            remember_voice_binding(
                library,
                CharacterVoiceRegistry.from_file(manifest),
                "Narrator",
                "character:centurion",
            )
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                settings,
                manifest_path=manifest,
            )

            source_evidence = root / "content" / "source-audio-semantic-evidence.json"
            original_evidence = source_evidence.read_bytes()
            source_evidence.write_bytes(b"corrupt")
            with self.assertRaisesRegex(
                PregenerationQueueError, "source-audio evidence is invalid"
            ):
                PregenerationInputStore(jobs).materialize(job, plan)
            source_evidence.write_bytes(original_evidence)

            with (
                patch(
                    "vntts.source_audio_semantics.validate_source_audio_semantic_evidence",
                    wraps=validate_source_audio_semantic_evidence,
                ) as evidence_validation,
                patch(
                    "vntts.source_audio_semantics.load_story_index_document",
                    wraps=load_story_index_document,
                ) as story_load,
            ):
                result = PregenerationInputStore(jobs).materialize(job, plan)
            evidence = load_source_audio_semantic_evidence(
                result.source_audio_semantic_evidence,
                result.story_index,
            )
            selected_story = load_story_index_document(result.story_index)

        self.assertEqual(len(evidence["entries"]), 1)
        self.assertEqual(evidence["entries"][0]["source_line_ids"], ["original"])
        self.assertEqual(
            selected_story.metadata["source_audio_semantics"]["applied_count"],
            1,
        )
        self.assertEqual(evidence_validation.call_count, 2)
        self.assertEqual(story_load.call_count, 1)

    def test_same_identity_resumes_without_rewriting(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)
            store = PregenerationInputStore(jobs)

            first = store.materialize(job, voice_plan)
            timestamp = first.queue.stat().st_mtime_ns
            second = store.materialize(job, voice_plan)

            self.assertEqual(
                first.identity,
                "e0d9cb8704ab8f8acf460f38b645fdbfd8714dd38a2db045e4fe9cf392bd0e83",
            )
            self.assertEqual(first, second)
            self.assertEqual(second.queue.stat().st_mtime_ns, timestamp)

    def test_missing_narrator_voice_is_one_actionable_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(
                root, narrator=False, backend="moss-tts"
            )

            with self.assertRaisesRegex(
                PregenerationQueueError, "Choose a narrator voice"
            ):
                PregenerationInputStore(jobs).materialize(job, voice_plan)

            self.assertFalse(
                any((root / "jobs" / job.job_id).glob("generation-input-*"))
            )

    def test_default_pocket_narrator_needs_no_manifest_or_human_decision(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("selected",))
            voice_plan = VoicePlanStore(jobs).create(job, AppSettings())

            result = PregenerationInputStore(jobs).materialize(job, voice_plan)

            manifest = json.loads(result.voice_manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["voices"],
                [
                    {
                        "aliases": [],
                        "character": "Narrator",
                        "references": [],
                        "speaker": "alba",
                        "vntts.source_character": "alba",
                    }
                ],
            )
            self.assertEqual(result.ready_items, 4)
            self.assertEqual(
                result.narrator_fallback_roles,
                ("Hotelier", "Rhiannon"),
            )

    def test_public_pocket_selected_narrator_never_stages_reference_audio(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            content = inspect_story_index(write_content(root / "content"))
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("selected",))
            manifest_path = write_manifest(root / "voices")
            library = VoiceLibrary(root / "library")
            library.select("Narrator", route="voice", source_id="preset:marius")
            plan = VoicePlanStore(jobs, voice_library=library).create(
                job,
                AppSettings(),
                manifest_path=manifest_path,
            )

            result = PregenerationInputStore(jobs).materialize(job, plan)
            manifest = json.loads(result.voice_manifest.read_text(encoding="utf-8"))

            self.assertEqual(
                manifest["voices"],
                [
                    {
                        "aliases": [],
                        "character": "Narrator",
                        "references": [],
                        "speaker": "marius",
                        "vntts.source_character": "marius",
                    }
                ],
            )
            self.assertEqual(result.narrator_fallback_roles, ("Hotelier", "Rhiannon"))

    def test_equivalent_display_roles_share_one_narrator_fallback(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            story_path = write_content(root / "content")
            story = load_story_index_document(story_path)
            records = [record.to_record() for record in story.records]
            next(record for record in records if record["line_id"] == "unknown").update(
                speaker='"Hotelier"',
                voice_character='"Hotelier"',
                portrait="disguise",
            )
            write_story_index_document(story_path, story.metadata, records)
            content = inspect_story_index(story_path)
            jobs = PregenerationJobStore(root / "jobs")
            job = jobs.create_or_resume(content, ("selected",))
            plan = VoicePlanStore(jobs).create(job, AppSettings())

            result = PregenerationInputStore(jobs).materialize(job, plan)

        self.assertEqual(result.narrator_fallback_roles, ("Hotelier", "Rhiannon"))

    def test_changed_source_reference_cannot_change_snapshotted_voice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, manifest = self.fixture(root)
            reference = manifest.parent / "references" / "rhiannon.wav"
            write_pcm16_wav(reference, [0.0, 0.8, -0.8, 0.0], 16_000)

            result = PregenerationInputStore(jobs).materialize(job, voice_plan)
            staged_manifest = json.loads(result.voice_manifest.read_text())
            staged_voice = next(
                voice
                for voice in staged_manifest["voices"]
                if voice["character"] == "Rhiannon"
            )
            staged_reference = result.directory / staged_voice["references"][0]
            planned = next(
                group for group in voice_plan.groups if group.character == "Rhiannon"
            )

            self.assertEqual(
                sha256_file(staged_reference), planned.reference_sha256s[0]
            )

    def test_cancelled_materialization_does_not_publish(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)
            cancellation = Event()
            cancellation.set()

            with self.assertRaisesRegex(PregenerationQueueError, "cancelled"):
                PregenerationInputStore(jobs).materialize(
                    job, voice_plan, cancellation=cancellation
                )

            self.assertFalse(
                any((root / "jobs" / job.job_id).glob("generation-input-*"))
            )


if __name__ == "__main__":
    unittest.main()
