import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from tests import test_authoring_cohort_review
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_bundle import (
    CohortReviewError,
    build_cohort_review_bundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    load_cohort_review_bundle_samples,
    refresh_cohort_review_bundle,
    write_cohort_review_bundle,
)


class AuthoringCohortBundleTest(unittest.TestCase):
    def create_sources(self, root):
        fixture = test_authoring_cohort_review.AuthoringCohortReviewTest()
        first, first_state, first_queue = fixture.create_pending_workspace(
            root / "first"
        )
        second, second_state, second_queue = fixture.create_pending_workspace(
            root / "second"
        )
        return (
            (first, first_state, first_queue),
            (second, second_state, second_queue),
        )

    def test_bundle_flattens_exact_source_samples_and_reasons(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])

        self.assertEqual(bundle.document["workspace_count"], 2)
        self.assertEqual(bundle.document["cohort_count"], 2)
        self.assertEqual(bundle.document["pending_item_count"], 2)
        self.assertEqual(bundle.document["sample_item_count"], 2)
        self.assertEqual(bundle.document["schema_version"], 2)
        self.assertEqual(len(bundle.document["cohorts"]), 2)
        for cohort in bundle.document["cohorts"]:
            self.assertEqual(len(cohort["samples"]), 1)
            self.assertIn(
                "technical-attention", cohort["samples"][0]["required_reason"]
            )

    def test_duplicate_source_and_tampered_inventory_are_rejected(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            workspace = sources[0][0]
            with self.assertRaisesRegex(CohortReviewError, "distinct"):
                build_cohort_review_bundle([workspace, workspace])
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            path = Path(directory) / "bundle.json"
            write_cohort_review_bundle(bundle, path)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["cohorts"][0]["samples"][0]["required_reason"] = "forged"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(CohortReviewError, "inventory changed"):
                load_cohort_review_bundle(path)

    def test_refresh_rejects_changed_source_authority(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            state_path = sources[1][1]
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = {"phase": "changed"}
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaises(CohortReviewError):
                refresh_cohort_review_bundle(bundle)

    def test_decision_projects_only_selected_source_and_returns_next_bundle(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            selected = bundle.document["cohorts"][0]
            other = next(
                value for value in sources if value[0].name != selected["workspace_id"]
            )
            other_state_before = other[1].read_bytes()

            projection = execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "accepted",
                reviewed_queue_ids=[selected["samples"][0]["queue_id"]],
            )

            self.assertEqual(projection.review_status, "approved")
            self.assertEqual(projection.next_bundle.document["pending_item_count"], 1)
            self.assertEqual(other[1].read_bytes(), other_state_before)

    def test_expand_is_source_local_and_keeps_pending_authority(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            selected = bundle.document["cohorts"][0]

            projection = execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "expand",
                reviewed_queue_ids=[selected["samples"][0]["queue_id"]],
                next_clean_samples_per_bucket=2,
            )

        self.assertIsNone(projection.review_status)
        self.assertEqual(projection.queue_ids, ())
        expanded = next(
            source
            for source in projection.next_bundle.document["sources"]
            if source["workspace_id"] == selected["workspace_id"]
        )
        unchanged = next(
            source
            for source in projection.next_bundle.document["sources"]
            if source["workspace_id"] != selected["workspace_id"]
        )
        self.assertEqual(expanded["plan"]["policy"]["clean_samples_per_bucket"], 2)
        self.assertEqual(unchanged["plan"]["policy"]["clean_samples_per_bucket"], 1)

    def test_cli_publishes_the_exact_bundle(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            output = Path(directory) / "bundle.json"
            stdout = StringIO()
            arguments = ["cohort-review-bundle"]
            for workspace, _state, _queue_id in sources:
                arguments.extend(("--workspace", str(workspace)))
            arguments.extend(("--output", str(output)))

            with redirect_stdout(stdout):
                exit_code = authoring_main(arguments)

            loaded = load_cohort_review_bundle(output)
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), loaded.document)

    def test_live_samples_bind_text_audio_and_source_workspace(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])

            current, samples = load_cohort_review_bundle_samples(bundle)

        self.assertEqual(current, bundle)
        self.assertEqual(len(samples), 2)
        self.assertEqual(
            {sample.workspace for sample in samples},
            {value[0].resolve() for value in sources},
        )
        self.assertTrue(all(sample.item.authority is not None for sample in samples))

    def test_live_loader_does_not_require_full_review_projection(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])

            _current, samples = load_cohort_review_bundle_samples(bundle)

        self.assertEqual(len(samples), 2)
