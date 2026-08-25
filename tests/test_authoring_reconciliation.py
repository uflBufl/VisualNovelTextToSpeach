import hashlib
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.reconciliation as reconciliation_module
from tests.test_authoring_cohort_review import AuthoringCohortReviewTest
from tests.test_authoring_source_reference_review import (
    AuthoringSourceReferenceReviewTest,
)
from vntts.authoring.cohort_bundle import (
    build_cohort_review_bundle,
    write_cohort_review_bundle,
)
from vntts.authoring.reconciliation import (
    AuthoringReconciliationError,
    build_authoring_reconciliation,
    load_authoring_reconciliation,
    write_authoring_reconciliation,
)
from vntts.authoring.reconciliation_cli import main as reconciliation_main


def _tree_hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class AuthoringReconciliationTest(unittest.TestCase):
    def create_fixture(self, root):
        authoring = root / "authoring"
        workspace, state, queue_id = (
            AuthoringCohortReviewTest().create_pending_workspace(authoring)
        )
        bundles = authoring / "review-bundles"
        bundles.mkdir()
        quality = authoring / "source-reference-quality-reviews"
        quality.mkdir()
        bundle = build_cohort_review_bundle([workspace])
        publication = bundles / "current.json"
        write_cohort_review_bundle(bundle, publication)
        return authoring, workspace, state, queue_id, bundles, quality, publication

    def test_report_reconciles_bundle_without_mutating_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _authoring,
                workspace,
                _state,
                queue_id,
                bundles,
                _quality,
                _publication,
            ) = self.create_fixture(root)
            before = _tree_hashes(root)

            first = build_authoring_reconciliation(workspace, bundles)
            second = build_authoring_reconciliation(workspace, bundles)

            self.assertEqual(first, second)
            self.assertEqual(before, _tree_hashes(root))
            self.assertEqual(first.document["summary"]["workspace_count"], 1)
            self.assertEqual(first.document["summary"]["bundle_count"], 1)
            self.assertEqual(
                first.document["summary"]["action_counts"],
                {"human_cohort_review": 1},
            )
            self.assertEqual(first.document["actions"][0]["queue_id"], queue_id)
            self.assertTrue(first.document["actions"][0]["cohort"]["sampled"])
            self.assertRegex(
                first.document["actions"][0]["audio_sha256"], r"^[0-9a-f]{64}$"
            )

    def test_unbundled_pending_item_is_not_assumed_approved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            authoring = root / "authoring"
            workspace, _state, queue_id = (
                AuthoringCohortReviewTest().create_pending_workspace(authoring)
            )
            bundles = authoring / "review-bundles"
            bundles.mkdir()
            (authoring / "source-reference-quality-reviews").mkdir()

            report = build_authoring_reconciliation(workspace, bundles)

        self.assertEqual(report.document["actions"][0]["queue_id"], queue_id)
        self.assertEqual(
            report.document["actions"][0]["action"], "review_plan_required"
        )

    def test_duplicate_current_bundle_authority_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _authoring,
                workspace,
                _state,
                _queue_id,
                bundles,
                _quality,
                publication,
            ) = self.create_fixture(root)
            (bundles / "duplicate.json").write_bytes(publication.read_bytes())

            with self.assertRaisesRegex(
                AuthoringReconciliationError, "ambiguous current review bundles"
            ):
                build_authoring_reconciliation(workspace, bundles)

    def test_only_explicit_quality_review_is_included(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                authoring,
                workspace,
                _state,
                _queue_id,
                bundles,
                _quality,
                _publication,
            ) = self.create_fixture(root)
            quality_root = authoring / "explicit-quality"
            quality_root.mkdir()
            _plan, _evaluation, _generation, quality = (
                AuthoringSourceReferenceReviewTest().publish_quality_fixture(
                    quality_root
                )
            )
            unrelated = authoring / "source-reference-quality-reviews/unrelated"
            unrelated.mkdir(parents=True)
            (unrelated / "review.json").write_text("{}", encoding="utf-8")

            report = build_authoring_reconciliation(
                workspace,
                bundles,
                quality_reviews=[quality.session],
            )

        self.assertEqual(report.document["summary"]["quality_review_count"], 1)
        self.assertEqual(
            report.document["summary"]["action_counts"]["human_source_quality_review"],
            2,
        )
        self.assertEqual(
            {
                item["character"]
                for item in report.document["actions"]
                if item["action"] == "human_source_quality_review"
            },
            {"Hero"},
        )

    def test_final_snapshot_change_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _authoring,
                workspace,
                state,
                _queue_id,
                bundles,
                _quality,
                _publication,
            ) = self.create_fixture(root)
            original = reconciliation_module._assert_snapshots_unchanged

            def mutate_then_check(snapshots):
                document = json.loads(state.read_text(encoding="utf-8"))
                document["active"] = {"phase": "changed"}
                state.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
                original(snapshots)

            with (
                patch.object(
                    reconciliation_module,
                    "_assert_snapshots_unchanged",
                    side_effect=mutate_then_check,
                ),
                self.assertRaisesRegex(
                    AuthoringReconciliationError,
                    "Authority changed during reconciliation",
                ),
            ):
                build_authoring_reconciliation(workspace, bundles)

    def test_report_publication_is_no_replace_and_cli_matches(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _authoring,
                workspace,
                _state,
                _queue_id,
                bundles,
                _quality,
                _publication,
            ) = self.create_fixture(root)
            report = build_authoring_reconciliation(workspace, bundles)
            output = root / "report.json"

            write_authoring_reconciliation(report, output)
            loaded = load_authoring_reconciliation(output)
            with self.assertRaisesRegex(AuthoringReconciliationError, "output exists"):
                write_authoring_reconciliation(report, output)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = reconciliation_main(
                    [
                        "--primary-workspace",
                        str(workspace),
                        "--bundle-root",
                        str(bundles),
                    ]
                )

        self.assertEqual(loaded, report)
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report.document)


if __name__ == "__main__":
    unittest.main()
