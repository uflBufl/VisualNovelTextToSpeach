import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tests.test_authoring_terminal_conflict_review as review_tests
import vntts.authoring.terminal_conflict_successor as successor_module
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.terminal_conflict_resolution import (
    publish_terminal_conflict_resolution,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    publish_terminal_conflict_review,
    record_terminal_conflict_decision,
)
from vntts.authoring.terminal_conflict_successor import (
    APPLY_APPROVED_OUTCOME,
    NEW_REPAIR_HYPOTHESIS,
    RETAIN_EXPLICIT_REJECTION,
    TerminalConflictSuccessorError,
    load_terminal_conflict_successor,
    load_terminal_conflict_successor_document,
    publish_terminal_conflict_successor,
    validate_terminal_conflict_successor_document,
)


class TerminalConflictSuccessorTest(unittest.TestCase):
    def create_resolution(self, root, authority):
        _primary, _secondary, queue_id, report_path = (
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
        return queue_id, report_path, resolution_root

    def test_all_decisions_project_exact_actions_and_retain_history(self):
        expectations = {
            "approved": APPLY_APPROVED_OUTCOME,
            "rejected": RETAIN_EXPLICIT_REJECTION,
            None: NEW_REPAIR_HYPOTHESIS,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (authority, expected_action) in enumerate(
                expectations.items(), start=1
            ):
                case_root = root / str(index)
                case_root.mkdir()
                queue_id, report_path, resolution_root = self.create_resolution(
                    case_root, authority
                )
                original_report = json.loads(report_path.read_text(encoding="utf-8"))
                output = case_root / "successor"

                created = publish_terminal_conflict_successor(
                    report_path, resolution_root, output
                )
                repeated = publish_terminal_conflict_successor(
                    report_path, resolution_root, output
                )

                self.assertTrue(created.created)
                self.assertFalse(repeated.created)
                self.assertEqual(created.successor_id, repeated.successor_id)
                document = load_terminal_conflict_successor_document(output)
                self.assertEqual(document["summary"]["resolved_conflict_count"], 1)
                self.assertEqual(
                    document["summary"]["action_counts"], {expected_action: 1}
                )
                record = document["resolved_terminal_conflicts"][0]
                self.assertEqual(record["queue_id"], queue_id)
                self.assertEqual(record["next_action"], expected_action)
                self.assertEqual(
                    record["historical_conflict"],
                    original_report["terminal_conflicts"][0],
                )
                self.assertEqual(
                    json.loads(report_path.read_text(encoding="utf-8")),
                    original_report,
                )

    def test_resolution_from_another_report_is_rejected_before_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            _queue_id, first_report, _first_resolution = self.create_resolution(
                first, "approved"
            )
            _queue_id, _second_report, second_resolution = self.create_resolution(
                second, "approved"
            )
            output = root / "successor"

            with self.assertRaisesRegex(
                TerminalConflictSuccessorError, "another reconciliation"
            ):
                publish_terminal_conflict_successor(
                    first_report, second_resolution, output
                )

            self.assertFalse(output.exists())

    def test_changed_source_progress_blocks_successor_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _queue_id, report_path, resolution_root = self.create_resolution(
                root, "approved"
            )
            resolution = json.loads(
                (resolution_root / "resolution.json").read_text(encoding="utf-8")
            )
            progress = Path(resolution["source_progress"])
            progress.write_bytes(progress.read_bytes() + b"\n")

            with self.assertRaisesRegex(
                TerminalConflictSuccessorError, "sources changed"
            ):
                publish_terminal_conflict_successor(
                    report_path, resolution_root, root / "successor"
                )

            self.assertFalse((root / "successor").exists())

    def test_selected_resolution_audio_tamper_blocks_successor_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _queue_id, report_path, resolution_root = self.create_resolution(
                root, "approved"
            )
            resolution = json.loads(
                (resolution_root / "resolution.json").read_text(encoding="utf-8")
            )
            audio = resolution_root / resolution["resolutions"][0]["selected_audio"]
            audio.write_bytes(b"changed")

            with self.assertRaisesRegex(TerminalConflictSuccessorError, "WAV changed"):
                publish_terminal_conflict_successor(
                    report_path, resolution_root, root / "successor"
                )

            self.assertFalse((root / "successor").exists())

    def test_source_mutation_after_staging_blocks_atomic_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _queue_id, report_path, resolution_root = self.create_resolution(
                root, "approved"
            )
            original_loader = successor_module.load_terminal_conflict_successor
            mutated = False

            def load_and_mutate(staging):
                nonlocal mutated
                result = original_loader(staging)
                if not mutated:
                    report_path.write_bytes(report_path.read_bytes() + b"\n")
                    mutated = True
                return result

            with patch.object(
                successor_module,
                "load_terminal_conflict_successor",
                side_effect=load_and_mutate,
            ):
                with self.assertRaisesRegex(
                    TerminalConflictSuccessorError,
                    "Source authoring reconciliation changed",
                ):
                    publish_terminal_conflict_successor(
                        report_path, resolution_root, root / "successor"
                    )

            self.assertFalse((root / "successor").exists())
            self.assertEqual(list(root.glob(".successor.staging-*")), [])

    def test_successor_schema_and_exact_inventory_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _queue_id, report_path, resolution_root = self.create_resolution(
                root, "approved"
            )
            output = root / "successor"
            publish_terminal_conflict_successor(report_path, resolution_root, output)
            document = json.loads(
                (output / "successor.json").read_text(encoding="utf-8")
            )

            changed = copy.deepcopy(document)
            changed["resolved_terminal_conflicts"][0]["next_action"] = (
                NEW_REPAIR_HYPOTHESIS
            )
            changed["successor_id"] = canonical_document_sha256(
                {key: value for key, value in changed.items() if key != "successor_id"}
            )
            with self.assertRaisesRegex(
                TerminalConflictSuccessorError, "action changed"
            ):
                validate_terminal_conflict_successor_document(changed, output)

            forged_authority = copy.deepcopy(document)
            resolution = forged_authority["resolved_terminal_conflicts"][0][
                "resolution"
            ]
            resolution["selected_authority"] = "newer_workspace"
            forged_authority["successor_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in forged_authority.items()
                    if key != "successor_id"
                }
            )
            with self.assertRaisesRegex(
                TerminalConflictSuccessorError, "authority is invalid"
            ):
                validate_terminal_conflict_successor_document(forged_authority, output)

            (output / "unexpected.txt").write_text("unbound", encoding="utf-8")
            with self.assertRaisesRegex(
                TerminalConflictSuccessorError, "inventory changed"
            ):
                load_terminal_conflict_successor(output)


if __name__ == "__main__":
    unittest.main()
