import copy
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tests.test_authoring_reconciliation as reconciliation_tests
import tests.test_authoring_terminal_conflict_review as review_tests
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.publication import AtomicPublicationError
from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionError,
    load_terminal_conflict_resolution,
    load_terminal_conflict_resolution_document,
    publish_terminal_conflict_resolution,
    validate_terminal_conflict_resolution_document,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    publish_terminal_conflict_review,
    record_terminal_conflict_decision,
)


class TerminalConflictResolutionTest(unittest.TestCase):
    def create_review(self, root):
        primary, secondary, queue_id, report = (
            review_tests.TerminalConflictReviewTest().create_fixture(root)
        )
        directory = root / "conflict-review"
        published = publish_terminal_conflict_review(report, directory)
        document = json.loads(published.review.read_text(encoding="utf-8"))
        return primary, secondary, queue_id, directory, document

    def source_hashes(self, root):
        return {
            key: value
            for key, value in reconciliation_tests._tree_hashes(root).items()
            if not key.startswith(("conflict-review/", "resolution/"))
        }

    def test_incomplete_review_cannot_publish_resolution(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, review, _document = self.create_review(
                root
            )

            with self.assertRaisesRegex(
                TerminalConflictResolutionError, "progress.*unavailable"
            ):
                publish_terminal_conflict_resolution(review, root / "resolution")

            self.assertFalse((root / "resolution").exists())

    def test_publication_failure_is_reported_as_a_resolution_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, review, document = self.create_review(root)
            case = document["cases"][0]
            record_terminal_conflict_decision(
                review, case["case_id"], case["candidates"][0]["candidate_id"]
            )

            with (
                patch(
                    "vntts.authoring.terminal_conflict_resolution.rename_directory_no_replace",
                    side_effect=AtomicPublicationError("simulated no-replace failure"),
                ),
                self.assertRaisesRegex(
                    TerminalConflictResolutionError, "simulated no-replace failure"
                ),
            ):
                publish_terminal_conflict_resolution(review, root / "resolution")

            self.assertFalse((root / "resolution").exists())

    def test_selected_candidate_is_copied_and_publication_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, queue_id, review, review_document = (
                self.create_review(root)
            )
            case = review_document["cases"][0]
            candidate = case["candidates"][0]
            record_terminal_conflict_decision(
                review,
                case["case_id"],
                candidate["candidate_id"],
            )
            before = self.source_hashes(root)

            created = publish_terminal_conflict_resolution(review, root / "resolution")
            repeated = publish_terminal_conflict_resolution(review, root / "resolution")

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.resolution_id, repeated.resolution_id)
            self.assertEqual(created.case_count, 1)
            self.assertEqual(created.selected_count, 1)
            self.assertEqual(created.neither_count, 0)
            document = load_terminal_conflict_resolution_document(root / "resolution")
            record = document["resolutions"][0]
            self.assertEqual(record["queue_id"], queue_id)
            self.assertEqual(record["decision"], "selected_candidate")
            self.assertEqual(record["selected_candidate_id"], candidate["candidate_id"])
            copied = root / "resolution" / record["selected_audio"]
            self.assertEqual(
                hashlib.sha256(copied.read_bytes()).hexdigest(),
                candidate["audio_sha256"],
            )
            self.assertEqual(before, self.source_hashes(root))

    def test_neither_decision_publishes_no_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, review, review_document = (
                self.create_review(root)
            )
            case = review_document["cases"][0]
            record_terminal_conflict_decision(
                review,
                case["case_id"],
                NEITHER_ACCEPTABLE,
            )

            result = publish_terminal_conflict_resolution(review, root / "resolution")

            document = load_terminal_conflict_resolution_document(root / "resolution")
            self.assertEqual(result.selected_count, 0)
            self.assertEqual(result.neither_count, 1)
            self.assertEqual(document["resolutions"][0]["decision"], NEITHER_ACCEPTABLE)
            self.assertIsNone(document["resolutions"][0]["selected_candidate_id"])
            self.assertFalse((root / "resolution" / "audio").exists())

    def test_selected_audio_tamper_and_directory_symlink_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, review, review_document = (
                self.create_review(root)
            )
            case = review_document["cases"][0]
            record_terminal_conflict_decision(
                review,
                case["case_id"],
                case["candidates"][0]["candidate_id"],
            )
            result = publish_terminal_conflict_resolution(review, root / "resolution")
            document = json.loads(result.resolution.read_text(encoding="utf-8"))
            audio = root / "resolution" / document["resolutions"][0]["selected_audio"]
            audio.write_bytes(b"changed")

            with self.assertRaisesRegex(TerminalConflictResolutionError, "WAV changed"):
                load_terminal_conflict_resolution(root / "resolution")

            alias = root / "resolution-alias"
            alias.symlink_to(root / "resolution", target_is_directory=True)
            with self.assertRaisesRegex(
                TerminalConflictResolutionError, "must not be a symlink"
            ):
                load_terminal_conflict_resolution(alias)

    def test_resolution_schema_binds_selected_identity_and_aware_timestamp(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, review, review_document = (
                self.create_review(root)
            )
            case = review_document["cases"][0]
            record_terminal_conflict_decision(
                review,
                case["case_id"],
                case["candidates"][0]["candidate_id"],
            )
            result = publish_terminal_conflict_resolution(review, root / "resolution")
            document = json.loads(result.resolution.read_text(encoding="utf-8"))

            changed_authority = copy.deepcopy(document)
            record = changed_authority["resolutions"][0]
            record["selected_authority"] = (
                "rejected" if record["selected_authority"] == "approved" else "approved"
            )
            changed_authority["resolution_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in changed_authority.items()
                    if key != "resolution_id"
                }
            )
            with self.assertRaisesRegex(
                TerminalConflictResolutionError, "Selected.*identity changed"
            ):
                validate_terminal_conflict_resolution_document(
                    changed_authority, root / "resolution"
                )

            naive_time = copy.deepcopy(document)
            naive_time["resolutions"][0]["reviewed_at"] = "2026-08-26T12:00:00"
            naive_time["resolution_id"] = canonical_document_sha256(
                {
                    key: value
                    for key, value in naive_time.items()
                    if key != "resolution_id"
                }
            )
            with self.assertRaisesRegex(
                TerminalConflictResolutionError, "requires a timezone"
            ):
                validate_terminal_conflict_resolution_document(
                    naive_time, root / "resolution"
                )

            (root / "resolution" / "unbound.txt").write_text(
                "not part of the immutable resolution",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                TerminalConflictResolutionError, "inventory changed"
            ):
                load_terminal_conflict_resolution(root / "resolution")


if __name__ == "__main__":
    unittest.main()
