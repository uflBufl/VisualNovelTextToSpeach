import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.story_index_snapshot as story_snapshot_module
from tests.authoring_fixtures import tree_hashes
from tests.missing_voice_reuse_fixtures import (
    build_failed_missing_voice_reuse_plan_fixture,
    build_missing_voice_reuse_plan_fixture,
    create_missing_voice_reuse_workspace,
)
from vntts.authoring import missing_voice_reuse as reuse_module
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.missing_voice_reuse import (
    MissingVoiceReuseError,
    _inline_pause_candidates,
    _replaceable_predecessor_reuse_binding,
    build_missing_voice_reuse_candidate_command,
    build_missing_voice_reuse_plan,
    load_missing_voice_reuse_plan,
    parse_cohort_arguments,
    prepare_missing_voice_reuse_candidate_workspace,
    write_missing_voice_reuse_plan,
)
from vntts.authoring.workbench import (
    inspect_generation_readiness,
)


class AuthoringMissingVoiceReuseTest(unittest.TestCase):
    def test_candidate_manifest_change_before_publication_cleans_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, workspace = self.create_workspace(root)
            plan = self.build_plan(workspace)
            candidate = plan.document["candidates"][0]
            source = (
                Path(plan.document["source"]["workspace"])
                / "inputs/voice/manifest.json"
            )
            original_copy = reuse_module._copy_candidate_references

            def change_source_after_copy(*arguments):
                inventory = original_copy(*arguments)
                document = json.loads(source.read_text(encoding="utf-8"))
                document["unreviewed_metadata"] = True
                source.write_text(json.dumps(document), encoding="utf-8")
                return inventory

            with (
                patch.object(
                    reuse_module,
                    "_copy_candidate_references",
                    side_effect=change_source_after_copy,
                ),
                self.assertRaisesRegex(MissingVoiceReuseError, "manifest changed"),
            ):
                reuse_module._publish_candidate_input(
                    plan.document,
                    candidate,
                    source.parents[2],
                    root / "candidate-inputs",
                )
            self.assertEqual(list((root / "candidate-inputs").iterdir()), [])

    def create_workspace(self, root, *, text=None, missing_voice_policy=None):
        return create_missing_voice_reuse_workspace(
            root, text=text, missing_voice_policy=missing_voice_policy
        )

    def build_plan(self, workspace):
        return build_missing_voice_reuse_plan_fixture(workspace)

    def build_failed_plan(self, fixture, workspace):
        return build_failed_missing_voice_reuse_plan_fixture(fixture, workspace)

    def test_final_source_read_failure_preserves_cause_and_prevents_plan_output(self):
        for kind in ("document", "reference"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                _, _, workspace = self.create_workspace(root)
                before = tree_hashes(root)
                output = root / "plan.json"
                failure = PermissionError("Final reuse source read was blocked")
                original_check = reuse_module._assert_sources_unchanged
                original_hash = reuse_module.sha256_file

                def fail_final_read(*arguments):
                    target = arguments[0] / "workspace.json"
                    if kind == "reference":
                        target = (
                            arguments[0]
                            / "inputs/voice"
                            / arguments[-1][0]["ordered_references"][0]["path"]
                        )

                    def rehash(path):
                        if path == target:
                            raise failure
                        return original_hash(path)

                    with patch.object(reuse_module, "sha256_file", side_effect=rehash):
                        return original_check(*arguments)

                with patch.object(
                    reuse_module,
                    "_assert_sources_unchanged",
                    side_effect=fail_final_read,
                ):
                    with self.assertRaises(MissingVoiceReuseError) as caught:
                        self.build_plan(workspace)
                    self.assertIs(caught.exception.__cause__, failure)
                    stdout, stderr = StringIO(), StringIO()
                    with (
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                        self.assertRaises(SystemExit) as exited,
                    ):
                        authoring_main(
                            [
                                "missing-voice-reuse-plan",
                                str(workspace),
                                "Aderyn",
                                "--cohort",
                                "adult family=314601.png",
                                "--candidate-voice",
                                "Adult Aderyn",
                                "--candidate-voice",
                                "Centurion",
                                "--output",
                                str(output),
                            ]
                        )
                    self.assertEqual(exited.exception.code, 2)
                    self.assertIn(str(failure), stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), "")
                self.assertFalse(output.exists())
                self.assertEqual(tree_hashes(root), before)

    def test_plan_is_exact_small_and_does_not_mutate_workspace(self):
        with TemporaryDirectory() as directory:
            fixture, _imported, workspace = self.create_workspace(Path(directory))
            state = workspace / "generated-audio/generation-state.json"
            before = state.read_bytes()

            first = self.build_plan(workspace)
            second = self.build_plan(workspace)
            after = state.read_bytes()

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(after, before)
        self.assertEqual(first.document["target_count"], 1)
        self.assertEqual(first.document["comparison_sample_count"], 1)
        self.assertEqual(
            first.document["comparison_sample_queue_ids"], [fixture["queue_id"]]
        )
        self.assertEqual(first.document["targets"][0]["portrait"], "314601.png")
        self.assertEqual(
            [
                candidate["voice_character"]
                for candidate in first.document["candidates"]
            ],
            ["Adult Aderyn", "Centurion"],
        )

    def test_plan_uses_captured_story_bytes_during_parser_replacement(self):
        import hashlib

        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, workspace = self.create_workspace(root)
            story = workspace / "inputs/story-index.jsonl"
            original = story.read_bytes()
            records = [json.loads(line) for line in original.decode().splitlines()]
            records[1]["text"] = "Transient replacement."
            records[1]["text_sha256"] = hashlib.sha256(
                records[1]["text"].encode("utf-8")
            ).hexdigest()
            replacement = "\n".join(json.dumps(record) for record in records).encode()
            original_parser = story_snapshot_module.load_story_index_document

            def parse_during_replacement(path):
                story.write_bytes(replacement)
                try:
                    return original_parser(path)
                finally:
                    story.write_bytes(original)

            with (
                patch.object(
                    reuse_module,
                    "load_story_index_document",
                    side_effect=parse_during_replacement,
                    create=True,
                ),
                patch.object(
                    story_snapshot_module,
                    "load_story_index_document",
                    side_effect=parse_during_replacement,
                ),
            ):
                snapshot = reuse_module._load_plan_source(workspace)

            self.assertEqual(story.read_bytes(), original)
            self.assertEqual(snapshot.story, original_parser(story))
            self.assertEqual(snapshot.story.path, story.resolve())
            self.assertEqual(
                snapshot.story_sha256, hashlib.sha256(original).hexdigest()
            )

    def test_publication_is_no_replace_and_tamper_evident(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, workspace = self.create_workspace(root)
            plan = self.build_plan(workspace)
            output = root / "plan.json"

            write_missing_voice_reuse_plan(plan, output)
            self.assertEqual(
                load_missing_voice_reuse_plan(output).plan_id, plan.plan_id
            )
            with self.assertRaisesRegex(MissingVoiceReuseError, "output exists"):
                write_missing_voice_reuse_plan(plan, output)
            document = json.loads(output.read_text(encoding="utf-8"))
            document["targets"][0]["portrait"] = "forged.png"
            output.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(MissingVoiceReuseError, "identity"):
                load_missing_voice_reuse_plan(output)

    def test_only_zero_override_neither_predecessor_can_be_layered(self):
        predecessor = {
            "schema": "vntts.authoring-missing-voice-reuse-binding",
            "schema_version": 2,
            "mode": "approved_cohort_reuse",
            "selected_candidates": [],
            "queue_voice_overrides": {},
            "queue_voice_overrides_sha256": (
                "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
            ),
            "authority": (
                "Exact cohort reuse binding. Candidate choices require human review; "
                "cohorts with no selectable candidate are deterministically unresolved. "
                "Neither decisions bind no voice."
            ),
            "decisions": [{"decision": "neither"}],
        }

        self.assertTrue(
            _replaceable_predecessor_reuse_binding(
                {"vntts.authoring.missing_voice_reuse": predecessor}
            )
        )
        for mutation in (
            {"selected_candidates": [{"candidate_id": "selected"}]},
            {"queue_voice_overrides": {"queue": "Rhiannon"}},
            {"decisions": [{"decision": "candidate"}]},
        ):
            blocked = {**predecessor, **mutation}
            self.assertFalse(
                _replaceable_predecessor_reuse_binding(
                    {"vntts.authoring.missing_voice_reuse": blocked}
                )
            )

    def test_inline_pause_plan_rejects_multiple_samples_before_publication(self):
        candidates = [{"candidate_id": "ignored", "voice_character": "Rhiannon"}]
        targets = [
            {
                "queue_id": queue_id,
                "text": "One sentence. Another sentence.",
                "text_sha256": digest,
            }
            for queue_id, digest in (("queue:1", "1" * 64), ("queue:2", "2" * 64))
        ]

        with self.assertRaisesRegex(MissingVoiceReuseError, "exactly one"):
            _inline_pause_candidates(
                candidates,
                [{"queue_id": "queue:1"}, {"queue_id": "queue:2"}],
                targets,
                180,
            )

    def test_exact_cohorts_candidates_and_retired_voices_fail_closed(self):
        with TemporaryDirectory() as directory:
            _fixture, _imported, workspace = self.create_workspace(Path(directory))
            with self.assertRaisesRegex(MissingVoiceReuseError, "outside"):
                build_missing_voice_reuse_plan(
                    workspace,
                    "Aderyn",
                    cohorts={"wrong": ("533704.png",)},
                    candidate_voice_characters=("Adult Aderyn", "Centurion"),
                )
            with self.assertRaisesRegex(MissingVoiceReuseError, "at least two"):
                build_missing_voice_reuse_plan(
                    workspace,
                    "Aderyn",
                    cohorts={"adult": ("314601.png",)},
                    candidate_voice_characters=("Adult Aderyn",),
                )
            with patch(
                "vntts.authoring.missing_voice_reuse."
                "retired_source_reference_variants_from_manifest",
                return_value=({"voice_character": "Adult Aderyn"},),
            ):
                with self.assertRaisesRegex(MissingVoiceReuseError, "Retired"):
                    self.build_plan(workspace)

    def test_cli_parses_exact_cohorts_and_publishes_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, workspace = self.create_workspace(root)
            output = root / "plan.json"
            stdout = StringIO()

            with redirect_stdout(stdout):
                exit_code = authoring_main(
                    [
                        "missing-voice-reuse-plan",
                        str(workspace),
                        "Aderyn",
                        "--cohort",
                        "adult family=314601.png",
                        "--candidate-voice",
                        "Adult Aderyn",
                        "--candidate-voice",
                        "Centurion",
                        "--output",
                        str(output),
                    ]
                )
            published = output.is_file()
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertTrue(published)
        self.assertEqual(payload["target_count"], 1)
        self.assertEqual(
            parse_cohort_arguments(("a=1.png,2.png",)),
            {"a": ("1.png", "2.png")},
        )

    def test_candidate_workspace_binds_and_generates_only_exact_samples(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, workspace = self.create_workspace(root)
            plan = self.build_plan(workspace)
            candidate = plan.document["candidates"][0]

            prepared = prepare_missing_voice_reuse_candidate_workspace(
                plan,
                candidate["candidate_id"],
                imported,
                root / "candidate-inputs",
                root / "workspaces",
            )
            repeated = prepare_missing_voice_reuse_candidate_workspace(
                plan,
                candidate["candidate_id"],
                imported,
                root / "candidate-inputs",
                root / "workspaces",
            )
            readiness = inspect_generation_readiness(
                prepared.workspace_directory,
                queue_ids=(fixture["queue_id"],),
            )
            command = build_missing_voice_reuse_candidate_command(
                plan,
                candidate["candidate_id"],
                prepared.workspace_directory,
            )

        self.assertTrue(prepared.input_created)
        self.assertTrue(prepared.workspace_created)
        self.assertFalse(repeated.input_created)
        self.assertFalse(repeated.workspace_created)
        self.assertEqual(readiness.selected, 1)
        self.assertEqual(readiness.ready, 1)
        self.assertEqual(readiness.missing_voice, 0)
        self.assertEqual(command.count("--queue-id"), 1)
        self.assertIn(fixture["queue_id"], command)
        self.assertNotIn("--regenerate-existing", command)

    def test_exact_failed_mode_accepts_one_candidate_and_keeps_control_out(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, workspace = self.create_workspace(root)
            plan = self.build_failed_plan(fixture, workspace)
            candidate = plan.document["candidates"][0]

            prepared = prepare_missing_voice_reuse_candidate_workspace(
                plan,
                candidate["candidate_id"],
                imported,
                root / "candidate-inputs",
                root / "workspaces",
            )
            candidate_state = json.loads(
                (
                    prepared.workspace_directory
                    / "generated-audio/generation-state.json"
                ).read_text(encoding="utf-8")
            )
            readiness = inspect_generation_readiness(
                prepared.workspace_directory,
                queue_ids=(fixture["queue_id"],),
            )

        target = plan.document["targets"][0]
        self.assertEqual(plan.document["target_mode"], "failed")
        self.assertEqual(plan.document["candidate_count"], 1)
        self.assertEqual(target["state"], "failed")
        self.assertEqual(target["failure_category"], "speech silence")
        self.assertEqual(len(target["source_state_item_sha256"]), 64)
        self.assertNotIn(fixture["queue_id"], candidate_state["items"])
        self.assertEqual(readiness.selected, 1)
        self.assertEqual(readiness.ready, 1)

    def test_failed_mode_rejects_absent_or_non_failed_exact_ids(self):
        with TemporaryDirectory() as directory:
            fixture, _imported, workspace = self.create_workspace(Path(directory))
            with self.assertRaisesRegex(MissingVoiceReuseError, "not an exact failed"):
                build_missing_voice_reuse_plan(
                    workspace,
                    "Aderyn",
                    cohorts={"failed family": ("314601.png",)},
                    candidate_voice_characters=("Centurion",),
                    failed_queue_ids=(fixture["queue_id"],),
                )
            with self.assertRaisesRegex(MissingVoiceReuseError, "absent"):
                build_missing_voice_reuse_plan(
                    workspace,
                    "Aderyn",
                    cohorts={"failed family": ("314601.png",)},
                    candidate_voice_characters=("Centurion",),
                    failed_queue_ids=("missing-queue-id",),
                )

    def test_inline_pause_candidate_binds_prompt_and_carries_exact_control(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, workspace = self.create_workspace(
                root,
                text="What happened? You're hurt.",
                missing_voice_policy={
                    "schema_version": 1,
                    "mode": "narrator_roles",
                    "roles": ["Aderyn"],
                },
            )
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            source_item = {
                "status": "failed",
                "attempts": 1,
                "last_error": "Generated WAV failed speech-silence validation",
            }
            state["items"][fixture["queue_id"]] = source_item
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            plan = build_missing_voice_reuse_plan(
                workspace,
                "Aderyn",
                cohorts={"failed": ("314601.png",)},
                candidate_voice_characters=("Centurion",),
                failed_queue_ids=(fixture["queue_id"],),
                inline_pause_ms=180,
            )
            candidate = plan.document["candidates"][0]
            prepared = prepare_missing_voice_reuse_candidate_workspace(
                plan,
                candidate["candidate_id"],
                imported,
                root / "inputs",
                root / "candidate-workspaces",
            )
            command = build_missing_voice_reuse_candidate_command(
                plan, candidate["candidate_id"], prepared.workspace_directory
            )
            carried = json.loads(
                (
                    prepared.workspace_directory
                    / "generated-audio/generation-state.json"
                ).read_text(encoding="utf-8")
            )["items"][fixture["queue_id"]]

        hypothesis = candidate["render_hypothesis"]
        self.assertEqual(plan.document["candidate_mode"], "inline_pause_marker")
        self.assertEqual(hypothesis["pause_ms"], 180)
        self.assertEqual(hypothesis["prompts"][0]["marker_count"], 1)
        self.assertEqual(carried, source_item)
        self.assertEqual(
            command[command.index("--inline-pause-failed") + 1],
            fixture["queue_id"],
        )
        self.assertEqual(command[command.index("--queue-id") + 1], fixture["queue_id"])

    def test_cli_publishes_single_candidate_failed_control_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, _imported, workspace = self.create_workspace(root)
            self.build_failed_plan(fixture, workspace)
            output = root / "failed-plan.json"

            with redirect_stdout(StringIO()):
                exit_code = authoring_main(
                    [
                        "missing-voice-reuse-plan",
                        str(workspace),
                        "Aderyn",
                        "--cohort",
                        "failed family=314601.png",
                        "--candidate-voice",
                        "Centurion",
                        "--failed-queue-id",
                        fixture["queue_id"],
                        "--output",
                        str(output),
                    ]
                )
            target_mode = load_missing_voice_reuse_plan(output).document["target_mode"]

        self.assertEqual(exit_code, 0)
        self.assertEqual(target_mode, "failed")

    def test_candidate_bundle_schema_version_requires_exact_integer(self):
        body = {
            "schema": reuse_module.MISSING_VOICE_REUSE_CANDIDATE_BUNDLE_SCHEMA,
            "schema_version": 1,
            "plan_id": "plan",
            "candidate_id": "candidate",
            "inventory": [],
        }
        valid = {**body, "bundle_id": reuse_module.canonical_document_sha256(body)}
        reuse_module._validate_candidate_bundle_identity(
            valid, {"plan_id": "plan"}, {"candidate_id": "candidate"}
        )
        for version in (True, 1.0):
            invalid_body = {**body, "schema_version": version}
            invalid = {
                **invalid_body,
                "bundle_id": reuse_module.canonical_document_sha256(invalid_body),
            }
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    MissingVoiceReuseError, "candidate bundle identity is invalid"
                ),
            ):
                reuse_module._validate_candidate_bundle_identity(
                    invalid, {"plan_id": "plan"}, {"candidate_id": "candidate"}
                )


if __name__ == "__main__":
    unittest.main()
