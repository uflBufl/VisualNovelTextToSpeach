import hashlib
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.authoring_fixtures import create_test_workspace, tree_hashes
from tests.symlink_support import symlink_or_skip
from vntts.authoring import voice_repair_comparison as comparison_module
from vntts.authoring.bulk_generation import _canonical_sha256
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.voice_repair_comparison import (
    VoiceRepairComparisonError,
    build_voice_repair_candidate_command,
    build_voice_repair_comparison_plan,
    load_voice_repair_comparison_plan,
    prepare_voice_repair_candidate_workspace,
    write_voice_repair_comparison_plan,
)


class AuthoringVoiceRepairComparisonTest(unittest.TestCase):
    def create_rejected_workspace(self, root):
        fixture, _imported, workspace = create_test_workspace(root)
        state_path = workspace.directory / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active"] = None
        result = state["items"][fixture["queue_id"]]
        result["status"] = "generated"
        result["review_status"] = "rejected"
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        return fixture, workspace.directory, state_path

    def test_captured_manifest_validates_original_reference_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            reference = root / "reference.wav"
            reference.write_bytes(b"original reference")
            manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Voice",
                                "speaker": "voice",
                                "references": ["reference.wav"],
                            },
                            {
                                "character": "Unused",
                                "speaker": "unused",
                                "references": ["missing.wav"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            plan = {
                "source": {
                    "voice_manifest_sha256": hashlib.sha256(
                        manifest.read_bytes()
                    ).hexdigest()
                }
            }
            document, entries = comparison_module._candidate_source_manifest(
                manifest, plan
            )
            self.assertEqual(document["voices"][0]["character"], "Voice")
            self.assertEqual(entries[0].references, ("reference.wav",))
            self.assertEqual(entries[1].references, ("missing.wav",))

            outside = root / "outside.wav"
            outside.write_bytes(b"external reference")
            symlink_or_skip(root / "missing.wav", outside)
            with self.assertRaisesRegex(VoiceRepairComparisonError, "symlink"):
                comparison_module._candidate_source_manifest(manifest, plan)

    def test_final_source_read_failure_preserves_cause_and_prevents_plan_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, workspace, _ = self.create_rejected_workspace(root)
            before = tree_hashes(root)
            output = root / "comparison.json"
            failure = PermissionError("Final repair source read was blocked")
            original_check = comparison_module._rehash_sources

            def fail_final_read(*arguments):
                with patch.object(
                    comparison_module, "sha256_file", side_effect=failure
                ):
                    return original_check(*arguments)

            with patch.object(
                comparison_module, "_rehash_sources", side_effect=fail_final_read
            ):
                with self.assertRaises(VoiceRepairComparisonError) as caught:
                    build_voice_repair_comparison_plan(workspace, "Rhiannon")
                self.assertIs(caught.exception.__cause__, failure)
                stdout, stderr = StringIO(), StringIO()
                with (
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as exited,
                ):
                    authoring_main(
                        [
                            "voice-repair-comparison-plan",
                            str(workspace),
                            "Rhiannon",
                            "--output",
                            str(output),
                        ]
                    )
                self.assertEqual(exited.exception.code, 2)
                self.assertIn(str(failure), stderr.getvalue())
                self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(output.exists())
            self.assertEqual(tree_hashes(root), before)

    def test_plan_binds_exact_unresolved_item_and_supported_profiles(self):
        with TemporaryDirectory() as directory:
            fixture, workspace, state_path = self.create_rejected_workspace(
                Path(directory)
            )
            state_before = state_path.read_bytes()

            first = build_voice_repair_comparison_plan(workspace, "Rhiannon")
            second = build_voice_repair_comparison_plan(workspace, "Rhiannon")
            state_after = state_path.read_bytes()

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.document["approved_count"], 0)
        self.assertEqual(first.document["target_count"], 1)
        self.assertEqual(
            first.document["comparison_sample_queue_ids"], [fixture["queue_id"]]
        )
        self.assertEqual(
            [value["generation_profile"] for value in first.document["candidates"]],
            ["stable", "natural"],
        )
        self.assertTrue(
            all(
                value["token_level_duration_control"] is False
                for value in first.document["candidates"]
            )
        )
        self.assertEqual(state_after, state_before)

    def test_approved_only_character_is_not_silently_repaired(self):
        with TemporaryDirectory() as directory:
            _fixture, _imported, workspace = create_test_workspace(Path(directory))
            state_path = workspace.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(VoiceRepairComparisonError, "no unresolved"):
                build_voice_repair_comparison_plan(workspace.directory, "Rhiannon")

    def test_changed_wav_and_unsafe_profiles_fail_closed(self):
        with TemporaryDirectory() as directory:
            fixture, workspace, _state_path = self.create_rejected_workspace(
                Path(directory)
            )
            wav = workspace / "generated-audio/audio/rhiannon/line.wav"
            wav.write_bytes(b"changed")
            with self.assertRaises(VoiceRepairComparisonError):
                build_voice_repair_comparison_plan(workspace, "Rhiannon")

            wav.write_bytes(fixture["wav"].read_bytes())
            with self.assertRaisesRegex(VoiceRepairComparisonError, "Unknown MOSS"):
                build_voice_repair_comparison_plan(
                    workspace, "Rhiannon", generation_profiles=("unsafe", "natural")
                )
            with self.assertRaisesRegex(VoiceRepairComparisonError, "duration control"):
                build_voice_repair_comparison_plan(
                    workspace,
                    "Rhiannon",
                    token_level_duration_control=True,
                )

    def test_publication_is_no_replace_and_tamper_evident(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, workspace, _state_path = self.create_rejected_workspace(root)
            plan = build_voice_repair_comparison_plan(workspace, "Rhiannon")
            output = root / "plan.json"

            write_voice_repair_comparison_plan(plan, output)
            loaded = load_voice_repair_comparison_plan(output)
            with self.assertRaisesRegex(VoiceRepairComparisonError, "output exists"):
                write_voice_repair_comparison_plan(plan, output)
            document = json.loads(output.read_text(encoding="utf-8"))
            document["targets"][0]["text"] = "forged"
            output.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(VoiceRepairComparisonError, "identity"):
                load_voice_repair_comparison_plan(output)

            document = plan.to_dict()
            document["targets"][0]["voice_binding_status"] = (
                "exact_reference_variant_unbound"
            )
            document["targets"][0]["voice_character"] = None
            document["comparison_ready_target_count"] = 0
            document["unbound_target_count"] = 1
            document["plan_id"] = _canonical_sha256(
                {key: value for key, value in document.items() if key != "plan_id"}
            )
            output.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(VoiceRepairComparisonError, "unbound"):
                load_voice_repair_comparison_plan(output)

        self.assertEqual(loaded.plan_id, plan.plan_id)

    def test_plan_rejects_non_integer_schema_and_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, workspace, _state_path = self.create_rejected_workspace(root)
            plan = build_voice_repair_comparison_plan(workspace, "Rhiannon")
            output = root / "plan.json"

            for field, value in (
                ("schema_version", True),
                ("schema_version", 1.0),
                *(
                    (field, float(plan.document[field]))
                    for field in (
                        "approved_count",
                        "target_count",
                        "comparison_ready_target_count",
                        "unbound_target_count",
                        "variant_count",
                        "candidate_count",
                        "comparison_sample_count",
                    )
                ),
            ):
                with self.subTest(field=field, value=value):
                    document = plan.to_dict()
                    document[field] = value
                    document["plan_id"] = _canonical_sha256(
                        {
                            key: item
                            for key, item in document.items()
                            if key != "plan_id"
                        }
                    )
                    output.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(
                        VoiceRepairComparisonError, "integer|schema"
                    ):
                        load_voice_repair_comparison_plan(output)

    def test_absent_target_does_not_require_review_projection(self):
        with TemporaryDirectory() as directory:
            _fixture, workspace, state_path = self.create_rejected_workspace(
                Path(directory)
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"] = {}
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            plan = build_voice_repair_comparison_plan(workspace, "Rhiannon")

        self.assertEqual(plan.document["target_count"], 1)
        self.assertEqual(plan.document["targets"][0]["status"], "absent")
        self.assertIsNone(plan.document["targets"][0]["audio_sha256"])

    def test_cli_publishes_without_mutating_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, workspace, state_path = self.create_rejected_workspace(root)
            output = root / "comparison.json"
            state_before = state_path.read_bytes()
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "voice-repair-comparison-plan",
                        str(workspace),
                        "Rhiannon",
                        "--output",
                        str(output),
                    ]
                )
            state_after = state_path.read_bytes()

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["target_count"], 1)
        self.assertEqual(state_after, state_before)

    def test_candidate_input_workspace_and_command_are_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, workspace_result = create_test_workspace(root / "seed")
            source_workspace = workspace_result.directory
            source_state = source_workspace / "generated-audio/generation-state.json"
            state = json.loads(source_state.read_text(encoding="utf-8"))
            state["active"] = None
            result = state["items"][fixture["queue_id"]]
            result["status"] = "generated"
            result["review_status"] = "rejected"
            source_state.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            source_before = source_state.read_bytes()
            plan = build_voice_repair_comparison_plan(source_workspace, "Rhiannon")
            candidate_id = plan.document["candidates"][1]["candidate_id"]

            first = prepare_voice_repair_candidate_workspace(
                plan,
                candidate_id,
                imported,
                root / "candidate-inputs",
                root / "candidate-workspaces",
            )
            second = prepare_voice_repair_candidate_workspace(
                plan,
                candidate_id,
                imported,
                root / "candidate-inputs",
                root / "candidate-workspaces",
            )
            candidate_state = (
                first.workspace_directory / "generated-audio/generation-state.json"
            )
            candidate_document = json.loads(candidate_state.read_text(encoding="utf-8"))
            candidate_document["active"] = None
            candidate_document["items"].pop(fixture["queue_id"], None)
            candidate_state.write_text(
                json.dumps(candidate_document, sort_keys=True), encoding="utf-8"
            )
            command = build_voice_repair_candidate_command(
                plan, candidate_id, first.workspace_directory
            )
            candidate_manifest = json.loads(
                (first.workspace_directory / "inputs/voice/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            source_after = source_state.read_bytes()

        self.assertTrue(first.input_created)
        self.assertTrue(first.workspace_created)
        self.assertFalse(second.input_created)
        self.assertFalse(second.workspace_created)
        self.assertEqual(first.input_directory, second.input_directory)
        self.assertEqual(first.workspace_directory, second.workspace_directory)
        self.assertEqual(source_before, source_after)
        self.assertEqual(
            candidate_manifest["vntts.authoring.voice_repair_comparison"][
                "candidate_id"
            ],
            candidate_id,
        )
        self.assertEqual(
            [
                command[index + 1]
                for index, value in enumerate(command)
                if value == "--queue-id"
            ],
            [fixture["queue_id"]],
        )

    def test_candidate_input_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, imported, workspace_result = create_test_workspace(root / "seed")
            source_state = (
                workspace_result.directory / "generated-audio/generation-state.json"
            )
            state = json.loads(source_state.read_text(encoding="utf-8"))
            state["active"] = None
            _queue_id, result = next(iter(state["items"].items()))
            result["status"] = "generated"
            result["review_status"] = "rejected"
            source_state.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            plan = build_voice_repair_comparison_plan(
                workspace_result.directory, "Rhiannon"
            )
            candidate_id = plan.document["candidates"][0]["candidate_id"]
            prepared = prepare_voice_repair_candidate_workspace(
                plan,
                candidate_id,
                imported,
                root / "candidate-inputs",
                root / "candidate-workspaces",
            )
            reference = next(
                path
                for path in prepared.input_directory.rglob("*")
                if path.is_file() and path.name not in {"manifest.json", "bundle.json"}
            )
            reference.write_bytes(b"changed")

            with self.assertRaisesRegex(VoiceRepairComparisonError, "changed"):
                prepare_voice_repair_candidate_workspace(
                    plan,
                    candidate_id,
                    imported,
                    root / "candidate-inputs",
                    root / "candidate-workspaces",
                )

    def test_candidate_json_encoding_and_shape_fail_as_domain_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, imported, workspace_result = create_test_workspace(root / "seed")
            source_state = (
                workspace_result.directory / "generated-audio/generation-state.json"
            )
            state = json.loads(source_state.read_text(encoding="utf-8"))
            state["active"] = None
            _queue_id, result = next(iter(state["items"].items()))
            result["status"] = "generated"
            result["review_status"] = "rejected"
            source_state.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            plan = build_voice_repair_comparison_plan(
                workspace_result.directory, "Rhiannon"
            )
            candidate_id = plan.document["candidates"][0]["candidate_id"]
            prepared = prepare_voice_repair_candidate_workspace(
                plan,
                candidate_id,
                imported,
                root / "candidate-inputs",
                root / "candidate-workspaces",
            )
            bundle_path = prepared.input_directory / "bundle.json"
            bundle = bundle_path.read_bytes()
            bundle_path.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                VoiceRepairComparisonError,
                "Unable to load voice repair candidate input",
            ):
                prepare_voice_repair_candidate_workspace(
                    plan,
                    candidate_id,
                    imported,
                    root / "candidate-inputs",
                    root / "candidate-workspaces",
                )
            bundle_path.write_bytes(bundle)
            for schema_version in (True, 1.0):
                bundle_document = json.loads(bundle)
                bundle_document["schema_version"] = schema_version
                bundle_document["bundle_id"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in bundle_document.items()
                        if key != "bundle_id"
                    }
                )
                bundle_path.write_text(json.dumps(bundle_document), encoding="utf-8")
                with self.assertRaisesRegex(
                    VoiceRepairComparisonError, "input conflicts"
                ):
                    prepare_voice_repair_candidate_workspace(
                        plan,
                        candidate_id,
                        imported,
                        root / "candidate-inputs",
                        root / "candidate-workspaces",
                    )
                bundle_path.write_bytes(bundle)

            manifest_path = prepared.input_directory / "manifest.json"
            manifest = manifest_path.read_bytes()
            for replacement, message in (
                (b"\xff", "Unable to load candidate voice manifest"),
                (b"[]", "Candidate manifest binding changed"),
            ):
                manifest_path.write_bytes(replacement)
                bundle_document = json.loads(bundle)
                manifest_entry = next(
                    item
                    for item in bundle_document["inventory"]
                    if item["path"] == "manifest.json"
                )
                manifest_entry["sha256"] = hashlib.sha256(replacement).hexdigest()
                bundle_document["bundle_id"] = _canonical_sha256(
                    {
                        key: value
                        for key, value in bundle_document.items()
                        if key != "bundle_id"
                    }
                )
                bundle_path.write_text(json.dumps(bundle_document), encoding="utf-8")
                with self.assertRaisesRegex(VoiceRepairComparisonError, message):
                    prepare_voice_repair_candidate_workspace(
                        plan,
                        candidate_id,
                        imported,
                        root / "candidate-inputs",
                        root / "candidate-workspaces",
                    )
                bundle_path.write_bytes(bundle)
            manifest_path.write_bytes(manifest)

    def test_candidate_input_symlink_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, imported, workspace_result = create_test_workspace(root / "seed")
            source_state = (
                workspace_result.directory / "generated-audio/generation-state.json"
            )
            state = json.loads(source_state.read_text(encoding="utf-8"))
            state["active"] = None
            _queue_id, result = next(iter(state["items"].items()))
            result["status"] = "generated"
            result["review_status"] = "rejected"
            source_state.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            plan = build_voice_repair_comparison_plan(
                workspace_result.directory, "Rhiannon"
            )
            candidate_id = plan.document["candidates"][0]["candidate_id"]
            prepared = prepare_voice_repair_candidate_workspace(
                plan,
                candidate_id,
                imported,
                root / "candidate-inputs",
                root / "candidate-workspaces",
            )
            reference = next(
                path
                for path in prepared.input_directory.rglob("*")
                if path.is_file() and path.name not in {"manifest.json", "bundle.json"}
            )
            reference.unlink()
            symlink_or_skip(reference, prepared.input_directory / "manifest.json")

            with self.assertRaisesRegex(VoiceRepairComparisonError, "symbolic link"):
                prepare_voice_repair_candidate_workspace(
                    plan,
                    candidate_id,
                    imported,
                    root / "candidate-inputs",
                    root / "candidate-workspaces",
                )

    def test_candidate_inventory_duplicate_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, imported, workspace_result = create_test_workspace(root / "seed")
            source_state = (
                workspace_result.directory / "generated-audio/generation-state.json"
            )
            state = json.loads(source_state.read_text(encoding="utf-8"))
            state["active"] = None
            _queue_id, result = next(iter(state["items"].items()))
            result["status"] = "generated"
            result["review_status"] = "rejected"
            source_state.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            plan = build_voice_repair_comparison_plan(
                workspace_result.directory, "Rhiannon"
            )
            candidate_id = plan.document["candidates"][0]["candidate_id"]
            prepared = prepare_voice_repair_candidate_workspace(
                plan,
                candidate_id,
                imported,
                root / "candidate-inputs",
                root / "candidate-workspaces",
            )
            bundle_path = prepared.input_directory / "bundle.json"
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle["inventory"].append(dict(bundle["inventory"][-1]))
            bundle["bundle_id"] = _canonical_sha256(
                {key: value for key, value in bundle.items() if key != "bundle_id"}
            )
            bundle_path.write_text(json.dumps(bundle, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(VoiceRepairComparisonError, "duplicate"):
                prepare_voice_repair_candidate_workspace(
                    plan,
                    candidate_id,
                    imported,
                    root / "candidate-inputs",
                    root / "candidate-workspaces",
                )


if __name__ == "__main__":
    unittest.main()
