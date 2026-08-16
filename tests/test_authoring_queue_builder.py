import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import (
    StoryIndexError,
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    write_story_index_document,
)
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.voice_manifest import VoiceManifestError, write_voice_manifest

from vntts.authoring.cli import main as authoring_main
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    publish_generation_queue,
)


def story_metadata():
    return {
        "game": "Synthetic Novel",
        "language": "en",
        "generated_at": "2026-08-16T12:00:00+00:00",
        "collections": [
            {
                "collection_id": "main",
                "title": "Main Story",
                "kind": "story",
                "order": 1,
            },
            {
                "collection_id": "side",
                "title": "Side Story",
                "kind": "story",
                "order": 2,
            },
        ],
    }


def story_record(line_id, status, *, collection="main", **overrides):
    text = f"Exact text for {line_id}."
    record = {
        "record_type": "line",
        "line_id": line_id,
        "chapter": "chapter-one",
        "sequence": int(line_id.rsplit("-", 1)[-1]),
        "speaker": "Ada (memory)",
        "voice_character": "Ada Alias",
        "text": text,
        "text_sha256": text_sha256(text),
        "kind": "dialogue",
        "previous_text": "Previous.",
        "next_text": "Next.",
        "context": {"scene": "observatory"},
        "source_audio_status": status,
        "source_audio_reason": f"fixture_{status}",
        "source_kind": "story",
        "speakable": True,
        "collection_id": collection,
        "emotion": {"primary": "quiet"},
        "prompt_adapters": {"generic": "Speak softly."},
        "producer_extension": {"preserve": True},
    }
    record.update(overrides)
    return record


def write_inputs(root, records):
    story_path = root / "story-index.jsonl"
    write_story_index_document(story_path, story_metadata(), records)
    references = root / "references"
    references.mkdir()
    (references / "ada.wav").write_bytes(b"reference")
    manifest_path = root / "voice-manifest.json"
    write_voice_manifest(
        manifest_path,
        {
            "version": 2,
            "voices": [
                {
                    "character": "Ada",
                    "speaker": "provider-ada",
                    "aliases": ["Ada Alias"],
                    "references": ["references/ada.wav"],
                },
                {
                    "character": "No Local Reference",
                    "speaker": "provider-missing",
                    "references": ["references/missing.wav"],
                },
            ],
        },
    )
    return story_path, manifest_path


class AuthoringQueueBuilderTest(unittest.TestCase):
    def test_collection_preflight_applies_canonical_policy_and_preserves_extensions(self):
        records = [
            story_record("line-1", "absent"),
            story_record("line-2", "available"),
            story_record("line-3", "unavailable"),
            story_record("line-4", "unknown"),
            story_record(
                "line-5",
                "absent",
                voice_character="No Local Reference",
                action="producer-must-not-override",
                queue_id="producer-must-not-override",
            ),
            story_record("line-6", "absent", speakable=False, kind="sound_effect"),
            story_record("line-7", "absent", collection="side"),
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(root, records)
            plan = inspect_generation_queue(
                story_path,
                manifest_path,
                collection_ids=("main",),
                unknown_action="manual_review",
                generated_at="2026-08-16T13:00:00+00:00",
            )
            output = publish_generation_queue(plan, root / "queue.jsonl")
            queue = VoiceGenerationQueue.load(output)
            story_hash = hashlib.sha256(story_path.read_bytes()).hexdigest()
            manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

        self.assertEqual(
            plan.summary.to_dict(),
            {
                "story_records": 7,
                "selected_records": 6,
                "queue_items": 4,
                "character_count": 2,
                "ready": 1,
                "missing_reference": 1,
                "recoverable_source_audio": 1,
                "manual_review": 1,
                "skipped_available": 1,
                "skipped_unspeakable": 1,
                "skipped_unselected": 1,
                "action_counts": {
                    "generate": 2,
                    "manual_review": 1,
                    "prefer_source_audio": 1,
                },
                "source_audio_status_counts": {
                    "absent": 2,
                    "unavailable": 1,
                    "unknown": 1,
                },
                "missing_reference_characters": ["No Local Reference"],
            },
        )
        self.assertEqual(
            [item.line_id for item in queue.items],
            ["line-1", "line-3", "line-4", "line-5"],
        )
        self.assertEqual(queue.items[0].voice_character, "Ada")
        self.assertEqual(queue.items[0].document["emotion"], {"primary": "quiet"})
        self.assertEqual(queue.items[0].document["prompt_adapters"], {"generic": "Speak softly."})
        self.assertEqual(queue.items[0].document["context"], {"scene": "observatory"})
        self.assertEqual(queue.items[-1].action, "generate")
        self.assertEqual(
            queue.items[-1].queue_id,
            f"line-5:{queue.items[-1].text_sha256[:16]}",
        )
        self.assertEqual(queue.metadata["filters"]["collection_ids"], ["main"])
        self.assertEqual(
            queue.metadata["source_story_index_sha256"],
            story_hash,
        )
        self.assertEqual(
            queue.metadata["source_voice_manifest_sha256"],
            manifest_hash,
        )

    def test_unknown_source_audio_requires_an_explicit_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "unknown")]
            )
            with self.assertRaisesRegex(VoiceGenerationQueueError, "explicit unknown_action"):
                inspect_generation_queue(story_path, manifest_path)
            resolved = inspect_generation_queue(
                story_path,
                manifest_path,
                unknown_action="resolve_audio",
            )

        self.assertEqual(resolved.items[0]["action"], "resolve_audio")
        self.assertEqual(resolved.summary.recoverable_source_audio, 1)
        self.assertEqual(resolved.summary.manual_review, 0)

    def test_collection_selection_rejects_unknown_and_empty_collections(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            with self.assertRaisesRegex(
                StoryIndexError, "Unknown story-index collection_id"
            ):
                inspect_generation_queue(
                    story_path, manifest_path, collection_ids=("missing",)
                )
            with self.assertRaisesRegex(GenerationQueueBuildError, "non-empty"):
                inspect_generation_queue(story_path, manifest_path, collection_ids=())

    def test_preflight_is_read_only_and_build_cli_publishes_same_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            output = root / "queue.jsonl"
            preflight_stdout = io.StringIO()
            with redirect_stdout(preflight_stdout):
                result = authoring_main(
                    [
                        "preflight-queue",
                        "--story-index",
                        str(story_path),
                        "--voice-manifest",
                        str(manifest_path),
                        "--collection",
                        "main",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertFalse(output.exists())
            build_stdout = io.StringIO()
            with redirect_stdout(build_stdout):
                result = authoring_main(
                    [
                        "build-queue",
                        "--story-index",
                        str(story_path),
                        "--voice-manifest",
                        str(manifest_path),
                        "--collection",
                        "main",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())
            preflight = json.loads(preflight_stdout.getvalue())
            built = json.loads(build_stdout.getvalue())
            self.assertEqual(preflight["summary"], built["summary"])
            self.assertEqual(built["output"], str(output.resolve()))
            self.assertEqual(VoiceGenerationQueue.load(output).items[0].action, "generate")

    def test_voice_manifest_must_use_current_validated_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            del raw["version"]
            manifest_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(VoiceManifestError, "requires version 2"):
                inspect_generation_queue(story_path, manifest_path)

    def test_unsafe_voice_reference_cannot_escape_preflight_or_publish_output(self):
        for reference in ("../outside.wav", "/absolute.wav", "..\\outside.wav"):
            with self.subTest(reference=reference), TemporaryDirectory() as directory:
                root = Path(directory)
                inputs = root / "inputs"
                inputs.mkdir()
                story_path, manifest_path = write_inputs(
                    inputs, [story_record("line-1", "absent")]
                )
                outside = root / "outside.wav"
                outside.write_bytes(b"outside")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["voices"][0]["references"] = [reference]
                write_voice_manifest(manifest_path, manifest)
                output = root / "queue.jsonl"

                errors = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stderr(errors):
                    authoring_main(
                        [
                            "build-queue",
                            "--story-index",
                            str(story_path),
                            "--voice-manifest",
                            str(manifest_path),
                            "--output",
                            str(output),
                        ]
                    )

                self.assertFalse(output.exists())
                self.assertRegex(
                    errors.getvalue(), "POSIX-relative|manifest directory"
                )

    def test_symlinked_voice_reference_cannot_leave_manifest_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()
            story_path, manifest_path = write_inputs(
                inputs, [story_record("line-1", "absent")]
            )
            outside = root / "outside-reference.wav"
            outside.write_bytes(b"outside")
            linked = inputs / "references" / "linked.wav"
            linked.symlink_to(outside)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["voices"][0]["references"] = ["references/linked.wav"]
            write_voice_manifest(manifest_path, manifest)

            with self.assertRaisesRegex(GenerationQueueBuildError, "leaves"):
                inspect_generation_queue(story_path, manifest_path)


if __name__ == "__main__":
    unittest.main()
