import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

from tests import test_authoring_missing_voice_reuse as reuse_fixtures
from vntts.authoring.failed_prompt_hypothesis import (
    FailedPromptHypothesisError,
    publish_failed_prompt_hypothesis_selection,
)
from vntts.authoring.missing_voice_reuse import (
    build_missing_voice_reuse_plan,
    write_missing_voice_reuse_plan,
)
from vntts.authoring.missing_voice_reuse_binding import (
    MissingVoiceReuseBindingError,
    publish_missing_voice_reuse_binding,
)
from vntts.authoring.missing_voice_reuse_review import (
    MissingVoiceReuseReviewError,
    build_missing_voice_reuse_review,
    load_missing_voice_reuse_review,
    record_missing_voice_reuse_decision,
    record_missing_voice_reuse_heard,
)


def write_wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * 800)


class AuthoringFailedPromptHypothesisTest(unittest.TestCase):
    def create_review(self, root, *, tamper_prompt=False):
        helper = reuse_fixtures.AuthoringMissingVoiceReuseTest()
        fixture, _imported, workspace = helper.create_workspace(
            root,
            text="What happened? You're hurt.",
            missing_voice_policy={
                "schema_version": 1,
                "mode": "narrator_roles",
                "roles": ["Aderyn"],
            },
        )
        state_path = workspace / "generated-audio/generation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["items"][fixture["queue_id"]] = {
            "status": "failed",
            "attempts": 1,
            "last_error": "Generated WAV failed speech-silence validation",
        }
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        plan = build_missing_voice_reuse_plan(
            workspace,
            "Aderyn",
            cohorts={"failed": ("314601.png",)},
            candidate_voice_characters=("Centurion",),
            failed_queue_ids=(fixture["queue_id"],),
            inline_pause_ms=180,
        )
        plan_path = root / "plan.json"
        write_missing_voice_reuse_plan(plan, plan_path)
        candidate = plan.document["candidates"][0]
        prompt = candidate["render_hypothesis"]["prompts"][0]
        candidate_root = (root / "candidate").resolve()
        audio = candidate_root / "generated-audio/audio/sample.wav"
        write_wav(audio)
        derived = "f" * 64 if tamper_prompt else prompt["derived_prompt_sha256"]
        item = {
            "status": "generated",
            "attempts": 2,
            "path": "audio/sample.wav",
            "file_sha256": sha256_file(audio),
            "quality": {"duration_seconds": 0.1},
            "source_reference_binding": {
                "queue_id": fixture["queue_id"],
                "synthesis_voice_character": candidate["voice_character"],
            },
            "failure_repair": {
                "strategy": "inline_pause_marker",
                "pause_ms": 180,
                "marker_count": prompt["marker_count"],
                "derived_prompt_sha256": derived,
            },
            "synthesis_text_sha256": derived,
        }
        snapshot = {
            "directory": candidate_root,
            "workspace": {"workspace_id": "candidate-workspace"},
            "state": {"items": {fixture["queue_id"]: item}},
            "authority": {
                "path": str(candidate_root),
                "workspace_id": "candidate-workspace",
                "workspace_sha256": "1" * 64,
                "state_sha256": "2" * 64,
                "voice_manifest_sha256": "3" * 64,
            },
        }
        with patch(
            "vntts.authoring.missing_voice_reuse_review._load_candidate_workspace",
            return_value=snapshot,
        ):
            session = build_missing_voice_reuse_review(
                plan_path,
                {candidate["candidate_id"]: (candidate_root,)},
                root / "review",
            )
        return fixture, workspace, plan_path, session

    def test_selection_is_prompt_only_and_never_approves_or_binds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, workspace, plan_path, session = self.create_review(root)
            bundle, _progress = load_missing_voice_reuse_review(session)
            cohort = bundle["cohorts"][0]
            label = bundle["candidates"][0]["label"]
            record_missing_voice_reuse_heard(
                session, cohort["cohort_id"], fixture["queue_id"], label
            )
            record_missing_voice_reuse_decision(session, cohort["cohort_id"], label)
            state_path = workspace / "generated-audio/generation-state.json"
            manifest_path = workspace / "inputs/voice/manifest.json"
            state_before = state_path.read_bytes()
            manifest_before = manifest_path.read_bytes()
            result = publish_failed_prompt_hypothesis_selection(
                plan_path, session, root / "selection.json"
            )
            selection = json.loads(result.output.read_text(encoding="utf-8"))

            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(result.selected_count, 1)
            self.assertEqual(selection["decisions"][0]["decision"], "select_hypothesis")
            self.assertNotIn("approved", json.dumps(selection))
            with self.assertRaisesRegex(
                MissingVoiceReuseBindingError, "selection artifact"
            ):
                publish_missing_voice_reuse_binding(
                    plan_path, session, root / "forbidden-binding"
                )

    def test_incomplete_review_and_prompt_tampering_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _workspace, plan_path, session = self.create_review(root)
            with self.assertRaisesRegex(
                FailedPromptHypothesisError, "completed decision"
            ):
                publish_failed_prompt_hypothesis_selection(
                    plan_path, session, root / "selection.json"
                )
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                MissingVoiceReuseReviewError, "exact render hypothesis"
            ):
                self.create_review(Path(directory), tamper_prompt=True)


if __name__ == "__main__":
    unittest.main()
