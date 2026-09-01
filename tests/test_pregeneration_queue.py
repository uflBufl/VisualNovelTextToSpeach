import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from vntts_artifacts import write_story_index_document
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import load_story_index_document
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.pregeneration_queue import (
    PregenerationInputStore,
    PregenerationQueueError,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoicePlanStore
from vntts.settings import AppSettings
from vntts.source_audio_semantics import (
    SEMANTIC_EVIDENCE_METHOD,
    canonical_document_sha256,
    load_source_audio_semantic_evidence,
    semantic_text_sha256,
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
        entry = {
            "locale": "en",
            "media_id": index,
            "media_sha256": media_sha256,
            "displayed_text_sha256": text_sha256,
            "normalized_displayed_text_sha256": semantic_text_sha256(record["text"]),
            "observed_transcript": record["text"],
            "normalized_observed_text_sha256": semantic_text_sha256(record["text"]),
            "verdict": "full",
            "reason": "exact-normalized-asr-transcript",
            "method": SEMANTIC_EVIDENCE_METHOD,
            "model_sha256": "2" * 64,
            "source_line_ids": [line_id],
        }
        entry["entry_id"] = canonical_document_sha256(
            {key: value for key, value in entry.items() if key != "source_line_ids"}
        )
        record.update(
            source_audio_duration_media_sha256=media_sha256,
            source_audio_completeness="full",
            source_audio_completeness_reason="exact-normalized-asr-transcript",
            source_audio_semantic_evidence_entry_id=entry["entry_id"],
        )
        entries.append(entry)
    evidence = {
        "schema": "r1999.source-audio-semantic-evidence",
        "schema_version": 1,
        "locale": "en",
        "source_story_index_sha256": "3" * 64,
        "model": {
            "kind": "whisper",
            "snapshot": "synthetic",
            "sha256": "2" * 64,
            "device": "cpu",
            "decoding": "deterministic_greedy_default",
        },
        "entries": entries,
    }
    evidence["evidence_id"] = canonical_document_sha256(evidence)
    evidence["generated_at"] = "2026-08-31T00:00:00+00:00"
    for record in records:
        if record.get("source_audio_semantic_evidence_entry_id") is not None:
            record["source_audio_semantic_evidence_id"] = evidence["evidence_id"]
    evidence_path = story_path.parent / "source-audio-semantic-evidence.json"
    atomic_write_json(evidence_path, evidence, sort_keys=True)
    metadata = dict(story.metadata)
    metadata["source_audio_semantics"] = {
        "evidence_id": evidence["evidence_id"],
        "evidence_sha256": sha256_file(evidence_path),
        "method": SEMANTIC_EVIDENCE_METHOD,
        "selected_chapters": ["1", "2"],
        "applied_count": 2,
    }
    write_story_index_document(story_path, metadata, records)
    return story_path


class PregenerationInputStoreTest(unittest.TestCase):
    def fixture(self, root, *, narrator=True, backend="pocket-tts"):
        content = inspect_story_index(write_content(root / "content"))
        jobs = PregenerationJobStore(root / "jobs")
        job = jobs.create_or_resume(content, ("selected",))
        manifest = write_manifest(root / "voices")
        settings = AppSettings(
            speech_backend=backend,
            voice_assignments=({"Narrator": "character:centurion"} if narrator else {}),
        )
        voice_plan = VoicePlanStore(jobs).create(job, settings, manifest_path=manifest)
        return job, jobs, voice_plan, manifest

    def test_materializes_selected_story_effective_voices_and_queue(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)

            result = PregenerationInputStore(jobs).materialize(job, voice_plan)

            self.assertEqual(result.queue_items, 3)
            self.assertEqual(result.ready_items, 3)
            self.assertEqual(result.narrator_fallback_roles, ("Hotelier",))
            queue = VoiceGenerationQueue.load(result.queue)
            self.assertEqual(
                tuple(item.line_id for item in queue.items),
                ("rhiannon", "hotelier", "unknown"),
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

    def test_materializes_distinct_voices_for_same_character_variants(self):
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
            plan = VoicePlanStore(jobs).create(
                job,
                AppSettings(voice_assignments={"Narrator": "character:centurion"}),
                manifest_path=manifest,
            )
            variants = [group for group in plan.groups if group.character == "Rhiannon"]
            self.assertEqual(len(variants), 2)
            centurion_reference = sha256_file(
                manifest.parent / "references" / "centurion.wav"
            )
            replacement = replace(
                variants[1],
                source_id="character:centurion",
                source_character="Centurion",
                source_speaker="centurion-v1",
                reference_sha256s=(centurion_reference,),
            )
            plan = replace(
                plan,
                groups=tuple(
                    replacement if group is variants[1] else group
                    for group in plan.groups
                ),
            )

            result = PregenerationInputStore(jobs).materialize(job, plan)
            queue = VoiceGenerationQueue.load(result.queue)
            routes = {item.line_id: item for item in queue.items}

        self.assertEqual(routes["rhiannon"].speaker, "Aderyn")
        self.assertEqual(routes["rhiannon"].voice_character, "Rhiannon")
        self.assertEqual(routes["not-selected"].speaker, "Rhiannon")
        self.assertTrue(
            routes["not-selected"].voice_character.startswith("Rhiannon__vntts_")
        )

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
                voice_assignments={"Narrator": "character:centurion"},
            )
            plan = VoicePlanStore(jobs).create(
                job,
                settings,
                manifest_path=manifest,
            )

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

    def test_same_identity_resumes_without_rewriting(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, _manifest = self.fixture(root)
            store = PregenerationInputStore(jobs)

            first = store.materialize(job, voice_plan)
            timestamp = first.queue.stat().st_mtime_ns
            second = store.materialize(job, voice_plan)

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
                    }
                ],
            )
            self.assertEqual(result.ready_items, 3)
            self.assertEqual(
                result.narrator_fallback_roles,
                ("Hotelier", "Rhiannon"),
            )

    def test_changed_reference_is_rejected_before_publication(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            job, jobs, voice_plan, manifest = self.fixture(root)
            reference = manifest.parent / "references" / "rhiannon.wav"
            write_pcm16_wav(reference, [0.0, 0.8, -0.8, 0.0], 16_000)

            with self.assertRaisesRegex(PregenerationQueueError, "changed"):
                PregenerationInputStore(jobs).materialize(job, voice_plan)

            self.assertFalse(
                any((root / "jobs" / job.job_id).glob("generation-input-*"))
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
