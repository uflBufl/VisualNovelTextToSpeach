import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tests.test_authoring_reconciliation as reconciliation_tests
import tests.test_authoring_terminal_conflict_review as review_tests
import vntts.authoring.terminal_conflict_workspace as terminal_workspace_module
import vntts.authoring.workbench as workbench_module
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    _GenerationLease,
    load_generation_state,
    process_is_alive,
    publish_generated_manifest,
    validate_terminal_conflict_publication_authority,
)
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import _load_bound_review_workspace
from vntts.authoring.reconciliation import build_authoring_reconciliation
from vntts.authoring.terminal_conflict_resolution import (
    publish_terminal_conflict_resolution,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    publish_terminal_conflict_review,
    record_terminal_conflict_decision,
)
from vntts.authoring.terminal_conflict_successor import (
    publish_terminal_conflict_successor,
)
from vntts.authoring.terminal_conflict_workspace import (
    merge_terminal_conflict_resolution,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    inspect_workspace,
    load_workspace_authority,
)


class TerminalConflictWorkspaceTest(unittest.TestCase):
    def create_successor(self, root, authority):
        primary, secondary, queue_id, report_path = (
            review_tests.TerminalConflictReviewTest().create_fixture(root)
        )
        review_root = root / "conflict-review"
        review = publish_terminal_conflict_review(report_path, review_root)
        review_document = json.loads(review.review.read_text(encoding="utf-8"))
        case = review_document["cases"][0]
        if authority is None:
            decision = NEITHER_ACCEPTABLE
        else:
            decision = next(
                candidate["candidate_id"]
                for candidate in case["candidates"]
                if candidate["authority"] == authority
            )
        record_terminal_conflict_decision(review_root, case["case_id"], decision)
        resolution_root = root / "resolution"
        publish_terminal_conflict_resolution(review_root, resolution_root)
        successor_root = root / "successor"
        publish_terminal_conflict_successor(
            report_path, resolution_root, successor_root
        )
        return primary, secondary, queue_id, report_path, review_root, successor_root

    def source_hashes(self, root):
        return {
            key: value
            for key, value in reconciliation_tests._tree_hashes(root).items()
            if not key.startswith("workspaces/")
        }

    def test_approved_and_rejected_choices_create_distinct_exact_workspaces(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, authority in enumerate(("approved", "rejected"), start=1):
                case_root = root / str(index)
                case_root.mkdir()
                primary, _secondary, queue_id, _report, _review, successor = (
                    self.create_successor(case_root, authority)
                )
                before = self.source_hashes(case_root)
                workspaces = case_root / "workspaces"

                created = merge_terminal_conflict_resolution(
                    primary, successor, workspaces
                )
                repeated = merge_terminal_conflict_resolution(
                    primary, successor, workspaces
                )

                self.assertTrue(created.created)
                self.assertFalse(repeated.created)
                self.assertEqual(created.directory, repeated.directory)
                document = load_workspace_authority(created.directory)[1]
                self.assertEqual(
                    document["terminal_conflict_merge"]["terminal_successor_id"],
                    json.loads(
                        (successor / "successor.json").read_text(encoding="utf-8")
                    )["successor_id"],
                )
                state = load_generation_state(
                    created.directory / "generated-audio/generation-state.json",
                    created.directory / "queue.jsonl",
                )
                item = state["items"][queue_id]
                expected = (
                    ("approved", "approved")
                    if authority == "approved"
                    else ("generated", "rejected")
                )
                self.assertEqual((item["status"], item["review_status"]), expected)
                self.assertIn("terminal_conflict_resolution", item)
                manifest = json.loads(
                    (created.directory / "generated-audio/manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                manifest_ids = {entry["queue_id"] for entry in manifest["entries"]}
                self.assertEqual(queue_id in manifest_ids, authority == "approved")
                if authority == "approved":
                    manifest_item = next(
                        entry
                        for entry in manifest["entries"]
                        if entry["queue_id"] == queue_id
                    )
                    self.assertEqual(
                        manifest_item["terminal_conflict_resolution"],
                        item["terminal_conflict_resolution"],
                    )
                self.assertEqual(before, self.source_hashes(case_root))
                self.assertEqual(
                    inspect_workspace(created.directory).approved,
                    int(authority == "approved"),
                )
                refreshed = build_authoring_reconciliation(
                    created.directory, case_root / "review-bundles"
                )
                self.assertEqual(refreshed.document["terminal_conflicts"], [])

    def test_neither_choice_cannot_create_a_publishable_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, _review, successor = (
                self.create_successor(root, None)
            )
            workspaces = root / "workspaces"
            before = set(workspaces.glob("resume-*"))

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "requires a new repair hypothesis"
            ):
                merge_terminal_conflict_resolution(primary, successor, workspaces)

            self.assertEqual(set(workspaces.glob("resume-*")), before)

    def test_valid_merged_workspace_passes_canonical_publication_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, _review, successor = (
                self.create_successor(root, "approved")
            )
            merged = merge_terminal_conflict_resolution(
                primary, successor, root / "workspaces"
            ).directory
            state_path = merged / "generated-audio/generation-state.json"
            state = load_generation_state(state_path, merged / "queue.jsonl")

            with _GenerationLease(
                state_path.parent,
                state["queue_sha256"],
                process_checker=process_is_alive,
            ):
                validate_terminal_conflict_publication_authority(state_path, state)

    def test_self_consistent_successor_source_substitution_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, _review, successor = (
                self.create_successor(root, "approved")
            )
            path = successor / "successor.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["source_report_id"] = "f" * 64
            document["successor_id"] = canonical_document_sha256(
                {key: value for key, value in document.items() if key != "successor_id"}
            )
            path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            workspaces = root / "workspaces"
            before = set(workspaces.glob("resume-*"))

            with self.assertRaisesRegex(AuthoringWorkbenchError, "different reports"):
                merge_terminal_conflict_resolution(primary, successor, workspaces)

            self.assertEqual(set(workspaces.glob("resume-*")), before)

    def test_source_change_during_staging_removes_partial_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, review, successor = (
                self.create_successor(root, "rejected")
            )
            review_document = json.loads(
                (review / "review.json").read_text(encoding="utf-8")
            )
            candidate = next(
                item
                for item in review_document["cases"][0]["candidates"]
                if item["authority"] == "rejected"
            )
            state_path = Path(candidate["source_authorities"][0]["state"])
            original_validator = (
                terminal_workspace_module.validate_workspace_provenance_extensions
            )
            mutated = False

            def validate_and_mutate(workspace, document, import_snapshot):
                nonlocal mutated
                result = original_validator(workspace, document, import_snapshot)
                if not mutated and Path(workspace).name.startswith(
                    ".conflict-merge-staging-"
                ):
                    state_path.write_bytes(state_path.read_bytes() + b"\n")
                    mutated = True
                return result

            workspaces = root / "workspaces"
            before = set(workspaces.glob("resume-*"))
            with patch.object(
                terminal_workspace_module,
                "validate_workspace_provenance_extensions",
                side_effect=validate_and_mutate,
            ):
                with self.assertRaisesRegex(AuthoringWorkbenchError, "source changed"):
                    merge_terminal_conflict_resolution(primary, successor, workspaces)

            self.assertEqual(set(workspaces.glob("resume-*")), before)
            self.assertEqual(
                list(workspaces.glob(".conflict-merge-staging-*"))
                if workspaces.exists()
                else [],
                [],
            )

    def test_workspace_terminal_ledger_tamper_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, _review, successor = (
                self.create_successor(root, "approved")
            )
            result = merge_terminal_conflict_resolution(
                primary, successor, root / "workspaces"
            )
            workspace_path = result.directory / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["terminal_conflict_merge"]["items"][0]["next_action"] = (
                "retain_explicit_rejection"
            )
            workspace["config_fingerprint"] = (
                workbench_module._workspace_config_fingerprint(
                    workspace["source"]["import_id"],
                    workspace["story_index"],
                    workspace["voice_manifest"],
                    workspace["narrator_character"],
                    workspace["run_config"],
                    workspace.get("carry_forward"),
                    workspace.get("outcome_merge"),
                    workspace.get("failure_reference_binding"),
                    workspace["terminal_conflict_merge"],
                )
            )
            workspace_path.write_text(
                json.dumps(workspace, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaises(AuthoringWorkbenchError):
                load_workspace_authority(result.directory)

    def test_merged_workspace_is_accepted_by_cohort_authority_loader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, _review, successor = (
                self.create_successor(root, "approved")
            )
            result = merge_terminal_conflict_resolution(
                primary, successor, root / "workspaces"
            )
            workspace = load_workspace_authority(result.directory)[1]

            loaded = _load_bound_review_workspace(
                result.directory,
                {
                    "workspace_id": workspace["workspace_id"],
                    "workspace_config_fingerprint": workspace["config_fingerprint"],
                    "queue_sha256": hashlib.sha256(
                        (result.directory / "queue.jsonl").read_bytes()
                    ).hexdigest(),
                    "state_sha256": hashlib.sha256(
                        (
                            result.directory / "generated-audio/generation-state.json"
                        ).read_bytes()
                    ).hexdigest(),
                },
            )

            self.assertEqual(loaded[1]["workspace_id"], workspace["workspace_id"])

    def test_orphaned_terminal_provenance_cannot_enter_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, queue_id, _report, _review, successor = (
                self.create_successor(root, "approved")
            )
            result = merge_terminal_conflict_resolution(
                primary, successor, root / "workspaces"
            )
            merged_state = load_generation_state(
                result.directory / "generated-audio/generation-state.json"
            )
            state_path = primary / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["terminal_conflict_resolution"] = merged_state[
                "items"
            ][queue_id]["terminal_conflict_resolution"]
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            load_generation_state(state_path, primary / "queue.jsonl")
            with self.assertRaisesRegex(
                BulkGenerationError, "merge ledger is missing or malformed"
            ):
                publish_generated_manifest(state_path)

    def test_malformed_terminal_provenance_is_rejected_by_state_loader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, queue_id, _report = (
                review_tests.TerminalConflictReviewTest().create_fixture(root)
            )
            state_path = primary / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["terminal_conflict_resolution"] = {"spoofed": True}
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                BulkGenerationError, "state-item provenance is malformed"
            ):
                load_generation_state(state_path, primary / "queue.jsonl")

    def test_source_generation_is_locked_through_final_workspace_rename(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, _report, _review, successor = (
                self.create_successor(root, "approved")
            )
            original_rename = terminal_workspace_module.rename_directory_no_replace
            observed = []

            def assert_source_locked(staging, destination):
                state = load_generation_state(
                    primary / "generated-audio/generation-state.json"
                )
                with self.assertRaisesRegex(
                    BulkGenerationError, "Another generation process is active"
                ):
                    with _GenerationLease(
                        primary / "generated-audio",
                        state["queue_sha256"],
                        process_checker=process_is_alive,
                    ):
                        pass
                observed.append(True)
                return original_rename(staging, destination)

            with patch.object(
                terminal_workspace_module,
                "rename_directory_no_replace",
                side_effect=assert_source_locked,
            ):
                result = merge_terminal_conflict_resolution(
                    primary, successor, root / "workspaces"
                )

            self.assertTrue(result.created)
            self.assertEqual(observed, [True])

    def test_supported_cli_applies_terminal_conflict_pipeline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, report, review, _successor = (
                self.create_successor(root, "approved")
            )
            resolution = root / "cli-resolution"
            successor = root / "cli-successor"
            workspaces = root / "cli-workspaces"
            output = io.StringIO()

            with redirect_stdout(output):
                self.assertEqual(
                    authoring_main(
                        [
                            "terminal-conflict-resolution",
                            str(review),
                            str(resolution),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    authoring_main(
                        [
                            "terminal-conflict-successor",
                            str(report),
                            str(resolution),
                            str(successor),
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    authoring_main(
                        [
                            "terminal-conflict-merge",
                            str(primary),
                            str(successor),
                            "--workspaces-root",
                            str(workspaces),
                        ]
                    ),
                    0,
                )

            payloads = [
                json.loads(chunk)
                for chunk in output.getvalue().replace("}\n{", "}\0{").split("\0")
            ]
            self.assertTrue(payloads[0]["created"])
            self.assertTrue(payloads[1]["created"])
            self.assertTrue(payloads[2]["created"])


if __name__ == "__main__":
    unittest.main()
