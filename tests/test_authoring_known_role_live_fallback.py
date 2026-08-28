import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import load_voice_manifest

from tests.test_authoring_missing_voice_live_fallback import (
    create_missing_voice_live_fallback_fixture,
)
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.generation_manifest import write_generated_manifest_from_state
from vntts.authoring.known_role_live_fallback import (
    create_known_role_live_fallback_workspace,
)
from vntts.authoring.known_role_reuse import publish_known_role_reuse_binding
from vntts.authoring.source_reference_bindings import (
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_resume_workspace,
    inspect_workspace,
)
from vntts.generated_audio import _live_fallback_index


class KnownRoleLiveFallbackTests(unittest.TestCase):
    def _fixture(self, root):
        source, unresolved, queue_id = create_missing_voice_live_fallback_fixture(root)
        binding = publish_known_role_reuse_binding(
            source,
            unresolved,
            "Aderyn",
            "Rhiannon",
            root / "known-role",
            accept_known_role_reuse=True,
        ).directory
        imported = next((root / "imports").iterdir())
        arguments = {
            "story_index": source / "inputs/story-index.jsonl",
            "voice_manifest": binding / "manifest.json",
            "narrator_character": "Centurion",
            "backend": "moss-tts",
            "model": "model",
        }
        base = create_resume_workspace(
            imported,
            root / "workspaces",
            generation_profile="base",
            **arguments,
        ).directory
        evidence = create_resume_workspace(
            imported,
            root / "workspaces",
            generation_profile="evidence",
            **arguments,
        ).directory
        queue = VoiceGenerationQueue.load(evidence / "queue.jsonl")
        queue_item = next(item for item in queue.items if item.queue_id == queue_id)
        manifest_path = evidence / "inputs/voice/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(
            manifest,
            queue_ids=(item.queue_id for item in queue.items),
            voices=voices,
        )
        override_sha256 = queue_voice_overrides_sha256(overrides)
        state_path = evidence / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["items"][queue_id] = {
            "status": "failed",
            "attempts": 1,
            "attempts_by_provider": {"moss-tts": 1},
            "provider": "moss-tts",
            "model": "model",
            "generation_profile": "evidence",
            "seed": 0,
            "seed_applied": True,
            "speaker": queue_item.speaker,
            "requested_voice_character": "Aderyn",
            "voice_character": "Rhiannon",
            "failure": {
                "schema_version": 1,
                "kind": "backend_error",
                "error_type": "BoundedBackendError",
                "text_features": {
                    "character_count": len(queue_item.text),
                    "word_count": len(queue_item.text.split()),
                    "comma_count": queue_item.text.count(","),
                    "ellipsis_count": queue_item.text.count("..."),
                    "sentence_boundary_count": 1,
                },
            },
            "synthesis_configuration": {
                "missing_voice_policy": {
                    "schema_version": 1,
                    "mode": "block",
                    "roles": [],
                },
                "synthesis_character_overrides": {},
                "failure_repair_policy": {
                    "schema_version": 1,
                    "segment_pause_ms": 180,
                    "sentence_segment_queue_ids": [],
                    "edge_silence_queue_ids": [],
                },
                "queue_voice_overrides_sha256": override_sha256,
            },
            "source_reference_binding": {
                "schema_version": 1,
                "queue_id": queue_id,
                "source_voice_character": "Aderyn",
                "synthesis_voice_character": "Rhiannon",
                "queue_voice_overrides_sha256": override_sha256,
            },
        }
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_generated_manifest_from_state(
            state,
            evidence / "generated-audio",
            evidence / "generated-audio/manifest.json",
        )
        return base, evidence, queue_id

    def test_exact_routed_fallback_is_valid_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            base_before = (base / "generated-audio/generation-state.json").read_bytes()
            evidence_before = (
                evidence / "generated-audio/generation-state.json"
            ).read_bytes()

            first = create_known_role_live_fallback_workspace(
                base, ((queue_id, evidence),), root / "workspaces"
            )
            second = create_known_role_live_fallback_workspace(
                base, ((queue_id, evidence),), root / "workspaces"
            )
            summary = inspect_workspace(first.directory)
            state = json.loads(
                (first.directory / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )
            item = state["items"][queue_id]
            decision = item["live_fallback"]
            runtime = _live_fallback_index(
                {
                    "vntts.authoring.live_fallback": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": [
                            {
                                **decision,
                                "decision_sha256": canonical_document_sha256(decision),
                            }
                        ],
                    }
                }
            )

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(first.directory, second.directory)
            self.assertEqual(summary.live_fallback, 1)
            self.assertEqual(item["speaker"], "Aderyn")
            self.assertEqual(item["requested_voice_character"], "Aderyn")
            self.assertEqual(item["voice_character"], "Rhiannon")
            self.assertEqual(decision["schema_version"], 5)
            self.assertEqual(decision["requested_voice_character"], "Rhiannon")
            self.assertEqual(
                next(iter(runtime.values())).requested_voice_character, "Rhiannon"
            )
            self.assertEqual(
                (base / "generated-audio/generation-state.json").read_bytes(),
                base_before,
            )
            self.assertEqual(
                (evidence / "generated-audio/generation-state.json").read_bytes(),
                evidence_before,
            )

    def test_rejects_stale_base_and_tampered_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            result = create_known_role_live_fallback_workspace(
                base, ((queue_id, evidence),), root / "workspaces"
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "base item is not absent"
            ):
                create_known_role_live_fallback_workspace(
                    result.directory,
                    ((queue_id, evidence),),
                    root / "workspaces",
                )

            state_path = result.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["voice_character"] = "Centurion"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(AuthoringWorkbenchError):
                inspect_workspace(result.directory)


if __name__ == "__main__":
    unittest.main()
