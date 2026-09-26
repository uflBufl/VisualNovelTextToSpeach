import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_authoring_missing_voice_reuse_binding import (
    create_missing_voice_reuse_binding_review,
)
from vntts.authoring.bulk_generation import (
    authorize_live_fallback,
    load_generation_state,
)
from vntts.authoring.generation_manifest import (
    RUNTIME_PROGRESS_MANIFEST_NAME,
    write_runtime_progress_manifest_from_state,
)
from vntts.authoring.missing_voice_live_fallback import (
    MissingVoiceLiveFallbackError,
    _existing_batch_id,
    authorize_missing_voice_live_fallback,
)
from vntts.authoring.missing_voice_reuse_binding import (
    publish_missing_voice_reuse_binding,
)
from vntts.generated_audio import GeneratedAudioLibrary


def create_missing_voice_live_fallback_fixture(root):
    return AuthoringMissingVoiceLiveFallbackTest().fixture(root)


class AuthoringMissingVoiceLiveFallbackTest(unittest.TestCase):
    def fixture(self, root):
        plan_path, session_path, queue_id = create_missing_voice_reuse_binding_review(
            root, statuses=("failed", "failed")
        )
        binding = publish_missing_voice_reuse_binding(
            plan_path, session_path, root / "binding"
        ).directory
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        workspace = Path(plan["source"]["workspace"])
        return workspace, binding, queue_id

    def test_preflight_apply_runtime_load_and_idempotency(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, binding, queue_id = self.fixture(root)
            state_path = workspace / "generated-audio/generation-state.json"
            queue_path = workspace / "queue.jsonl"
            progress_path = state_path.parent / RUNTIME_PROGRESS_MANIFEST_NAME
            write_runtime_progress_manifest_from_state(
                load_generation_state(state_path, queue_path),
                state_path.parent,
                progress_path,
                validate_files=False,
            )
            progress_before = progress_path.read_bytes()
            runtime = GeneratedAudioLibrary.load_optional(progress_path)
            self.assertIsNotNone(runtime)
            before = state_path.read_bytes()

            preflight = authorize_missing_voice_live_fallback(
                workspace, binding, "Aderyn"
            )
            after_preflight = state_path.read_bytes()
            self.assertEqual(progress_path.read_bytes(), progress_before)
            applied = authorize_missing_voice_live_fallback(
                workspace,
                binding,
                "Aderyn",
                accept_known_role_narrator_fallback=True,
            )
            after_apply = state_path.read_bytes()
            progress_after_apply = progress_path.read_bytes()
            repeated = authorize_missing_voice_live_fallback(
                workspace,
                binding,
                "Aderyn",
                accept_known_role_narrator_fallback=True,
            )
            repeated_unchanged = after_apply == state_path.read_bytes()
            self.assertEqual(progress_path.read_bytes(), progress_after_apply)
            state = load_generation_state(state_path, queue_path)
            item = state["items"][queue_id]
            decision = item["live_fallback"]
            fallback = runtime.find_live_fallback(
                decision["line_id"], decision["text_sha256"]
            )

        self.assertFalse(preflight.applied)
        self.assertEqual(after_preflight, before)
        self.assertTrue(applied.applied)
        self.assertTrue(applied.created)
        self.assertFalse(repeated.created)
        self.assertTrue(repeated_unchanged)
        self.assertEqual(item["status"], "live_fallback")
        self.assertEqual(decision["schema_version"], 4)
        self.assertEqual(decision["evidence"]["batch_id"], applied.batch_id)
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback.requested_voice_character, "Aderyn")

    def test_wrong_role_stale_authority_and_partial_scope_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, binding, queue_id = self.fixture(root)
            state_path = workspace / "generated-audio/generation-state.json"
            queue_path = workspace / "queue.jsonl"
            with self.assertRaisesRegex(MissingVoiceLiveFallbackError, "wrong role"):
                authorize_missing_voice_live_fallback(workspace, binding, "Rhiannon")

            authorize_live_fallback(
                state_path,
                queue_path,
                queue_id,
                reason="reference_unavailable_after_audit",
                model="pocket-tts",
            )
            with self.assertRaisesRegex(
                MissingVoiceLiveFallbackError, "conflicting terminal authority"
            ):
                authorize_missing_voice_live_fallback(workspace, binding, "Aderyn")

        with self.assertRaisesRegex(MissingVoiceLiveFallbackError, "partially applied"):
            _existing_batch_id(
                [{"status": "live_fallback"}, None],
                [{"queue_id": "q1"}, {"queue_id": "q2"}],
                {},
                "Aderyn",
                "Centurion",
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, binding, _queue_id = self.fixture(root)
            decision_path = binding / "decision.json"
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            decision["binding"]["queue_voice_overrides"] = {"forged": "Centurion"}
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            with self.assertRaises(MissingVoiceLiveFallbackError):
                authorize_missing_voice_live_fallback(workspace, binding, "Aderyn")


if __name__ == "__main__":
    unittest.main()
