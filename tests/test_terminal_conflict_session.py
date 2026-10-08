"""Coherent terminal review reads and late authority failure boundaries."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.terminal_conflict_review as review_module
from tests.symlink_support import symlink_or_skip
from tests.terminal_conflict_fixtures import (
    create_terminal_conflict_review,
)
from vntts.authoring.authority import AuthoringAuthorityError
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
