import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.missing_voice_reuse_fixtures import (
    create_missing_voice_live_fallback_fixture,
)
from vntts.authoring.bulk_generation import (
    load_generation_state,
)
from vntts.authoring.generation_fallback import (
    authorize_live_fallback,
)
from vntts.authoring.missing_voice_live_fallback import (
    MissingVoiceLiveFallbackError,
    _existing_batch_id,
    authorize_missing_voice_live_fallback,
)
from vntts.generated_audio import GeneratedAudioLibrary


class AuthoringMissingVoiceLiveFallbackTest(unittest.TestCase):
    def test_preflight_apply_runtime_load_and_idempotency(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, binding, queue_id = create_missing_voice_live_fallback_fixture(
                root
            )
            state_path = workspace / "generated-audio/generation-state.sqlite3"
            queue_path = workspace / "queue.jsonl"
            runtime = GeneratedAudioLibrary.load_optional(state_path)
            self.assertIsNotNone(runtime)
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

    def test_fallback_commit_updates_existing_database_reader(self):
        for missing_voice_batch in (False, True):
            with (
                self.subTest(missing_voice_batch=missing_voice_batch),
                TemporaryDirectory() as directory,
            ):
                workspace, binding, queue_id = (
                    create_missing_voice_live_fallback_fixture(Path(directory))
                )
                state_path = workspace / "generated-audio/generation-state.sqlite3"
                with closing(sqlite3.connect(state_path)) as reader:
                    self.assertNotEqual(
                        reader.execute(
                            "SELECT status FROM items WHERE queue_id=?", (queue_id,)
                        ).fetchone(),
                        ("live_fallback",),
                    )
                    if missing_voice_batch:
                        authorize_missing_voice_live_fallback(
                            workspace,
                            binding,
                            "Aderyn",
                            accept_known_role_narrator_fallback=True,
                        )
                    else:
                        authorize_live_fallback(
                            state_path,
                            workspace / "queue.jsonl",
                            queue_id,
                            reason="reference_unavailable_after_audit",
                            model="pocket-tts",
                        )
                    self.assertEqual(
                        reader.execute(
                            "SELECT status FROM items WHERE queue_id=?", (queue_id,)
                        ).fetchone(),
                        ("live_fallback",),
                    )

    def test_wrong_role_stale_authority_and_partial_scope_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, binding, queue_id = create_missing_voice_live_fallback_fixture(
                root
            )
            state_path = workspace / "generated-audio/generation-state.sqlite3"
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
            workspace, binding, _queue_id = create_missing_voice_live_fallback_fixture(
                root
            )
            decision_path = binding / "decision.json"
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            decision["binding"]["queue_voice_overrides"] = {"forged": "Centurion"}
            decision_path.write_text(json.dumps(decision), encoding="utf-8")
            with self.assertRaises(MissingVoiceLiveFallbackError):
                authorize_missing_voice_live_fallback(workspace, binding, "Aderyn")


if __name__ == "__main__":
    unittest.main()
