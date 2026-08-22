import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.cohort_bundle as cohort_bundle_module
from tests import test_authoring_cohort_review
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_bundle import (
    CohortReviewError,
    build_cohort_review_bundle,
    execute_cohort_bundle_decision,
    load_cohort_review_bundle,
    load_cohort_review_bundle_samples,
    load_resumable_cohort_review_bundle,
    load_resumable_cohort_review_bundle_samples,
    load_resumable_cohort_review_session,
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

    def test_exact_source_selections_survive_refresh(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            selections = {
                workspace: [queue_id] for workspace, _state, queue_id in sources
            }
            bundle = build_cohort_review_bundle(
                [value[0] for value in sources],
                queue_ids_by_workspace=selections,
            )

            refreshed = refresh_cohort_review_bundle(bundle)

        self.assertEqual(refreshed, bundle)
        self.assertEqual(
            {
                source["workspace_id"]: source["plan"]["policy"]["selected_queue_ids"]
                for source in bundle.document["sources"]
            },
            {workspace.name: [queue_id] for workspace, _state, queue_id in sources},
        )

    def test_terminal_selected_decision_removes_completed_source(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            selections = {
                workspace: [queue_id] for workspace, _state, queue_id in sources
            }
            bundle = build_cohort_review_bundle(
                [value[0] for value in sources],
                queue_ids_by_workspace=selections,
            )
            selected = bundle.document["cohorts"][0]

            projection = execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "accepted",
                reviewed_queue_ids=[selected["samples"][0]["queue_id"]],
            )

        self.assertEqual(projection.next_bundle.document["workspace_count"], 1)
        self.assertEqual(projection.next_bundle.document["pending_item_count"], 1)

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

    def test_terminal_decision_does_not_rebuild_full_workspace_plans(self):
        with TemporaryDirectory() as directory:
            sources = self.create_sources(Path(directory))
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            selected = bundle.document["cohorts"][0]

            with patch(
                "vntts.authoring.cohort_bundle.build_cohort_review_plan",
                side_effect=AssertionError("terminal decision rebuilt a full plan"),
            ):
                projection = execute_cohort_bundle_decision(
                    bundle,
                    selected["workspace_id"],
                    selected["cohort_id"],
                    "accepted",
                    reviewed_queue_ids=[selected["samples"][0]["queue_id"]],
                )

        self.assertEqual(projection.review_status, "approved")
        self.assertEqual(projection.next_bundle.document["cohort_count"], 1)

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

    def test_recovery_allows_only_deterministic_sample_membership_growth(self):
        item = {
            "queue_id": "queue-id",
            "line_id": "line-id",
            "text_sha256": "a" * 64,
            "audio_sha256": "b" * 64,
            "sampled": False,
        }
        cohort = {
            "cohort_id": "c" * 64,
            "identity": {"voice": "Narrator"},
            "items": [item],
            "sample_queue_ids": [],
        }
        original = {
            "workspace_id": "workspace",
            "plan": {
                "workspace_config_fingerprint": "d" * 64,
                "queue_sha256": "e" * 64,
            },
        }
        rebuilt = {
            "workspace_id": "workspace",
            "workspace_config_fingerprint": "d" * 64,
            "queue_sha256": "e" * 64,
            "cohorts": [
                {
                    **cohort,
                    "items": [{**item, "sampled": True}],
                    "sample_queue_ids": ["queue-id"],
                }
            ],
        }

        cohort_bundle_module._validate_reconciled_source(
            original,
            [cohort],
            rebuilt,
        )
        rebuilt["cohorts"][0]["items"][0]["audio_sha256"] = "f" * 64
        with self.assertRaisesRegex(CohortReviewError, "authority changed"):
            cohort_bundle_module._validate_reconciled_source(
                original,
                [cohort],
                rebuilt,
            )

    def test_resume_restores_exact_expand_sample_assessments(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.create_sources(root)
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            selected = bundle.document["cohorts"][0]
            queue_id = selected["samples"][0]["queue_id"]
            execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "expand",
                reviewed_queue_ids=[queue_id],
                sample_assessments={queue_id: "bad"},
                next_clean_samples_per_bucket=2,
            )

            _resume, _current, _samples, assessments = (
                load_resumable_cohort_review_session(publication, persist=False)
            )

        self.assertEqual(len(assessments), 1)
        self.assertEqual(assessments[0].workspace_id, selected["workspace_id"])
        self.assertEqual(assessments[0].cohort_id, selected["cohort_id"])
        self.assertEqual(assessments[0].queue_id, queue_id)
        self.assertEqual(assessments[0].assessment, "bad")

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

    def test_resume_recovers_terminal_decision_committed_before_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.create_sources(root)
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            selected = bundle.document["cohorts"][0]
            execute_cohort_bundle_decision(
                bundle,
                selected["workspace_id"],
                selected["cohort_id"],
                "accepted",
                reviewed_queue_ids=[selected["samples"][0]["queue_id"]],
            )

            resume = load_resumable_cohort_review_bundle(publication, persist=True)
            progress_before = resume.progress.read_bytes()
            loaded, current, samples = load_resumable_cohort_review_bundle_samples(
                publication
            )
            repeated = load_resumable_cohort_review_bundle(publication, persist=True)
            self.assertEqual(resume.to_dict()["completed_cohorts"], 1)
            self.assertEqual(resume.to_dict()["remaining_cohorts"], 1)
            self.assertTrue(resume.progress_current)
            self.assertTrue(resume.progress.is_file())
            self.assertEqual(loaded.current.bundle_id, current.bundle_id)
            self.assertEqual(len(samples), 1)
            self.assertTrue(repeated.progress_current)
            self.assertEqual(repeated.progress.read_bytes(), progress_before)

    def test_resume_recovers_a_fully_completed_multi_step_session(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.create_sources(root)
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            first = bundle.document["cohorts"][0]
            projection = execute_cohort_bundle_decision(
                bundle,
                first["workspace_id"],
                first["cohort_id"],
                "accepted",
                reviewed_queue_ids=[first["samples"][0]["queue_id"]],
            )
            second = projection.next_bundle.document["cohorts"][0]
            execute_cohort_bundle_decision(
                projection.next_bundle,
                second["workspace_id"],
                second["cohort_id"],
                "rejected",
                reviewed_queue_ids=[second["samples"][0]["queue_id"]],
            )

            resume = load_resumable_cohort_review_bundle(publication, persist=True)
            _loaded, current, samples = load_resumable_cohort_review_bundle_samples(
                publication
            )

            self.assertEqual(resume.to_dict()["completed_cohorts"], 2)
            self.assertEqual(resume.to_dict()["remaining_cohorts"], 0)
            self.assertEqual(current.document["workspace_count"], 0)
            self.assertEqual(samples, ())

    def test_resume_rejects_state_change_without_exact_decision_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.create_sources(root)
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            state_path = sources[0][1]
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id = sources[0][2]
            state["items"][queue_id]["status"] = "approved"
            state["items"][queue_id]["review_status"] = "approved"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(CohortReviewError, "without exact terminal"):
                load_resumable_cohort_review_bundle(publication)

    def test_resume_rejects_changed_wav_and_progress_symlink(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.create_sources(root)
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            source = bundle.document["sources"][0]
            queue_id = source["plan"]["cohorts"][0]["items"][0]["queue_id"]
            workspace = Path(source["workspace"])
            state = json.loads(
                (workspace / "generated-audio/generation-state.json").read_text()
            )
            audio = workspace / "generated-audio" / state["items"][queue_id]["path"]
            audio.write_bytes(b"changed")

            with self.assertRaisesRegex(CohortReviewError, "WAV changed"):
                load_resumable_cohort_review_bundle(publication)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = self.create_sources(root)
            bundle = build_cohort_review_bundle([value[0] for value in sources])
            publication = root / "bundle.json"
            write_cohort_review_bundle(bundle, publication)
            progress = root / "bundle.progress.json"
            progress.symlink_to(root / "outside.json")
            with self.assertRaisesRegex(CohortReviewError, "cannot be a symlink"):
                load_resumable_cohort_review_bundle(publication)
