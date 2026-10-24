import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_manifest import load_voice_manifest

from tests.authoring_fixtures import tree_hashes
from tests.missing_voice_reuse_fixtures import (
    create_missing_voice_live_fallback_fixture,
)
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.symlink_support import symlink_or_skip
from vntts.authoring import known_role_reuse as known_role_module
from vntts.authoring.known_role_reuse import (
    KnownRoleReuseError,
    publish_known_role_reuse_binding,
)
from vntts.authoring.source_reference_bindings import (
    KNOWN_ROLE_REUSE_BINDING_FIELD,
    MISSING_VOICE_REUSE_BINDING_FIELD,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.workbench import AuthoringWorkbenchError, create_resume_workspace


class AuthoringKnownRoleReuseTest(unittest.TestCase):
    def fixture(self, root):
        workspace, unresolved, queue_id = create_missing_voice_live_fallback_fixture(
            root
        )
        return workspace, unresolved, queue_id

    def test_output_symlinks_are_refused_in_preflight_and_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, unresolved, _queue_id = self.fixture(root)
            before = tree_hashes(workspace)
            for state in ("missing", "empty", "occupied"):
                target = root / f"target-{state}"
                if state != "missing":
                    target.mkdir()
                if state == "occupied":
                    (target / "keep").write_bytes(b"unchanged")
                output = root / f"output-{state}"
                symlink_or_skip(output, target, target_is_directory=True)
                for accepted in (False, True):
                    with (
                        self.subTest(target=state, accepted=accepted),
                        self.assertRaisesRegex(KnownRoleReuseError, "symlink"),
                    ):
                        publish_known_role_reuse_binding(
                            workspace,
                            unresolved,
                            "Aderyn",
                            "Rhiannon",
                            output,
                            accept_known_role_reuse=accepted,
                        )
                    self.assertTrue(output.is_symlink())
                    self.assertEqual(output.readlink(), target)
                    self.assertEqual(target.exists(), state != "missing")
                    if target.exists():
                        self.assertEqual(
                            list(target.iterdir()),
                            [target / "keep"] if state == "occupied" else [],
                        )
                    if state == "occupied":
                        self.assertEqual((target / "keep").read_bytes(), b"unchanged")
                    self.assertEqual(tree_hashes(workspace), before)
                    self.assertEqual(list(root.glob(".known-role-reuse-*")), [])

    def test_parent_and_source_aliases_preserve_preflight_and_idempotency(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace, unresolved, _queue_id = self.fixture(root)
            workspace_alias = root / "workspace-alias"
            unresolved_alias = root / "unresolved-alias"
            parent = root / "bindings"
            parent.mkdir()
            parent_alias = root / "bindings-alias"
            symlink_or_skip(workspace_alias, workspace, target_is_directory=True)
            symlink_or_skip(unresolved_alias, unresolved, target_is_directory=True)
            symlink_or_skip(parent_alias, parent, target_is_directory=True)
            output = parent_alias / "known-role"
            preflight = publish_known_role_reuse_binding(
                workspace_alias, unresolved_alias, "Aderyn", "Rhiannon", output
            )
            self.assertFalse(preflight.created)
            self.assertFalse(preflight.applied)
            self.assertFalse(output.exists())
            created = publish_known_role_reuse_binding(
                workspace_alias,
                unresolved_alias,
                "Aderyn",
                "Rhiannon",
                output,
                accept_known_role_reuse=True,
            )
            repeated = publish_known_role_reuse_binding(
                workspace_alias,
                unresolved_alias,
                "Aderyn",
                "Rhiannon",
                output,
                accept_known_role_reuse=True,
            )
            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.directory, parent / "known-role")
            self.assertEqual(preflight.decision_id, created.decision_id)
            self.assertEqual(created.decision_id, repeated.decision_id)

    def test_preflight_publish_and_repeat_are_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, unresolved, queue_id = self.fixture(root)
            output = root / "known-role"
            state_path = workspace / "generated-audio/generation-state.sqlite3"
            before = state_path.read_bytes()

            preflight = publish_known_role_reuse_binding(
                workspace,
                unresolved,
                "Aderyn",
                "Rhiannon",
                output,
            )
            output_after_preflight = output.exists()
            state_after_preflight = state_path.read_bytes()
            first = publish_known_role_reuse_binding(
                workspace,
                unresolved,
                "Aderyn",
                "Rhiannon",
                output,
                accept_known_role_reuse=True,
            )
            second = publish_known_role_reuse_binding(
                workspace,
                unresolved,
                "Aderyn",
                "Rhiannon",
                output,
                accept_known_role_reuse=True,
            )
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
            overrides = queue_voice_overrides_from_manifest(
                manifest,
                queue_ids=(queue_id,),
                voices=voices,
            )
            copied_references = {
                reference: (output / reference).is_file()
                for voice in voices
                for reference in voice.references
            }

        self.assertFalse(preflight.applied)
        self.assertFalse(preflight.created)
        self.assertFalse(output_after_preflight)
        self.assertEqual(state_after_preflight, before)
        self.assertTrue(first.applied)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(first.absent_count, 1)
        self.assertEqual(first.rejected_count, 0)
        self.assertEqual(first.preserved_approved_count, 0)
        self.assertEqual(overrides, {queue_id: "Rhiannon"})
        self.assertEqual(
            copied_references,
            {
                "adult.wav": True,
                "narrator.wav": True,
                "rhiannon.wav": True,
            },
        )
        self.assertEqual(
            manifest[KNOWN_ROLE_REUSE_BINDING_FIELD]["source_character"], "Aderyn"
        )

    def test_missing_voice_and_tampered_output_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, unresolved, _queue_id = self.fixture(root)
            with self.assertRaisesRegex(KnownRoleReuseError, "missing or ambiguous"):
                publish_known_role_reuse_binding(
                    workspace,
                    unresolved,
                    "Aderyn",
                    "Not Configured",
                    root / "missing",
                )

            result = publish_known_role_reuse_binding(
                workspace,
                unresolved,
                "Aderyn",
                "Rhiannon",
                root / "known-role-binding",
                accept_known_role_reuse=True,
            )
            manifest = result.directory / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(KnownRoleReuseError, "artifact changed"):
                publish_known_role_reuse_binding(
                    workspace,
                    unresolved,
                    "Aderyn",
                    "Rhiannon",
                    root / "known-role-binding",
                    accept_known_role_reuse=True,
                )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, unresolved, _queue_id = self.fixture(root)
            output = root / "known-role-binding"
            publish_known_role_reuse_binding(
                workspace,
                unresolved,
                "Aderyn",
                "Rhiannon",
                output,
                accept_known_role_reuse=True,
            )
            (output / "adult.wav").unlink()
            with self.assertRaisesRegex(KnownRoleReuseError, "artifact changed"):
                publish_known_role_reuse_binding(
                    workspace,
                    unresolved,
                    "Aderyn",
                    "Rhiannon",
                    output,
                    accept_known_role_reuse=True,
                )

    def test_reviewed_reuse_overlay_can_be_composed_without_changing_voice_controls(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_workspace, unresolved, queue_id = self.fixture(root)
            imported = next((root / "imports").iterdir())
            additive = create_resume_workspace(
                imported,
                root / "additive-workspaces",
                story_index=source_workspace / "inputs/story-index.jsonl",
                voice_manifest=unresolved / "manifest.json",
                backend="moss-tts",
                model="model",
                generation_profile="stable",
                narrator_character="Centurion",
            ).directory

            result = publish_known_role_reuse_binding(
                additive,
                unresolved,
                "Aderyn",
                "Rhiannon",
                root / "composed-known-role",
                accept_known_role_reuse=True,
            )
            selected = json.loads(
                (unresolved / "manifest.json").read_text(encoding="utf-8")
            )
            composed = json.loads(
                (result.directory / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(
            composed[MISSING_VOICE_REUSE_BINDING_FIELD],
            selected[MISSING_VOICE_REUSE_BINDING_FIELD],
        )
        self.assertEqual(
            composed[KNOWN_ROLE_REUSE_BINDING_FIELD]["queue_voice_overrides"],
            {queue_id: "Rhiannon"},
        )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_file_phases_refuse_swapped_acquisition_clean_up_and_recover(self):
        phases = (
            ("_load_workspace_snapshot", False, False, "file_sha256"),
            ("_select_manifest", False, False, "file_sha256"),
            ("_reference_records", False, False, "file_sha256"),
            ("_copy_references", False, False, "file_sha256"),
            ("_copy_tree", False, False, "file_sha256"),
            ("_copy_tree", True, False, "file_sha256"),
            ("_write_bundle", False, False, "file_sha256"),
            ("_validate_inventory_files", False, False, "file_sha256"),
            ("_validate_inventory_files", False, True, "file_sha256"),
            ("_copy_references", False, False, "copy_regular_file"),
            ("_copy_tree", True, False, "copy_regular_file"),
        )
        for owner_name, authority_source, retained, operation_name in phases:
            with (
                self.subTest(
                    phase=owner_name,
                    operation=operation_name,
                    authority_source=authority_source,
                    retained=retained,
                ),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                workspace, unresolved, _queue_id = self.fixture(root)
                output = root / "known-role"

                def publish():
                    return publish_known_role_reuse_binding(
                        workspace,
                        unresolved,
                        "Aderyn",
                        "Rhiannon",
                        output,
                        accept_known_role_reuse=True,
                    )

                preflight = publish_known_role_reuse_binding(
                    workspace, unresolved, "Aderyn", "Rhiannon", output
                )
                if retained:
                    publish()
                before = tree_hashes(root)
                owner = getattr(known_role_module, owner_name)
                operation = getattr(known_role_module, operation_name)
                armed = False
                acquired = []

                def arm(*args, **kwargs):
                    nonlocal armed
                    armed = True
                    try:
                        return owner(*args, **kwargs)
                    finally:
                        armed = False

                def swap_acquisition(path, *args, **kwargs):
                    if not armed or (
                        owner_name == "_copy_tree"
                        and Path(path).is_relative_to(unresolved) != authority_source
                    ):
                        return operation(path, *args, **kwargs)
                    self.assertEqual(
                        kwargs,
                        {"error_type": OSError}
                        if operation_name == "file_sha256"
                        else {},
                    )
                    acquired.append(Path(path))
                    gate: RegularFileGate = {"path": Path(path), "armed": True}
                    # Restore staged candidates before publication cleanup removes them.
                    with (
                        swap_at_acquisition(self, gate),
                        self.assertRaisesRegex(OSError, "regular file") as refused,
                    ):
                        operation(path, *args, **kwargs)
                    raise refused.exception

                with (
                    patch.object(known_role_module, owner_name, side_effect=arm),
                    patch.object(
                        known_role_module,
                        operation_name,
                        side_effect=swap_acquisition,
                    ),
                    self.assertRaisesRegex(
                        KnownRoleReuseError, "regular file"
                    ) as refused,
                ):
                    publish()
                self.assertEqual(len(acquired), 1)
                self.assertIsInstance(refused.exception.__cause__, OSError)
                self.assertEqual(tree_hashes(root), before)
                self.assertEqual(output.exists(), retained)
                self.assertEqual(list(root.glob(".known-role-reuse-*")), [])
                recovered = publish()
                repeated = publish()
                self.assertEqual(recovered.created, not retained)
                self.assertFalse(repeated.created)
                self.assertEqual(recovered.decision_id, preflight.decision_id)
                self.assertEqual(repeated.decision_id, preflight.decision_id)
                bundle = json.loads((output / "bundle.json").read_text())
                self.assertEqual(
                    {
                        record["path"]: record["sha256"]
                        for record in bundle["inventory"]
                    },
                    {
                        path: digest
                        for path, digest in tree_hashes(output).items()
                        if path != "bundle.json"
                    },
                )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_reference_checksum_retains_raw_oserror(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "reference.wav"
            payload = b"exact-reference"
            source.write_bytes(payload)
            gate: RegularFileGate = {"path": source, "armed": True}
            with (
                swap_at_acquisition(self, gate),
                self.assertRaisesRegex(OSError, "regular file"),
            ):
                known_role_module._reference_records(root, (source.name,))
            self.assertEqual(source.read_bytes(), payload)
            self.assertEqual(
                known_role_module._reference_records(root, (source.name,))[0]["sha256"],
                hashlib.sha256(payload).hexdigest(),
            )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_authority_copy_retains_raw_oserror(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            authority = root / "authority"
            authority.mkdir()
            source = authority / "decision.json"
            payload = b"exact-authority"
            source.write_bytes(payload)
            destination = root / "copied"
            gate: RegularFileGate = {"path": source, "armed": True}
            with (
                swap_at_acquisition(self, gate),
                self.assertRaisesRegex(OSError, "regular file"),
            ):
                known_role_module._copy_tree(authority, destination)
            self.assertEqual(source.read_bytes(), payload)
            self.assertFalse((destination / source.name).exists())
            known_role_module._copy_tree(authority, destination)
            self.assertEqual((destination / source.name).read_bytes(), payload)

    def test_reference_hash_identity_and_existing_alias_policy_are_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            references = root / "references"
            references.mkdir()
            source = references / "reference.wav"
            payload = b"exact-reference"
            source.write_bytes(payload)
            alias = references / "alias.wav"
            symlink_or_skip(alias, source)
            records = known_role_module._reference_records(
                references, (source.name, alias.name, source.name)
            )
            self.assertEqual(
                records,
                [
                    {
                        "relative": Path(name),
                        "source": source,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                    for name in (source.name, alias.name)
                ],
            )
            outside = root / "outside.wav"
            outside.write_bytes(payload)
            escape = references / "escape.wav"
            symlink_or_skip(escape, outside)
            with self.assertRaisesRegex(AuthoringWorkbenchError, "owning directory"):
                known_role_module._reference_records(references, (escape.name,))
            with self.assertRaisesRegex(KnownRoleReuseError, "contains a symlink"):
                known_role_module._copy_tree(references, root / "copied")

    def test_bundle_schema_version_requires_exact_integer(self):
        body = {
            "schema": known_role_module.KNOWN_ROLE_REUSE_BUNDLE_SCHEMA,
            "schema_version": 1,
            "decision_id": known_role_module.canonical_document_sha256({}),
            "inventory": [{"path": "manifest.json", "sha256": "1" * 64}],
        }
        valid = {**body, "bundle_id": known_role_module.canonical_document_sha256(body)}
        self.assertEqual(
            known_role_module._validated_bundle_inventory(valid, {}), body["inventory"]
        )
        for version in (True, 1.0):
            invalid_body = {**body, "schema_version": version}
            invalid = {
                **invalid_body,
                "bundle_id": known_role_module.canonical_document_sha256(invalid_body),
            }
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(
                    KnownRoleReuseError, "bundle identity is invalid"
                ),
            ):
                known_role_module._validated_bundle_inventory(invalid, {})


if __name__ == "__main__":
    unittest.main()
