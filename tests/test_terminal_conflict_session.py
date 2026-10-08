"""Coherent terminal review reads and late authority failure boundaries."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.terminal_conflict_review as review_module
import vntts.authoring.terminal_conflict_workspace as workspace_module
from tests.authoring_fixtures import tree_hashes
from tests.symlink_support import symlink_or_skip
from tests.terminal_conflict_fixtures import (
    create_terminal_conflict_fixture,
    create_terminal_conflict_review,
)
from vntts.authoring.authority import AuthoringAuthorityError
from vntts.authoring.bulk_generation import load_generation_state
from vntts.authoring.terminal_conflict_resolution import (
    publish_terminal_conflict_resolution,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    TerminalConflictReviewError,
    carry_terminal_conflict_decisions,
    load_terminal_conflict_review,
    load_terminal_conflict_review_document,
    load_terminal_conflict_review_progress,
    load_terminal_conflict_review_session,
    publish_terminal_conflict_review,
    record_terminal_conflict_decision,
)
from vntts.authoring.terminal_conflict_successor import (
    publish_terminal_conflict_successor,
)
from vntts.authoring.terminal_conflict_workspace import (
    merge_terminal_conflict_resolution,
)
from vntts.authoring.workbench import AuthoringWorkbenchError, load_workspace_authority


class TerminalConflictSessionTest(unittest.TestCase):
    def test_optional_session_and_summary_validate_each_wav_once(self):
        with TemporaryDirectory() as directory:
            review = create_terminal_conflict_review(Path(directory))
            expected = load_terminal_conflict_review_document(review)
            for loader in (
                load_terminal_conflict_review_session,
                load_terminal_conflict_review,
            ):
                with (
                    self.subTest(loader=loader.__name__),
                    patch.object(
                        review_module,
                        "_validate_candidate_audio",
                        wraps=review_module._validate_candidate_audio,
                    ) as validate_wav,
                ):
                    result = loader(review)
                self.assertEqual(validate_wav.call_count, expected["candidate_count"])
                if loader is load_terminal_conflict_review_session:
                    self.assertEqual(result.review, expected)
                    self.assertIsNone(result.progress)
                else:
                    self.assertEqual(result.review_id, expected["review_id"])
                    self.assertEqual(result.completed_count, 0)

    def test_required_progress_and_optional_symlink_fail_closed_document_stays_independent(
        self,
    ):
        with TemporaryDirectory() as directory:
            review = create_terminal_conflict_review(Path(directory))
            expected = load_terminal_conflict_review_document(review)
            with self.assertRaisesRegex(
                TerminalConflictReviewError, "progress.*unavailable"
            ):
                load_terminal_conflict_review_progress(review)
            symlink_or_skip(review / "progress.json", "missing-progress.json")
            with self.assertRaisesRegex(
                TerminalConflictReviewError, "progress.*unavailable"
            ):
                load_terminal_conflict_review_session(review)
            self.assertEqual(load_terminal_conflict_review_document(review), expected)

    def test_review_symlink_is_rejected_by_session_and_summary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review = create_terminal_conflict_review(root)
            original = root / "original-review.json"
            (review / "review.json").rename(original)
            symlink_or_skip(review / "review.json", original)
            for loader in (
                load_terminal_conflict_review_session,
                load_terminal_conflict_review,
            ):
                with (
                    self.subTest(loader=loader.__name__),
                    self.assertRaisesRegex(
                        TerminalConflictReviewError, "review.*unavailable"
                    ),
                ):
                    loader(review)

    def test_review_mutation_between_progress_capture_and_final_cas_is_contextual(self):
        with TemporaryDirectory() as directory:
            review = create_terminal_conflict_review(Path(directory))
            document = load_terminal_conflict_review_document(review)
            record_terminal_conflict_decision(
                review, document["cases"][0]["case_id"], NEITHER_ACCEPTABLE
            )
            progress_payload = (review / "progress.json").read_bytes()
            capture = review_module.capture_authority_file

            def capture_and_mutate(path, label, **options):
                snapshot = capture(path, label, **options)
                if label == "terminal conflict progress":
                    review_path = review / "review.json"
                    review_path.write_bytes(review_path.read_bytes() + b"\n")
                return snapshot

            with patch.object(
                review_module, "capture_authority_file", side_effect=capture_and_mutate
            ):
                with self.assertRaisesRegex(
                    TerminalConflictReviewError, "Terminal conflict review changed"
                ) as rejected:
                    load_terminal_conflict_review_session(review)
            self.assertIsInstance(rejected.exception.__cause__, AuthoringAuthorityError)
            self.assertEqual(str(rejected.exception), str(rejected.exception.__cause__))
            self.assertEqual((review / "progress.json").read_bytes(), progress_payload)

    def test_late_review_and_source_failures_release_write_lease_without_progress(self):
        for label in ("terminal conflict review", "source reconciliation"):
            with self.subTest(label=label), TemporaryDirectory() as directory:
                review = create_terminal_conflict_review(Path(directory))
                document = load_terminal_conflict_review_document(review)
                case_id = document["cases"][0]["case_id"]
                assert_snapshot = review_module.assert_authority_snapshot
                changed = []

                def mutate_before_assert(snapshot, actual_label, **options):
                    if actual_label == label:
                        changed.append(snapshot)
                        snapshot.path.write_bytes(snapshot.payload + b"\n")
                    return assert_snapshot(snapshot, actual_label, **options)

                with patch.object(
                    review_module,
                    "assert_authority_snapshot",
                    side_effect=mutate_before_assert,
                ):
                    with self.assertRaisesRegex(
                        TerminalConflictReviewError, "changed"
                    ) as rejected:
                        record_terminal_conflict_decision(
                            review, case_id, NEITHER_ACCEPTABLE
                        )
                self.assertEqual(len(changed), 1)
                self.assertIsInstance(
                    rejected.exception.__cause__, AuthoringAuthorityError
                )
                self.assertEqual(
                    str(rejected.exception), str(rejected.exception.__cause__)
                )
                self.assertFalse((review / "progress.json").exists())
                self.assertFalse((review / ".progress.lock").exists())
                changed[0].path.write_bytes(changed[0].payload)
                progress = record_terminal_conflict_decision(
                    review, case_id, NEITHER_ACCEPTABLE
                )
                self.assertEqual(
                    progress["decisions"][0]["decision"], NEITHER_ACCEPTABLE
                )

    def test_two_selected_items_share_one_workspace_snapshot_and_keep_late_cas(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, successor, queue_ids = (
                self._create_two_item_approved_successor(root)
            )
            source_state_path = primary / "generated-audio/generation-state.json"
            source_state_payload = source_state_path.read_bytes()
            source_state = json.loads(source_state_payload)
            source_hashes = {
                workspace: tree_hashes(workspace) for workspace in (primary, secondary)
            }

            with patch.object(
                workspace_module,
                "_assert_review_authorities",
                wraps=workspace_module._assert_review_authorities,
            ) as validate_selected:
                result = merge_terminal_conflict_resolution(
                    primary, successor, root / "workspaces"
                )
            self.assertTrue(result.created)
            validate_selected.assert_called_once()
            state_path, authorities, queue_path = validate_selected.call_args.args
            self.assertEqual(state_path, source_state_path)
            self.assertEqual(queue_path, primary / "queue.jsonl")
            self.assertEqual(set(authorities), queue_ids)
            self.assertEqual(
                len({value.item_sha256 for value in authorities.values()}), 2
            )
            self._assert_exact_approved_merge(
                primary, result.directory, queue_ids, source_state
            )
            for workspace, before in source_hashes.items():
                self.assertEqual(tree_hashes(workspace), before)

            self._assert_staging_mutation_rejected(
                primary, successor, root / "late-workspaces", source_state_payload
            )
            for workspace, before in source_hashes.items():
                self.assertEqual(tree_hashes(workspace), before)

    def _create_two_item_approved_successor(self, root):
        primary, secondary, _queue_id, report = create_terminal_conflict_fixture(
            root, item_count=2
        )
        review = root / "conflict-review"
        publish_terminal_conflict_review(report, review)
        document = load_terminal_conflict_review_document(review)
        self.assertEqual(document["case_count"], 2)
        queue_ids = {case["queue_id"] for case in document["cases"]}
        self.assertEqual(len(queue_ids), 2)
        for case in document["cases"]:
            candidate = next(
                candidate
                for candidate in case["candidates"]
                if candidate["authority"] == "approved"
            )
            record_terminal_conflict_decision(
                review, case["case_id"], candidate["candidate_id"]
            )
        resolution = root / "resolution"
        publish_terminal_conflict_resolution(review, resolution)
        successor = root / "successor"
        publish_terminal_conflict_successor(report, resolution, successor)
        return primary, secondary, successor, queue_ids

    def _assert_exact_approved_merge(self, primary, merged, queue_ids, source_state):
        workspace_document = load_workspace_authority(merged)[1]
        self.assertEqual(
            workspace_document["terminal_conflict_merge"]["sources"][0][
                "terminal_item_count"
            ],
            2,
        )
        merged_state = load_generation_state(
            merged / "generated-audio/generation-state.json",
            merged / "queue.jsonl",
        )
        expected_items = source_state["items"]
        self.assertEqual(set(merged_state["items"]), queue_ids)
        expected_state = dict(merged_state)
        expected_state["items"] = {
            queue_id: {
                key: value
                for key, value in item.items()
                if key != "terminal_conflict_resolution"
            }
            for queue_id, item in merged_state["items"].items()
        }
        self.assertEqual(expected_state, source_state)
        manifest = json.loads(
            (merged / "generated-audio/manifest.json").read_text(encoding="utf-8")
        )
        entries = {entry["queue_id"]: entry for entry in manifest["entries"]}
        self.assertEqual(set(entries), queue_ids)
        source_manifest = json.loads(
            (primary / "generated-audio/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [
                {
                    key: value
                    for key, value in entry.items()
                    if key != "terminal_conflict_resolution"
                }
                for entry in manifest["entries"]
            ],
            source_manifest["entries"],
        )
        for queue_id in queue_ids:
            item = merged_state["items"][queue_id]
            self.assertEqual(
                (item["status"], item["review_status"]), ("approved", "approved")
            )
            self.assertEqual(
                entries[queue_id]["terminal_conflict_resolution"],
                item["terminal_conflict_resolution"],
            )
            self.assertEqual(
                (merged / "generated-audio" / item["path"]).read_bytes(),
                (
                    primary / "generated-audio" / expected_items[queue_id]["path"]
                ).read_bytes(),
            )

    def _assert_staging_mutation_rejected(
        self, primary, successor, late_workspaces, source_state_payload
    ):
        source_state_path = primary / "generated-audio/generation-state.json"
        validate_staging = workspace_module.validate_workspace_provenance_extensions
        mutated = False

        def validate_and_mutate(workspace, workspace_document, import_snapshot):
            nonlocal mutated
            validated = validate_staging(workspace, workspace_document, import_snapshot)
            if Path(workspace).name.startswith(".conflict-merge-staging-"):
                source_state_path.write_bytes(source_state_payload + b"\n")
                mutated = True
            return validated

        with patch.object(
            workspace_module,
            "validate_workspace_provenance_extensions",
            side_effect=validate_and_mutate,
        ):
            with self.assertRaisesRegex(AuthoringWorkbenchError, "base changed"):
                merge_terminal_conflict_resolution(primary, successor, late_workspaces)
        self.assertTrue(mutated)
        self.assertEqual(list(late_workspaces.glob("resume-*")), [])
        self.assertEqual(list(late_workspaces.glob(".conflict-merge-staging-*")), [])
        source_state_path.write_bytes(source_state_payload)

    def test_late_carry_failure_releases_target_lease_without_progress(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_terminal_conflict_review(root)
            document = load_terminal_conflict_review_document(source)
            case_id = document["cases"][0]["case_id"]
            record_terminal_conflict_decision(source, case_id, NEITHER_ACCEPTABLE)
            target = root / "target-review"
            publish_terminal_conflict_review(document["source_reconciliation"], target)
            assert_snapshot = review_module.assert_authority_snapshot
            changed = []

            def mutate_before_assert(snapshot, label, **options):
                if label == "target terminal conflict review":
                    changed.append(snapshot)
                    snapshot.path.write_bytes(snapshot.payload + b"\n")
                return assert_snapshot(snapshot, label, **options)

            with patch.object(
                review_module,
                "assert_authority_snapshot",
                side_effect=mutate_before_assert,
            ):
                with self.assertRaisesRegex(
                    TerminalConflictReviewError,
                    "Target terminal conflict review changed",
                ) as rejected:
                    carry_terminal_conflict_decisions(source, target)
            self.assertEqual(len(changed), 1)
            self.assertIsInstance(rejected.exception.__cause__, AuthoringAuthorityError)
            self.assertEqual(str(rejected.exception), str(rejected.exception.__cause__))
            self.assertFalse((target / "progress.json").exists())
            self.assertFalse((target / ".progress.lock").exists())
            changed[0].path.write_bytes(changed[0].payload)
            carried = carry_terminal_conflict_decisions(source, target)
            self.assertEqual(carried["decisions"][0]["case_id"], case_id)


if __name__ == "__main__":
    unittest.main()
