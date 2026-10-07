import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

from tests.missing_voice_reuse_fixtures import (
    build_failed_missing_voice_reuse_plan_fixture,
    create_missing_voice_reuse_binding_review,
    create_missing_voice_reuse_workspace,
)
from vntts.authoring import missing_voice_reuse_binding as binding_module
from vntts.authoring.authority import canonical_document_sha256
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


class AuthoringMissingVoiceReuseBindingTest(unittest.TestCase):
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
            plan_path, session_path, queue_id = (
                create_missing_voice_reuse_binding_review(root)
            )
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

    def test_manifest_change_after_capture_cannot_publish_or_return_existing(self):
        for existing in (False, True):
            with self.subTest(existing=existing), TemporaryDirectory() as directory:
                root = Path(directory)
                plan_path, session_path, _queue_id = self.create_failed_review(root)
                output = root / "binding"
                if existing:
                    publish_missing_voice_reuse_binding(plan_path, session_path, output)
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                source = (
                    Path(plan["source"]["workspace"]) / "inputs/voice/manifest.json"
                ).resolve()
                changed = json.loads(source.read_text(encoding="utf-8"))
                changed["unreviewed_metadata"] = "must not be imported"
                original_open = Path.open
                original_review_loader = binding_module.load_missing_voice_reuse_review
                ready = False
                reads = 0

                def mark_source_boundary(path):
                    nonlocal ready
                    result = original_review_loader(path)
                    ready = True
                    return result

                def change_on_second_read(path, mode="r", *args, **kwargs):
                    nonlocal reads
                    if ready and path == source and mode in ("r", "rb"):
                        reads += 1
                        if reads == 2:
                            source.write_text(json.dumps(changed), encoding="utf-8")
                    return original_open(path, mode, *args, **kwargs)

                with (
                    patch.object(
                        binding_module,
                        "load_missing_voice_reuse_review",
                        side_effect=mark_source_boundary,
                    ),
                    patch.object(Path, "open", change_on_second_read),
                    self.assertRaisesRegex(
                        MissingVoiceReuseBindingError, "manifest changed"
                    ),
                ):
                    publish_missing_voice_reuse_binding(plan_path, session_path, output)
                self.assertEqual(output.exists(), existing)
                self.assertFalse(list(root.glob(".missing-voice-binding-*")))

    def test_incomplete_review_and_tampered_bundle_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, session_path, _queue_id = (
                create_missing_voice_reuse_binding_review(root)
            )
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

    def test_blind_key_mutation_preserves_domain_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, session_path, _queue_id = self.create_failed_review(root)
            key_path = session_path.with_name(".blind-key.json")
            original_key = key_path.read_bytes()
            load_review = binding_module.load_missing_voice_reuse_review
            output = root / "binding"
            for replacement in ([], {"candidates": None}, {"candidates": [{}]}):

                def change_key(path):
                    review = load_review(path)
                    key_path.write_text(json.dumps(replacement), encoding="utf-8")
                    return review

                with self.subTest(key=replacement):
                    with patch.object(
                        binding_module, "load_missing_voice_reuse_review", change_key
                    ):
                        with self.assertRaisesRegex(
                            MissingVoiceReuseBindingError, "Missing-voice blind"
                        ):
                            publish_missing_voice_reuse_binding(
                                plan_path, session_path, output
                            )
                    self.assertFalse(output.exists())
                key_path.write_bytes(original_key)

    def test_binding_bundle_json_object_shape_is_required(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bundle.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(
                MissingVoiceReuseBindingError, "binding bundle.*JSON object"
            ):
                binding_module._validate_binding_bundle(root, {}, {})

    def test_existing_binding_rejects_non_integer_bundle_and_decision_versions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, session_path, queue_id = (
                create_missing_voice_reuse_binding_review(root)
            )
            bundle, _session = load_missing_voice_reuse_review(session_path)
            cohort = bundle["cohorts"][0]
            selected = cohort["complete_candidate_labels"][0]
            record_missing_voice_reuse_heard(
                session_path, cohort["cohort_id"], queue_id, selected
            )
            record_missing_voice_reuse_decision(
                session_path, cohort["cohort_id"], selected
            )
            output = publish_missing_voice_reuse_binding(
                plan_path, session_path, root / "binding"
            ).directory
            bundle_path = output / "bundle.json"
            decision_path = output / "decision.json"
            originals = {
                path: path.read_bytes() for path in (bundle_path, decision_path)
            }
            for path, field, message in (
                (bundle_path, "schema_version", "bundle identity is invalid"),
                (decision_path, "schema_version", "decision identity changed"),
            ):
                for version in (True, 1.0):
                    bundle_document = json.loads(originals[bundle_path].decode())
                    decision_document = json.loads(originals[decision_path].decode())
                    if path == bundle_path:
                        bundle_document[field] = version
                    else:
                        decision_document[field] = version
                        decision_document["decision_id"] = canonical_document_sha256(
                            {
                                key: value
                                for key, value in decision_document.items()
                                if key != "decision_id"
                            }
                        )
                        decision_path.write_text(
                            json.dumps(decision_document), encoding="utf-8"
                        )
                    for item in bundle_document["inventory"]:
                        if item["path"] == "decision.json":
                            item["sha256"] = sha256_file(decision_path)
                    bundle_document["bundle_id"] = canonical_document_sha256(
                        {
                            key: value
                            for key, value in bundle_document.items()
                            if key != "bundle_id"
                        }
                    )
                    bundle_path.write_text(
                        json.dumps(bundle_document), encoding="utf-8"
                    )
                    with (
                        self.subTest(path=path.name, version=version),
                        self.assertRaisesRegex(MissingVoiceReuseBindingError, message),
                    ):
                        publish_missing_voice_reuse_binding(
                            plan_path, session_path, output
                        )
                    for restore_path, payload in originals.items():
                        restore_path.write_bytes(payload)


if __name__ == "__main__":
    unittest.main()
