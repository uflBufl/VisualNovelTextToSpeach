import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)

import vntts.authoring as authoring_package
import vntts.authoring.workbench as workbench_module
from tests.test_authoring_legacy_import import write_legacy_fixture
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.workbench import (
    AuthoringRuntimeStatus,
    AuthoringWorkbenchError,
    CollectionSelection,
    create_resume_workspace,
    discover_imports,
    discover_workspaces,
    generation_command,
    generation_control_bindings,
    immutable_history_timestamps,
    inspect_collection_selection,
    inspect_generation_readiness,
    inspect_workspace,
    list_review_items,
    review_workspace_item,
)


def create_test_workspace(root):
    fixture = write_legacy_fixture(root / "legacy")
    queue_item = VoiceGenerationQueue.load(fixture["queue"]).items[0]
    side_text = "A source-audio line outside the generation queue."
    write_story_index_document(
        fixture["job"]["story_index"],
        {
            "game": "Reverse: 1999",
            "language": "en",
            "generated_at": "2026-08-16T15:00:00+00:00",
            "collections": [
                {
                    "collection_id": "main",
                    "title": "The Eaglet Takes Wing",
                    "kind": "character-story",
                    "order": 1,
                },
                {
                    "collection_id": "source-only",
                    "title": "Installed source audio",
                    "kind": "reference",
                    "order": 2,
                },
            ],
        },
        [
            {
                "record_type": "line",
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "text": queue_item.text,
                "speaker": queue_item.speaker,
                "voice_character": queue_item.voice_character,
                "kind": "dialogue",
                "chapter": "315401",
                "sequence": 7,
                "collection_id": "main",
                "source_audio_status": "absent",
                "source_audio_reason": "fixture_absent",
                "source_kind": "story",
                "speakable": True,
            },
            {
                "record_type": "line",
                "line_id": "reverse1999:source:1",
                "text_sha256": text_sha256(side_text),
                "text": side_text,
                "speaker": "Rhiannon",
                "voice_character": "Rhiannon",
                "kind": "dialogue",
                "chapter": "source",
                "sequence": 1,
                "collection_id": "source-only",
                "source_audio_status": "available",
                "source_audio_reason": "fixture_available",
                "source_kind": "story",
                "speakable": True,
            },
        ],
    )
    voice_reference = root / "legacy" / "rhiannon.wav"
    voice_reference.write_bytes(b"voice-reference")
    second_voice_reference = root / "legacy" / "rhiannon-2.wav"
    second_voice_reference.write_bytes(b"second-voice-reference")
    Path(fixture["job"]["voice_manifest"]).write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "Rhiannon",
                        "references": ["rhiannon.wav", "rhiannon-2.wav"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    imported = import_legacy_job(fixture["job_directory"], root / "imports").destination
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


class AuthoringWorkbenchTest(unittest.TestCase):
    def create_workspace(self, root):
        return create_test_workspace(root)

    def test_collection_selection_api_is_exported_from_authoring_package(self):
        self.assertIs(
            authoring_package.inspect_collection_selection,
            inspect_collection_selection,
        )
        self.assertIs(
            authoring_package.CollectionSelection,
            CollectionSelection,
        )

    def test_exact_unknown_label_uses_configured_narrator_reference(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            workspace = json.loads(
                (created.directory / "workspace.json").read_text(encoding="utf-8")
            )
            manifest = created.directory / "inputs/voice/manifest.json"
            unknown_label = SimpleNamespace(
                queue_id="unknown-label",
                speaker="???",
                voice_character="Rhiannon",
            )
            named_unknown = SimpleNamespace(
                queue_id="named-unknown",
                speaker="Selone",
                voice_character="Selone",
            )

            missing, reasons = workbench_module._voice_readiness(
                workspace,
                (unknown_label, named_unknown),
                set(),
                manifest,
            )

        self.assertEqual(missing, {"named-unknown"})
        self.assertTrue(any("1 queued line" in reason for reason in reasons))

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
        self.assertEqual(items[0].collection_id, "main")
        self.assertEqual(items[0].voice_character, "Rhiannon")
        self.assertEqual(source_hash_after, source_hash)

    def test_review_decision_is_compare_and_swap_bound_to_displayed_state_and_wav(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id = next(iter(state["items"]))
            state["items"][queue_id]["status"] = "generated"
            state["items"][queue_id]["review_status"] = "pending_review"
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            displayed = list_review_items(created.directory)[0]

            review_workspace_item(created.directory, queue_id, "approved")
            before = state_path.read_bytes()
            manifest = created.directory / "generated-audio/manifest.json"
            manifest_before = manifest.read_bytes()
            with self.assertRaisesRegex(AuthoringWorkbenchError, "authority changed"):
                review_workspace_item(
                    created.directory,
                    queue_id,
                    "rejected",
                    displayed.authority,
                )

            self.assertEqual(state_path.read_bytes(), before)
            self.assertEqual(manifest.read_bytes(), manifest_before)

    def test_review_decision_rejects_queue_change_before_state_or_manifest_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_id = next(iter(state["items"]))
            state["items"][queue_id]["status"] = "generated"
            state["items"][queue_id]["review_status"] = "pending_review"
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            displayed = list_review_items(created.directory)[0]
            state_before = state_path.read_bytes()
            manifest = created.directory / "generated-audio/manifest.json"
            manifest_before = manifest.read_bytes()
            (created.directory / "queue.jsonl").write_bytes(b"changed queue")

            with self.assertRaises(AuthoringWorkbenchError):
                review_workspace_item(
                    created.directory,
                    queue_id,
                    "approved",
                    displayed.authority,
                )

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(manifest.read_bytes(), manifest_before)

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

    def test_partial_voice_manifest_allows_exact_covered_failed_retry_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            queue = VoiceGenerationQueue.load(fixture["queue"])
            rhiannon = queue.items[0]
            missing_text = "An uncovered speaker remains outside this retry."
            missing_hash = text_sha256(missing_text)
            missing_line = "reverse1999:missing:1"
            missing_queue_id = expected_voice_generation_queue_id(
                missing_line, missing_hash
            )
            write_voice_generation_queue(
                fixture["queue"],
                queue.metadata,
                [
                    rhiannon.document,
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
                ],
            )
            queue_digest = sha256_file(fixture["queue"])
            output = Path(fixture["job"]["output"])
            state_path = output / "generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["queue_sha256"] = queue_digest
            state["active"] = None
            state["items"] = {
                rhiannon.queue_id: {
                    "status": "failed",
                    "attempts": 3,
                    "seed": 2,
                    "last_error": "limited before EOS",
                    "updated_at": "2026-08-17T00:00:00+00:00",
                }
            }
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_queue_sha256"] = queue_digest
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
                            "collection_id": "covered",
                            "title": "Covered retry",
                            "kind": "story",
                            "order": 1,
                        },
                        {
                            "collection_id": "uncovered",
                            "title": "Uncovered pending",
                            "kind": "story",
                            "order": 2,
                        },
                    ],
                },
                [
                    {
                        "record_type": "line",
                        "line_id": rhiannon.line_id,
                        "text_sha256": rhiannon.text_sha256,
                        "text": rhiannon.text,
                        "speaker": "Rhiannon",
                        "voice_character": "Rhiannon",
                        "kind": "dialogue",
                        "chapter": "covered",
                        "sequence": 1,
                        "collection_id": "covered",
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
                        "chapter": "uncovered",
                        "sequence": 1,
                        "collection_id": "uncovered",
                        "source_audio_status": "absent",
                        "source_kind": "story",
                    },
                ],
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
            created = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="moss-v1.5",
                generation_profile="stable",
                narrator_character="Rhiannon",
            )

            summary = inspect_workspace(created.directory)
            covered = inspect_collection_selection(
                created.directory, collection_ids=("covered",)
            )
            command = generation_command(
                created.directory,
                queue_ids=covered.readiness.queue_ids,
                retries=0,
                seed=0,
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "Voice references are missing"
            ):
                generation_command(created.directory)
            review = next(
                item
                for item in list_review_items(created.directory)
                if item.queue_id == rhiannon.queue_id
            )
            from tests.test_authoring_bulk_generation import SyntheticRenderer
            from vntts.authoring.bulk_generation import (
                load_generation_state,
                run_bulk_generation,
            )

            renderer = SyntheticRenderer()
            result = run_bulk_generation(
                created.directory / "queue.jsonl",
                created.directory / "generated-audio",
                renderer,
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=covered.readiness.queue_ids,
            )
            resumed = load_generation_state(
                created.directory / "generated-audio/generation-state.json",
                created.directory / "queue.jsonl",
            )

        self.assertEqual(summary.runtime_status, AuthoringRuntimeStatus.NEEDS_ATTENTION)
        self.assertEqual(summary.missing_voice, 1)
        self.assertEqual(covered.readiness.failed, 1)
        self.assertEqual(covered.readiness.ready, 1)
        self.assertEqual(covered.readiness.queue_ids, (rhiannon.queue_id,))
        self.assertEqual(command[command.index("--queue-id") + 1], rhiannon.queue_id)
        self.assertEqual((review.attempts, review.seed), (3, 2))
        self.assertEqual([request.seed for request in renderer.requests], [3])
        self.assertEqual(result.generated, 1)
        self.assertEqual(
            (
                resumed["items"][rhiannon.queue_id]["attempts"],
                resumed["items"][rhiannon.queue_id]["seed"],
            ),
            (4, 3),
        )
        self.assertNotIn(missing_queue_id, resumed["items"])

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

    def test_collection_selection_maps_exact_queue_ids_and_empty_is_explicit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active"] = None
            state["items"] = {}
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            selected = inspect_collection_selection(created.directory)
            main = inspect_collection_selection(
                created.directory, collection_ids=("main",)
            )
            source_only = inspect_collection_selection(
                created.directory, collection_ids=("source-only",)
            )
            empty = inspect_collection_selection(created.directory, collection_ids=())
            command = generation_command(
                created.directory,
                queue_ids=selected.readiness.queue_ids,
            )

        command_ids = tuple(
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--queue-id"
        )
        self.assertEqual(selected.collection_count, 2)
        self.assertEqual(selected.story_records, 2)
        self.assertEqual(selected.queue_items, 1)
        self.assertEqual(selected.queue_ids, selected.readiness.queue_ids)
        self.assertEqual(main.queue_ids, selected.queue_ids)
        self.assertEqual(source_only.story_records, 1)
        self.assertEqual(source_only.queue_ids, ())
        self.assertEqual(command_ids, selected.readiness.queue_ids)
        self.assertEqual(empty.collection_ids, ())
        self.assertEqual(empty.queue_ids, ())
        self.assertEqual(empty.readiness.ready, 0)
        self.assertIn("No pending", empty.readiness.blocked_reasons[0])

    def test_collection_selection_rejects_unknown_id(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "absent from the story index"
            ):
                inspect_collection_selection(
                    created.directory, collection_ids=("missing",)
                )

    def test_collection_selection_rejects_transient_story_swap_and_restore(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            story = created.directory / "inputs/story-index.jsonl"
            original = story.read_bytes()
            rows = [
                json.loads(value) for value in original.decode("utf-8").splitlines()
            ]
            for row in rows[1:]:
                if row["collection_id"] == "main":
                    row["collection_id"] = "source-only"
                elif row["collection_id"] == "source-only":
                    row["collection_id"] = "main"
            swapped = (
                "\n".join(
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    for value in rows
                )
                + "\n"
            ).encode("utf-8")
            import vntts.authoring.workbench as workbench_module

            original_load = workbench_module._load_workspace
            changed = False

            def swap_after_workspace_validation(path):
                nonlocal changed
                result = original_load(path)
                if not changed:
                    story.write_bytes(swapped)
                    changed = True
                return result

            try:
                with (
                    patch(
                        "vntts.authoring.workbench._load_workspace",
                        side_effect=swap_after_workspace_validation,
                    ),
                    self.assertRaisesRegex(
                        AuthoringWorkbenchError, "Story index snapshot was modified"
                    ),
                ):
                    inspect_collection_selection(
                        created.directory, collection_ids=("source-only",)
                    )
            finally:
                story.write_bytes(original)

            restored = inspect_collection_selection(
                created.directory, collection_ids=("source-only",)
            )

        self.assertEqual(restored.queue_ids, ())

    def test_immutable_history_timestamps_are_utc_and_chronological(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)

            timestamps = immutable_history_timestamps(created.directory)

        self.assertEqual(
            [value.kind for value in timestamps],
            ["Source created", "Source updated", "Imported", "Workspace created"],
        )
        self.assertEqual(
            [value.instant for value in timestamps],
            sorted(value.instant for value in timestamps),
        )
        self.assertTrue(all(value.display.endswith(" UTC") for value in timestamps))
        for value in timestamps:
            self.assertRegex(
                value.display,
                r"^[A-Za-z ]+: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$",
            )

    def test_history_timestamps_keep_old_imports_compatible_without_mtime_inference(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            snapshot_path = created.directory / "provenance/import.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["schema_version"] = 1
            snapshot["legacy_job"].pop("created_at")
            snapshot["legacy_job"].pop("updated_at")
            snapshot_path.write_text(
                json.dumps(snapshot, sort_keys=True), encoding="utf-8"
            )
            snapshot_sha256 = sha256_file(snapshot_path)
            workspace_path = created.directory / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["source"]["import_sha256"] = snapshot_sha256
            workspace["seed_inventory"][0]["sha256"] = snapshot_sha256
            workspace_path.write_text(
                json.dumps(workspace, sort_keys=True), encoding="utf-8"
            )

            timestamps = immutable_history_timestamps(created.directory)

        self.assertEqual(
            [value.kind for value in timestamps], ["Imported", "Workspace created"]
        )

    def test_workspace_creation_timestamp_is_required_authoritative_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            workspace_path = created.directory / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["created_at"] = "not-a-timestamp"
            workspace_path.write_text(
                json.dumps(workspace, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "creation timestamp is missing or invalid"
            ):
                immutable_history_timestamps(created.directory)

    def test_history_timestamps_reject_import_snapshot_swap_after_workspace_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            snapshot_path = created.directory / "provenance/import.json"
            original = snapshot_path.read_bytes()
            original_loader = workbench_module._load_json_snapshot
            calls = 0

            def swap_on_history_read(path, label):
                nonlocal calls
                calls += 1
                if calls != 2:
                    return original_loader(path, label)
                snapshot = json.loads(original)
                snapshot["imported_at"] = "2030-01-01T00:00:00+00:00"
                snapshot_path.write_text(
                    json.dumps(snapshot, sort_keys=True), encoding="utf-8"
                )
                try:
                    return original_loader(path, label)
                finally:
                    snapshot_path.write_bytes(original)

            with (
                patch(
                    "vntts.authoring.workbench._load_json_snapshot",
                    side_effect=swap_on_history_read,
                ),
                self.assertRaisesRegex(
                    AuthoringWorkbenchError, "import snapshot was modified"
                ),
            ):
                immutable_history_timestamps(created.directory)

    def test_version_two_import_rejects_naive_source_time_on_create_and_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, created = self.create_workspace(root)
            import_path = imported / "import.json"
            import_manifest = json.loads(import_path.read_text(encoding="utf-8"))
            import_manifest["legacy_job"]["created_at"] = "2026-08-16T16:00:00"
            import_path.write_text(
                json.dumps(import_manifest, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "timezone-aware source created_at"
            ):
                create_resume_workspace(
                    imported,
                    root / "new-workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                )

            import_manifest["legacy_job"]["created_at"] = "2026-08-16T16:00:00+00:00"
            import_manifest["source"]["kind"] = (
                "reverse1999-extractor-standalone-generation"
            )
            import_path.write_text(
                json.dumps(import_manifest, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError,
                "inconsistent legacy job provenance",
            ):
                create_resume_workspace(
                    imported,
                    root / "new-workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                )

            snapshot_path = created.directory / "provenance/import.json"
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["legacy_job"]["created_at"] = "2026-08-16T16:00:00"
            snapshot_path.write_text(
                json.dumps(snapshot, sort_keys=True), encoding="utf-8"
            )
            snapshot_sha256 = sha256_file(snapshot_path)
            workspace_path = created.directory / "workspace.json"
            workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace["source"]["import_sha256"] = snapshot_sha256
            workspace["seed_inventory"][0]["sha256"] = snapshot_sha256
            workspace_path.write_text(
                json.dumps(workspace, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "timezone-aware source created_at"
            ):
                immutable_history_timestamps(created.directory)

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
