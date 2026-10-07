import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.missing_voice_reuse_fixtures import (
    create_failed_prompt_hypothesis_review,
)
from vntts.authoring.failed_prompt_hypothesis import (
    FailedPromptHypothesisError,
    publish_failed_prompt_hypothesis_selection,
)
from vntts.authoring.missing_voice_reuse_binding import (
    MissingVoiceReuseBindingError,
    publish_missing_voice_reuse_binding,
)
from vntts.authoring.missing_voice_reuse_review import (
    MissingVoiceReuseReviewError,
    load_missing_voice_reuse_review,
    record_missing_voice_reuse_decision,
    record_missing_voice_reuse_heard,
)


class AuthoringFailedPromptHypothesisTest(unittest.TestCase):
    def test_selection_is_prompt_only_and_never_approves_or_binds(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, workspace, plan_path, session = (
                create_failed_prompt_hypothesis_review(root)
            )
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
            _fixture, _workspace, plan_path, session = (
                create_failed_prompt_hypothesis_review(root)
            )
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
                create_failed_prompt_hypothesis_review(
                    Path(directory), tamper_prompt=True
                )


if __name__ == "__main__":
    unittest.main()
