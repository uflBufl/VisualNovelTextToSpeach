import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)

import vntts.authoring.explicit_fallback_merge as fallback_merge_module
from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.bulk_generation import authorize_live_fallback
from vntts.authoring.explicit_fallback_merge import (
    merge_explicit_live_fallbacks,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.queue_extension import publish_additive_generation_queue
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_resume_workspace,
    inspect_workspace,
)


class ExplicitFallbackMergeTests(unittest.TestCase):
    def _fixture(self, root):
        fixture, imported, base = create_test_workspace(root)
        base_directory = base.directory
        source = create_resume_workspace(
            imported,
            root / "workspaces",
            story_index=base_directory / "inputs/story-index.jsonl",
            voice_manifest=base_directory / "inputs/voice/manifest.json",
            narrator_character="Rhiannon",
            backend="moss-tts",
            model="model with spaces",
            generation_profile="fallback-source",
        ).directory
        queue_id = fixture["queue_id"]
        source_state = source / "generated-audio/generation-state.json"
        source_document = json.loads(source_state.read_text(encoding="utf-8"))
        removed_source = source_document["items"].pop(queue_id)
        source_document["active"] = None
        (source / "generated-audio" / removed_source["path"]).unlink()
        source_state.write_text(
            json.dumps(source_document, sort_keys=True), encoding="utf-8"
        )
        write_generated_manifest_from_state(
            source_document,
            source / "generated-audio",
            source / "generated-audio/manifest.json",
        )
        authorize_live_fallback(
            source_state,
            source / "queue.jsonl",
            queue_id,
            reason="reference_unavailable_after_audit",
            model="pocket-tts",
        )

        base_state_path = base_directory / "generated-audio/generation-state.json"
        base_state = json.loads(base_state_path.read_text(encoding="utf-8"))
        removed = base_state["items"].pop(queue_id)
        base_state["active"] = None
        audio = base_directory / "generated-audio" / removed["path"]
        audio.unlink()
        base_state_path.write_text(
            json.dumps(base_state, sort_keys=True), encoding="utf-8"
        )
        write_generated_manifest_from_state(
            base_state,
            base_directory / "generated-audio",
            base_directory / "generated-audio/manifest.json",
        )
        return base_directory, source, queue_id

    def _additive_queues(self, root):
        _legacy_base, source, _queue_id = self._fixture(root)
        source_queue = VoiceGenerationQueue.load(source / "queue.jsonl")
        original = source_queue.items[0]
        text = "An additive line."
        line_id = "reverse1999:315401:8"
        text_hash = text_sha256(text)
        added = {
            **original.document,
            "queue_id": expected_voice_generation_queue_id(line_id, text_hash),
            "line_id": line_id,
            "text_sha256": text_hash,
            "text": text,
            "sequence": 8,
            "story_order": 1008,
        }
        extension = write_voice_generation_queue(
            root / "extension.jsonl", source_queue.metadata, [added]
        )
        combined = publish_additive_generation_queue(
            source / "queue.jsonl", extension, root / "combined.jsonl"
        )
        return source_queue, VoiceGenerationQueue.load(combined), source, combined

    def test_exact_fallback_merge_is_valid_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, source, queue_id = self._fixture(root)
            base_before = (base / "generated-audio/generation-state.json").read_bytes()
            source_before = (
                source / "generated-audio/generation-state.json"
            ).read_bytes()

            created = merge_explicit_live_fallbacks(
                base, source, (queue_id,), root / "workspaces"
            )
            repeated = merge_explicit_live_fallbacks(
                base, source, (queue_id,), root / "workspaces"
            )
            summary = inspect_workspace(created.directory)
            state = json.loads(
                (created.directory / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(repeated.directory, created.directory)
            self.assertEqual(summary.live_fallback, 1)
            self.assertEqual(state["items"][queue_id]["status"], "live_fallback")
            self.assertIn("explicit_fallback_merge", state["items"][queue_id])
            self.assertEqual(
                (base / "generated-audio/generation-state.json").read_bytes(),
                base_before,
            )
            self.assertEqual(
                (source / "generated-audio/generation-state.json").read_bytes(),
                source_before,
            )

    def test_rejects_terminal_base_and_tampered_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, source, queue_id = self._fixture(root)
            result = merge_explicit_live_fallbacks(
                base, source, (queue_id,), root / "workspaces"
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "conflicts with base authority"
            ):
                merge_explicit_live_fallbacks(
                    result.directory, source, (queue_id,), root / "workspaces"
                )

            state_path = result.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["live_fallback"]["model"] = "changed"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(AuthoringWorkbenchError):
                inspect_workspace(result.directory)

    def test_additive_source_requires_exact_immutable_base_item_set(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_queue, additive_queue, source, combined = self._additive_queues(root)
            source_sha256 = sha256_file(source / "queue.jsonl")
            additive_sha256 = sha256_file(combined)
            workspace = {
                "queue_extension": {
                    "base_queue_sha256": source_sha256,
                    "queue_sha256": additive_sha256,
                }
            }

            compatible = fallback_merge_module._is_additive_source_queue(
                workspace,
                additive_queue,
                source_queue,
                additive_sha256,
                source_sha256,
            )
            workspace["queue_extension"]["base_queue_sha256"] = "0" * 64
            incompatible = fallback_merge_module._is_additive_source_queue(
                workspace,
                additive_queue,
                source_queue,
                additive_sha256,
                source_sha256,
            )

        self.assertTrue(compatible)
        self.assertFalse(incompatible)


if __name__ == "__main__":
    unittest.main()
