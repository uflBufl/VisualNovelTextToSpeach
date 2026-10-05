import hashlib
import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests import test_authoring_missing_voice_reuse as reuse_fixtures
from vntts.authoring import failed_control_carry as carry_module
from vntts.authoring.bulk_generation import load_generation_state
from vntts.authoring.failed_control_carry import (
    FailedControlCarryError,
    carry_failed_controls,
)
from vntts.authoring.workbench import create_resume_workspace


class AuthoringFailedControlCarryTest(unittest.TestCase):
    def create_source_and_target(self, root, *, target_narrator="Centurion"):
        fixture, imported, base = (
            reuse_fixtures.AuthoringMissingVoiceReuseTest().create_workspace(root)
        )
        base_workspace = json.loads((base / "workspace.json").read_text())
        run = base_workspace["run_config"]
        policy = {
            "schema_version": 1,
            "mode": "narrator_roles",
            "roles": ["Aderyn"],
        }
        source = create_resume_workspace(
            imported,
            root / "workspaces",
            story_index=base / "inputs/story-index.jsonl",
            voice_manifest=base / "inputs/voice/manifest.json",
            narrator_character="Centurion",
            backend=run["backend"],
            model=run["model"],
            generation_profile=run["generation_profile"],
            missing_voice_policy=policy,
            failure_repair_policy=run["failure_repair_policy"],
        ).directory
        source_state_path = source / "generated-audio/generation-state.json"
        source_state = json.loads(source_state_path.read_text(encoding="utf-8"))
        source_state["items"][fixture["queue_id"]] = {
            "status": "failed",
            "attempts": 3,
            "last_error": "Generated WAV failed speech-silence validation",
        }
        source_state_path.write_text(
            json.dumps(source_state, sort_keys=True), encoding="utf-8"
        )

        target_voice = root / f"target-voice-{target_narrator}"
        shutil.copytree(source / "inputs/voice", target_voice)
        manifest_path = target_voice / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        (target_voice / "experimental.wav").write_bytes(b"experimental")
        manifest["voices"].append(
            {
                "character": "Experimental Hotelier",
                "speaker": "experimental-hotelier",
                "references": ["experimental.wav"],
            }
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        target = create_resume_workspace(
            imported,
            root / "workspaces",
            story_index=source / "inputs/story-index.jsonl",
            voice_manifest=manifest_path,
            narrator_character=target_narrator,
            backend=run["backend"],
            model=run["model"],
            generation_profile=run["generation_profile"],
            missing_voice_policy=policy,
            failure_repair_policy=run["failure_repair_policy"],
        ).directory
        return fixture, source, target

    def test_carries_exact_failed_item_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            fixture, source, target = self.create_source_and_target(Path(directory))
            first = carry_failed_controls(source, target, (fixture["queue_id"],))
            second = carry_failed_controls(source, target, (fixture["queue_id"],))
            source_state = load_generation_state(
                source / "generated-audio/generation-state.json",
                source / "queue.jsonl",
            )
            target_state = load_generation_state(
                target / "generated-audio/generation-state.json",
                target / "queue.jsonl",
            )
            report = json.loads(first.report.read_text(encoding="utf-8"))

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.carry_id, second.carry_id)
        self.assertEqual(
            target_state["items"][fixture["queue_id"]],
            source_state["items"][fixture["queue_id"]],
        )
        self.assertEqual(len(report["items"]), 1)
        self.assertIn("no audio", report["authority"])

    def test_state_change_after_capture_cannot_publish_or_overwrite(self):
        for role in ("source", "target"):
            with self.subTest(role=role), TemporaryDirectory() as directory:
                fixture, source, target = self.create_source_and_target(Path(directory))
                source_state_path = source / "generated-audio/generation-state.json"
                target_state_path = target / "generated-audio/generation-state.json"
                changed_path = (
                    source_state_path if role == "source" else target_state_path
                )
                changed = json.loads(changed_path.read_text(encoding="utf-8"))
                changed["items"][fixture["queue_id"]] = {
                    "status": "failed",
                    "attempts": 1,
                    "last_error": "external update",
                }
                target_before = target_state_path.read_bytes()
                original_route = carry_module._route_reference_identity
                routes = 0

                def change_after_route(*arguments, **keywords):
                    nonlocal routes
                    result = original_route(*arguments, **keywords)
                    routes += 1
                    if routes == 2:
                        changed_path.write_text(json.dumps(changed), encoding="utf-8")
                    return result

                with (
                    patch.object(
                        carry_module,
                        "_route_reference_identity",
                        side_effect=change_after_route,
                    ),
                    self.assertRaisesRegex(
                        FailedControlCarryError, role + " generation state changed"
                    ),
                ):
                    carry_failed_controls(source, target, (fixture["queue_id"],))
                self.assertFalse(
                    (
                        target_state_path.parent
                        / carry_module.FAILED_CONTROL_CARRY_FILENAME
                    ).exists()
                )
                if role == "source":
                    self.assertEqual(target_state_path.read_bytes(), target_before)
                else:
                    self.assertEqual(json.loads(target_state_path.read_text()), changed)

    def test_existing_report_rechecks_source_after_validation(self):
        with TemporaryDirectory() as directory:
            fixture, source, target = self.create_source_and_target(Path(directory))
            first = carry_failed_controls(source, target, (fixture["queue_id"],))
            report_before = first.report.read_bytes()
            source_path = source / "generated-audio/generation-state.json"
            changed = json.loads(source_path.read_text())
            changed["items"][fixture["queue_id"]]["last_error"] = "external update"
            original_validate = carry_module._validate_existing_report

            def change_after_validation(*arguments):
                original_validate(*arguments)
                source_path.write_text(json.dumps(changed), encoding="utf-8")

            with (
                patch.object(
                    carry_module,
                    "_validate_existing_report",
                    side_effect=change_after_validation,
                ),
                self.assertRaisesRegex(
                    FailedControlCarryError, "source generation state changed"
                ),
            ):
                carry_failed_controls(source, target, (fixture["queue_id"],))
            self.assertEqual(first.report.read_bytes(), report_before)

    def test_carry_preserves_legacy_state_and_captured_source_checksum(self):
        with TemporaryDirectory() as directory:
            fixture, source, target = self.create_source_and_target(Path(directory))
            path = source / "generated-audio/generation-state.json"
            document = json.loads(path.read_text())
            document["schema"] = "r1999.bulk-generation-state"
            path.write_text(json.dumps(document), encoding="utf-8")
            payload = path.read_bytes()
            result = carry_failed_controls(source, target, (fixture["queue_id"],))
            report = json.loads(result.report.read_text())
            target_state = load_generation_state(
                target / "generated-audio/generation-state.json", target / "queue.jsonl"
            )
            self.assertEqual(
                report["source_state_sha256"], hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(
                target_state["items"][fixture["queue_id"]],
                document["items"][fixture["queue_id"]],
            )
            self.assertEqual(path.read_bytes(), payload)

    def test_rejects_changed_effective_reference(self):
        with TemporaryDirectory() as directory:
            fixture, source, target = self.create_source_and_target(
                Path(directory), target_narrator="Adult Aderyn"
            )
            state_path = target / "generated-audio/generation-state.json"
            before = state_path.read_bytes()
            with self.assertRaisesRegex(
                FailedControlCarryError, "changes the effective reference"
            ):
                carry_failed_controls(source, target, (fixture["queue_id"],))
            self.assertEqual(state_path.read_bytes(), before)
            self.assertFalse(
                (
                    state_path.parent / carry_module.FAILED_CONTROL_CARRY_FILENAME
                ).exists()
            )

    def test_rejects_different_existing_target_item(self):
        with TemporaryDirectory() as directory:
            fixture, source, target = self.create_source_and_target(Path(directory))
            state_path = target / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["items"][fixture["queue_id"]] = {
                "status": "failed",
                "attempts": 1,
                "last_error": "different",
            }
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(FailedControlCarryError, "already different"):
                carry_failed_controls(source, target, (fixture["queue_id"],))


if __name__ == "__main__":
    unittest.main()
