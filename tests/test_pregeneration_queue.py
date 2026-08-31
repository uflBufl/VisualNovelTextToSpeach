import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from vntts_artifacts import write_story_index_document
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.pregeneration_queue import (
    PregenerationInputStore,
    PregenerationQueueError,
)
from vntts.pregeneration_setup import PregenerationJobStore, inspect_story_index
from vntts.pregeneration_voices import VoicePlanStore
from vntts.settings import AppSettings


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
