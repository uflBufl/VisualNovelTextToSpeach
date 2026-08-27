import hashlib
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

import vntts.authoring.config_rebase as config_rebase_module
from tests.test_authoring_workbench import (
    create_carry_source_workspace,
    write_carry_target_manifest,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    publish_generated_manifest,
    review_generation_item,
)
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.config_rebase import (
    _prior_config_rebase_target_route,
    _target_route_status,
    rebase_workspace_config,
    validate_config_rebase_workspace,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    create_resume_workspace,
    load_workspace_authority,
)


def _tree_hashes(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".guard")
    }


def _prepare(root, *, target_reference_payloads=None, source_queue_override=None):
    fixture, imported, source = create_carry_source_workspace(
        root, queue_voice_override=source_queue_override
    )
    review_generation_item(
        source.directory / "generated-audio" / "generation-state.json",
        fixture["queue_id"],
        "approved",
    )
    target_manifest = write_carry_target_manifest(
        root,
        rhiannon_payloads=target_reference_payloads,
    )
    target = create_resume_workspace(
        imported,
        root / "workspaces",
        story_index=fixture["job"]["story_index"],
        voice_manifest=target_manifest,
        backend="moss-tts",
        model="model with spaces",
        generation_profile="stable",
        narrator_character="Rhiannon",
    )
    return fixture, source.directory, target.directory


class AuthoringConfigRebaseTest(unittest.TestCase):
    def test_chained_rebase_uses_the_immediate_predecessor_target_route(self):
        result = {
            "voice_character": "Historical voice absent from current manifest",
            "config_rebase": {
                "target_effective_character": "Current selected route",
                "target_reference_sha256s": ["1" * 64, "2" * 64],
            },
        }

        self.assertEqual(
            _prior_config_rebase_target_route(result),
            ("Current selected route", ("1" * 64, "2" * 64)),
        )

    def test_chained_rebase_rejects_malformed_predecessor_target_route(self):
        with self.assertRaisesRegex(
            AuthoringWorkbenchError, "target reference SHA-256"
        ):
            _prior_config_rebase_target_route(
                {
                    "config_rebase": {
                        "target_effective_character": "Current selected route",
                        "target_reference_sha256s": ["not-a-digest"],
                    }
                }
            )

    def test_retired_route_preserves_only_exact_rejection(self):
        queue_id = "line:child"
        source_route = ("Child variant", ("1" * 64,))
        target_route = ("Aderyn", ("2" * 64,))
        retirement = (
            {
                "variant_id": "child-variant",
                "source_reference_plan_sha256": "3" * 64,
                "voice_character": "Child variant",
                "reference_sha256": "1" * 64,
                "queue_ids": [queue_id],
                "reason": "real_story_quality_failure",
            },
        )

        self.assertEqual(
            _target_route_status(
                queue_id,
                "generated",
                "rejected",
                source_route,
                target_route,
                retirement,
            ),
            "retired_rejected",
        )
        with self.assertRaisesRegex(
            AuthoringWorkbenchError, "changes the effective reference"
        ):
            _target_route_status(
                queue_id,
                "approved",
                "approved",
                source_route,
                target_route,
                retirement,
            )
        self.assertEqual(
            _target_route_status(
                queue_id,
                "generated",
                "rejected",
                source_route,
                ("Aderyn", ()),
                retirement,
            ),
            "retired_rejected",
        )

    def test_allows_distinct_variant_labels_bound_to_same_reference_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, source, target = _prepare(
                root,
                source_queue_override="Source reference Rhiannon exact-variant",
            )

            result = rebase_workspace_config(source, target, root / "workspaces")
            state = load_generation_state(
                result.directory / "generated-audio" / "generation-state.json",
                result.directory / "queue.jsonl",
            )
            authority = state["items"][fixture["queue_id"]]["config_rebase"]
            self.assertEqual(
                authority["source_effective_character"],
                "Source reference Rhiannon exact-variant",
            )
            self.assertEqual(authority["target_effective_character"], "Rhiannon")
            self.assertTrue(
                set(authority["source_reference_sha256s"]).issubset(
                    authority["target_reference_sha256s"]
                )
            )

    def test_cli_publishes_config_rebase_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, source, target = _prepare(root)
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "rebase-workspace-config",
                        str(source),
                        str(target),
                        "--workspaces-root",
                        str(root / "workspaces"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["created"])
            self.assertTrue(Path(result["directory"]).is_dir())

    def test_rebases_exact_terminal_item_without_mutating_authorities(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, source, target = _prepare(root)
            source_before = _tree_hashes(source)
            target_before = _tree_hashes(target)

            result = rebase_workspace_config(source, target, root / "workspaces")
            repeated = rebase_workspace_config(source, target, root / "workspaces")

            self.assertTrue(result.created)
            self.assertFalse(repeated.created)
            self.assertEqual(repeated.directory, result.directory)
            self.assertEqual(_tree_hashes(source), source_before)
            self.assertEqual(_tree_hashes(target), target_before)
            _loaded_directory, workspace, _workspace_sha256 = load_workspace_authority(
                result.directory
            )
            state = load_generation_state(
                result.directory / "generated-audio" / "generation-state.json",
                result.directory / "queue.jsonl",
            )
            item = state["items"][fixture["queue_id"]]
            self.assertEqual(item["review_status"], "approved")
            self.assertEqual(item["status"], "approved")
            self.assertEqual(
                item["config_rebase"]["source_effective_character"], "Rhiannon"
            )
            self.assertEqual(
                item["config_rebase"]["target_effective_character"], "Rhiannon"
            )
            self.assertEqual(len(workspace["config_rebase"]["items"]), 1)
            self.assertEqual(
                sha256_file(result.directory / "generated-audio" / item["path"]),
                item["file_sha256"],
            )
            manifest = publish_generated_manifest(
                result.directory / "generated-audio" / "generation-state.json"
            )
            manifest_document = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest_document["entry_count"], 1)

    def test_validation_allows_only_ledger_bound_later_terminal_overlay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, source, target = _prepare(root)
            result = rebase_workspace_config(source, target, root / "workspaces")
            workspace = json.loads(
                (result.directory / "workspace.json").read_text(encoding="utf-8")
            )
            state = load_generation_state(
                result.directory / "generated-audio/generation-state.json",
                result.directory / "queue.jsonl",
            )
            queue_id = fixture["queue_id"]
            extension = {
                "source_workspace_id": result.directory.name,
                "source_state_sha256": "1" * 64,
                "source_item_sha256": "2" * 64,
                "audio_sha256": state["items"][queue_id]["file_sha256"],
                "status": "approved",
                "review_status": "approved",
                "selected_candidate_id": "3" * 64,
                "next_action": "apply_selected_approved_outcome",
            }
            state["items"][queue_id]["terminal_conflict_resolution"] = extension

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "item projection changed"
            ):
                validate_config_rebase_workspace(result.directory, workspace, state)

            workspace["terminal_conflict_merge"] = {
                "items": [{"queue_id": queue_id, **extension}]
            }
            validate_config_rebase_workspace(result.directory, workspace, state)
            state["items"][queue_id].pop("config_rebase")
            validate_config_rebase_workspace(result.directory, workspace, state)
            state["items"][queue_id].pop("terminal_conflict_resolution")
            workspace.pop("terminal_conflict_merge")
            state["items"][queue_id]["outcome_merge"] = extension
            workspace["outcome_merge"] = {
                "items": [{"queue_id": queue_id, **extension}]
            }
            validate_config_rebase_workspace(result.directory, workspace, state)
            state["items"][queue_id]["config_rebase"] = {"changed": True}
            with self.assertRaisesRegex(AuthoringWorkbenchError, "state item changed"):
                validate_config_rebase_workspace(result.directory, workspace, state)

    def test_rejects_changed_reference_bytes_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, source, target = _prepare(
                root,
                target_reference_payloads=(b"different-one", b"different-two"),
            )

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "omits source reference bytes"
            ):
                rebase_workspace_config(source, target, root / "workspaces")

            self.assertFalse(
                any(
                    path.name.startswith("resume-")
                    and path.resolve() not in {source.resolve(), target.resolve()}
                    for path in (root / "workspaces").iterdir()
                )
            )

    def test_rejects_source_wav_mutation_during_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, source, target = _prepare(root)
            state = load_generation_state(
                source / "generated-audio" / "generation-state.json",
                source / "queue.jsonl",
            )
            wav = (
                source / "generated-audio" / state["items"][fixture["queue_id"]]["path"]
            )
            original_writer = config_rebase_module._write_generated_manifest_from_state

            def mutate_after_staging(*args, **kwargs):
                result = original_writer(*args, **kwargs)
                wav.write_bytes(wav.read_bytes() + b"changed")
                return result

            try:
                with patch.object(
                    config_rebase_module,
                    "_write_generated_manifest_from_state",
                    side_effect=mutate_after_staging,
                ):
                    with self.assertRaisesRegex(
                        AuthoringWorkbenchError, "source changed during publication"
                    ):
                        rebase_workspace_config(source, target, root / "workspaces")
            finally:
                wav.write_bytes(wav.read_bytes()[: -len(b"changed")])

    def test_orphaned_rebase_provenance_cannot_enter_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, source, _target = _prepare(root)
            state_path = source / "generated-audio" / "generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            item = state["items"][fixture["queue_id"]]
            item["config_rebase"] = {
                "source_item_sha256": "1" * 64,
                "audio_sha256": item["file_sha256"],
            }
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                BulkGenerationError, "canonical workspace ledger"
            ):
                publish_generated_manifest(state_path)

    def test_tampered_snapshot_invalidates_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, source, target = _prepare(root)
            result = rebase_workspace_config(source, target, root / "workspaces")
            snapshot = (
                result.directory
                / "provenance"
                / "config-rebase"
                / "source-root"
                / "workspace.json"
            )
            snapshot.write_bytes(snapshot.read_bytes() + b"\n")

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "source workspace snapshot changed"
            ):
                load_workspace_authority(result.directory)


if __name__ == "__main__":
    unittest.main()
