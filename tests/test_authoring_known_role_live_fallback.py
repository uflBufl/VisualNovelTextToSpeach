import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import load_voice_manifest

import vntts.authoring.known_role_live_fallback as fallback_module
import vntts.authoring.workspace_state as workspace_state_module
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

    def test_public_validation_uses_one_generation_state_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            created = create_known_role_live_fallback_workspace(
                base, ((queue_id, evidence),), root / "workspaces"
            ).directory
            workspace = json.loads((created / "workspace.json").read_text())
            with (
                patch.object(
                    fallback_module,
                    "load_generation_state",
                    wraps=fallback_module.load_generation_state,
                ) as plain,
                patch.object(
                    workspace_state_module,
                    "validate_generation_state_document",
                    wraps=workspace_state_module.validate_generation_state_document,
                ) as validation,
            ):
                fallback_module.validate_known_role_live_fallback_workspace(
                    created, workspace
                )
            plain.assert_not_called()
            validation.assert_called_once()

    def test_public_validation_rejects_noninteger_batch_versions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            created = create_known_role_live_fallback_workspace(
                base, ((queue_id, evidence),), root / "workspaces"
            ).directory
            original = json.loads((created / "workspace.json").read_text())
            state_path = created / "generated-audio/generation-state.json"
            state_before = state_path.read_bytes()
            for version in (True, 1.0):
                with self.subTest(version=version):
                    workspace = json.loads(json.dumps(original))
                    batch = workspace["known_role_live_fallback"]
                    batch["schema_version"] = version
                    batch["batch_id"] = canonical_document_sha256(
                        {
                            key: value
                            for key, value in batch.items()
                            if key != "batch_id"
                        }
                    )
                    state = json.loads(state_before)
                    state["items"][queue_id]["live_fallback"]["evidence"][
                        "batch_id"
                    ] = batch["batch_id"]
                    state_path.write_text(json.dumps(state))
                    with self.assertRaisesRegex(
                        AuthoringWorkbenchError, "batch is malformed"
                    ):
                        fallback_module.validate_known_role_live_fallback_workspace(
                            created, workspace
                        )
                    self.assertEqual(json.loads(state_path.read_text()), state)
            state_path.write_bytes(state_before)
            fallback_module.validate_known_role_live_fallback_workspace(
                created, original
            )

    def test_ledger_map_validation_grows_linearly(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            created = create_known_role_live_fallback_workspace(
                base, ((queue_id, evidence),), root / "workspaces"
            ).directory
            original_batch = json.loads((created / "workspace.json").read_text())[
                "known_role_live_fallback"
            ]
            original_state = json.loads(
                (created / "generated-audio/generation-state.json").read_text()
            )
            visits = {}
            for count in (1, 8):
                batch = copy.deepcopy(original_batch)
                batch["items"] = []
                state = {"items": {}}
                overrides = {}
                for index in range(count):
                    key = f"queue-{index}"
                    ledger = copy.deepcopy(original_batch["items"][0])
                    ledger["queue_id"] = key
                    result = copy.deepcopy(original_state["items"][queue_id])
                    result["live_fallback"]["evidence"]["queue_id"] = key
                    batch["items"].append(ledger)
                    state["items"][key] = result
                    overrides[key] = batch["synthesis_character"]
                visited = []
                original_items = fallback_module._generation_state_items

                def count_items(value):
                    items = original_items(value)
                    visited.append(len(items))
                    return items

                with patch.object(
                    fallback_module, "_generation_state_items", side_effect=count_items
                ):
                    fallback_module._validate_known_role_ledgers(
                        batch, state, overrides, overrides
                    )
                visits[count] = sum(visited)
            self.assertGreater(visits[1], 0)
            self.assertLessEqual(visits[8], 8 * visits[1])

    def test_evidence_capture_is_reused_only_within_one_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            original_ledger = fallback_module._known_role_evidence_ledger
            original_load = fallback_module.load_workspace_authority

            def replay_ledger(*args, **kwargs):
                first = original_ledger(*args, **kwargs)
                self.assertEqual(original_ledger(*args, **kwargs), first)
                return first

            loads = []

            def count_load(path):
                if Path(path).resolve() == evidence:
                    loads.append(path)
                return original_load(path)

            with (
                patch.object(
                    fallback_module,
                    "_known_role_evidence_ledger",
                    side_effect=replay_ledger,
                ),
                patch.object(
                    fallback_module, "load_workspace_authority", side_effect=count_load
                ),
            ):
                first = create_known_role_live_fallback_workspace(
                    base, ((queue_id, evidence),), root / "workspaces"
                )
                self.assertEqual(len(loads), 1)
                second = create_known_role_live_fallback_workspace(
                    base, ((queue_id, evidence),), root / "workspaces"
                )
                self.assertEqual(len(loads), 2)
            self.assertEqual(first.directory, second.directory)
            self.assertFalse(second.created)

    def test_changed_captured_evidence_refuses_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, evidence, queue_id = self._fixture(root)
            workspaces = root / "workspaces"
            directories_before = set(workspaces.iterdir())
            base_state = base / "generated-audio/generation-state.json"
            base_before = base_state.read_bytes()
            evidence_state = evidence / "generated-audio/generation-state.json"
            changed_evidence = evidence_state.read_bytes() + b"\n"
            original_validate = fallback_module._validate_staged_known_role_fallback

            def change_evidence_after_staging(*args, **kwargs):
                original_validate(*args, **kwargs)
                evidence_state.write_bytes(changed_evidence)

            with (
                patch.object(
                    fallback_module,
                    "_validate_staged_known_role_fallback",
                    side_effect=change_evidence_after_staging,
                ),
                self.assertRaisesRegex(
                    AuthoringWorkbenchError, "authority changed before publication"
                ),
            ):
                create_known_role_live_fallback_workspace(
                    base, ((queue_id, evidence),), workspaces
                )
            self.assertEqual(set(workspaces.iterdir()), directories_before)
            self.assertEqual(base_state.read_bytes(), base_before)
            self.assertEqual(evidence_state.read_bytes(), changed_evidence)

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
