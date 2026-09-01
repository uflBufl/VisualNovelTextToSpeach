import hashlib
import json
import socket
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

import tests.test_authoring_reconciliation as reconciliation_tests
from tests.symlink_support import symlink_or_skip
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import inspect_generated_wav
from vntts.authoring.cohort_bundle import (
    build_cohort_review_bundle,
    write_cohort_review_bundle,
)
from vntts.authoring.publication import AtomicPublicationError
from vntts.authoring.reconciliation import (
    build_authoring_reconciliation,
    write_authoring_reconciliation,
)
from vntts.authoring.terminal_conflict_review import (
    NEITHER_ACCEPTABLE,
    PROGRESS_LEASE_SCHEMA,
    PROGRESS_LEASE_VERSION,
    TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION,
    TerminalConflictReviewError,
    carry_approved_cohort_terminal_conflict_decisions,
    carry_terminal_conflict_decisions,
    load_terminal_conflict_review,
    load_terminal_conflict_review_progress,
    publish_terminal_conflict_review,
    record_terminal_conflict_decision,
    validate_terminal_conflict_review_document,
)


class TerminalConflictReviewTest(unittest.TestCase):
    def create_fixture(self, root):
        helper = reconciliation_tests.AuthoringReconciliationTest()
        primary, secondary, queue_id, bundles, publication = (
            helper.create_parallel_fixture(root)
        )
        publication.unlink()
        state_path = secondary / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        result = state["items"][queue_id]
        audio = secondary / "generated-audio" / result["path"]
        samples = np.linspace(-0.25, 0.25, 4_000, dtype=np.float32)
        write_pcm16_wav(audio, samples, 16_000)
        result["file_sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
        result["quality"] = asdict(inspect_generated_wav(audio))
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_cohort_review_bundle(
            build_cohort_review_bundle((primary, secondary)), publication
        )
        helper.decide_parallel_bundle(
            publication,
            ((primary.name, "accepted"), (secondary.name, "rejected")),
        )
        report = build_authoring_reconciliation(primary, bundles)
        report_path = root / "reconciliation.json"
        write_authoring_reconciliation(report, report_path)
        return primary, secondary, queue_id, report_path

    def test_publish_collapses_only_identical_authorities_and_records_neither(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, queue_id, report_path = self.create_fixture(root)
            before = reconciliation_tests._tree_hashes(root)
            output = root / "conflict-review"

            created = publish_terminal_conflict_review(report_path, output)
            repeated = publish_terminal_conflict_review(report_path, output)

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.review_id, repeated.review_id)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            self.assertEqual(review["case_count"], 1)
            self.assertEqual(review["candidate_count"], 2)
            case = review["cases"][0]
            self.assertEqual(case["queue_id"], queue_id)
            self.assertEqual(
                {item["authority"] for item in case["candidates"]},
                {"approved", "rejected"},
            )
            self.assertEqual(
                len({item["audio_sha256"] for item in case["candidates"]}), 2
            )
            source_after_publish = {
                key: value
                for key, value in reconciliation_tests._tree_hashes(root).items()
                if not key.startswith("conflict-review/")
            }
            self.assertEqual(before, source_after_publish)

            progress = record_terminal_conflict_decision(
                output, case["case_id"], NEITHER_ACCEPTABLE
            )

            self.assertEqual(progress["decisions"][0]["decision"], NEITHER_ACCEPTABLE)
            self.assertEqual(load_terminal_conflict_review(output).completed_count, 1)
            self.assertEqual(load_terminal_conflict_review_progress(output), progress)
            self.assertEqual(
                before,
                {
                    key: value
                    for key, value in reconciliation_tests._tree_hashes(root).items()
                    if not key.startswith("conflict-review/")
                },
            )
            self.assertTrue(primary.is_dir())
            self.assertTrue(secondary.is_dir())

    def test_carries_only_content_identical_decision_with_predecessor_ledger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            source = root / "source-review"
            target = root / "target-review"
            source_result = publish_terminal_conflict_review(report_path, source)
            target_result = publish_terminal_conflict_review(report_path, target)
            review = json.loads(source_result.review.read_text(encoding="utf-8"))
            case = review["cases"][0]
            chosen = case["candidates"][0]["candidate_id"]
            source_progress = record_terminal_conflict_decision(
                source, case["case_id"], chosen
            )

            carried = carry_terminal_conflict_decisions(source, target)

            self.assertEqual(
                carried["schema_version"], TERMINAL_CONFLICT_PROGRESS_CARRY_VERSION
            )
            self.assertEqual(carried["review_id"], target_result.review_id)
            self.assertEqual(carried["decisions"], source_progress["decisions"])
            self.assertEqual(carried["carry_forward"]["case_ids"], [case["case_id"]])
            self.assertEqual(
                carried["carry_forward"]["source_review_id"], source_result.review_id
            )
            self.assertEqual(load_terminal_conflict_review(target).completed_count, 1)
            target_progress_before = (target / "progress.json").read_bytes()
            with self.assertRaisesRegex(
                TerminalConflictReviewError, "decision identity changed"
            ):
                record_terminal_conflict_decision(
                    target, case["case_id"], NEITHER_ACCEPTABLE, overwrite=True
                )
            self.assertEqual(
                (target / "progress.json").read_bytes(), target_progress_before
            )
            with self.assertRaisesRegex(
                TerminalConflictReviewError, "already has progress"
            ):
                carry_terminal_conflict_decisions(source, target)

    def test_carry_survives_refreshed_source_state_for_same_candidate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, queue_id, report_path = self.create_fixture(root)
            source = root / "source-review"
            target = root / "target-review"
            source_result = publish_terminal_conflict_review(report_path, source)
            review = json.loads(source_result.review.read_text(encoding="utf-8"))
            case = review["cases"][0]
            chosen = case["candidates"][0]["candidate_id"]
            source_progress = record_terminal_conflict_decision(
                source, case["case_id"], chosen
            )

            state_path = secondary / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["updated_at"] = "2026-08-27T12:00:00+00:00"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            refreshed_report = build_authoring_reconciliation(
                primary, root / "review-bundles"
            )
            refreshed_report_path = root / "refreshed-reconciliation.json"
            write_authoring_reconciliation(refreshed_report, refreshed_report_path)
            publish_terminal_conflict_review(refreshed_report_path, target)

            carried = carry_terminal_conflict_decisions(source, target)

            self.assertEqual(carried["decisions"], source_progress["decisions"])
            self.assertEqual(load_terminal_conflict_review(target).completed_count, 1)
            with self.assertRaisesRegex(
                TerminalConflictReviewError, "authority changed"
            ):
                record_terminal_conflict_decision(
                    source,
                    case["case_id"],
                    case["candidates"][1]["candidate_id"],
                    overwrite=True,
                )

    def test_carries_only_exact_approved_cohort_candidate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            case = review["cases"][0]
            approved = next(
                candidate
                for candidate in case["candidates"]
                if candidate["authority"] == "approved"
            )

            progress = carry_approved_cohort_terminal_conflict_decisions(output)

            self.assertEqual(
                progress["decisions"],
                [
                    {
                        "case_id": case["case_id"],
                        "decision": approved["candidate_id"],
                        "reviewed_at": progress["decisions"][0]["reviewed_at"],
                    }
                ],
            )
            self.assertEqual(load_terminal_conflict_review(output).completed_count, 1)

            with self.assertRaisesRegex(
                TerminalConflictReviewError, "No exact approved cohort"
            ):
                carry_approved_cohort_terminal_conflict_decisions(output)

    def test_carried_progress_rejects_changed_predecessor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            source = root / "source-review"
            target = root / "target-review"
            source_result = publish_terminal_conflict_review(report_path, source)
            publish_terminal_conflict_review(report_path, target)
            review = json.loads(source_result.review.read_text(encoding="utf-8"))
            case = review["cases"][0]
            first, second = [
                candidate["candidate_id"] for candidate in case["candidates"]
            ]
            record_terminal_conflict_decision(source, case["case_id"], first)
            carry_terminal_conflict_decisions(source, target)
            record_terminal_conflict_decision(
                source, case["case_id"], second, overwrite=True
            )

            with self.assertRaisesRegex(
                TerminalConflictReviewError, "authority changed"
            ):
                load_terminal_conflict_review_progress(target)

    def test_tampered_copied_wav_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            audio = output / review["cases"][0]["candidates"][0]["audio"]
            audio.write_bytes(b"not a wav")

            with self.assertRaisesRegex(TerminalConflictReviewError, "WAV changed"):
                load_terminal_conflict_review(output)

    def test_source_authority_change_blocks_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            case = review["cases"][0]
            state_path = primary / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["review_status"] = "rejected"
            state["items"][queue_id]["status"] = "generated"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                TerminalConflictReviewError, "authority changed"
            ):
                record_terminal_conflict_decision(
                    output,
                    case["case_id"],
                    case["candidates"][0]["candidate_id"],
                )

            self.assertFalse(created.progress.exists())

    def test_source_state_change_after_reconciliation_blocks_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, queue_id, report_path = self.create_fixture(root)
            state_path = primary / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["updated_at"] = "2026-08-26T12:00:00+00:00"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                TerminalConflictReviewError, "changed after reconciliation"
            ):
                publish_terminal_conflict_review(
                    report_path,
                    root / "conflict-review",
                )

            self.assertFalse((root / "conflict-review").exists())

    def test_progress_rejects_overwrite_without_explicit_permission(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            case = review["cases"][0]
            first = case["candidates"][0]["candidate_id"]
            second = case["candidates"][1]["candidate_id"]
            record_terminal_conflict_decision(output, case["case_id"], first)

            with self.assertRaisesRegex(TerminalConflictReviewError, "already decided"):
                record_terminal_conflict_decision(output, case["case_id"], second)

            progress = record_terminal_conflict_decision(
                output, case["case_id"], second, overwrite=True
            )
            self.assertEqual(progress["decisions"][0]["decision"], second)

    def test_review_directory_symlink_is_rejected_by_read_and_write_apis(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            alias = root / "review-alias"
            symlink_or_skip(alias, output, target_is_directory=True)

            with self.assertRaisesRegex(
                TerminalConflictReviewError, "must not be a symlink"
            ):
                load_terminal_conflict_review(alias)
            with self.assertRaisesRegex(
                TerminalConflictReviewError, "must not be a symlink"
            ):
                record_terminal_conflict_decision(
                    alias,
                    review["cases"][0]["case_id"],
                    NEITHER_ACCEPTABLE,
                )

            self.assertFalse(created.progress.exists())

    def test_interrupted_publication_removes_its_staging_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"

            with patch(
                "vntts.authoring.terminal_conflict_review._assert_source_authorities",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    publish_terminal_conflict_review(report_path, output)

            self.assertFalse(output.exists())
            self.assertEqual(
                list(root.glob(".conflict-review.staging-*")),
                [],
            )

    def test_publication_failure_is_reported_as_a_review_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"

            with (
                patch(
                    "vntts.authoring.terminal_conflict_review.rename_directory_no_replace",
                    side_effect=AtomicPublicationError("simulated no-replace failure"),
                ),
                self.assertRaisesRegex(
                    TerminalConflictReviewError, "simulated no-replace failure"
                ),
            ):
                publish_terminal_conflict_review(report_path, output)

            self.assertFalse(output.exists())

    def test_wire_validator_rejects_more_than_two_candidates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            result = publish_terminal_conflict_review(
                report_path, root / "conflict-review"
            )
            document = json.loads(result.review.read_text(encoding="utf-8"))
            document["cases"][0]["candidates"].append(
                dict(document["cases"][0]["candidates"][0])
            )
            document["candidate_count"] += 1
            document["review_id"] = canonical_document_sha256(
                {key: value for key, value in document.items() if key != "review_id"}
            )

            with self.assertRaisesRegex(
                TerminalConflictReviewError, "exactly two candidates"
            ):
                validate_terminal_conflict_review_document(
                    document, root / "conflict-review"
                )

    def test_dead_progress_lease_is_archived_and_review_continues(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            lock = output / ".progress.lock"
            lock.write_text(
                json.dumps(
                    {
                        "schema": PROGRESS_LEASE_SCHEMA,
                        "schema_version": PROGRESS_LEASE_VERSION,
                        "pid": 999999,
                        "hostname": socket.gethostname(),
                        "process_started_at": "stale",
                        "lease_id": "dead-owner",
                        "started_at": "2026-08-26T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "vntts.authoring.terminal_conflict_review.process_is_alive",
                return_value=False,
            ):
                progress = record_terminal_conflict_decision(
                    output,
                    review["cases"][0]["case_id"],
                    NEITHER_ACCEPTABLE,
                )

            self.assertEqual(len(progress["decisions"]), 1)
            self.assertFalse(lock.exists())
            self.assertEqual(len(list(output.glob(".progress.lock.interrupted-*"))), 1)

    def test_live_progress_lease_blocks_concurrent_decision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))
            (output / ".progress.lock").write_text(
                json.dumps(
                    {
                        "schema": PROGRESS_LEASE_SCHEMA,
                        "schema_version": PROGRESS_LEASE_VERSION,
                        "pid": 1234,
                        "hostname": socket.gethostname(),
                        "process_started_at": "same-start",
                        "lease_id": "live-owner",
                        "started_at": "2026-08-26T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "vntts.authoring.terminal_conflict_review.process_is_alive",
                    return_value=True,
                ),
                patch(
                    "vntts.authoring.terminal_conflict_review.process_started_at",
                    return_value="same-start",
                ),
                self.assertRaisesRegex(
                    TerminalConflictReviewError,
                    "Another terminal conflict decision is being saved",
                ),
            ):
                record_terminal_conflict_decision(
                    output,
                    review["cases"][0]["case_id"],
                    NEITHER_ACCEPTABLE,
                )

    def test_progress_recovery_guard_blocks_a_successor_writer(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _primary, _secondary, _queue_id, report_path = self.create_fixture(root)
            output = root / "conflict-review"
            created = publish_terminal_conflict_review(report_path, output)
            review = json.loads(created.review.read_text(encoding="utf-8"))

            with (
                exclusive_advisory_lock(output / ".progress.lock.guard"),
                self.assertRaisesRegex(
                    TerminalConflictReviewError,
                    "Another terminal conflict decision is being saved",
                ),
            ):
                record_terminal_conflict_decision(
                    output,
                    review["cases"][0]["case_id"],
                    NEITHER_ACCEPTABLE,
                )

            self.assertFalse((output / ".progress.lock").exists())


if __name__ == "__main__":
    unittest.main()
