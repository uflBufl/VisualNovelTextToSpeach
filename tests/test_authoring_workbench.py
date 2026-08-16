import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

from tests.test_authoring_legacy_import import write_legacy_fixture
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.workbench import (
    AuthoringRuntimeStatus,
    AuthoringWorkbenchError,
    create_resume_workspace,
    discover_imports,
    discover_workspaces,
    generation_command,
    generation_control_bindings,
    inspect_generation_readiness,
    inspect_workspace,
    list_review_items,
    review_workspace_item,
)


class AuthoringWorkbenchTest(unittest.TestCase):
    def create_workspace(self, root):
        fixture = write_legacy_fixture(root / "legacy")
        voice_reference = root / "legacy" / "rhiannon.wav"
        voice_reference.write_bytes(b"voice-reference")
        Path(fixture["job"]["voice_manifest"]).write_text(
            json.dumps(
                {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Rhiannon",
                            "speaker": "Rhiannon",
                            "reference": "rhiannon.wav",
                        }
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
            voice_manifest=fixture["job"]["voice_manifest"],
            backend="moss-tts",
            model="model with spaces",
            generation_profile="stable",
            narrator_character="Rhiannon",
        )
        return fixture, imported, workspace

    def test_resume_workspace_is_separate_hash_bound_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, first = self.create_workspace(root)
            imported_hashes = {
                path.relative_to(imported).as_posix(): sha256_file(path)
                for path in imported.rglob("*")
                if path.is_file()
            }
            second = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
            )
            workspace = json.loads(
                (first.directory / "workspace.json").read_text(encoding="utf-8")
            )
            imported_hashes_after = {
                path.relative_to(imported).as_posix(): sha256_file(path)
                for path in imported.rglob("*")
                if path.is_file()
            }
            workspace_queue_hash = sha256_file(first.directory / "queue.jsonl")
            fixture_queue_hash = sha256_file(fixture["queue"])

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.directory, second.directory)
        self.assertNotEqual(first.directory, imported)
        self.assertEqual(workspace["source"]["import_id"], imported.name)
        self.assertEqual(imported_hashes, imported_hashes_after)
        self.assertEqual(workspace_queue_hash, fixture_queue_hash)

    def test_idempotent_reopen_rejects_changed_voice_snapshot_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, created = self.create_workspace(root)
            manifest = created.directory / "inputs" / "voice" / "manifest.json"
            manifest.write_bytes(manifest.read_bytes() + b"\n")

            with self.assertRaisesRegex(AuthoringWorkbenchError, "voice manifest"):
                create_resume_workspace(
                    imported,
                    root / "workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                )

    def test_import_manifest_mutation_during_creation_aborts_without_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            reference = root / "legacy" / "rhiannon.wav"
            reference.write_bytes(b"voice-reference")
            Path(fixture["job"]["voice_manifest"]).write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Rhiannon",
                                "speaker": "Rhiannon",
                                "reference": "rhiannon.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            imported = import_legacy_job(
                fixture["job_directory"], root / "imports"
            ).destination
            import_path = imported / "import.json"
            import vntts.authoring.workbench as workbench_module

            original_validate = workbench_module._validated_import_inventory

            def mutate_after_inventory(source, manifest):
                inventory = original_validate(source, manifest)
                changed = json.loads(import_path.read_text(encoding="utf-8"))
                changed["source"]["source_fingerprint"] = "0" * 64
                import_path.write_text(
                    json.dumps(changed, sort_keys=True), encoding="utf-8"
                )
                return inventory

            with (
                patch(
                    "vntts.authoring.workbench._validated_import_inventory",
                    side_effect=mutate_after_inventory,
                ),
                self.assertRaisesRegex(AuthoringWorkbenchError, "manifest changed"),
            ):
                create_resume_workspace(imported, root / "workspaces")

            self.assertEqual(list((root / "workspaces").iterdir()), [])

    def test_publication_race_never_replaces_competing_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            reference = root / "legacy" / "rhiannon.wav"
            reference.write_bytes(b"voice-reference")
            Path(fixture["job"]["voice_manifest"]).write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Rhiannon",
                                "speaker": "Rhiannon",
                                "reference": "rhiannon.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            imported = import_legacy_job(
                fixture["job_directory"], root / "imports"
            ).destination
            marker = b"competitor"

            def publish_competitor(_source, destination):
                destination.mkdir()
                (destination / "marker").write_bytes(marker)
                raise FileExistsError(destination)

            with (
                patch(
                    "vntts.authoring.workbench._rename_directory_no_replace",
                    side_effect=publish_competitor,
                ),
                self.assertRaises(AuthoringWorkbenchError),
            ):
                create_resume_workspace(
                    imported,
                    root / "workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="moss-v1.5",
                    generation_profile="stable",
                )

            destinations = [
                path
                for path in (root / "workspaces").iterdir()
                if not path.name.startswith(".")
            ]
            self.assertEqual(len(destinations), 1)
            self.assertEqual((destinations[0] / "marker").read_bytes(), marker)

    def test_rejects_noncanonical_import_identity_before_path_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            imported = import_legacy_job(
                fixture["job_directory"], root / "imports"
            ).destination
            manifest_path = imported / "import.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["import_id"] = "legacy-foo/../../escaped"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(AuthoringWorkbenchError, "canonical"):
                create_resume_workspace(imported, root / "workspaces")

            self.assertFalse((root / "escaped").exists())

    def test_discovery_rejects_symlinked_imports_and_workspaces(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, created = self.create_workspace(root / "outside")
            import_root = root / "imports"
            workspace_root = root / "workspaces"
            import_root.mkdir()
            workspace_root.mkdir()
            (import_root / imported.name).symlink_to(imported, target_is_directory=True)
            (workspace_root / created.directory.name).symlink_to(
                created.directory, target_is_directory=True
            )

            imports = discover_imports(import_root)
            workspaces = discover_workspaces(workspace_root)

        self.assertEqual(imports, ())
        self.assertEqual(workspaces, ())
        self.assertTrue(fixture["queue"].name)

    def test_immutable_queue_and_core_paths_are_anchored_to_import_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, created = self.create_workspace(root)
            queue_path = created.directory / "queue.jsonl"
            queue_path.write_text("tampered\n", encoding="utf-8")
            workspace_path = created.directory / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            original_workspace = json.loads(json.dumps(workspace))
            queue_seed = next(
                value
                for value in workspace["seed_inventory"]
                if value["path"] == "queue.jsonl"
            )
            queue_seed["sha256"] = sha256_file(queue_path)
            workspace_path.write_text(json.dumps(workspace), encoding="utf-8")

            with self.assertRaisesRegex(AuthoringWorkbenchError, "seed inventory"):
                inspect_workspace(created.directory)
            with self.assertRaises(AuthoringWorkbenchError):
                create_resume_workspace(
                    imported,
                    root / "workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                )

            original_workspace["output"] = "forked-history"
            workspace_path.write_text(json.dumps(original_workspace), encoding="utf-8")
            with self.assertRaisesRegex(AuthoringWorkbenchError, "core paths"):
                inspect_workspace(created.directory)

    def test_selected_inputs_are_self_contained_and_may_replace_legacy_voice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            imported = import_legacy_job(
                fixture["job_directory"], root / "imports"
            ).destination
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "rhiannon.wav").write_bytes(b"new-reference")
            voice_manifest = replacement / "voices.json"
            voice_manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Rhiannon",
                                "speaker": "Rhiannon",
                                "reference": "rhiannon.wav",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            created = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=voice_manifest,
            )
            workspace = json.loads(
                (created.directory / "workspace.json").read_text(encoding="utf-8")
            )
            Path(fixture["job"]["story_index"]).unlink()
            voice_manifest.unlink()
            (replacement / "rhiannon.wav").unlink()

            summary = inspect_workspace(created.directory)

        self.assertFalse(workspace["voice_manifest"]["matches_legacy"])
        self.assertEqual(summary.runtime_status, AuthoringRuntimeStatus.INTERRUPTED)
        self.assertTrue(summary.voice_manifest.is_relative_to(created.directory))

    def test_progress_is_disjoint_and_review_changes_only_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id = next(iter(state["items"]))
            state["items"][queue_id]["status"] = "generated"
            state["items"][queue_id]["review_status"] = "pending_review"
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            source_state = imported / "generated-audio/generation-state.json"
            source_hash = sha256_file(source_state)

            before = inspect_workspace(created.directory)
            reviewed = review_workspace_item(created.directory, queue_id, "approved")
            items = list_review_items(created.directory)
            source_hash_after = sha256_file(source_state)

        self.assertEqual(before.runtime_status, AuthoringRuntimeStatus.NEEDS_REVIEW)
        self.assertEqual(before.generated, 1)
        self.assertEqual(before.approved, 0)
        self.assertEqual(before.rejected, 0)
        self.assertEqual(before.failed, 0)
        self.assertEqual(before.pending, 0)
        self.assertEqual(reviewed.approved, 1)
        self.assertEqual(items[0].review_status, "approved")
        self.assertEqual(source_hash_after, source_hash)

    def test_runtime_distinguishes_local_external_pid_reuse_and_interruption(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            output = created.directory / "generated-audio"
            state_path = output / "generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            lease = {
                "schema": "vntts.authoring-generation-lease",
                "schema_version": 1,
                "queue_sha256": state["queue_sha256"],
                "pid": 41,
                "hostname": None,
                "process_started_at": "start-a",
                "lease_id": "owner",
                "started_at": "2026-08-17T00:00:00+00:00",
            }
            (output / ".generation-lease.json").write_text(
                json.dumps(lease), encoding="utf-8"
            )

            local = inspect_workspace(
                created.directory,
                local_process_id=41,
                local_process_started_at="start-a",
                process_checker=lambda _pid: True,
                process_start_checker=lambda _pid: "start-a",
            )
            external = inspect_workspace(
                created.directory,
                process_checker=lambda _pid: True,
                process_start_checker=lambda _pid: "start-a",
            )
            reused = inspect_workspace(
                created.directory,
                process_checker=lambda _pid: True,
                process_start_checker=lambda _pid: "different-start",
            )
            dead = inspect_workspace(
                created.directory,
                process_checker=lambda _pid: False,
                process_start_checker=lambda _pid: None,
            )
            local_mismatch = inspect_workspace(
                created.directory,
                local_process_id=41,
                local_process_started_at="different-local-start",
                process_checker=lambda _pid: True,
                process_start_checker=lambda _pid: "start-a",
            )
            unknown_start = inspect_workspace(
                created.directory,
                process_checker=lambda _pid: True,
                process_start_checker=lambda _pid: None,
            )

        self.assertEqual(local.runtime_status, AuthoringRuntimeStatus.RUNNING_HERE)
        self.assertEqual(
            external.runtime_status, AuthoringRuntimeStatus.RUNNING_EXTERNAL
        )
        self.assertEqual(reused.runtime_status, AuthoringRuntimeStatus.INTERRUPTED)
        self.assertEqual(dead.runtime_status, AuthoringRuntimeStatus.INTERRUPTED)
        self.assertEqual(
            local_mismatch.runtime_status, AuthoringRuntimeStatus.RUNNING_EXTERNAL
        )
        self.assertEqual(
            unknown_start.runtime_status, AuthoringRuntimeStatus.RUNNING_EXTERNAL
        )

    def test_missing_voice_configuration_never_reports_ready_or_complete(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            imported = import_legacy_job(
                fixture["job_directory"], root / "imports"
            ).destination
            created = create_resume_workspace(imported, root / "workspaces")
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state["items"] = {}
            state_path.write_text(json.dumps(state), encoding="utf-8")

            summary = inspect_workspace(created.directory)
            readiness = inspect_generation_readiness(created.directory)

        self.assertEqual(summary.runtime_status, AuthoringRuntimeStatus.BLOCKED)
        self.assertEqual(summary.pending, 1)
        self.assertIsNone(summary.missing_voice)
        self.assertEqual(readiness.ready, 0)
        self.assertTrue(
            any("voice manifest" in reason for reason in readiness.blocked_reasons)
        )

    def test_active_attempt_and_generation_argv_are_exact(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id, item = next(iter(state["items"].items()))
            from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

            queue = VoiceGenerationQueue.load(created.directory / "queue.jsonl")
            queue_item = next(
                value for value in queue.items if value.queue_id == queue_id
            )
            state["active"] = {
                "queue_id": queue_id,
                "line_id": item["line_id"],
                "speaker": "Rhiannon",
                "text": queue_item.text,
                "phase": "retrying",
                "attempt": 2,
                "attempt_limit": 3,
                "total_attempts": 4,
                "seed": 12,
                "started_at": "2026-08-17T00:00:00+00:00",
                "updated_at": "2026-08-17T00:01:00+00:00",
                "last_error": "Earlier limited render",
            }
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            summary = inspect_workspace(created.directory)
            state["active"] = None
            state["items"] = {}
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            command = generation_command(
                created.directory,
                backend="moss-tts",
                model="model with spaces",
                retries=4,
                seed=9,
            )

        self.assertEqual(summary.runtime_status, AuthoringRuntimeStatus.INTERRUPTED)
        self.assertEqual(summary.active.phase, "retrying")
        self.assertEqual(summary.active.total_attempts, 4)
        self.assertEqual(summary.active.last_error, "Earlier limited render")
        self.assertEqual(command[0], os.sys.executable)
        self.assertIn("model with spaces", command)
        narrator_index = command.index("--narrator-character")
        self.assertEqual(command[narrator_index + 1], "Rhiannon")
        self.assertNotIn(" ".join(command), command)

    def test_child_rejects_control_mutation_before_backend_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state["items"] = {}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            command = generation_command(created.directory, backend="moss-tts")
            reference = created.directory / "inputs" / "voice" / "rhiannon.wav"
            reference.write_bytes(b"mutated-after-parent-preflight")

            completed = subprocess.run(command, capture_output=True, text=True)

            with (
                patch("vntts.authoring.cli.create_backend") as create_backend,
                self.assertRaises(SystemExit),
            ):
                authoring_main(list(command[3:]))

            create_backend.assert_not_called()
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("voice control inventory", completed.stderr)

    def test_child_rejects_queue_and_output_symlink_escape(self):
        for target_name in ("queue.jsonl", "generated-audio"):
            with (
                self.subTest(target_name=target_name),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                _fixture, _imported, created = self.create_workspace(root)
                target = created.directory / target_name
                outside = root / f"outside-{target_name.replace('.', '-')}"
                target.rename(outside)
                target.symlink_to(outside, target_is_directory=outside.is_dir())

                with self.assertRaisesRegex(
                    AuthoringWorkbenchError, "queue|generated-audio"
                ):
                    generation_control_bindings(
                        created.directory,
                        queue=created.directory / "queue.jsonl",
                        output=created.directory / "generated-audio",
                        voice_manifest=created.directory / "inputs/voice/manifest.json",
                        backend="moss-tts",
                        model="model with spaces",
                        generation_profile="stable",
                        narrator_character="Rhiannon",
                    )

    def test_child_detects_output_swap_after_backend_construction(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state["items"] = {}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            command = generation_command(created.directory, backend="moss-tts")
            external = root / "external-output"
            external.mkdir()

            class Backend:
                name = "moss-tts"
                model_name = "model with spaces"

                def render(self, _request):
                    raise AssertionError("output identity must fail before render")

                def stop(self):
                    return None

            def swap_output(*_arguments, **_options):
                output = created.directory / "generated-audio"
                preserved = root / "preserved-output"
                shutil.move(output, preserved)
                output.symlink_to(external, target_is_directory=True)
                return Backend()

            with (
                patch("vntts.authoring.cli.create_backend", side_effect=swap_output),
                self.assertRaises(SystemExit),
            ):
                authoring_main(list(command[3:]))

            self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
