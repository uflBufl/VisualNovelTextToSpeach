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
    def create_workspace(self, root):
        fixture = write_legacy_fixture(root / "legacy")
        queue = VoiceGenerationQueue.load(fixture["queue"])
        item = queue.items[0]
        record = item.to_record()
        record["speaker"] = "Aderyn"
        record["voice_character"] = "Aderyn"
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
                    "text_sha256": item.text_sha256,
                    "text": item.text,
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
        )
        return fixture, imported, workspace.directory

    def build_plan(self, workspace):
        return build_missing_voice_reuse_plan(
            workspace,
            "Aderyn",
            cohorts={"adult family": ("314601.png",)},
            candidate_voice_characters=("Adult Aderyn", "Centurion"),
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


if __name__ == "__main__":
    unittest.main()
