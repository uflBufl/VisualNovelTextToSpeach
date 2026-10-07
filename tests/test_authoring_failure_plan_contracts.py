import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

import tests.test_authoring_specialist_failure_plan as specialist_fixture
import vntts.authoring.failure_regeneration as regeneration
import vntts.authoring.specialist_failure_plan as specialist
from tests.bulk_generation_fixtures import queue_item, write_queue
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.bulk_generation import LEGACY_STATE_SCHEMA
from vntts.authoring.cohort_review import CohortReviewError


class FailurePlanContractsTest(unittest.TestCase):
    def specialist_fixture(self, root):
        return specialist_fixture.SpecialistFailurePlanTest().create_workspace(
            root, "sentence_boundary_segmentation", "a"
        )

    def rewrite_plan(self, path, document):
        document["plan_id"] = canonical_document_sha256(
            {k: v for k, v in document.items() if k != "plan_id"}
        )
        path.write_text(json.dumps(document))

    def test_checksum_valid_specialist_summaries_must_match_items(self):
        for field in ("actions", "clusters", "sources", "items"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = self.specialist_fixture(root)
                document = specialist.build_specialist_failure_plan(
                    (workspace,)
                ).document
                if field == "actions":
                    document["action_counts"] = {specialist.REFERENCE_OR_LIVE: 1}
                elif field == "clusters":
                    document["clusters"][0]["queue_ids"] = ["not-selected"]
                elif field == "sources":
                    document["sources"][0]["workspace"] = "different-source"
                else:
                    document["items"][0] = None
                path = root / "plan.json"
                self.rewrite_plan(path, document)
                with self.assertRaises(CohortReviewError):
                    specialist.load_specialist_failure_plan(path)

    def test_duplicate_queue_records_are_not_silently_replaced(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.specialist_fixture(root)
            path = workspace / "queue.jsonl"
            rows = path.read_text().splitlines()
            path.write_text("\n".join([*rows, rows[-1]]) + "\n")
            with self.assertRaises(CohortReviewError):
                specialist.build_specialist_failure_plan((workspace,))

    def test_selected_queue_text_is_not_coerced_to_a_string(self):
        for text in (123, [], None):
            with self.subTest(text=text), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = self.specialist_fixture(root)
                path = workspace / "queue.jsonl"
                rows = [json.loads(s) for s in path.read_text().splitlines()]
                rows[-1]["text"] = text
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                with self.assertRaises(CohortReviewError):
                    specialist.build_specialist_failure_plan((workspace,))

    def test_malformed_action_evidence_raises_domain_error(self):
        for field in ("provider", "strategy"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = self.specialist_fixture(root)
                path = workspace / "generated-audio/generation-state.json"
                doc = json.loads(path.read_text())
                if field == "provider":
                    doc["items"]["a"]["failure_repair"]["strategy"] = (
                        specialist.INLINE_PAUSE_MARKER
                    )
                    doc["items"]["a"]["failure"]["kind"] = "speech_silence"
                    doc["items"]["a"]["provider"] = []
                else:
                    doc["items"]["a"]["failure_repair"]["strategy"] = []
                path.write_text(json.dumps(doc))
                with self.assertRaises(CohortReviewError):
                    specialist.build_specialist_failure_plan((workspace,))

    def test_later_workspace_does_not_hide_earlier_source_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.specialist_fixture(root)
            second = specialist_fixture.SpecialistFailurePlanTest().create_workspace(
                root, "sentence_boundary_segmentation", "b"
            )
            original = specialist._next_action
            calls = [0]

            def mutate(*args):
                result = original(*args)
                calls[0] += 1
                if calls[0] == 2:
                    path = first / "workspace.json"
                    doc = json.loads(path.read_text())
                    doc["context"] = doc.get("context", 0) + 1
                    path.write_text(json.dumps(doc))
                return result

            with patch.object(specialist, "_next_action", side_effect=mutate):
                with self.assertRaises(CohortReviewError):
                    specialist.build_specialist_failure_plan((first, second))

    def regeneration_fixture(self, root):
        workspace = {"workspace_id": "fixture", "config_fingerprint": "a" * 64}
        (root / "workspace.json").write_text(json.dumps(workspace))
        item = queue_item("legacy")
        queue = write_queue(root / "queue.jsonl", [item])
        generated = root / "generated-audio"
        generated.mkdir()
        state = generated / "generation-state.json"
        state.write_text(
            json.dumps(
                {
                    "schema": LEGACY_STATE_SCHEMA,
                    "schema_version": 1,
                    "queue_sha256": sha256_file(queue),
                    "game": None,
                    "language": None,
                    "active": None,
                    "items": {
                        item["queue_id"]: {
                            "status": "failed",
                            "attempts": 7,
                            "seed": 6,
                            "last_error": "Legacy limit",
                            "updated_at": "2026-08-16T10:00:00+00:00",
                        }
                    },
                }
            )
        )
        return workspace, item, state

    def test_repair_and_regeneration_state_must_be_the_same_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, item, state = self.regeneration_fixture(root)
            original = regeneration.generation_failure_repair_plan

            def mutate(*args):
                result = original(*args)
                doc = json.loads(state.read_text())
                doc["items"][item["queue_id"]]["attempts"] = 8
                state.write_text(json.dumps(doc))
                return result

            with (
                patch.object(
                    regeneration,
                    "load_workspace_authority",
                    return_value=(
                        root,
                        workspace,
                        sha256_file(root / "workspace.json"),
                    ),
                ),
                patch.object(
                    regeneration, "generation_failure_repair_plan", side_effect=mutate
                ),
            ):
                with self.assertRaises(regeneration.FailureRegenerationError):
                    regeneration.build_failure_regeneration_plan(root)

    def test_valid_legacy_regeneration_plan_preserves_raw_item_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, item, state = self.regeneration_fixture(root)
            with patch.object(
                regeneration,
                "load_workspace_authority",
                return_value=(root, workspace, sha256_file(root / "workspace.json")),
            ):
                plan = regeneration.build_failure_regeneration_plan(root)
            self.assertEqual(plan.document["failure_count"], 1)
            self.assertEqual(plan.document["state_sha256"], sha256_file(state))
            self.assertEqual(
                plan.document["records"][0]["item_sha256"],
                canonical_document_sha256(
                    json.loads(state.read_text())["items"][item["queue_id"]]
                ),
            )

    def test_invalid_selected_result_and_negative_attempts_raise_domain_errors(self):
        for kind in ("result", "attempts", "provider_attempts", "workspace_id"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = self.specialist_fixture(root)
                path = workspace / "generated-audio/generation-state.json"
                doc = json.loads(path.read_text())
                if kind == "result":
                    doc["items"]["a"] = []
                elif kind == "attempts":
                    doc["items"]["a"]["attempts"] = -1
                elif kind == "provider_attempts":
                    doc["items"]["a"]["attempts_by_provider"]["pocket-tts"] = -1
                else:
                    config_path = workspace / "workspace.json"
                    config = json.loads(config_path.read_text())
                    config["workspace_id"] = None
                    config_path.write_text(json.dumps(config))
                path.write_text(json.dumps(doc))
                with self.assertRaises(CohortReviewError):
                    specialist.build_specialist_failure_plan((workspace,))
