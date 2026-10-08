import hashlib
import json
import unittest
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

from tests.story_fixtures import semantic_evidence_document, semantic_evidence_entry
from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.pregeneration_queue import (
    PregenerationInputStore,
    PregenerationQueueError,
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
            input_path = result.directory / "input.json"
            original = json.loads(input_path.read_text(encoding="utf-8"))
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
                    atomic_write_json(input_path, {**original, field: value})
                    with self.assertRaisesRegex(PregenerationQueueError, error):
                        PregenerationInputStore(jobs).materialize(job, plan)
            atomic_write_json(input_path, original)
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
