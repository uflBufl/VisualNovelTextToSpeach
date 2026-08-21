import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_authoring_workbench import create_test_workspace
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAuditError,
    load_failure_reference_audit,
    load_failure_reference_decisions,
    publish_failure_reference_audit,
    record_failure_reference_decision,
)


class FailureReferenceAuditTest(unittest.TestCase):
    def create_failed_workspace(self, root):
        _fixture, _imported, created = create_test_workspace(root)
        state_path = created.directory / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text())
        queue_id, result = next(iter(state["items"].items()))
        for field in ("path", "file_sha256", "quality", "review_status"):
            result.pop(field, None)
        result.update(
            {
                "status": "failed",
                "provider": "moss-tts",
                "model": "model",
                "generation_profile": "stable",
                "voice_character": "Rhiannon",
                "synthesis_provenance_sha256": "a" * 64,
                "failure": {
                    "schema_version": 1,
                    "kind": "speech_silence",
                    "completion": "complete",
                    "error_type": "SpeechSilenceValidationError",
                    "speech_quality": {
                        "leading_silence_seconds": 0.0,
                        "trailing_silence_seconds": 0.0,
                        "longest_internal_silence_seconds": 2.0,
                        "silence_ratio": 0.4,
                    },
                    "text_features": {
                        "word_count": 4,
                        "character_count": 20,
                        "sentence_boundary_count": 1,
                        "comma_count": 0,
                        "ellipsis_count": 0,
                    },
                },
            }
        )
        state_path.write_text(json.dumps(state, sort_keys=True))
        return created.directory, queue_id

    def test_audit_binds_case_candidates_and_private_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            output = root / "audit"

            result = publish_failure_reference_audit(workspace, output, seed=7)
            loaded = load_failure_reference_audit(output)
            document = json.loads((output / "audit.json").read_text())

        self.assertEqual(result, loaded)
        self.assertEqual(document["case_count"], 1)
        self.assertEqual(document["groups"][0]["cases"][0]["queue_id"], queue_id)
        self.assertIn("neither_acceptable", document["groups"][0]["decision_options"])

    def test_audio_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            document = json.loads((output / "audit.json").read_text())
            audio = output / document["groups"][0]["candidates"][0]["audio"]
            audio.write_bytes(b"changed")

            with self.assertRaisesRegex(FailureReferenceAuditError, "audio changed"):
                load_failure_reference_audit(output)

    def test_private_mapping_tamper_fails_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            key_path = output / ".blind-key.json"
            key = json.loads(key_path.read_text())
            key["groups"][0]["candidates"][0]["source_reference"] = "forged.wav"
            key_path.write_text(json.dumps(key))

            with self.assertRaisesRegex(FailureReferenceAuditError, "blind key"):
                load_failure_reference_audit(output)

    def test_candidate_and_neither_decisions_are_exact_and_checksum_bound(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)
            audit = json.loads((output / "audit.json").read_text())
            group = audit["groups"][0]
            candidate = group["candidates"][0]

            first = record_failure_reference_decision(
                output, group["group_id"], candidate["candidate_id"]
            )
            self.assertEqual(first, load_failure_reference_decisions(output))
            self.assertEqual(
                first["decisions"][0]["selected_reference_sha256"],
                candidate["sha256"],
            )
            self.assertEqual(first["decisions"][0]["case_queue_ids"], [queue_id])

            second = record_failure_reference_decision(
                output, group["group_id"], "neither_acceptable"
            )
            self.assertIsNone(second["decisions"][0]["selected_reference_sha256"])

            decisions_path = output / "decisions.json"
            decisions = json.loads(decisions_path.read_text())
            decisions["decisions"][0]["case_queue_ids"] = ["forged"]
            decisions_path.write_text(json.dumps(decisions))
            with self.assertRaisesRegex(
                FailureReferenceAuditError, "decision identity changed"
            ):
                load_failure_reference_decisions(output)

    def test_unrelated_state_review_does_not_invalidate_exact_failure_cases(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, queue_id = self.create_failed_workspace(root)
            state_path = workspace / "generated-audio/generation-state.json"
            state = json.loads(state_path.read_text())
            state["audit_unrelated_diagnostic"] = {"refresh": 1}
            state_path.write_text(json.dumps(state, sort_keys=True))
            output = root / "audit"
            publish_failure_reference_audit(workspace, output)

            state = json.loads(state_path.read_text())
            state["audit_unrelated_diagnostic"]["refresh"] = 2
            state_path.write_text(json.dumps(state, sort_keys=True))
            self.assertEqual(load_failure_reference_audit(output).case_count, 1)

            state = json.loads(state_path.read_text())
            state["items"][queue_id]["attempts"] += 1
            state_path.write_text(json.dumps(state, sort_keys=True))
            with self.assertRaisesRegex(FailureReferenceAuditError, queue_id):
                load_failure_reference_audit(output)


if __name__ == "__main__":
    unittest.main()
