"""Raw JSON admission at the immutable terminal-conflict boundaries."""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.terminal_conflict_fixtures import create_terminal_conflict_review
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.terminal_conflict_records import (
    TerminalConflictRecordError,
    is_terminal_review_outcome,
    validate_terminal_conflict_state_binding,
)
from vntts.authoring.terminal_conflict_resolution import (
    TerminalConflictResolutionError,
    publish_terminal_conflict_resolution,
    validate_terminal_conflict_resolution_document,
)
from vntts.authoring.terminal_conflict_review import (
    TerminalConflictReviewError,
    record_terminal_conflict_decision,
    validate_terminal_conflict_review_document,
    validate_terminal_conflict_review_progress_document,
)
from vntts.authoring.terminal_conflict_successor import (
    TerminalConflictSuccessorError,
    publish_terminal_conflict_successor,
    validate_terminal_conflict_successor_document,
)
from vntts.json_types import has_schema_version


def identify(document, field):
    document[field] = canonical_document_sha256(
        {key: value for key, value in document.items() if key != field}
    )


class TerminalConflictDocumentAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        review_root = create_terminal_conflict_review(root)
        review = json.loads((review_root / "review.json").read_text())
        case = review["cases"][0]
        record_terminal_conflict_decision(
            review_root, case["case_id"], case["candidates"][0]["candidate_id"]
        )
        progress = json.loads((review_root / "progress.json").read_text())
        resolution = publish_terminal_conflict_resolution(
            review_root, root / "resolution"
        )
        resolved = json.loads(resolution.resolution.read_text())
        successor = publish_terminal_conflict_successor(
            review["source_reconciliation"], root / "resolution", root / "successor"
        )
        following = json.loads(successor.successor.read_text())
        boundaries = (
            (
                review,
                "review_id",
                review_root,
                validate_terminal_conflict_review_document,
                TerminalConflictReviewError,
                ("schema_version", "case_count", "candidate_count"),
            ),
            (
                resolved,
                "resolution_id",
                root / "resolution",
                validate_terminal_conflict_resolution_document,
                TerminalConflictResolutionError,
                ("schema_version", "case_count", "selected_count", "neither_count"),
            ),
            (
                following,
                "successor_id",
                root / "successor",
                validate_terminal_conflict_successor_document,
                TerminalConflictSuccessorError,
                ("schema_version",),
            ),
        )
        self.boundaries = boundaries
        self.review = review
        self.progress = progress

    def test_versions_and_counts_are_exact_integers(self):
        boundaries = self.boundaries
        for document, identity, path, validate, error_type, fields in boundaries:
            original = copy.deepcopy(document)
            self.assertEqual(validate(document, path), original)
            for field in fields:
                for invalid in (float(document[field]), bool(document[field])):
                    with self.subTest(boundary=identity, field=field, invalid=invalid):
                        forged = copy.deepcopy(document)
                        forged[field] = invalid
                        identify(forged, identity)
                        with self.assertRaises(error_type):
                            validate(forged, path)
            self.assertEqual(document, original)

    def test_raw_candidate_ids_and_tags_raise_domain_errors(self):
        boundaries = self.boundaries
        for document, identity, path, validate, error_type, rows in (
            (*boundaries[1][:5], "resolutions"),
            (*boundaries[2][:5], "resolved_terminal_conflicts"),
        ):
            for invalid in ([{}, []], [None, None], [1, 2], ["a" * 64, "a" * 64]):
                with self.subTest(boundary=identity, candidate_ids=invalid):
                    forged = copy.deepcopy(document)
                    record = forged[rows][0]
                    if rows == "resolved_terminal_conflicts":
                        record = record["resolution"]
                    record["candidate_ids"] = invalid
                    identify(forged, identity)
                    with self.assertRaises(error_type):
                        validate(forged, path)
        for document, identity, path, validate, error_type, location in (
            (*boundaries[1][:5], ("resolutions", 0, "selected_authority")),
            (*boundaries[2][:5], ("resolved_terminal_conflicts", 0, "next_action")),
            (
                *boundaries[2][:5],
                (
                    "resolved_terminal_conflicts",
                    0,
                    "resolution",
                    "selected_authority",
                ),
            ),
            (
                *boundaries[2][:5],
                (
                    "resolved_terminal_conflicts",
                    0,
                    "historical_conflict",
                    "occurrences",
                    0,
                    "authority",
                ),
            ),
        ):
            for invalid in ([], {}, 7):
                with self.subTest(boundary=identity, field=location, invalid=invalid):
                    forged = copy.deepcopy(document)
                    owner = forged
                    for component in location[:-1]:
                        owner = owner[component]
                    owner[location[-1]] = invalid
                    identify(forged, identity)
                    with self.assertRaises(error_type):
                        validate(forged, path)

    def test_progress_and_audio_metadata_admission(self):
        boundaries = self.boundaries
        review = self.review
        progress = self.progress
        review_root = boundaries[0][2]
        for field in ("case_id", "decision"):
            for invalid in ([], {}, 7):
                with self.subTest(progress=field, invalid=invalid):
                    forged = copy.deepcopy(progress)
                    forged["decisions"][0][field] = invalid
                    with self.assertRaises(TerminalConflictReviewError):
                        validate_terminal_conflict_review_progress_document(
                            forged, review
                        )
        for field in ("sample_rate", "sample_count", "authority"):
            forged = copy.deepcopy(review)
            value = forged["cases"][0]["candidates"][0][field]
            forged["cases"][0]["candidates"][0][field] = (
                [] if field == "authority" else float(value)
            )
            identify(forged, "review_id")
            with (
                self.subTest(candidate=field),
                self.assertRaises(TerminalConflictReviewError),
            ):
                validate_terminal_conflict_review_document(forged, review_root)

    def test_successor_summary_is_integer_valued(self):
        boundaries = self.boundaries
        following = boundaries[2][0]
        root = boundaries[2][2].parent
        for field in following["summary"]:
            forged = copy.deepcopy(following)
            if field == "action_counts":
                action = next(iter(forged["summary"][field]))
                forged["summary"][field][action] = 1.0
            else:
                forged["summary"][field] = float(forged["summary"][field])
            identify(forged, "successor_id")
            with (
                self.subTest(summary=field),
                self.assertRaises(TerminalConflictSuccessorError),
            ):
                validate_terminal_conflict_successor_document(
                    forged, root / "successor"
                )


class TerminalConflictAdmissionTest(unittest.TestCase):
    def test_merge_versions_and_raw_outcome_predicate(self):
        document = {
            "schema": "vntts.authoring-terminal-conflict-workspace-merge",
            "schema_version": 1,
            "items": [],
        }
        self.assertEqual(
            validate_terminal_conflict_state_binding({"items": {}}, document), document
        )
        for value in (True, 1.0, "1", None, {}, []):
            with self.subTest(version=value):
                forged = document | {"schema_version": value}
                self.assertFalse(has_schema_version(forged, 1))
                with self.assertRaises(TerminalConflictRecordError):
                    validate_terminal_conflict_state_binding({"items": {}}, forged)
            self.assertFalse(
                is_terminal_review_outcome(
                    {"status": value, "review_status": "approved"}
                )
            )
        self.assertTrue(has_schema_version(document, 1))
        self.assertTrue(
            is_terminal_review_outcome(
                {"status": "approved", "review_status": "approved"}
            )
        )
        self.assertTrue(
            is_terminal_review_outcome(
                {"status": "generated", "review_status": "rejected"}
            )
        )
