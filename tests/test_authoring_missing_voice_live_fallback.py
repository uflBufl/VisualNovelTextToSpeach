import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_authoring_missing_voice_reuse_binding import (
    create_missing_voice_reuse_binding_review,
)
from vntts.authoring.bulk_generation import (
    _canonical_sha256,
    authorize_live_fallback,
    load_generation_state,
)
from vntts.authoring.missing_voice_live_fallback import (
    MissingVoiceLiveFallbackError,
    _existing_batch_id,
    authorize_missing_voice_live_fallback,
)
from vntts.authoring.missing_voice_reuse_binding import (
    publish_missing_voice_reuse_binding,
)
from vntts.generated_audio import _live_fallback_index


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
            before = state_path.read_bytes()

            preflight = authorize_missing_voice_live_fallback(
                workspace, binding, "Aderyn"
            )
            after_preflight = state_path.read_bytes()
            applied = authorize_missing_voice_live_fallback(
                workspace,
                binding,
                "Aderyn",
                accept_known_role_narrator_fallback=True,
            )
            after_apply = state_path.read_bytes()
            repeated = authorize_missing_voice_live_fallback(
                workspace,
                binding,
                "Aderyn",
                accept_known_role_narrator_fallback=True,
            )
            repeated_unchanged = after_apply == state_path.read_bytes()
            state = load_generation_state(state_path, queue_path)
            item = state["items"][queue_id]
            decision = item["live_fallback"]
            runtime = _live_fallback_index(
                {
                    "vntts.authoring.live_fallback": {
                        "schema_version": 1,
                        "mode": "explicit",
                        "entries": [
                            {
                                **copy.deepcopy(decision),
                                "decision_sha256": _canonical_sha256(decision),
                            }
                        ],
                    }
                }
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
        self.assertEqual(
            next(iter(runtime.values())).requested_voice_character, "Aderyn"
        )

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
