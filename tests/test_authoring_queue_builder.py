import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts import (
    StoryIndexDocument,
    StoryIndexError,
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    write_story_index_document,
)
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.voice_manifest import load_voice_manifest, write_voice_manifest

from vntts.authoring.audio_events import AUDIO_EVENT_PLAN_FIELD
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.queue_builder import (
    GenerationQueueBuildError,
    inspect_generation_queue,
    plan_generation_queue,
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
    write_pcm16_wav(references / "ada.wav", [0.0, 0.1, -0.1, 0.0], 16_000)
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
                {
                    "character": "Narrator",
                    "speaker": "provider-narrator",
                    "references": ["references/ada.wav"],
                },
            ],
        },
    )
    return story_path, manifest_path


class AuthoringQueueBuilderTest(unittest.TestCase):
    def test_inline_audio_event_plan_is_additive_and_canonical(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text = "N-No! *gurgle*"
            story_path, manifest_path = write_inputs(
                root,
                [
                    story_record(
                        "line-1",
                        "absent",
                        text=text,
                        text_sha256=text_sha256(text),
                    )
                ],
            )

            plan = inspect_generation_queue(story_path, manifest_path)
            item = plan.items[0]

        self.assertEqual(item["text"], text)
        self.assertEqual(item["text_sha256"], text_sha256(text))
        self.assertEqual(item[AUDIO_EVENT_PLAN_FIELD]["spoken_text"], "N-No!")
        self.assertEqual(
            item[AUDIO_EVENT_PLAN_FIELD]["events"][0]["kind"], "human-gurgle"
        )
        self.assertEqual(plan.summary.audio_event_composition, 1)
        self.assertEqual(plan.summary.ready, 0)

    def test_source_cannot_spoof_reserved_audio_event_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root,
                [
                    story_record(
                        "line-1",
                        "absent",
                        **{AUDIO_EVENT_PLAN_FIELD: {"spoofed": True}},
                    )
                ],
            )

            with self.assertRaisesRegex(
                GenerationQueueBuildError, "reserved audio-event plan"
            ):
                inspect_generation_queue(story_path, manifest_path)

    def test_exact_unknown_voice_character_is_planned_as_narrator(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root,
                [
                    story_record(
                        "line-1",
                        "absent",
                        speaker="???",
                        voice_character="Ada Alias",
                    )
                ],
            )

            plan = inspect_generation_queue(story_path, manifest_path)

        self.assertEqual(plan.items[0]["speaker"], "???")
        self.assertEqual(plan.items[0]["voice_character"], "Narrator")
        self.assertEqual(plan.summary.ready, 1)
        self.assertEqual(plan.summary.missing_reference, 0)

    def test_collection_preflight_applies_canonical_policy_and_preserves_extensions(
        self,
    ):
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
                "audio_event_composition": 0,
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
        self.assertEqual(
            queue.items[0].document["prompt_adapters"], {"generic": "Speak softly."}
        )
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
            with self.assertRaisesRegex(
                VoiceGenerationQueueError, "explicit unknown_action"
            ):
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
            self.assertEqual(
                VoiceGenerationQueue.load(output).items[0].action, "generate"
            )

    def test_voice_manifest_must_use_current_validated_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            del raw["version"]
            manifest_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(
                GenerationQueueBuildError, "requires version 2"
            ):
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
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True), encoding="utf-8"
                )
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
                self.assertRegex(errors.getvalue(), "POSIX-relative|manifest directory")

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

    def test_direct_typed_plan_is_publishable_and_binds_typed_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            document = StoryIndexDocument.load(story_path)
            _manifest, entries = load_voice_manifest(manifest_path, allow_legacy=False)

            plan = plan_generation_queue(document, entries, manifest_path)
            output = publish_generation_queue(plan, root / "direct-queue.jsonl")
            queue = VoiceGenerationQueue.load(output)

        self.assertEqual(queue.items[0].line_id, "line-1")
        self.assertEqual(plan.summary.ready, 1)
        self.assertEqual(plan.summary.missing_reference, 0)
        self.assertRegex(
            queue.metadata["source_story_index_document_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            queue.metadata["source_voice_manifest_entries_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertRegex(queue.metadata["source_story_index_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            queue.metadata["source_voice_manifest_sha256"], r"^[0-9a-f]{64}$"
        )

    def test_pathless_typed_plan_is_rejected_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            document = StoryIndexDocument.load(story_path)
            _manifest, entries = load_voice_manifest(manifest_path, allow_legacy=False)

            with self.assertRaisesRegex(
                (GenerationQueueBuildError, TypeError), "voice_manifest_path|required"
            ):
                plan_generation_queue(document, entries)

    def test_ready_requires_every_configured_reference_to_exist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["voices"][0]["references"] = [
                "references/missing.wav",
                "references/ada.wav",
            ]
            write_voice_manifest(manifest_path, manifest)

            plan = inspect_generation_queue(story_path, manifest_path)

        self.assertEqual(plan.summary.ready, 0)
        self.assertEqual(plan.summary.missing_reference, 1)

    def test_ready_rejects_existing_non_wav_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story_path, manifest_path = write_inputs(
                root, [story_record("line-1", "absent")]
            )
            reference = root / "references" / "not-a-wave.wav"
            reference.write_bytes(b"not a wave")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["voices"][0]["references"] = ["references/not-a-wave.wav"]
            write_voice_manifest(manifest_path, manifest)

            plan = inspect_generation_queue(story_path, manifest_path)

        self.assertEqual(plan.summary.ready, 0)
        self.assertEqual(plan.summary.missing_reference, 1)


if __name__ == "__main__":
    unittest.main()
