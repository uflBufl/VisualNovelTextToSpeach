import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.workbench as workbench_module
from tests.test_authoring_bulk_generation import SyntheticRenderer
from tests.test_authoring_failure_reference_audit import FailureReferenceAuditTest
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import (
    apply_cohort_review_decision,
    build_cohort_review_decision,
    build_cohort_review_plan,
)
from vntts.authoring.failure_reference_audit import (
    publish_failure_reference_audit,
    record_failure_reference_decision,
)
from vntts.authoring.failure_reference_binding import (
    FailureReferenceBindingError,
    load_failure_reference_binding,
    load_failure_reference_binding_document,
    publish_failure_reference_binding,
)
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_failure_reference_workspace,
    create_resume_workspace,
    failure_reference_runtime_binding,
    generation_command,
    generation_control_bindings,
    inspect_generation_readiness,
    inspect_workspace,
    review_workspace_item,
)


class FailureReferenceBindingTest(unittest.TestCase):
    def create_decided_audit(self, root):
        fixture = FailureReferenceAuditTest()
        workspace, queue_id = fixture.create_failed_workspace(root)
        state_path = workspace / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text())
        state["active"] = None
        state_path.write_text(json.dumps(state, sort_keys=True))
        audit = root / "audit"
        publish_failure_reference_audit(workspace, audit, seed=7)
        document = json.loads((audit / "audit.json").read_text())
        group = document["groups"][0]
        candidate = group["candidates"][0]
        decisions = record_failure_reference_decision(
            audit, group["group_id"], candidate["candidate_id"]
        )
        return audit, workspace, queue_id, group, candidate, decisions

    def test_publish_is_self_contained_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, queue_id, group, candidate, decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            source_before = {
                path.relative_to(audit).as_posix(): path.read_bytes()
                for path in audit.rglob("*")
                if path.is_file()
            }

            created = publish_failure_reference_binding(audit, output)
            repeated = publish_failure_reference_binding(audit, output)
            loaded = load_failure_reference_binding(output)
            document = load_failure_reference_binding_document(output)

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.binding_id, repeated.binding_id)
            self.assertEqual(created.binding_id, loaded.binding_id)
            self.assertEqual(created.decision_set_id, decisions["decision_set_id"])
            self.assertEqual(created.case_count, 1)
            selected = document["groups"][0]
            self.assertEqual(selected["candidate_id"], candidate["candidate_id"])
            self.assertEqual(selected["reference_sha256"], candidate["sha256"])
            self.assertEqual(
                document["queue_voice_overrides"][queue_id],
                selected["voice_character"],
            )
            self.assertEqual(
                (output / selected["reference"]).read_bytes(),
                (audit / candidate["audio"]).read_bytes(),
            )
            self.assertEqual(
                source_before,
                {
                    path.relative_to(audit).as_posix(): path.read_bytes()
                    for path in audit.rglob("*")
                    if path.is_file()
                },
            )
            self.assertEqual(selected["cases"][0]["queue_id"], queue_id)
            self.assertEqual(selected["group_id"], group["group_id"])

    def test_incomplete_and_neither_decisions_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = FailureReferenceAuditTest().create_failed_workspace(
                root
            )
            audit = root / "audit"
            publish_failure_reference_audit(workspace, audit)
            with self.assertRaisesRegex(
                FailureReferenceBindingError, "terminal decision"
            ):
                publish_failure_reference_binding(audit, root / "missing")

            document = json.loads((audit / "audit.json").read_text())
            record_failure_reference_decision(
                audit, document["groups"][0]["group_id"], "neither_acceptable"
            )
            with self.assertRaisesRegex(FailureReferenceBindingError, "rejected group"):
                publish_failure_reference_binding(audit, root / "rejected")

    def test_tampered_selected_reference_and_binding_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, _queue_id, _group, candidate, _decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            selected = load_failure_reference_binding_document(output)["groups"][0]
            (output / selected["reference"]).write_bytes(b"changed")
            with self.assertRaisesRegex(FailureReferenceBindingError, "changed"):
                load_failure_reference_binding(output)

            (audit / candidate["audio"]).write_bytes(b"changed")
            with self.assertRaisesRegex(
                FailureReferenceBindingError,
                "audio changed|Selected reference authority",
            ):
                publish_failure_reference_binding(audit, root / "tampered-audit")

    def test_binding_rejects_symlinked_selected_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, _workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            output = root / "binding"
            publish_failure_reference_binding(audit, output)
            selected = load_failure_reference_binding_document(output)["groups"][0]
            reference = output / selected["reference"]
            payload = reference.read_bytes()
            replacement = root / "same-bytes.wav"
            replacement.write_bytes(payload)
            reference.unlink()
            reference.symlink_to(replacement)

            with self.assertRaisesRegex(FailureReferenceBindingError, "unsafe"):
                load_failure_reference_binding(output)

    def test_successor_preserves_state_and_adds_exact_runtime_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            state_path = workspace / "generated-audio/generation-state.json"
            state_before = state_path.read_bytes()
            audio_before = {
                path.relative_to(
                    workspace / "generated-audio"
                ).as_posix(): path.read_bytes()
                for path in (workspace / "generated-audio").rglob("*")
                if path.is_file()
            }

            created = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            )
            repeated = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            )
            summary = inspect_workspace(created.directory)
            runtime = failure_reference_runtime_binding(created.directory)
            readiness = inspect_generation_readiness(
                created.directory,
                queue_ids=(queue_id,),
            )

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.directory, repeated.directory)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(readiness.queue_ids, (queue_id,))
            self.assertEqual(readiness.ready, 1)
            self.assertEqual(readiness.missing_voice, 0)
            self.assertIsNotNone(runtime)
            self.assertEqual(set(runtime.queue_voice_overrides), {queue_id})
            self.assertEqual(len(runtime.voices), 1)
            self.assertEqual(
                (
                    created.directory / "generated-audio/generation-state.json"
                ).read_bytes(),
                state_before,
            )
            self.assertEqual(
                {
                    path.relative_to(
                        created.directory / "generated-audio"
                    ).as_posix(): path.read_bytes()
                    for path in (created.directory / "generated-audio").rglob("*")
                    if path.is_file()
                },
                audio_before,
            )

    def test_successor_rejects_stale_base_and_tampered_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            next(iter(state["items"].values()))["attempts"] += 1
            state_path.write_text(json.dumps(state, sort_keys=True))
            with self.assertRaisesRegex(AuthoringWorkbenchError, "authority is stale"):
                create_failure_reference_workspace(
                    workspace,
                    binding,
                    root / "stale-successors",
                )

            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root / "second")
            )
            binding = root / "second-binding"
            publish_failure_reference_binding(audit, binding)
            created = create_failure_reference_workspace(
                workspace,
                binding,
                root / "tamper-successors",
            )
            runtime = failure_reference_runtime_binding(created.directory)
            next(
                path for path in runtime.controls if path.name != "binding.json"
            ).write_bytes(b"changed")
            with self.assertRaisesRegex(AuthoringWorkbenchError, "modified|changed"):
                inspect_workspace(created.directory)

    def test_successor_rejects_base_mutation_during_snapshot_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            state_path = workspace / "generated-audio/generation-state.json"
            original_copy = workbench_module._copy_workspace_tree_snapshot

            def copy_then_mutate(source, target, snapshots):
                original_copy(source, target, snapshots)
                if Path(source).resolve() == (workspace / "generated-audio").resolve():
                    document = json.loads(state_path.read_text())
                    document["active"] = {"phase": "changed-during-copy"}
                    state_path.write_text(json.dumps(document, sort_keys=True))

            with patch.object(
                workbench_module,
                "_copy_workspace_tree_snapshot",
                copy_then_mutate,
            ):
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError,
                    "source changed before workspace publication",
                ):
                    create_failure_reference_workspace(
                        workspace,
                        binding,
                        root / "successors",
                    )
            self.assertFalse(any((root / "successors").glob("resume-*")))

    def test_successor_rejects_binding_mutation_during_snapshot_copy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, _queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            binding_document = load_failure_reference_binding_document(binding)
            reference = binding / binding_document["groups"][0]["reference"]
            original_copy = workbench_module._copy_workspace_tree_snapshot

            def copy_then_mutate(source, target, snapshots):
                original_copy(source, target, snapshots)
                if Path(source).resolve() == binding.resolve():
                    reference.write_bytes(b"changed-during-copy")

            with patch.object(
                workbench_module,
                "_copy_workspace_tree_snapshot",
                copy_then_mutate,
            ):
                with self.assertRaisesRegex(
                    AuthoringWorkbenchError,
                    "source changed before workspace publication",
                ):
                    create_failure_reference_workspace(
                        workspace,
                        binding,
                        root / "successors",
                    )
            self.assertFalse(any((root / "successors").glob("resume-*")))

    def test_child_uses_only_the_bound_synthetic_voice_and_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            created = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            )
            command = generation_command(
                created.directory,
                queue_ids=(queue_id,),
                retries=0,
                seed=0,
            )
            renderers = []

            def create_backend(_name, registry, *_args, **options):
                renderer = SyntheticRenderer()
                renderer.name = "moss-tts"
                renderer.model_name = options["model_name"]
                renderer.registry = registry
                renderers.append(renderer)
                return renderer

            with patch("vntts.authoring.cli.create_backend", create_backend):
                self.assertEqual(authoring_main(command[3:]), 0)

            runtime = failure_reference_runtime_binding(created.directory)
            state = json.loads(
                (
                    created.directory / "generated-audio/generation-state.json"
                ).read_text()
            )
            result = state["items"][queue_id]
            self.assertEqual(len(renderers), 1)
            self.assertEqual(len(renderers[0].requests), 1)
            self.assertEqual(
                renderers[0].requests[0].voice,
                runtime.queue_voice_overrides[queue_id],
            )
            self.assertEqual(result["voice_character"], renderers[0].requests[0].voice)
            bindings = generation_control_bindings(
                created.directory,
                queue=created.directory / "queue.jsonl",
                output=created.directory / "generated-audio",
                voice_manifest=created.directory / "inputs/voice/manifest.json",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character=json.loads(
                    (created.directory / "workspace.json").read_text()
                )["narrator_character"],
            )
            self.assertIn(runtime.directory / "binding.json", bindings)
            self.assertEqual(
                result["source_reference_binding"]["synthesis_voice_character"],
                renderers[0].requests[0].voice,
            )

            plan = build_cohort_review_plan(
                created.directory,
                queue_ids=(queue_id,),
            )
            cohort = plan.document["cohorts"][0]
            decision = build_cohort_review_decision(
                plan,
                cohort["cohort_id"],
                "accepted",
                reviewed_queue_ids=[queue_id],
            )
            projection = apply_cohort_review_decision(
                created.directory,
                plan,
                decision,
            )
            reviewed_state = json.loads(
                (
                    created.directory / "generated-audio/generation-state.json"
                ).read_text()
            )

            self.assertEqual(projection.review_status, "approved")
            self.assertEqual(reviewed_state["items"][queue_id]["status"], "approved")

    def test_same_backend_repair_successor_preserves_the_exact_overlay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audit, workspace, queue_id, _group, _candidate, _decisions = (
                self.create_decided_audit(root)
            )
            binding = root / "binding"
            publish_failure_reference_binding(audit, binding)
            successor = create_failure_reference_workspace(
                workspace,
                binding,
                root / "successors",
            ).directory
            runtime = failure_reference_runtime_binding(successor)
            state_path = successor / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            failed = state["items"][queue_id]
            text_features = failed["failure"]["text_features"]
            failed.update(
                {
                    "attempts": 2,
                    "seed": 1,
                    "last_error": (
                        "MOSS generation hit the text-length audio limit before EOS"
                    ),
                    "provider": "moss-tts",
                    "model": "model with spaces",
                    "generation_profile": "stable",
                    "voice_character": runtime.queue_voice_overrides[queue_id],
                    "failure": {
                        "schema_version": 1,
                        "kind": "missed_eos_audio_limit",
                        "error_type": "SynthesisLimitedError",
                        "text_features": text_features,
                        "completion": "limited",
                    },
                }
            )
            state_path.write_text(json.dumps(state, sort_keys=True))
            imported = next((root / "imports").glob("legacy-*"))
            imported_state_path = imported / "generated-audio/generation-state.json"
            imported_state = json.loads(imported_state_path.read_text())
            imported_state["active"] = None
            imported_state_path.write_text(json.dumps(imported_state, sort_keys=True))
            import_path = imported / "import.json"
            import_document = json.loads(import_path.read_text())
            imported_state_sha256 = workbench_module.sha256_file(imported_state_path)
            next(
                item
                for item in import_document["artifacts"]
                if item["path"] == "generated-audio/generation-state.json"
            )["sha256"] = imported_state_sha256
            import_path.write_text(json.dumps(import_document, sort_keys=True))

            repair = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=successor / "inputs/story-index.jsonl",
                voice_manifest=successor / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                failure_repair_policy=FailureRepairPolicy(
                    bounded_seed_retry_queue_ids=(queue_id,)
                ),
                carry_forward_from=successor,
            )
            repeated = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=successor / "inputs/story-index.jsonl",
                voice_manifest=successor / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                failure_repair_policy=FailureRepairPolicy(
                    bounded_seed_retry_queue_ids=(queue_id,)
                ),
                carry_forward_from=successor,
            )

            repair_runtime = failure_reference_runtime_binding(repair.directory)
            repair_state = json.loads(
                (repair.directory / "generated-audio/generation-state.json").read_text()
            )
            readiness = inspect_generation_readiness(
                repair.directory,
                queue_ids=(queue_id,),
            )
            self.assertTrue(repair.created)
            self.assertFalse(repeated.created)
            self.assertEqual(repair.directory, repeated.directory)
            self.assertEqual(repair_runtime.document, runtime.document)
            self.assertEqual(
                repair_state["items"][queue_id]["voice_character"],
                runtime.queue_voice_overrides[queue_id],
            )
            self.assertEqual(readiness.selected, 1)
            self.assertEqual(readiness.ready, 1)
            self.assertEqual(readiness.missing_voice, 0)
            inspect_workspace(repair.directory)

            def create_backend(_name, registry, *_args, **options):
                renderer = SyntheticRenderer()
                renderer.name = "moss-tts"
                renderer.model_name = options["model_name"]
                renderer.registry = registry
                return renderer

            repair_command = generation_command(
                repair.directory,
                queue_ids=(queue_id,),
                retries=0,
                seed=0,
            )
            with patch("vntts.authoring.cli.create_backend", create_backend):
                self.assertEqual(authoring_main(repair_command[3:]), 0)
            review_workspace_item(repair.directory, queue_id, "approved")
            reviewed_state = json.loads(
                (repair.directory / "generated-audio/generation-state.json").read_text()
            )
            self.assertEqual(reviewed_state["items"][queue_id]["status"], "approved")
            inspect_workspace(repair.directory)

            state = json.loads(state_path.read_text())
            state["items"][queue_id]["attempts"] = 3
            state["items"][queue_id]["seed"] = 2
            state_path.write_text(json.dumps(state, sort_keys=True))
            fallback = create_resume_workspace(
                imported,
                root / "fallbacks",
                story_index=successor / "inputs/story-index.jsonl",
                voice_manifest=successor / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                failure_repair_policy=FailureRepairPolicy(
                    offline_fallback_queue_ids=(queue_id,)
                ),
                carry_forward_from=successor,
            )
            fallback_runtime = failure_reference_runtime_binding(fallback.directory)
            fallback_readiness = inspect_generation_readiness(
                fallback.directory,
                queue_ids=(queue_id,),
            )
            self.assertEqual(fallback_runtime.document, runtime.document)
            self.assertEqual(fallback_readiness.selected, 1)
            self.assertEqual(fallback_readiness.ready, 1)
            self.assertEqual(fallback_readiness.missing_voice, 0)
            inspect_workspace(fallback.directory)


if __name__ == "__main__":
    unittest.main()
