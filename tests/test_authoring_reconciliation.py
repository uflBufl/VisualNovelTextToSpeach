import hashlib
import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)

import vntts.authoring.reconciliation as reconciliation_module
from tests.test_authoring_cohort_review import create_pending_cohort_workspace
from tests.test_authoring_legacy_import import write_legacy_fixture
from tests.test_authoring_source_reference_review import (
    publish_source_reference_quality_fixture,
)
from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.bulk_generation import authorize_live_fallback
from vntts.authoring.cohort_bundle import (
    build_cohort_review_bundle,
    execute_cohort_bundle_decision,
    write_cohort_review_bundle,
)
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.reconciliation import (
    AuthoringReconciliationError,
    build_authoring_reconciliation,
    load_authoring_reconciliation,
    write_authoring_reconciliation,
)
from vntts.authoring.reconciliation_cli import main as reconciliation_main
from vntts.authoring.reconciliation_merge import merge_reconciled_terminal_outcomes
from vntts.authoring.workbench import create_resume_workspace, generation_command


def _tree_hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class AuthoringReconciliationTest(unittest.TestCase):
    def create_fixture(self, root):
        authoring = root / "authoring"
        workspace, state, queue_id = create_pending_cohort_workspace(authoring)
        bundles = authoring / "review-bundles"
        bundles.mkdir()
        quality = authoring / "source-reference-quality-reviews"
        quality.mkdir()
        bundle = build_cohort_review_bundle([workspace])
        publication = bundles / "current.json"
        write_cohort_review_bundle(bundle, publication)
        return authoring, workspace, state, queue_id, bundles, quality, publication

    def create_parallel_fixture(self, root):
        _fixture, imported, primary = create_test_workspace(root)
        primary_directory = primary.directory
        secondary = create_resume_workspace(
            imported,
            root / "workspaces",
            story_index=primary_directory / "inputs/story-index.jsonl",
            voice_manifest=primary_directory / "inputs/voice/manifest.json",
            narrator_character="Rhiannon",
            backend="moss-tts",
            model="model with spaces",
            generation_profile="alternate",
        ).directory
        queue_id = None
        for workspace, profile in (
            (primary_directory, "stable"),
            (secondary, "alternate"),
        ):
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id, result = next(iter(state["items"].items()))
            result.update(
                {
                    "status": "generated",
                    "review_status": "pending_review",
                    "generation_profile": profile,
                    "voice_character": "Rhiannon",
                    "prompt_applied": False,
                    "synthesis_provenance_sha256": "b" * 64,
                }
            )
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        bundles = root / "review-bundles"
        bundles.mkdir()
        publication = bundles / "parallel.json"
        write_cohort_review_bundle(
            build_cohort_review_bundle((primary_directory, secondary)), publication
        )
        return primary_directory, secondary, queue_id, bundles, publication

    def decide_parallel_bundle(self, publication, decisions):
        bundle = reconciliation_module.validate_cohort_review_bundle_document(
            json.loads(publication.read_text(encoding="utf-8"))
        )
        for workspace_id, decision in decisions:
            cohort = next(
                value
                for value in bundle.document["cohorts"]
                if value["workspace_id"] == workspace_id
            )
            projection = execute_cohort_bundle_decision(
                bundle,
                workspace_id,
                cohort["cohort_id"],
                decision,
                reviewed_queue_ids=[cohort["samples"][0]["queue_id"]],
            )
            bundle = projection.next_bundle

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
            workspace, _state, queue_id = create_pending_cohort_workspace(authoring)
            bundles = authoring / "review-bundles"
            bundles.mkdir()
            (authoring / "source-reference-quality-reviews").mkdir()

            report = build_authoring_reconciliation(workspace, bundles)

        self.assertEqual(report.document["actions"][0]["queue_id"], queue_id)
        self.assertEqual(
            report.document["actions"][0]["action"], "review_plan_required"
        )

    def test_partial_manifest_reports_exact_covered_pending_item_as_ready(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            original = VoiceGenerationQueue.load(fixture["queue"])
            covered = original.items[0]
            missing_text = "An uncovered speaker remains outside this selection."
            missing_hash = text_sha256(missing_text)
            missing_line = "reverse1999:missing:1"
            missing_queue_id = expected_voice_generation_queue_id(
                missing_line, missing_hash
            )
            write_voice_generation_queue(
                fixture["queue"],
                original.metadata,
                (
                    covered.document,
                    {
                        "record_type": "generation_item",
                        "queue_id": missing_queue_id,
                        "line_id": missing_line,
                        "text_sha256": missing_hash,
                        "text": missing_text,
                        "speaker": "Uncovered",
                        "voice_character": "Uncovered",
                        "action": "generate",
                        "state": "pending",
                    },
                ),
            )
            queue_sha256 = sha256_file(fixture["queue"])
            output = Path(fixture["job"]["output"])
            state_path = output / "generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update({"queue_sha256": queue_sha256, "active": None, "items": {}})
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_queue_sha256"] = queue_sha256
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True), encoding="utf-8"
            )
            write_story_index_document(
                fixture["job"]["story_index"],
                {
                    "game": "Reverse: 1999",
                    "language": "en",
                    "generated_at": "2026-08-16T15:00:00+00:00",
                    "collections": [
                        {
                            "collection_id": "story",
                            "title": "Partial voice coverage",
                            "kind": "story",
                            "order": 1,
                        }
                    ],
                },
                (
                    {
                        "record_type": "line",
                        "line_id": covered.line_id,
                        "text_sha256": covered.text_sha256,
                        "text": covered.text,
                        "speaker": covered.speaker,
                        "voice_character": covered.voice_character,
                        "kind": "dialogue",
                        "chapter": "story",
                        "sequence": 1,
                        "collection_id": "story",
                        "source_audio_status": "absent",
                        "source_kind": "story",
                    },
                    {
                        "record_type": "line",
                        "line_id": missing_line,
                        "text_sha256": missing_hash,
                        "text": missing_text,
                        "speaker": "Uncovered",
                        "voice_character": "Uncovered",
                        "kind": "dialogue",
                        "chapter": "story",
                        "sequence": 2,
                        "collection_id": "story",
                        "source_audio_status": "absent",
                        "source_kind": "story",
                    },
                ),
            )
            reference = root / "legacy/rhiannon.wav"
            reference.write_bytes(b"voice-reference")
            Path(fixture["job"]["voice_manifest"]).write_text(
                json.dumps(
                    {
                        "version": 2,
                        "game": "Reverse: 1999",
                        "language": "en",
                        "voices": [
                            {
                                "character": covered.voice_character,
                                "speaker": covered.speaker,
                                "reference": "rhiannon.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            imported = import_legacy_job(
                fixture["job_directory"], root / "authoring/imports"
            ).destination
            workspace = create_resume_workspace(
                imported,
                root / "authoring/workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="moss-v1.5",
                generation_profile="stable",
                narrator_character=covered.voice_character,
            ).directory
            bundles = root / "authoring/review-bundles"
            bundles.mkdir()

            report = build_authoring_reconciliation(workspace, bundles)
            actions = {item["queue_id"]: item for item in report.document["actions"]}
            command = generation_command(
                workspace,
                queue_ids=(covered.queue_id,),
                retries=0,
            )

        self.assertEqual(
            actions[covered.queue_id]["action"], "generation_ready_unselected"
        )
        self.assertEqual(
            actions[missing_queue_id]["action"],
            "source_reference_or_explicit_fallback",
        )
        self.assertEqual(command[command.index("--queue-id") + 1], covered.queue_id)

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

    def test_explicit_bundle_selection_ignores_unselected_duplicate(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _authoring,
                workspace,
                _state,
                queue_id,
                bundles,
                _quality,
                publication,
            ) = self.create_fixture(root)
            (bundles / "superseded.json").write_bytes(publication.read_bytes())

            report = build_authoring_reconciliation(
                workspace,
                bundles,
                bundle_publications=(publication,),
            )

        self.assertEqual(report.document["summary"]["bundle_count"], 1)
        self.assertEqual(report.document["actions"][0]["queue_id"], queue_id)
        self.assertEqual(
            Path(report.document["review_bundles"][0]["publication"]),
            publication.resolve(),
        )

    def test_explicit_bundle_selection_rejects_selected_non_bundle(self):
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
            selected = bundles / "not-a-bundle.json"
            selected.write_text('{"schema":"different"}', encoding="utf-8")

            with self.assertRaisesRegex(
                AuthoringReconciliationError,
                "Unsupported selected review bundle",
            ):
                build_authoring_reconciliation(
                    workspace,
                    bundles,
                    bundle_publications=(selected,),
                )

    def test_completed_secondary_terminal_conflict_uses_original_bundle_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, queue_id, bundles, publication = (
                self.create_parallel_fixture(root)
            )
            self.decide_parallel_bundle(
                publication,
                ((primary.name, "accepted"), (secondary.name, "rejected")),
            )

            report = build_authoring_reconciliation(primary, bundles)

            self.assertEqual(
                reconciliation_module._validated_report(report), report.document
            )

        secondary_report = next(
            value
            for value in report.document["workspaces"]
            if value["workspace_id"] == secondary.name
        )
        self.assertEqual(secondary_report["report_scope"], "original_bundle_items_only")
        self.assertEqual(secondary_report["reported_queue_item_count"], 1)
        self.assertEqual(secondary_report["terminal_counts"], {"rejected": 1})
        self.assertEqual(report.document["summary"]["terminal_conflict_count"], 1)
        conflict = report.document["terminal_conflicts"][0]
        self.assertEqual(conflict["queue_id"], queue_id)
        self.assertEqual(
            {value["authority"] for value in conflict["occurrences"]},
            {"approved", "rejected"},
        )
        self.assertEqual(
            len({value["queue_record_sha256"] for value in conflict["occurrences"]}),
            1,
        )

    def test_single_secondary_terminal_outcome_replaces_primary_failure_action(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, imported, primary = create_test_workspace(root)
            secondary = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=primary.directory / "inputs/story-index.jsonl",
                voice_manifest=primary.directory / "inputs/voice/manifest.json",
                narrator_character="Rhiannon",
                backend="moss-tts",
                model="model with spaces",
                generation_profile="terminal-source",
            ).directory
            primary_state_path = (
                primary.directory / "generated-audio/generation-state.json"
            )
            primary_state = json.loads(primary_state_path.read_text(encoding="utf-8"))
            queue_id = next(iter(primary_state["items"]))
            primary_state["active"] = None
            primary_state["items"][queue_id] = {
                "status": "failed",
                "attempts": 1,
                "seed": 0,
                "last_error": "bounded primary failure",
                "updated_at": "2026-08-27T00:00:00+00:00",
            }
            primary_state_path.write_text(
                json.dumps(primary_state, sort_keys=True), encoding="utf-8"
            )
            secondary_state_path = secondary / "generated-audio/generation-state.json"
            secondary_state = json.loads(
                secondary_state_path.read_text(encoding="utf-8")
            )
            secondary_state["active"] = None
            secondary_state["items"][queue_id].update(
                {
                    "status": "generated",
                    "review_status": "pending_review",
                    "generation_profile": "terminal-source",
                    "voice_character": "Rhiannon",
                    "prompt_applied": False,
                    "synthesis_provenance_sha256": "b" * 64,
                }
            )
            secondary_state_path.write_text(
                json.dumps(secondary_state, sort_keys=True), encoding="utf-8"
            )
            bundles = root / "review-bundles"
            bundles.mkdir()
            publication = bundles / "secondary.json"
            write_cohort_review_bundle(
                build_cohort_review_bundle((secondary,)), publication
            )
            self.decide_parallel_bundle(publication, ((secondary.name, "accepted"),))

            report = build_authoring_reconciliation(primary.directory, bundles)

        action = next(
            value
            for value in report.document["actions"]
            if value["queue_id"] == queue_id
        )
        self.assertEqual(action["action"], "terminal_merge_required")
        self.assertEqual(action["terminal_source"]["workspace_id"], secondary.name)
        self.assertEqual(action["terminal_source"]["authority"], "approved")
        self.assertRegex(
            action["terminal_source"]["state_item_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual(
            report.document["summary"]["action_counts"],
            {"terminal_merge_required": 1},
        )
        primary_report = next(
            value
            for value in report.document["workspaces"]
            if value["workspace_id"] == primary.directory.name
        )
        self.assertEqual(
            primary_report["action_counts"], {"terminal_merge_required": 1}
        )
        self.assertEqual(report.document["summary"]["terminal_conflict_count"], 0)
        tampered = deepcopy(report.document)
        tampered["actions"][0]["terminal_source"].pop("state_item_sha256")
        tampered["report_id"] = reconciliation_module.canonical_document_sha256(
            {key: value for key, value in tampered.items() if key != "report_id"}
        )
        with self.assertRaisesRegex(
            AuthoringReconciliationError, "missing required fields"
        ):
            reconciliation_module._validated_report(tampered)

    def test_single_secondary_terminal_outcome_replaces_primary_review_action(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, queue_id, bundles, publication = (
                self.create_parallel_fixture(root)
            )
            self.decide_parallel_bundle(
                publication,
                ((secondary.name, "accepted"),),
            )

            report = build_authoring_reconciliation(primary, bundles)
            report_path = root / "pending-terminal-report.json"
            write_authoring_reconciliation(report, report_path)
            merged = merge_reconciled_terminal_outcomes(
                primary,
                report_path,
                root / "workspaces",
            )
            merged_state = json.loads(
                (merged.directory / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )

        action = next(
            value
            for value in report.document["actions"]
            if value["workspace_id"] == primary.name and value["queue_id"] == queue_id
        )
        self.assertEqual(action["action"], "terminal_merge_required")
        self.assertEqual(action["terminal_source"]["workspace_id"], secondary.name)
        self.assertEqual(action["terminal_source"]["authority"], "approved")
        self.assertEqual(report.document["summary"]["terminal_conflict_count"], 0)
        self.assertEqual(
            (
                merged_state["items"][queue_id]["status"],
                merged_state["items"][queue_id]["review_status"],
            ),
            ("approved", "approved"),
        )

    def test_completed_scoped_nonspoken_bundle_projects_terminal_merge(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, queue_id, _bundles, _publication = (
                self.create_parallel_fixture(root)
            )
            bundles = root / "nonspoken-bundles"
            bundles.mkdir()
            publication = bundles / "secondary.json"
            write_cohort_review_bundle(
                build_cohort_review_bundle((secondary,)), publication
            )
            self.decide_parallel_bundle(
                publication,
                ((secondary.name, "accepted"),),
            )

            with patch.object(
                reconciliation_module,
                "is_spoken_queue_item",
                return_value=False,
            ):
                report = build_authoring_reconciliation(primary, bundles)
                validated = reconciliation_module._validated_report(report)

        action = next(
            value
            for value in validated["actions"]
            if value["workspace_id"] == primary.name and value["queue_id"] == queue_id
        )
        secondary_report = next(
            value
            for value in validated["workspaces"]
            if value["workspace_id"] == secondary.name
        )
        self.assertEqual(action["action"], "terminal_merge_required")
        self.assertEqual(action["terminal_source"]["workspace_id"], secondary.name)
        self.assertEqual(secondary_report["reported_queue_item_count"], 1)
        self.assertEqual(secondary_report["terminal_counts"], {"approved": 1})

    def test_explicit_fallback_conflicts_with_parallel_approval(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, secondary, queue_id, bundles, publication = (
                self.create_parallel_fixture(root)
            )
            self.decide_parallel_bundle(
                publication,
                ((primary.name, "accepted"), (secondary.name, "rejected")),
            )
            authorize_live_fallback(
                secondary / "generated-audio/generation-state.json",
                secondary / "queue.jsonl",
                queue_id,
                reason="generated_audio_rejected",
                model="pocket-tts",
            )

            report = build_authoring_reconciliation(primary, bundles)

        self.assertEqual(report.document["summary"]["terminal_conflict_count"], 1)
        self.assertEqual(
            {
                value["authority"]
                for value in report.document["terminal_conflicts"][0]["occurrences"]
            },
            {"approved", "explicit_fallback"},
        )

    def test_changed_queue_record_is_a_conflict_even_without_terminal_disagreement(
        self,
    ):
        base = {
            "workspace_id": "resume-" + "a" * 24 + "-" + "b" * 16,
            "authority": "approved",
            "line_id": "line-1",
            "text_sha256": "c" * 64,
            "queue_record_sha256": "d" * 64,
        }
        changed = {
            **base,
            "workspace_id": "resume-" + "e" * 24 + "-" + "f" * 16,
            "queue_record_sha256": "0" * 64,
        }

        conflicts = reconciliation_module._terminal_conflicts(
            {"queue-1": [base, changed]}
        )

        self.assertEqual(len(conflicts), 1)
        self.assertIn("different queue records", conflicts[0]["reason"])

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
                publish_source_reference_quality_fixture(quality_root)
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

    def test_final_snapshot_symlink_substitution_fails_closed(self):
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

            def substitute_then_check(snapshots):
                replacement = root / "replacement-state.json"
                replacement.write_bytes(state.read_bytes())
                state.unlink()
                state.symlink_to(replacement)
                original(snapshots)

            with (
                patch.object(
                    reconciliation_module,
                    "_assert_snapshots_unchanged",
                    side_effect=substitute_then_check,
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
                        "--bundle",
                        str(_publication),
                    ]
                )

        self.assertEqual(loaded, report)
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), report.document)

    def test_self_consistent_incomplete_report_is_rejected(self):
        document = {
            "schema": reconciliation_module.AUTHORING_RECONCILIATION_SCHEMA,
            "schema_version": reconciliation_module.AUTHORING_RECONCILIATION_VERSION,
        }
        document["report_id"] = reconciliation_module.canonical_document_sha256(
            document
        )

        with self.assertRaisesRegex(
            AuthoringReconciliationError, "missing required fields"
        ):
            reconciliation_module._validated_report(document)

    def test_report_count_tamper_is_rejected_even_with_recomputed_id(self):
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
            document = deepcopy(report.document)
            document["summary"]["nonterminal_action_count"] += 1
            document["report_id"] = reconciliation_module.canonical_document_sha256(
                {key: value for key, value in document.items() if key != "report_id"}
            )

            with self.assertRaisesRegex(
                AuthoringReconciliationError, "nonterminal_action_count is inconsistent"
            ):
                reconciliation_module._validated_report(document)

    def test_legacy_current_bundle_scope_report_remains_loadable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            primary, _secondary, _queue_id, bundles, _publication = (
                self.create_parallel_fixture(root)
            )
            report = build_authoring_reconciliation(primary, bundles)
            document = deepcopy(report.document)
            secondary = next(
                value
                for value in document["workspaces"]
                if value["workspace_id"] != primary.name
            )
            secondary["report_scope"] = "current_bundle_items_only"
            document["report_id"] = reconciliation_module.canonical_document_sha256(
                {key: value for key, value in document.items() if key != "report_id"}
            )

            validated = reconciliation_module._validated_report(document)

        self.assertEqual(
            next(
                value
                for value in validated["workspaces"]
                if value["workspace_id"] != primary.name
            )["report_scope"],
            "current_bundle_items_only",
        )


if __name__ == "__main__":
    unittest.main()
