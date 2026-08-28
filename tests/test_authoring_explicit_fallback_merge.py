import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.bulk_generation import authorize_live_fallback
from vntts.authoring.explicit_fallback_merge import (
    merge_explicit_live_fallbacks,
)
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
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


if __name__ == "__main__":
    unittest.main()
