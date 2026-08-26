import hashlib
import json
import os
import shutil
import subprocess
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import write_generated_audio_manifest
from vntts_artifacts.hashing import text_sha256
from vntts_artifacts.story_index import write_story_index_document
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)

import vntts.authoring as authoring_package
import vntts.authoring.bulk_generation as bulk_generation_module
import vntts.authoring.workbench as workbench_module
from tests.test_authoring_legacy_import import write_legacy_fixture
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.legacy_import import import_legacy_job
from vntts.authoring.missing_voice_policy import NARRATOR_ROLES, MissingVoicePolicy
from vntts.authoring.reference_selection import select_voice_reference
from vntts.authoring.source_reference_bindings import queue_voice_overrides_sha256
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
    merge_workspace_outcomes,
    prepare_review_audio,
    review_selected_item,
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


def create_carry_source_workspace(root, *, text=None, queue_voice_override=None):
    kwargs = {} if text is None else {"text": text}
    fixture = write_legacy_fixture(root / "legacy", **kwargs)
    queue_item = VoiceGenerationQueue.load(fixture["queue"]).items[0]
    write_story_index_document(
        fixture["job"]["story_index"],
        {
            "game": "Reverse: 1999",
            "language": "en",
            "generated_at": "2026-08-16T15:00:00+00:00",
            "collections": [
                {
                    "collection_id": "main",
                    "title": "Carry-forward fixture",
                    "kind": "character-story",
                    "order": 1,
                }
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
            }
        ],
    )
    for name, payload in (
        ("rhiannon.wav", b"rhiannon-reference-one"),
        ("rhiannon-2.wav", b"rhiannon-reference-two"),
    ):
        (root / "legacy" / name).write_bytes(payload)
    voice_manifest = Path(fixture["job"]["voice_manifest"])
    voice_document = {
        "version": 2,
        "voices": [
            {
                "character": "Rhiannon",
                "speaker": "Rhiannon",
                "references": ["rhiannon.wav", "rhiannon-2.wav"],
            }
        ],
    }
    if queue_voice_override is not None:
        voice_document["voices"].append(
            {
                "character": queue_voice_override,
                "speaker": "bound-variant",
                "reference": "rhiannon-2.wav",
            }
        )
        overrides = {fixture["queue_id"]: queue_voice_override}
        voice_document["vntts.authoring.source_reference_bindings"] = {
            "schema": "vntts.authoring-source-reference-bindings",
            "schema_version": 1,
            "source_reference_plan_sha256": "a" * 64,
            "selected_variants": [
                {
                    "variant_id": "bound-variant",
                    "voice_character": queue_voice_override,
                }
            ],
            "queue_voice_overrides": overrides,
            "queue_voice_overrides_sha256": queue_voice_overrides_sha256(overrides),
        }
    voice_manifest.write_text(json.dumps(voice_document), encoding="utf-8")
    state = json.loads(fixture["state"].read_text(encoding="utf-8"))
    state["active"] = None
    state["items"][fixture["queue_id"]]["status"] = "generated"
    state["items"][fixture["queue_id"]]["review_status"] = "pending_review"
    fixture["state"].write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    write_generated_audio_manifest(
        fixture["manifest"],
        {
            "game": "Reverse: 1999",
            "language": "en",
            "source_queue_sha256": sha256_file(fixture["queue"]),
            "generated_at": "2026-08-16T17:06:00+00:00",
        },
        [],
    )
    imported = import_legacy_job(fixture["job_directory"], root / "imports").destination
    source = create_resume_workspace(
        imported,
        root / "workspaces",
        story_index=fixture["job"]["story_index"],
        voice_manifest=voice_manifest,
        backend="moss-tts",
        model="model with spaces",
        generation_profile="stable",
        narrator_character="Rhiannon",
    )
    return fixture, imported, source


def downgrade_workspace_run_config_to_legacy(directory):
    directory = Path(directory)
    workspace_path = directory / "workspace.json"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    run_config = workspace["run_config"]
    run_config.pop("missing_voice_policy")
    run_config.pop("failure_repair_policy")
    fingerprint = workbench_module._workspace_config_fingerprint(
        workspace["source"]["import_id"],
        workspace.get("story_index"),
        workspace.get("voice_manifest"),
        workspace["narrator_character"],
        run_config,
        workspace.get("carry_forward"),
    )
    workspace["config_fingerprint"] = fingerprint
    workspace["workspace_id"] = (
        f"resume-{workspace['source']['import_id'].removeprefix('legacy-')}-"
        f"{fingerprint[:16]}"
    )
    workspace_path.write_text(json.dumps(workspace, sort_keys=True), encoding="utf-8")
    legacy_directory = directory.with_name(workspace["workspace_id"])
    directory.rename(legacy_directory)
    return legacy_directory


def write_carry_target_manifest(root, *, rhiannon_payloads=None):
    target = root / "target-voices"
    target.mkdir()
    payloads = rhiannon_payloads or (
        b"rhiannon-reference-one",
        b"rhiannon-reference-two",
    )
    (target / "rhiannon.wav").write_bytes(payloads[0])
    (target / "rhiannon-2.wav").write_bytes(payloads[1])
    (target / "paper-heron.wav").write_bytes(b"paper-heron-reference")
    manifest = target / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "voices": [
                    {
                        "character": "Rhiannon",
                        "speaker": "Rhiannon",
                        "references": ["rhiannon.wav", "rhiannon-2.wav"],
                    },
                    {
                        "character": "Paper Heron",
                        "speaker": "Paper Heron",
                        "reference": "paper-heron.wav",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


class AuthoringWorkbenchTest(unittest.TestCase):
    def test_workspace_binds_selected_reference_extension_to_copied_wavs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, _workspace = create_test_workspace(root)
            manifest = Path(fixture["job"]["voice_manifest"])
            for index, name in enumerate(("rhiannon.wav", "rhiannon-2.wav"), start=1):
                samples = (
                    np.sin(np.linspace(0, np.pi * 2 * index * 220, 32_000)).astype(
                        np.float32
                    )
                    * 0.2
                )
                write_pcm16_wav(manifest.parent / name, samples, 16_000)
            selected = manifest.with_name("selected-voice-manifest.json")
            select_voice_reference(manifest, "Rhiannon", 2, selected)

            created = create_resume_workspace(
                imported,
                root / "selected-workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=selected,
                backend="moss-tts",
                model="model",
                generation_profile="stable",
                narrator_character="Rhiannon",
            )
            (manifest.parent / "rhiannon-2.wav").write_bytes(b"mutated")
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "selection candidate changed"
            ):
                create_resume_workspace(
                    imported,
                    root / "tampered-workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=selected,
                    backend="moss-tts",
                    model="model",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                )

        self.assertTrue(created.directory.name.startswith("resume-"))

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

    def test_carry_forward_preserves_exact_seed_review_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root)
            review_workspace_item(source.directory, fixture["queue_id"], "approved")
            source_directory = downgrade_workspace_run_config_to_legacy(
                source.directory
            )
            source_state = source_directory / "generated-audio/generation-state.json"
            source_state_sha256 = sha256_file(source_state)
            target_manifest = write_carry_target_manifest(root)

            carried = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=target_manifest,
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Paper Heron",
                carry_forward_from=source_directory,
                carry_forward_characters=("Rhiannon",),
            )
            repeated = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=target_manifest,
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Paper Heron",
                carry_forward_from=source_directory,
                carry_forward_characters=("Rhiannon",),
            )
            state = json.loads(
                (carried.directory / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )
            workspace = json.loads(
                (carried.directory / "workspace.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (carried.directory / "generated-audio/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            source_state_sha256_after = sha256_file(source_state)

        result = state["items"][fixture["queue_id"]]
        self.assertTrue(carried.created)
        self.assertFalse(repeated.created)
        self.assertEqual(carried.directory, repeated.directory)
        self.assertNotEqual(carried.directory, source_directory)
        self.assertEqual(
            (result["status"], result["review_status"]), ("approved", "approved")
        )
        self.assertEqual(result["carry_forward"]["mode"], "review-only")
        self.assertEqual(workspace["carry_forward"]["characters"], ["Rhiannon"])
        self.assertEqual(workspace["narrator_character"], "Paper Heron")
        self.assertEqual(manifest["entries"][0]["carry_forward"]["mode"], "review-only")
        self.assertEqual(source_state_sha256_after, source_state_sha256)

    def test_offline_pocket_fallback_carries_exact_failure_with_fresh_seed_space(self):
        from tests.test_authoring_bulk_generation import SyntheticRenderer
        from vntts.authoring.bulk_generation import (
            load_generation_state,
            run_bulk_generation,
        )
        from vntts.synthesis import SynthesisCompletion

        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root)
            queue_path = source.directory / "queue.jsonl"
            source_output = source.directory / "generated-audio"
            moss_renderer = SyntheticRenderer(
                [
                    SynthesisCompletion.LIMITED,
                    SynthesisCompletion.LIMITED,
                    SynthesisCompletion.LIMITED,
                ],
                diagnostics_backend="moss-tts",
            )
            moss_renderer.name = "moss-tts"
            moss_renderer.model_name = "model with spaces"
            run_bulk_generation(
                queue_path,
                source_output,
                moss_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=2,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                regenerate_existing=True,
            )
            source_state_path = source_output / "generation-state.json"
            source_state_before = source_state_path.read_bytes()
            source_state = load_generation_state(source_state_path, queue_path)
            moss_attempts = source_state["items"][fixture["queue_id"]]["attempts"]
            policy = FailureRepairPolicy(
                offline_fallback_queue_ids=(fixture["queue_id"],)
            )
            with self.assertRaisesRegex(AuthoringWorkbenchError, "Pocket TTS default"):
                create_resume_workspace(
                    imported,
                    root / "unsafe-same-backend",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="another-moss-model",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                    failure_repair_policy=policy,
                    carry_forward_from=source.directory,
                )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "require a source workspace"
            ):
                create_resume_workspace(
                    imported,
                    root / "unsafe-no-source",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="pocket-tts",
                    model="pocket-tts",
                    generation_profile="default",
                    narrator_character="Rhiannon",
                    failure_repair_policy=policy,
                )

            fallback = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                narrator_character="Rhiannon",
                failure_repair_policy=policy,
                carry_forward_from=source.directory,
            )
            carried = load_generation_state(
                fallback.directory / "generated-audio/generation-state.json",
                fallback.directory / "queue.jsonl",
            )
            fallback_state_path = (
                fallback.directory / "generated-audio/generation-state.json"
            )
            fallback_state_bytes = fallback_state_path.read_bytes()
            tampered = json.loads(fallback_state_bytes)
            tampered["items"][fixture["queue_id"]]["last_error"] = "tampered"
            fallback_state_path.write_text(
                json.dumps(tampered, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "carried failure changed"
            ):
                generation_command(fallback.directory, retries=0, seed=0)
            fallback_state_path.write_bytes(fallback_state_bytes)
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "single backend-owned unseeded attempt"
            ):
                generation_command(fallback.directory, retries=1, seed=0)
            command = generation_command(fallback.directory, retries=0, seed=0)
            renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED], diagnostics_backend="pocket-tts"
            )
            renderer.name = "pocket-tts"
            renderer.model_name = "pocket-tts"
            first_result = run_bulk_generation(
                fallback.directory / "queue.jsonl",
                fallback.directory / "generated-audio",
                renderer,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=policy,
            )
            after_first = load_generation_state(
                first_result.state, fallback.directory / "queue.jsonl"
            )
            second_renderer = SyntheticRenderer(diagnostics_backend="pocket-tts")
            second_renderer.name = "pocket-tts"
            second_renderer.model_name = "pocket-tts"
            result = run_bulk_generation(
                fallback.directory / "queue.jsonl",
                fallback.directory / "generated-audio",
                second_renderer,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=policy,
            )
            final = load_generation_state(
                result.state, fallback.directory / "queue.jsonl"
            )
            source_state_after = source_state_path.read_bytes()

        carried_item = carried["items"][fixture["queue_id"]]
        final_item = final["items"][fixture["queue_id"]]
        self.assertEqual(carried_item["carry_forward"]["mode"], "failed-outcome")
        self.assertEqual(carried_item["provider"], "moss-tts")
        self.assertEqual([request.seed for request in renderer.requests], [None])
        self.assertEqual([request.seed for request in second_renderer.requests], [None])
        self.assertEqual(after_first["items"][fixture["queue_id"]]["status"], "failed")
        self.assertEqual(final_item["attempts"], moss_attempts + 2)
        self.assertEqual(
            final_item["attempts_by_provider"],
            {"moss-tts": moss_attempts, "pocket-tts": 2},
        )
        self.assertEqual(final_item["provider"], "pocket-tts")
        self.assertEqual(final_item["seed"], 1)
        self.assertFalse(final_item["seed_applied"])
        self.assertEqual(
            final_item["failure_repair"]["source_failure"]["source_provider"],
            "moss-tts",
        )
        self.assertEqual(
            command[command.index("--offline-fallback-failed") + 1],
            fixture["queue_id"],
        )
        self.assertEqual(source_state_after, source_state_before)

    def test_sentence_repair_carries_exact_current_failure_between_workspaces(self):
        from tests.test_authoring_bulk_generation import SyntheticRenderer
        from vntts.authoring.bulk_generation import (
            load_generation_state,
            run_bulk_generation,
        )
        from vntts.synthesis import SynthesisCompletion

        text = "The first sentence is complete. The second sentence is also complete."
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root, text=text)
            queue_path = source.directory / "queue.jsonl"
            source_output = source.directory / "generated-audio"
            failed_renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED], diagnostics_backend="moss-tts"
            )
            failed_renderer.name = "moss-tts"
            failed_renderer.model_name = "model with spaces"
            run_bulk_generation(
                queue_path,
                source_output,
                failed_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                regenerate_existing=True,
            )
            source_state_path = source_output / "generation-state.json"
            source_state_before = source_state_path.read_bytes()
            source_item = load_generation_state(source_state_path, queue_path)["items"][
                fixture["queue_id"]
            ]
            policy = FailureRepairPolicy(
                sentence_segment_queue_ids=(fixture["queue_id"],)
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "exact source backend"
            ):
                create_resume_workspace(
                    imported,
                    root / "mismatched",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=fixture["job"]["voice_manifest"],
                    backend="moss-tts",
                    model="different model",
                    generation_profile="stable",
                    narrator_character="Rhiannon",
                    failure_repair_policy=policy,
                    carry_forward_from=source.directory,
                )

            repaired = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=policy,
                carry_forward_from=source.directory,
            )
            repaired_state_path = (
                repaired.directory / "generated-audio/generation-state.json"
            )
            carried_item = load_generation_state(
                repaired_state_path, repaired.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            command = generation_command(repaired.directory, retries=0, seed=0)
            success_renderer = SyntheticRenderer(diagnostics_backend="moss-tts")
            success_renderer.name = "moss-tts"
            success_renderer.model_name = "model with spaces"
            result = run_bulk_generation(
                repaired.directory / "queue.jsonl",
                repaired.directory / "generated-audio",
                success_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=policy,
            )
            final_item = load_generation_state(
                result.state, repaired.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            inspect_workspace(repaired.directory)
            source_state_after = source_state_path.read_bytes()

        self.assertEqual(carried_item["status"], "failed")
        self.assertEqual(carried_item["attempts"], source_item["attempts"])
        self.assertEqual(carried_item["carry_forward"]["mode"], "failed-outcome")
        self.assertEqual(final_item["status"], "generated")
        self.assertEqual(final_item["attempts"], source_item["attempts"] + 1)
        self.assertEqual(
            final_item["failure_repair"]["strategy"],
            "sentence_boundary_segmentation",
        )
        self.assertEqual(final_item["carry_forward"], carried_item["carry_forward"])
        self.assertEqual(source_state_after, source_state_before)
        self.assertEqual(
            command[command.index("--sentence-segment-failed") + 1],
            fixture["queue_id"],
        )

    def test_sentence_repair_carries_typed_internal_silence_failure(self):
        from tests.test_authoring_bulk_generation import (
            SyntheticRenderer,
            audio_samples,
        )
        from vntts.authoring.bulk_generation import (
            load_generation_state,
            run_bulk_generation,
        )

        text = "The first warning is clear. The second warning is equally clear."
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root, text=text)
            queue_path = source.directory / "queue.jsonl"
            source_output = source.directory / "generated-audio"
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )
            failed_renderer = SyntheticRenderer(
                diagnostics_backend="moss-tts", pcm=failed_pcm
            )
            failed_renderer.name = "moss-tts"
            failed_renderer.model_name = "model with spaces"
            run_bulk_generation(
                queue_path,
                source_output,
                failed_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                regenerate_existing=True,
            )
            source_state_path = source_output / "generation-state.json"
            source_state_before = source_state_path.read_bytes()
            source_item = load_generation_state(source_state_path, queue_path)["items"][
                fixture["queue_id"]
            ]
            policy = FailureRepairPolicy(
                sentence_segment_queue_ids=(fixture["queue_id"],)
            )

            repaired = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=policy,
                carry_forward_from=source.directory,
            )
            repaired_state = load_generation_state(
                repaired.directory / "generated-audio/generation-state.json",
                repaired.directory / "queue.jsonl",
            )
            summary = inspect_workspace(repaired.directory)
            source_state_after = source_state_path.read_bytes()

        self.assertEqual(source_item["failure"]["kind"], "speech_silence")
        self.assertEqual(
            repaired_state["items"][fixture["queue_id"]]["carry_forward"]["mode"],
            "failed-outcome",
        )
        self.assertEqual(summary.failed, 1)
        self.assertEqual(source_state_after, source_state_before)

    def test_inline_pause_repair_carries_exact_internal_silence_failure(self):
        from tests.test_authoring_bulk_generation import (
            SyntheticRenderer,
            audio_samples,
        )
        from vntts.authoring.bulk_generation import (
            load_generation_state,
            publish_generated_manifest,
            run_bulk_generation,
        )

        text = "All of that is remarkable. But, Aderyn is still waiting."
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root, text=text)
            queue_path = source.directory / "queue.jsonl"
            source_output = source.directory / "generated-audio"
            source_state_path = source_output / "generation-state.json"
            initial = load_generation_state(source_state_path, queue_path)
            initial_item = initial["items"].pop(fixture["queue_id"])
            (source_output / initial_item["path"]).unlink()
            source_state_path.write_text(
                json.dumps(initial, sort_keys=True), encoding="utf-8"
            )
            publish_generated_manifest(source_state_path)
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )
            failed_renderer = SyntheticRenderer(
                diagnostics_backend="moss-tts", pcm=failed_pcm
            )
            failed_renderer.name = "moss-tts"
            failed_renderer.model_name = "model with spaces"
            run_bulk_generation(
                queue_path,
                source_output,
                failed_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                regenerate_existing=True,
            )
            source_state_before = source_state_path.read_bytes()
            source_item = load_generation_state(source_state_path, queue_path)["items"][
                fixture["queue_id"]
            ]
            policy = FailureRepairPolicy(
                inline_pause_queue_ids=(fixture["queue_id"],), inline_pause_ms=180
            )
            repaired = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=policy,
                carry_forward_from=source.directory,
            )
            carried = load_generation_state(
                repaired.directory / "generated-audio/generation-state.json",
                repaired.directory / "queue.jsonl",
            )["items"][fixture["queue_id"]]
            command = generation_command(repaired.directory, retries=0, seed=0)
            success_renderer = SyntheticRenderer(diagnostics_backend="moss-tts")
            success_renderer.name = "moss-tts"
            success_renderer.model_name = "model with spaces"
            result = run_bulk_generation(
                repaired.directory / "queue.jsonl",
                repaired.directory / "generated-audio",
                success_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=policy,
            )
            final_item = load_generation_state(
                result.state, repaired.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            inspect_workspace(repaired.directory)
            review_workspace_item(repaired.directory, fixture["queue_id"], "approved")
            merged = merge_workspace_outcomes(
                source.directory, (repaired.directory,), root / "merged-inline"
            )
            merged_item = load_generation_state(
                merged.directory / "generated-audio/generation-state.json",
                merged.directory / "queue.jsonl",
            )["items"][fixture["queue_id"]]
            inspect_workspace(merged.directory)
            source_state_after = source_state_path.read_bytes()

        self.assertEqual(source_item["failure"]["kind"], "speech_silence")
        self.assertEqual(carried["carry_forward"]["mode"], "failed-outcome")
        self.assertEqual(final_item["status"], "generated")
        self.assertEqual(final_item["attempts"], source_item["attempts"] + 1)
        self.assertEqual(
            final_item["failure_repair"]["strategy"], "inline_pause_marker"
        )
        self.assertEqual(final_item["carry_forward"], carried["carry_forward"])
        self.assertEqual(merged_item["status"], "approved")
        self.assertEqual(
            merged_item["failure_repair"]["strategy"], "inline_pause_marker"
        )
        self.assertEqual(
            merged_item["outcome_merge"]["source_workspace_id"],
            repaired.directory.name,
        )
        self.assertEqual(source_state_after, source_state_before)
        self.assertEqual(
            command[command.index("--inline-pause-failed") + 1],
            fixture["queue_id"],
        )

    def test_exhausted_inline_pause_failure_moves_to_one_typed_pocket_attempt(self):
        from tests.test_authoring_bulk_generation import (
            SyntheticRenderer,
            audio_samples,
        )
        from vntts.authoring.bulk_generation import (
            generation_failure_repair_plan,
            load_generation_state,
            publish_generated_manifest,
            run_bulk_generation,
        )
        from vntts.synthesis import SynthesisCompletion

        text = "What happened? You're hurt."
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root, text=text)
            queue_path = source.directory / "queue.jsonl"
            source_output = source.directory / "generated-audio"
            source_state_path = source_output / "generation-state.json"
            initial = load_generation_state(source_state_path, queue_path)
            initial_item = initial["items"].pop(fixture["queue_id"])
            (source_output / initial_item["path"]).unlink()
            source_state_path.write_text(
                json.dumps(initial, sort_keys=True), encoding="utf-8"
            )
            publish_generated_manifest(source_state_path)
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )

            def failed_renderer():
                renderer = SyntheticRenderer(
                    diagnostics_backend="moss-tts", pcm=failed_pcm
                )
                renderer.name = "moss-tts"
                renderer.model_name = "model with spaces"
                return renderer

            run_bulk_generation(
                queue_path,
                source_output,
                failed_renderer(),
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                regenerate_existing=True,
            )
            inline_policy = FailureRepairPolicy(
                inline_pause_queue_ids=(fixture["queue_id"],), inline_pause_ms=180
            )
            repaired = create_resume_workspace(
                imported,
                root / "inline",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=inline_policy,
                carry_forward_from=source.directory,
            )
            for attempt in range(2):
                renderer = (
                    failed_renderer()
                    if attempt == 0
                    else SyntheticRenderer(
                        [SynthesisCompletion.LIMITED],
                        diagnostics_backend="moss-tts",
                    )
                )
                renderer.name = "moss-tts"
                renderer.model_name = "model with spaces"
                run_bulk_generation(
                    repaired.directory / "queue.jsonl",
                    repaired.directory / "generated-audio",
                    renderer,
                    provider="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    retries=0,
                    seed=0,
                    include_queue_ids=(fixture["queue_id"],),
                    failure_repair_policy=inline_policy,
                )
            repaired_state_path = (
                repaired.directory / "generated-audio/generation-state.json"
            )
            repaired_state_before = repaired_state_path.read_bytes()
            repaired_item = load_generation_state(
                repaired_state_path, repaired.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            plan = generation_failure_repair_plan(
                repaired_state_path, repaired.directory / "queue.jsonl"
            )
            fallback_policy = FailureRepairPolicy(
                offline_fallback_queue_ids=(fixture["queue_id"],)
            )
            fallback = create_resume_workspace(
                imported,
                root / "fallback",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                narrator_character="Rhiannon",
                failure_repair_policy=fallback_policy,
                carry_forward_from=repaired.directory,
            )
            fallback_state_path = (
                fallback.directory / "generated-audio/generation-state.json"
            )
            carried_item = load_generation_state(
                fallback_state_path, fallback.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            fallback_state_before = fallback_state_path.read_bytes()
            tampered = json.loads(fallback_state_before)
            tampered["items"][fixture["queue_id"]]["carry_forward"][
                "source_repair_strategy"
            ] = "sentence_boundary_segmentation"
            fallback_state_path.write_text(
                json.dumps(tampered, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError,
                "carried failure source changed",
            ):
                inspect_workspace(fallback.directory)
            fallback_state_path.write_bytes(fallback_state_before)
            command = generation_command(fallback.directory, retries=0, seed=0)
            pocket = SyntheticRenderer(diagnostics_backend="pocket-tts")
            pocket.name = "pocket-tts"
            pocket.model_name = "pocket-tts"
            result = run_bulk_generation(
                fallback.directory / "queue.jsonl",
                fallback.directory / "generated-audio",
                pocket,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=fallback_policy,
            )
            final_item = load_generation_state(
                result.state, fallback.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            inspect_workspace(fallback.directory)
            final_state_before = result.state.read_bytes()
            for field, value in (
                ("source_repair_strategy", "edge_silence_trim"),
                ("source_provider_attempts", 2),
            ):
                tampered = json.loads(final_state_before)
                tampered["items"][fixture["queue_id"]]["failure_repair"][
                    "source_failure"
                ][field] = value
                result.state.write_text(
                    json.dumps(tampered, sort_keys=True), encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    bulk_generation_module.BulkGenerationError,
                    "offline fallback source "
                    "(is inconsistent|attempts are not exhausted|repair is invalid)",
                ):
                    load_generation_state(
                        result.state, fallback.directory / "queue.jsonl"
                    )
                result.state.write_bytes(final_state_before)
            repaired_state_after = repaired_state_path.read_bytes()

        source_failure = carried_item["carry_forward"]
        self.assertEqual(repaired_item["status"], "failed")
        self.assertEqual(repaired_item["attempts"], 3)
        self.assertEqual(repaired_item["attempts_by_provider"], {"moss-tts": 3})
        self.assertEqual(plan["records"][0]["action"], "offline_fallback_backend")
        self.assertEqual(
            source_failure["source_failure_kind"], "missed_eos_audio_limit"
        )
        self.assertEqual(
            source_failure["source_repair_strategy"], "inline_pause_marker"
        )
        self.assertEqual(source_failure["source_provider_attempts"], 3)
        self.assertEqual([request.seed for request in pocket.requests], [None])
        self.assertEqual(final_item["status"], "generated")
        self.assertEqual(final_item["attempts"], 4)
        self.assertEqual(
            final_item["attempts_by_provider"],
            {"moss-tts": 3, "pocket-tts": 1},
        )
        self.assertEqual(
            final_item["failure_repair"]["source_failure"]["source_repair_strategy"],
            "inline_pause_marker",
        )
        self.assertFalse(final_item["seed_applied"])
        self.assertEqual(repaired_state_after, repaired_state_before)
        self.assertEqual(
            command[command.index("--offline-fallback-failed") + 1],
            fixture["queue_id"],
        )

    def test_exhausted_raw_inline_pause_failure_moves_to_pocket(self):
        from tests.test_authoring_bulk_generation import (
            SyntheticRenderer,
            audio_samples,
        )
        from vntts.authoring.bulk_generation import (
            generation_failure_repair_plan,
            load_generation_state,
            publish_generated_manifest,
            run_bulk_generation,
        )

        text = "What happened? You're hurt."
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root, text=text)
            queue_path = source.directory / "queue.jsonl"
            source_output = source.directory / "generated-audio"
            source_state_path = source_output / "generation-state.json"
            initial = load_generation_state(source_state_path, queue_path)
            initial_item = initial["items"].pop(fixture["queue_id"])
            (source_output / initial_item["path"]).unlink()
            source_state_path.write_text(
                json.dumps(initial, sort_keys=True), encoding="utf-8"
            )
            publish_generated_manifest(source_state_path)
            tone = audio_samples()
            failed_pcm = np.concatenate(
                (tone, np.zeros(16_000 * 2, dtype=np.float32), tone)
            )

            def failed_renderer():
                renderer = SyntheticRenderer(
                    diagnostics_backend="moss-tts", pcm=failed_pcm
                )
                renderer.name = "moss-tts"
                renderer.model_name = "model with spaces"
                return renderer

            for attempt in range(3):
                run_bulk_generation(
                    queue_path,
                    source_output,
                    failed_renderer(),
                    provider="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    retries=0,
                    seed=0,
                    include_queue_ids=(fixture["queue_id"],),
                    regenerate_existing=attempt == 0,
                )
            source_state_before = source_state_path.read_bytes()
            source_item = load_generation_state(source_state_path, queue_path)["items"][
                fixture["queue_id"]
            ]
            plan = generation_failure_repair_plan(source_state_path, queue_path)
            fallback_policy = FailureRepairPolicy(
                offline_fallback_queue_ids=(fixture["queue_id"],)
            )
            fallback = create_resume_workspace(
                imported,
                root / "fallback",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                narrator_character="Rhiannon",
                failure_repair_policy=fallback_policy,
                carry_forward_from=source.directory,
            )
            carried_item = load_generation_state(
                fallback.directory / "generated-audio/generation-state.json",
                fallback.directory / "queue.jsonl",
            )["items"][fixture["queue_id"]]
            pocket = SyntheticRenderer(diagnostics_backend="pocket-tts")
            pocket.name = "pocket-tts"
            pocket.model_name = "pocket-tts"
            generated = run_bulk_generation(
                fallback.directory / "queue.jsonl",
                fallback.directory / "generated-audio",
                pocket,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=fallback_policy,
            )
            final_item = load_generation_state(
                generated.state, fallback.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            inspect_workspace(fallback.directory)
            source_state_after = source_state_path.read_bytes()

        self.assertEqual(source_item["attempts_by_provider"], {"moss-tts": 3})
        self.assertNotIn("failure_repair", source_item)
        self.assertEqual(plan["records"][0]["action"], "offline_fallback_backend")
        self.assertNotIn("source_repair_strategy", carried_item["carry_forward"])
        self.assertEqual(
            carried_item["carry_forward"]["source_failure_kind"], "speech_silence"
        )
        self.assertEqual([request.seed for request in pocket.requests], [None])
        self.assertEqual(final_item["status"], "generated")
        self.assertEqual(
            final_item["attempts_by_provider"],
            {"moss-tts": 3, "pocket-tts": 1},
        )
        self.assertEqual(source_state_after, source_state_before)

    def test_bounded_seed_repair_carries_provider_attempts_and_stops_at_three(self):
        from tests.test_authoring_bulk_generation import SyntheticRenderer
        from vntts.authoring.bulk_generation import (
            load_generation_state,
            publish_generated_manifest,
            run_bulk_generation,
        )
        from vntts.synthesis import SynthesisCompletion

        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root)
            queue_path = source.directory / "queue.jsonl"
            source_state_path = (
                source.directory / "generated-audio/generation-state.json"
            )
            initial = load_generation_state(source_state_path, queue_path)
            initial_item = initial["items"].pop(fixture["queue_id"])
            (source.directory / "generated-audio" / initial_item["path"]).unlink()
            source_state_path.write_text(
                json.dumps(initial, sort_keys=True), encoding="utf-8"
            )
            publish_generated_manifest(source_state_path)
            failed_renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED], diagnostics_backend="moss-tts"
            )
            failed_renderer.name = "moss-tts"
            failed_renderer.model_name = "model with spaces"
            run_bulk_generation(
                queue_path,
                source.directory / "generated-audio",
                failed_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
            )
            source_state_before = source_state_path.read_bytes()
            policy = FailureRepairPolicy(
                bounded_seed_retry_queue_ids=(fixture["queue_id"],)
            )
            repaired = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=policy,
                carry_forward_from=source.directory,
            )
            carried = load_generation_state(
                repaired.directory / "generated-audio/generation-state.json",
                repaired.directory / "queue.jsonl",
            )["items"][fixture["queue_id"]]
            command = generation_command(repaired.directory, retries=1, seed=0)
            renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED, SynthesisCompletion.LIMITED],
                diagnostics_backend="moss-tts",
            )
            renderer.name = "moss-tts"
            renderer.model_name = "model with spaces"
            result = run_bulk_generation(
                repaired.directory / "queue.jsonl",
                repaired.directory / "generated-audio",
                renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=1,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=policy,
            )
            final = load_generation_state(
                result.state, repaired.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            inspect_workspace(repaired.directory)
            fallback_policy = FailureRepairPolicy(
                offline_fallback_queue_ids=(fixture["queue_id"],)
            )
            fallback = create_resume_workspace(
                imported,
                root / "fallback",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                narrator_character="Rhiannon",
                failure_repair_policy=fallback_policy,
                carry_forward_from=repaired.directory,
            )
            fallback_item = load_generation_state(
                fallback.directory / "generated-audio/generation-state.json",
                fallback.directory / "queue.jsonl",
            )["items"][fixture["queue_id"]]
            inspect_workspace(fallback.directory)
            repaired_state_path = (
                repaired.directory / "generated-audio/generation-state.json"
            )
            repaired_state_before = repaired_state_path.read_bytes()
            pocket = SyntheticRenderer(diagnostics_backend="pocket-tts")
            pocket.name = "pocket-tts"
            pocket.model_name = "pocket-tts"
            pocket_result = run_bulk_generation(
                fallback.directory / "queue.jsonl",
                fallback.directory / "generated-audio",
                pocket,
                provider="pocket-tts",
                model="pocket-tts",
                generation_profile="default",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=fallback_policy,
            )
            pocket_item = load_generation_state(
                pocket_result.state, fallback.directory / "queue.jsonl"
            )["items"][fixture["queue_id"]]
            inspect_workspace(fallback.directory)
            review_workspace_item(fallback.directory, fixture["queue_id"], "approved")
            merged = merge_workspace_outcomes(
                source.directory, (fallback.directory,), root / "merged"
            )
            merged_item = load_generation_state(
                merged.directory / "generated-audio/generation-state.json",
                merged.directory / "queue.jsonl",
            )["items"][fixture["queue_id"]]
            inspect_workspace(merged.directory)
            source_state_after = source_state_path.read_bytes()
            repaired_state_after = repaired_state_path.read_bytes()

        self.assertEqual(carried["carry_forward"]["source_provider_attempts"], 1)
        self.assertEqual([request.seed for request in renderer.requests], [1, 2])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["attempts"], 3)
        self.assertEqual(final["attempts_by_provider"], {"moss-tts": 3})
        self.assertEqual(final["failure_repair"]["strategy"], "bounded_seed_retry")
        self.assertEqual(
            fallback_item["carry_forward"]["source_parent_carry_forward"],
            final["carry_forward"],
        )
        self.assertEqual([request.seed for request in pocket.requests], [None])
        self.assertEqual(pocket_item["status"], "generated")
        self.assertFalse(pocket_item["seed_applied"])
        self.assertEqual(merged_item["status"], "approved")
        self.assertEqual(
            merged_item["outcome_merge"]["source_workspace_id"],
            fallback.directory.name,
        )
        self.assertEqual(repaired_state_after, repaired_state_before)
        self.assertEqual(source_state_after, source_state_before)
        self.assertEqual(
            command[command.index("--bounded-seed-failed") + 1],
            fixture["queue_id"],
        )

    def test_outcome_merge_copies_only_exact_reviewed_repair_and_is_idempotent(self):
        from tests.test_authoring_bulk_generation import SyntheticRenderer
        from vntts.authoring.bulk_generation import (
            load_generation_state,
            run_bulk_generation,
        )
        from vntts.synthesis import SynthesisCompletion

        text = "The first sentence is complete. The second sentence is also complete."
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root, text=text)
            queue_path = source.directory / "queue.jsonl"
            source_state_path = (
                source.directory / "generated-audio/generation-state.json"
            )
            failed_renderer = SyntheticRenderer(
                [SynthesisCompletion.LIMITED], diagnostics_backend="moss-tts"
            )
            failed_renderer.name = "moss-tts"
            failed_renderer.model_name = "model with spaces"
            run_bulk_generation(
                queue_path,
                source.directory / "generated-audio",
                failed_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                regenerate_existing=True,
            )
            source_state_before = source_state_path.read_bytes()
            policy = FailureRepairPolicy(
                sentence_segment_queue_ids=(fixture["queue_id"],)
            )
            repaired = create_resume_workspace(
                imported,
                root / "repairs",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=policy,
                carry_forward_from=source.directory,
            )
            success_renderer = SyntheticRenderer(diagnostics_backend="moss-tts")
            success_renderer.name = "moss-tts"
            success_renderer.model_name = "model with spaces"
            run_bulk_generation(
                repaired.directory / "queue.jsonl",
                repaired.directory / "generated-audio",
                success_renderer,
                provider="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                retries=0,
                seed=0,
                include_queue_ids=(fixture["queue_id"],),
                failure_repair_policy=policy,
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "no reviewed repair outcomes"
            ):
                merge_workspace_outcomes(
                    source.directory, (repaired.directory,), root / "merged"
                )
            review_workspace_item(repaired.directory, fixture["queue_id"], "approved")
            repair_state_path = (
                repaired.directory / "generated-audio/generation-state.json"
            )
            repair_state_before = repair_state_path.read_bytes()

            merged = merge_workspace_outcomes(
                source.directory, (repaired.directory,), root / "merged"
            )
            repeated = merge_workspace_outcomes(
                source.directory, (repaired.directory,), root / "merged"
            )
            summary = inspect_workspace(merged.directory)
            merged_state = load_generation_state(
                merged.directory / "generated-audio/generation-state.json",
                merged.directory / "queue.jsonl",
            )
            item = merged_state["items"][fixture["queue_id"]]
            manifest = json.loads(
                (merged.directory / "generated-audio/manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            workspace = json.loads(
                (merged.directory / "workspace.json").read_text(encoding="utf-8")
            )

            self.assertTrue(merged.created)
            self.assertFalse(repeated.created)
            self.assertEqual(summary.approved, 1)
            self.assertEqual(item["status"], "approved")
            self.assertEqual(item["review_status"], "approved")
            self.assertEqual(
                item["outcome_merge"]["source_workspace_id"],
                repaired.directory.name,
            )
            self.assertEqual(len(manifest["entries"]), 1)
            self.assertEqual(
                manifest["entries"][0]["outcome_merge"], item["outcome_merge"]
            )
            self.assertEqual(
                workspace["outcome_merge"]["items"][0]["queue_id"], fixture["queue_id"]
            )
            self.assertEqual(source_state_path.read_bytes(), source_state_before)
            self.assertEqual(repair_state_path.read_bytes(), repair_state_before)

            tampered = json.loads(source_state_path.read_text(encoding="utf-8"))
            tampered["items"][fixture["queue_id"]]["last_error"] = "changed authority"
            source_state_path.write_text(
                json.dumps(tampered, sort_keys=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(AuthoringWorkbenchError, "authority is stale"):
                merge_workspace_outcomes(
                    source.directory, (repaired.directory,), root / "different-root"
                )

    def test_carry_forward_copies_new_full_outcome_with_exact_controls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root)
            source_state_path = (
                source.directory / "generated-audio/generation-state.json"
            )
            source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
            queue_item = VoiceGenerationQueue.load(
                source.directory / "queue.jsonl"
            ).items[0]
            item = source_state["items"][fixture["queue_id"]]
            audio = source.directory / "generated-audio" / item["path"]
            write_pcm16_wav(
                audio,
                np.sin(np.linspace(0, 6 * np.pi, 6_000, dtype=np.float32)) * 0.2,
                16_000,
            )
            item.update(self._current_carry_fields(source.directory, queue_item, audio))
            source_state["active"] = None
            source_state_path.write_text(
                json.dumps(source_state, sort_keys=True), encoding="utf-8"
            )
            source_audio = audio.read_bytes()
            target_manifest = write_carry_target_manifest(root)
            source_directory = downgrade_workspace_run_config_to_legacy(
                source.directory
            )

            carried = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=target_manifest,
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Paper Heron",
                carry_forward_from=source_directory,
                carry_forward_characters=("Rhiannon",),
            )
            target_state = json.loads(
                (carried.directory / "generated-audio/generation-state.json").read_text(
                    encoding="utf-8"
                )
            )
            target_item = target_state["items"][fixture["queue_id"]]
            target_audio = carried.directory / "generated-audio" / target_item["path"]
            target_audio_sha256 = sha256_file(target_audio)
            target_audio_payload = target_audio.read_bytes()

        self.assertEqual(target_item["carry_forward"]["mode"], "full-outcome")
        self.assertEqual(target_item["file_sha256"], target_audio_sha256)
        self.assertEqual(target_audio_payload, source_audio)

    def test_full_carry_forward_honors_exact_queue_voice_override(self):
        variant = "Rhiannon portrait variant"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(
                root, queue_voice_override=variant
            )
            source_state_path = (
                source.directory / "generated-audio/generation-state.json"
            )
            source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
            queue_item = VoiceGenerationQueue.load(
                source.directory / "queue.jsonl"
            ).items[0]
            item = source_state["items"][fixture["queue_id"]]
            audio = source.directory / "generated-audio" / item["path"]
            write_pcm16_wav(
                audio,
                np.sin(np.linspace(0, 6 * np.pi, 6_000, dtype=np.float32)) * 0.2,
                16_000,
            )
            item.update(
                self._current_carry_fields(
                    source.directory,
                    queue_item,
                    audio,
                    voice_character=variant,
                )
            )
            source_state["active"] = None
            source_state_path.write_text(
                json.dumps(source_state, sort_keys=True), encoding="utf-8"
            )

            carried = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="model with spaces",
                generation_profile="stable",
                narrator_character="Rhiannon",
                carry_forward_from=source.directory,
                carry_forward_characters=("Rhiannon",),
            )
            target_state = bulk_generation_module.load_generation_state(
                carried.directory / "generated-audio/generation-state.json",
                carried.directory / "queue.jsonl",
            )
            target_item = target_state["items"][fixture["queue_id"]]

        self.assertEqual(target_item["carry_forward"]["mode"], "full-outcome")
        self.assertEqual(target_item["voice_character"], variant)
        self.assertEqual(target_item["status"], "approved")

    def test_full_carry_forward_rejects_changed_character_references(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root)
            state_path = source.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            queue_item = VoiceGenerationQueue.load(
                source.directory / "queue.jsonl"
            ).items[0]
            item = state["items"][fixture["queue_id"]]
            audio = source.directory / "generated-audio" / item["path"]
            item.update(self._current_carry_fields(source.directory, queue_item, audio))
            state["active"] = None
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            target_manifest = write_carry_target_manifest(
                root,
                rhiannon_payloads=(b"different-rhiannon", b"rhiannon-reference-two"),
            )

            with self.assertRaisesRegex(AuthoringWorkbenchError, "references differ"):
                create_resume_workspace(
                    imported,
                    root / "workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=target_manifest,
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Paper Heron",
                    carry_forward_from=source.directory,
                    carry_forward_characters=("Rhiannon",),
                )
            remaining = [
                path
                for path in (root / "workspaces").iterdir()
                if not path.name.startswith(".")
            ]

        self.assertEqual(
            [path.resolve() for path in remaining], [source.directory.resolve()]
        )

    def test_carry_forward_rejects_narrator_and_moving_source_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = create_carry_source_workspace(root)
            review_workspace_item(source.directory, fixture["queue_id"], "approved")
            target_manifest = write_carry_target_manifest(root)
            with self.assertRaisesRegex(AuthoringWorkbenchError, "exclude Narrator"):
                create_resume_workspace(
                    imported,
                    root / "workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=target_manifest,
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Paper Heron",
                    carry_forward_from=source.directory,
                    carry_forward_characters=("Narrator",),
                )

            state_path = source.directory / "generated-audio/generation-state.json"
            real_publish = workbench_module.publish_generated_manifest

            def mutate_source_after_staging(*args, **kwargs):
                result = real_publish(*args, **kwargs)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["external_change"] = True
                state_path.write_text(
                    json.dumps(state, sort_keys=True), encoding="utf-8"
                )
                return result

            with (
                patch.object(
                    workbench_module,
                    "publish_generated_manifest",
                    side_effect=mutate_source_after_staging,
                ),
                self.assertRaisesRegex(AuthoringWorkbenchError, "source state changed"),
            ):
                create_resume_workspace(
                    imported,
                    root / "workspaces",
                    story_index=fixture["job"]["story_index"],
                    voice_manifest=target_manifest,
                    backend="moss-tts",
                    model="model with spaces",
                    generation_profile="stable",
                    narrator_character="Paper Heron",
                    carry_forward_from=source.directory,
                    carry_forward_characters=("Rhiannon",),
                )
            remaining = [
                path.resolve()
                for path in (root / "workspaces").iterdir()
                if not path.name.startswith(".")
            ]

        self.assertEqual(remaining, [source.directory.resolve()])

    def _current_carry_fields(
        self, workspace_directory, queue_item, audio, *, voice_character="Rhiannon"
    ):
        workspace = json.loads(
            (workspace_directory / "workspace.json").read_text(encoding="utf-8")
        )
        return {
            "status": "approved",
            "review_status": "approved",
            "file_sha256": sha256_file(audio),
            "provider": "moss-tts",
            "model": "model with spaces",
            "prompt_sha256": bulk_generation_module.NO_PROMPT_SHA256,
            "prompt_applied": False,
            "queue_annotations_sha256": bulk_generation_module._canonical_sha256(
                queue_item.document.get("prompt_adapters") or {}
            ),
            "synthesis_text_sha256": hashlib.sha256(
                queue_item.text.encode("utf-8")
            ).hexdigest(),
            "text_transform": "short-trailing-ellipsis-v1",
            "synthesis_provenance_sha256": workbench_module._workspace_generation_provenance(
                workspace_directory, workspace
            ),
            "generation_profile": "stable",
            "voice_character": voice_character,
            "quality": asdict(bulk_generation_module.inspect_generated_wav(audio)),
            "speech_quality": asdict(
                bulk_generation_module.inspect_generated_speech(audio)
            ),
        }

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
        self.assertGreater(items[0].duration_seconds, 0)
        self.assertGreater(items[0].words_per_minute, 0)
        self.assertGreater(items[0].peak, 0)
        self.assertIsInstance(items[0].technical_flags, tuple)
        self.assertEqual(source_hash_after, source_hash)

    def test_review_technical_metrics_are_conservative_attention_aids(self):
        result = {
            "quality": {"duration_seconds": 6.0, "peak": 0.99},
            "speech_quality": {
                "silence_ratio": 0.2,
                "longest_internal_silence_seconds": 0.6,
            },
        }

        duration, words_per_minute, peak, flags = (
            workbench_module._review_technical_metrics(result, "Three deliberate words")
        )

        self.assertEqual(duration, 6.0)
        self.assertEqual(words_per_minute, 30.0)
        self.assertEqual(peak, 0.99)
        self.assertEqual(
            flags,
            ("near clipping", "slow pace"),
        )
        item = SimpleNamespace(
            duration_seconds=6.0,
            failure_category=None,
            internal_pause_seconds=None,
            words_per_minute=30.0,
            peak=0.99,
            technical_flags=flags,
            repair_strategy=None,
        )
        summary = workbench_module.review_technical_summary(item)
        self.assertIn("advisory measurements (listen to decide):", summary)
        self.assertNotIn("notable silence", summary)
        self.assertEqual(
            workbench_module._review_technical_metrics({}, "No WAV"),
            (None, None, None, ()),
        )
        self.assertEqual(
            workbench_module.generation_failure_category(
                "Typed render completed as limited; WAV was not published"
            ),
            "audio limit / missed EOS",
        )
        self.assertEqual(
            workbench_module.generation_failure_category(
                "Generated WAV failed speech-silence validation"
            ),
            "speech silence",
        )
        long_pause_failure = {
            "failure": {
                "schema_version": 1,
                "kind": "speech_silence",
                "error_type": "SpeechSilenceValidationError",
                "text_features": {},
                "speech_quality": {
                    "leading_silence_seconds": 0.0,
                    "trailing_silence_seconds": 0.0,
                    "longest_internal_silence_seconds": 2.4,
                    "silence_ratio": 0.4,
                },
            }
        }
        self.assertEqual(
            workbench_module.generation_failure_category(
                long_pause_failure,
                text="A complete first sentence. A complete second sentence.",
            ),
            "Long sentence-boundary pause",
        )
        raw_failure = workbench_module.ReviewItem(
            queue_id="queue-failed",
            line_id="line-failed",
            speaker="Dobharchú",
            voice_character="Dobharchú",
            text="A complete first sentence. A complete second sentence.",
            status="failed",
            review_status=None,
            attempts=1,
            seed=0,
            last_error="Generated WAV failed speech-silence validation",
            audio=None,
            failure_category="Long sentence-boundary pause",
            internal_pause_seconds=2.4,
        )
        self.assertEqual(
            workbench_module.review_technical_summary(raw_failure),
            "Failure: Long sentence-boundary pause | measured raw pause 2.40s",
        )
        repaired = workbench_module.ReviewItem(
            queue_id="queue-repaired",
            line_id="line-repaired",
            speaker="Dobharchú",
            voice_character="Dobharchú",
            text="A complete first sentence. A complete second sentence.",
            status="generated",
            review_status="pending_review",
            attempts=2,
            seed=1,
            last_error=None,
            audio=Path("repaired.wav"),
            duration_seconds=3.0,
            internal_pause_seconds=0.72,
            repair_strategy="sentence_boundary_segmentation",
        )
        self.assertEqual(
            workbench_module.review_technical_summary(repaired),
            "3.00s | technical pass | repaired pause 0.72s",
        )
        self.assertEqual(
            workbench_module._review_voice_character(
                SimpleNamespace(speaker="???", voice_character="Hero"), {}
            ),
            "Narrator",
        )

    def test_review_silence_attention_policy_v2_boundaries(self):
        def flags(silence_ratio, internal_pause):
            result = {
                "quality": {"duration_seconds": 1.5, "peak": 0.2},
                "speech_quality": {
                    "silence_ratio": silence_ratio,
                    "longest_internal_silence_seconds": internal_pause,
                },
            }
            return workbench_module._review_technical_metrics(
                result, "Three deliberate words"
            )[3]

        self.assertEqual(workbench_module.REVIEW_ATTENTION_POLICY_VERSION, 2)
        self.assertEqual(flags(0.2245, 0.96), ())
        self.assertEqual(flags(0.2999, 0.999), ())
        self.assertEqual(flags(0.30, 1.0), ("notable silence", "notable pause"))
        self.assertEqual(flags(0.40, 2.4), ("notable silence", "notable pause"))

        self.assertEqual(bulk_generation_module.MAX_SILENCE_RATIO, 0.5)
        self.assertEqual(
            bulk_generation_module.MAX_INTERNAL_SILENCE_SECONDS,
            1.2,
        )
        self.assertEqual(
            bulk_generation_module.MAX_LEADING_SILENCE_SECONDS,
            0.8,
        )
        self.assertEqual(
            bulk_generation_module.MAX_TRAILING_SILENCE_SECONDS,
            0.8,
        )

    def test_legacy_review_metrics_remeasure_digest_bound_nonzero_pause(self):
        with TemporaryDirectory() as directory:
            wav = Path(directory) / "legacy-pause.wav"
            sample_rate = 16_000
            indexes = np.arange(sample_rate // 2, dtype=np.float32)
            tone = 0.2 * np.sin(2 * np.pi * 220 * indexes / sample_rate)
            room_tone = np.full(sample_rate * 2, 0.001, dtype=np.float32)
            write_pcm16_wav(
                wav,
                np.concatenate((tone, room_tone, tone)).astype(np.float32),
                sample_rate,
            )

            quality = workbench_module._corrected_legacy_speech_quality(
                str(wav), sha256_file(wav)
            )
            _duration, _wpm, _peak, flags = workbench_module._review_technical_metrics(
                {"quality": {"duration_seconds": 3.0, "peak": 0.2}},
                "A measured phrase after another phrase",
                projected_speech_quality=quality,
            )

        self.assertEqual(quality["analysis_version"], 2)
        self.assertGreater(quality["longest_internal_silence_seconds"], 1.2)
        self.assertIn("notable pause", flags)

    def test_legacy_review_metric_remeasurement_rejects_changed_wav(self):
        with TemporaryDirectory() as directory:
            wav = Path(directory) / "legacy.wav"
            write_pcm16_wav(wav, np.zeros(1_600, dtype=np.float32), 16_000)

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "Generated WAV changed"
            ):
                workbench_module._corrected_legacy_speech_quality(str(wav), "0" * 64)

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

    def test_selected_review_and_replay_do_not_rescan_unrelated_outcomes(self):
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

            started = time.monotonic()
            with (
                patch.object(
                    bulk_generation_module,
                    "load_generation_state",
                    side_effect=AssertionError("full state scan is forbidden"),
                ),
                patch.object(
                    bulk_generation_module,
                    "_validate_success_file",
                    side_effect=AssertionError("unrelated WAV scan is forbidden"),
                ),
                patch.object(
                    workbench_module,
                    "inspect_workspace",
                    side_effect=AssertionError("full workspace scan is forbidden"),
                ),
            ):
                audio = prepare_review_audio(displayed)
                committed = review_selected_item(displayed, "approved")
            elapsed = time.monotonic() - started

            self.assertEqual(
                hashlib.sha256(audio).hexdigest(), displayed.authority.audio_sha256
            )
            self.assertEqual(committed.queue_id, queue_id)
            self.assertEqual(committed.review_status, "approved")
            self.assertLess(elapsed, 0.25)

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
            fallback_policy = MissingVoicePolicy(NARRATOR_ROLES, ("Uncovered",))
            fallback = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="moss-v1.5",
                generation_profile="stable",
                narrator_character="Rhiannon",
                missing_voice_policy=fallback_policy,
            )
            repair_policy = FailureRepairPolicy((rhiannon.queue_id,))
            repair = create_resume_workspace(
                imported,
                root / "workspaces",
                story_index=fixture["job"]["story_index"],
                voice_manifest=fixture["job"]["voice_manifest"],
                backend="moss-tts",
                model="moss-v1.5",
                generation_profile="stable",
                narrator_character="Rhiannon",
                failure_repair_policy=repair_policy,
            )
            repair_command = generation_command(repair.directory, retries=0)
            fallback_summary = inspect_workspace(fallback.directory)
            fallback_readiness = inspect_generation_readiness(fallback.directory)
            fallback_command = generation_command(fallback.directory)
            cli_output = StringIO()
            with redirect_stdout(cli_output):
                self.assertEqual(
                    authoring_main(
                        [
                            "create-workspace",
                            str(imported),
                            "--workspaces-root",
                            str(root / "workspaces"),
                            "--story-index",
                            str(fixture["job"]["story_index"]),
                            "--voice-manifest",
                            str(fixture["job"]["voice_manifest"]),
                            "--backend",
                            "moss-tts",
                            "--model",
                            "moss-v1.5",
                            "--generation-profile",
                            "stable",
                            "--narrator-character",
                            "Rhiannon",
                            "--narrator-fallback-role",
                            "Uncovered",
                        ]
                    ),
                    0,
                )
            cli_workspace = json.loads(cli_output.getvalue())

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
        self.assertNotEqual(created.directory, fallback.directory)
        self.assertNotEqual(created.directory, repair.directory)
        self.assertEqual(
            repair_command[repair_command.index("--sentence-segment-failed") + 1],
            rhiannon.queue_id,
        )
        self.assertEqual(
            repair_command[repair_command.index("--queue-id") + 1],
            rhiannon.queue_id,
        )
        self.assertEqual(fallback_summary.missing_voice, 0)
        self.assertEqual(fallback_readiness.ready, 2)
        self.assertEqual(
            fallback_command[fallback_command.index("--narrator-fallback-role") + 1],
            "Uncovered",
        )
        self.assertFalse(cli_workspace["created"])
        self.assertEqual(cli_workspace["directory"], str(fallback.directory))
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
            queue_id = (
                VoiceGenerationQueue.load(created.directory / "queue.jsonl")
                .items[0]
                .queue_id
            )
            regeneration = generation_command(
                created.directory,
                queue_ids=(queue_id,),
                regenerate_existing=True,
            )
            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "requires explicit queue IDs"
            ):
                generation_command(
                    created.directory,
                    regenerate_existing=True,
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
        self.assertIn("--regenerate-existing", regeneration)
        self.assertEqual(regeneration[regeneration.index("--queue-id") + 1], queue_id)

    def test_scoped_regeneration_accepts_only_pending_review_outcomes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _imported, created = self.create_workspace(root)
            queue = VoiceGenerationQueue.load(created.directory / "queue.jsonl")
            queue_id = queue.items[0].queue_id
            state_path = created.directory / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][queue_id]["status"] = "generated"
            state["items"][queue_id]["review_status"] = "pending_review"
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            workbench_module.publish_generated_manifest(state_path)

            ordinary = inspect_generation_readiness(
                created.directory,
                queue_ids=(queue_id,),
            )
            regeneration = inspect_generation_readiness(
                created.directory,
                queue_ids=(queue_id,),
                regenerate_existing=True,
            )
            command = generation_command(
                created.directory,
                queue_ids=(queue_id,),
                regenerate_existing=True,
            )

            self.assertEqual(ordinary.selected, 0)
            self.assertTrue(ordinary.blocked_reasons)
            self.assertEqual(regeneration.selected, 1)
            self.assertEqual(regeneration.ready, 1)
            self.assertEqual(regeneration.queue_ids, (queue_id,))
            self.assertFalse(regeneration.blocked_reasons)
            self.assertIn("--regenerate-existing", command)
            self.assertEqual(command[command.index("--queue-id") + 1], queue_id)

            review_workspace_item(created.directory, queue_id, "approved")
            protected = inspect_generation_readiness(
                created.directory,
                queue_ids=(queue_id,),
                regenerate_existing=True,
            )
            self.assertEqual(protected.selected, 0)
            self.assertTrue(protected.blocked_reasons)
            with self.assertRaisesRegex(
                AuthoringWorkbenchError,
                "No pending, failed, or regenerable pending-review",
            ):
                generation_command(
                    created.directory,
                    queue_ids=(queue_id,),
                    regenerate_existing=True,
                )

            with self.assertRaisesRegex(
                AuthoringWorkbenchError, "requires explicit queue IDs"
            ):
                inspect_generation_readiness(
                    created.directory,
                    regenerate_existing=True,
                )

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
