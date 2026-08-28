import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    write_voice_generation_queue,
)

from tests.test_authoring_legacy_import import write_legacy_fixture
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.missing_voice_reuse import (
    MissingVoiceReuseError,
    build_missing_voice_reuse_candidate_command,
    build_missing_voice_reuse_plan,
    load_missing_voice_reuse_plan,
    parse_cohort_arguments,
    prepare_missing_voice_reuse_candidate_workspace,
    write_missing_voice_reuse_plan,
)
from vntts.authoring.workbench import (
    create_resume_workspace,
    inspect_generation_readiness,
)


class AuthoringMissingVoiceReuseTest(unittest.TestCase):
    def create_workspace(self, root, *, text=None, missing_voice_policy=None):
        fixture = write_legacy_fixture(root / "legacy")
        queue = VoiceGenerationQueue.load(fixture["queue"])
        item = queue.items[0]
        record = item.to_record()
        record["speaker"] = "Aderyn"
        record["voice_character"] = "Aderyn"
        if text is not None:
            import hashlib

            record["text"] = text
            record["text_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            record["queue_id"] = f"{record['line_id']}:{record['text_sha256'][:16]}"
            fixture["queue_id"] = record["queue_id"]
        write_voice_generation_queue(fixture["queue"], queue.metadata, [record])
        queue_sha256 = sha256_file(fixture["queue"])

        state = json.loads(fixture["state"].read_text(encoding="utf-8"))
        state["queue_sha256"] = queue_sha256
        state["active"] = None
        state["items"] = {}
        fixture["state"].write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        write_generated_audio_manifest(
            fixture["manifest"],
            {
                "game": "Reverse: 1999",
                "language": "en",
                "source_queue_sha256": queue_sha256,
                "generated_at": "2026-08-16T17:06:00+00:00",
            },
            [],
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
                        "title": "Aderyn story",
                        "kind": "character-story",
                        "order": 1,
                    }
                ],
            },
            [
                {
                    "record_type": "line",
                    "line_id": item.line_id,
                    "text_sha256": record["text_sha256"],
                    "text": record["text"],
                    "speaker": "Aderyn",
                    "voice_character": "Aderyn",
                    "kind": "dialogue",
                    "chapter": "315401",
                    "sequence": 7,
                    "collection_id": "story",
                    "source_audio_status": "absent",
                    "source_audio_reason": "fixture_absent",
                    "source_kind": "story",
                    "speakable": True,
                    "portrait": "314601.png",
                }
            ],
        )
        voice_manifest = Path(fixture["job"]["voice_manifest"])
        (voice_manifest.parent / "adult.wav").write_bytes(b"adult-reference")
        (voice_manifest.parent / "narrator.wav").write_bytes(b"narrator-reference")
        voice_manifest.write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Adult Aderyn",
                            "speaker": "adult-aderyn",
                            "references": ["adult.wav"],
                        },
                        {
                            "character": "Centurion",
                            "speaker": "centurion",
                            "references": ["narrator.wav"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        imported = import_legacy_job(
            fixture["job_directory"], root / "imports"
        ).destination
        workspace = create_resume_workspace(
            imported,
            root / "workspaces",
            story_index=fixture["job"]["story_index"],
            voice_manifest=voice_manifest,
            backend="moss-tts",
            model="model",
            generation_profile="stable",
            narrator_character="Centurion",
            missing_voice_policy=missing_voice_policy,
        )
        return fixture, imported, workspace.directory

    def build_plan(self, workspace):
        return build_missing_voice_reuse_plan(
            workspace,
            "Aderyn",
            cohorts={"adult family": ("314601.png",)},
            candidate_voice_characters=("Adult Aderyn", "Centurion"),
        )

    def build_failed_plan(self, fixture, workspace):
        state_path = workspace / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["items"][fixture["queue_id"]] = {
            "status": "failed",
            "attempts": 3,
            "last_error": "Generated WAV failed speech-silence validation",
        }
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        return build_missing_voice_reuse_plan(
            workspace,
            "Aderyn",
            cohorts={"failed family": ("314601.png",)},
            candidate_voice_characters=("Centurion",),
            failed_queue_ids=(fixture["queue_id"],),
        )

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


if __name__ == "__main__":
    unittest.main()
