import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_manifest import load_voice_manifest

from tests.test_authoring_missing_voice_live_fallback import (
    create_missing_voice_live_fallback_fixture,
)
from vntts.authoring.known_role_reuse import (
    KnownRoleReuseError,
    publish_known_role_reuse_binding,
)
from vntts.authoring.source_reference_bindings import (
    KNOWN_ROLE_REUSE_BINDING_FIELD,
    MISSING_VOICE_REUSE_BINDING_FIELD,
    queue_voice_overrides_from_manifest,
)
from vntts.authoring.workbench import create_resume_workspace


class AuthoringKnownRoleReuseTest(unittest.TestCase):
    def fixture(self, root):
        workspace, unresolved, queue_id = create_missing_voice_live_fallback_fixture(
            root
        )
        return workspace, unresolved, queue_id

    def test_preflight_publish_and_repeat_are_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, unresolved, queue_id = self.fixture(root)
            output = root / "known-role"
            state_path = workspace / "generated-audio/generation-state.json"
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


if __name__ == "__main__":
    unittest.main()
