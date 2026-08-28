import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.test_authoring_missing_voice_reuse import (
    build_failed_missing_voice_reuse_plan_fixture,
    create_missing_voice_reuse_workspace,
)
from tests.test_authoring_missing_voice_reuse_review import (
    create_missing_voice_reuse_review_fixture,
)
from vntts.authoring.missing_voice_reuse import write_missing_voice_reuse_plan
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


def create_missing_voice_reuse_binding_review(root, statuses=("generated", "failed")):
    return AuthoringMissingVoiceReuseBindingTest().create_review(
        root, statuses=statuses
    )


class AuthoringMissingVoiceReuseBindingTest(unittest.TestCase):
    def create_review(self, root, statuses=("generated", "failed")):
        plan_path, evidence, snapshots, queue_id = (
            create_missing_voice_reuse_review_fixture(root, statuses=statuses)
        )
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            side_effect=lambda _plan, _candidate, path: snapshots[Path(path).resolve()],
        ):
            session_path = build_missing_voice_reuse_review(
                plan_path, evidence, root / "review", seed=7
            )
        return plan_path, session_path, queue_id

    def create_failed_review(self, root):
        fixture, _imported, workspace = create_missing_voice_reuse_workspace(root)
        plan = build_failed_missing_voice_reuse_plan_fixture(fixture, workspace)
        plan_path = root / "failed-plan.json"
        write_missing_voice_reuse_plan(plan, plan_path)
        candidate = plan.document["candidates"][0]
        candidate_root = (root / "failed-candidate").resolve()
        candidate_root.mkdir()
        queue_id = fixture["queue_id"]
        snapshot = {
            "directory": candidate_root,
            "workspace": {"workspace_id": "failed-candidate-workspace"},
            "state": {
                "items": {
                    queue_id: {
                        "status": "failed",
                        "attempts": 1,
                        "failure": {"kind": "missed_eos_audio_limit"},
                        "last_error": "Typed limited render",
                        "source_reference_binding": {
                            "queue_id": queue_id,
                            "synthesis_voice_character": candidate["voice_character"],
                        },
                    }
                }
            },
            "authority": {
                "path": str(candidate_root),
                "workspace_id": "failed-candidate-workspace",
                "workspace_sha256": "1" * 64,
                "state_sha256": "2" * 64,
                "voice_manifest_sha256": "3" * 64,
            },
        }
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            return_value=snapshot,
        ):
            session_path = build_missing_voice_reuse_review(
                plan_path,
                {candidate["candidate_id"]: (candidate_root,)},
                root / "failed-review",
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
            plan_path, session_path, queue_id = self.create_failed_review(root)

            result = publish_missing_voice_reuse_binding(
                plan_path, session_path, root / "binding"
            )
            manifest = json.loads(
                (result.directory / "manifest.json").read_text(encoding="utf-8")
            )
            binding = manifest[MISSING_VOICE_REUSE_BINDING_FIELD]
            source_state_item_sha256 = json.loads(
                plan_path.read_text(encoding="utf-8")
            )["targets"][0]["source_state_item_sha256"]

        self.assertEqual(result.selected_cohort_count, 0)
        self.assertEqual(result.neither_cohort_count, 1)
        self.assertEqual(result.bound_queue_count, 0)
        self.assertEqual(binding["queue_voice_overrides"], {})
        self.assertEqual(queue_voice_overrides_from_manifest(manifest), {})
        self.assertEqual(
            binding["decisions"][0]["review_decision_origin"],
            "automatic_no_complete_candidate",
        )
        self.assertEqual(
            binding["source_failed_state_item_sha256s"],
            {queue_id: source_state_item_sha256},
        )

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
