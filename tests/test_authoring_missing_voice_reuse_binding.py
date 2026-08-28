import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_authoring_missing_voice_reuse_review import (
    AuthoringMissingVoiceReuseReviewTest,
)
from vntts.authoring.missing_voice_reuse_binding import (
    MissingVoiceReuseBindingError,
    publish_missing_voice_reuse_binding,
)
from vntts.authoring.missing_voice_reuse_review import (
    build_missing_voice_reuse_review,
    load_missing_voice_reuse_review,
    record_missing_voice_reuse_decision,
    record_missing_voice_reuse_heard,
)
from vntts.authoring.source_reference_bindings import (
    MISSING_VOICE_REUSE_BINDING_FIELD,
    queue_voice_overrides_from_manifest,
)


class AuthoringMissingVoiceReuseBindingTest(unittest.TestCase):
    def create_review(self, root, statuses=("generated", "failed")):
        plan_path, evidence, snapshots, queue_id = (
            AuthoringMissingVoiceReuseReviewTest().fixture(root, statuses=statuses)
        )
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            side_effect=lambda _plan, _candidate, path: snapshots[Path(path).resolve()],
        ):
            session_path = build_missing_voice_reuse_review(
                plan_path, evidence, root / "review", seed=7
            )
        return plan_path, session_path, queue_id

    def test_selected_candidate_binds_the_full_exact_cohort(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, session_path, queue_id = self.create_review(root)
            bundle, _session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]
            selected = cohort["complete_candidate_labels"][0]
            record_missing_voice_reuse_heard(
                session_path, cohort["cohort_id"], queue_id, selected
            )
            record_missing_voice_reuse_decision(
                session_path, cohort["cohort_id"], selected
            )

            first = publish_missing_voice_reuse_binding(
                plan_path, session_path, root / "binding"
            )
            second = publish_missing_voice_reuse_binding(
                plan_path, session_path, root / "binding"
            )
            manifest = json.loads(
                (first.directory / "manifest.json").read_text(encoding="utf-8")
            )
            binding = manifest[MISSING_VOICE_REUSE_BINDING_FIELD]

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.selected_cohort_count, 1)
        self.assertEqual(first.neither_cohort_count, 0)
        self.assertEqual(first.bound_queue_count, 1)
        self.assertEqual(set(binding["queue_voice_overrides"]), {queue_id})
        self.assertEqual(
            queue_voice_overrides_from_manifest(manifest)[queue_id],
            binding["selected_candidates"][0]["voice_character"],
        )

    def test_neither_publishes_auditable_zero_override_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, session_path, _queue_id = self.create_review(
                root, statuses=("failed", "failed")
            )
            bundle, _session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]
            record_missing_voice_reuse_decision(
                session_path, cohort["cohort_id"], "neither"
            )

            result = publish_missing_voice_reuse_binding(
                plan_path, session_path, root / "binding"
            )
            manifest = json.loads(
                (result.directory / "manifest.json").read_text(encoding="utf-8")
            )
            binding = manifest[MISSING_VOICE_REUSE_BINDING_FIELD]

        self.assertEqual(result.selected_cohort_count, 0)
        self.assertEqual(result.neither_cohort_count, 1)
        self.assertEqual(result.bound_queue_count, 0)
        self.assertEqual(binding["queue_voice_overrides"], {})
        self.assertEqual(queue_voice_overrides_from_manifest(manifest), {})

    def test_incomplete_review_and_tampered_bundle_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, session_path, _queue_id = self.create_review(root)
            with self.assertRaisesRegex(MissingVoiceReuseBindingError, "Every"):
                publish_missing_voice_reuse_binding(
                    plan_path, session_path, root / "binding"
                )

            bundle, _session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]
            generated = cohort["complete_candidate_labels"][0]
            queue_id = cohort["samples"][0]["queue_id"]
            record_missing_voice_reuse_heard(
                session_path, cohort["cohort_id"], queue_id, generated
            )
            record_missing_voice_reuse_decision(
                session_path, cohort["cohort_id"], generated
            )
            result = publish_missing_voice_reuse_binding(
                plan_path, session_path, root / "binding"
            )
            (result.directory / "manifest.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(
                MissingVoiceReuseBindingError, "artifact changed"
            ):
                publish_missing_voice_reuse_binding(
                    plan_path, session_path, root / "binding"
                )


if __name__ == "__main__":
    unittest.main()
