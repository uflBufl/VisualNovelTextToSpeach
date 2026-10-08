"""Public authoring commands retain their error contract if a source disappears."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from tests.authoring_fixtures import (
    create_explicit_fallback_merge_fixture,
    create_reviewed_waveform_fixture,
    create_voice_quality_review,
    queue_builder_story_record,
    write_queue_builder_inputs,
)
from tests.bulk_generation_fixtures import additive_queue_item
from tests.missing_voice_reuse_fixtures import (
    create_failed_prompt_hypothesis_review,
    create_missing_voice_live_fallback_fixture,
    create_missing_voice_reuse_binding_review,
)
from tests.source_reference_fixtures import (
    publish_source_reference_quality_fixture,
    write_source_reference_review_inputs,
)
from vntts.authoring import (
    explicit_fallback_merge,
    failed_prompt_hypothesis,
    missing_voice_live_fallback,
    missing_voice_reuse_binding,
    queue_builder,
    queue_extension,
    reviewed_waveform_publication,
    source_reference_quality,
    source_reference_review,
    voice_quality_gate,
)
from vntts.authoring.workbench import AuthoringWorkbenchError


class ChecksumBoundaryTest(unittest.TestCase):
    def _disappear_after(self, module, loader_name, source, invoke, error_type, output):
        loader = getattr(module, loader_name)

        def load_then_remove(*args, **kwargs):
            result = loader(*args, **kwargs)
            if source.exists():
                source.unlink()
            return result

        with patch.object(module, loader_name, side_effect=load_then_remove):
            with self.assertRaises(error_type) as caught:
                invoke()
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
        self.assertFalse(output.exists())

    def test_explicit_fallback_missing_queue_stays_a_workbench_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, source, queue_id = create_explicit_fallback_merge_fixture(root)
            self._disappear_after(
                explicit_fallback_merge,
                "load_stable_workspace_generation_state",
                base / "queue.jsonl",
                lambda: explicit_fallback_merge.merge_explicit_live_fallbacks(
                    base, source, (queue_id,), root / "outputs"
                ),
                AuthoringWorkbenchError,
                root / "outputs",
            )

    def test_reviewed_waveform_missing_queue_stays_a_workbench_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base, _item = create_reviewed_waveform_fixture(root)
            self._disappear_after(
                reviewed_waveform_publication,
                "load_stable_workspace_generation_state",
                base / "queue.jsonl",
                lambda: (
                    reviewed_waveform_publication.create_reviewed_waveform_publication_workspace(
                        base, root / "outputs"
                    )
                ),
                AuthoringWorkbenchError,
                root / "outputs",
            )

    def test_reuse_binding_missing_plan_stays_a_binding_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, session, _queue_id = create_missing_voice_reuse_binding_review(root)
            self._disappear_after(
                missing_voice_reuse_binding,
                "_capture_binding_review",
                plan,
                lambda: missing_voice_reuse_binding.publish_missing_voice_reuse_binding(
                    plan, session, root / "outputs"
                ),
                missing_voice_reuse_binding.MissingVoiceReuseBindingError,
                root / "outputs",
            )

    def test_prompt_selection_missing_plan_stays_a_selection_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture, _workspace, plan, session = (
                create_failed_prompt_hypothesis_review(root)
            )
            self._disappear_after(
                failed_prompt_hypothesis,
                "load_missing_voice_reuse_review",
                plan,
                lambda: (
                    failed_prompt_hypothesis.publish_failed_prompt_hypothesis_selection(
                        plan, session, root / "outputs.json"
                    )
                ),
                failed_prompt_hypothesis.FailedPromptHypothesisError,
                root / "outputs.json",
            )

    def test_live_fallback_missing_workspace_stays_a_fallback_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, authority, _queue_id = (
                create_missing_voice_live_fallback_fixture(root)
            )
            state = workspace / "generated-audio/generation-state.json"
            before = state.read_bytes()
            self._disappear_after(
                missing_voice_live_fallback,
                "_read_json",
                workspace / "workspace.json",
                lambda: (
                    missing_voice_live_fallback.authorize_missing_voice_live_fallback(
                        workspace, authority, "Aderyn"
                    )
                ),
                missing_voice_live_fallback.MissingVoiceLiveFallbackError,
                root / "outputs",
            )
            self.assertEqual(state.read_bytes(), before)

    def test_additive_publication_missing_base_stays_an_extension_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = {"game": "Reverse: 1999", "language": "en"}
            base = write_voice_generation_queue(
                root / "base.jsonl", metadata, [additive_queue_item(1)]
            )
            extension = write_voice_generation_queue(
                root / "extension.jsonl", metadata, [additive_queue_item(2)]
            )
            loader = queue_extension.load_stable_generation_queue

            def load_then_remove(path):
                result = loader(path)
                if path == extension:
                    base.unlink()
                return result

            with patch.object(
                queue_extension,
                "load_stable_generation_queue",
                side_effect=load_then_remove,
            ):
                with self.assertRaises(queue_extension.QueueExtensionError) as caught:
                    queue_extension.publish_additive_generation_queue(
                        base, extension, root / "output.jsonl"
                    )
            self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            self.assertFalse((root / "output.jsonl").exists())

    def test_queue_source_disappearance_stays_a_planning_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story, manifest = write_queue_builder_inputs(
                root, [queue_builder_story_record("line-1", "absent")]
            )
            metadata = queue_builder._source_queue_metadata

            def remove_then_snapshot(*args, **kwargs):
                manifest.unlink()
                return metadata(*args, **kwargs)

            with patch.object(
                queue_builder,
                "_source_queue_metadata",
                side_effect=remove_then_snapshot,
            ):
                with self.assertRaises(
                    queue_builder.GenerationQueueBuildError
                ) as caught:
                    queue_builder.inspect_generation_queue(story, manifest)
            self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            self.assertTrue(story.is_file())

    def test_quality_publication_missing_plan_stays_a_quality_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, evaluation, generation, _quality = (
                publish_source_reference_quality_fixture(root)
            )
            self._disappear_after(
                source_reference_quality,
                "load_source_reference_plan",
                plan.directory / "plan.json",
                lambda: (
                    source_reference_quality.publish_source_reference_quality_review(
                        plan.directory,
                        evaluation.directory,
                        generation.state,
                        root / "outputs",
                    )
                ),
                source_reference_quality.SourceReferenceQualityError,
                root / "outputs",
            )

    def test_voice_reference_read_failure_stays_a_gate_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, _state, queue_id, plan, decision = create_voice_quality_review(
                root
            )
            gate = voice_quality_gate.build_voice_quality_gate(
                workspace, plan, decision
            )
            loader = voice_quality_gate.load_workspace_authority
            original_open = Path.open
            authority_loaded = False

            def load_then_arm(*args, **kwargs):
                nonlocal authority_loaded
                result = loader(*args, **kwargs)
                authority_loaded = True
                return result

            def open_unless_reference(path, *args, **kwargs):
                if authority_loaded and path.suffix == ".wav":
                    raise PermissionError("Reference became unreadable")
                return original_open(path, *args, **kwargs)

            with (
                patch.object(
                    voice_quality_gate,
                    "load_workspace_authority",
                    side_effect=load_then_arm,
                ),
                patch.object(
                    Path, "open", autospec=True, side_effect=open_unless_reference
                ),
            ):
                with self.assertRaises(
                    voice_quality_gate.VoiceQualityGateError
                ) as caught:
                    voice_quality_gate.inspect_voice_quality_gate(
                        gate, workspace, queue_id
                    )
            self.assertIsInstance(caught.exception.__cause__, PermissionError)

    def test_reference_evaluation_missing_plan_stays_a_review_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report, review, story = write_source_reference_review_inputs(root)
            plan = source_reference_review.import_source_reference_review(
                report, review, story, root / "plan"
            )
            self._disappear_after(
                source_reference_review,
                "load_source_reference_plan",
                plan.directory / "plan.json",
                lambda: source_reference_review.publish_source_reference_evaluation(
                    plan.directory, root / "outputs"
                ),
                source_reference_review.SourceReferenceReviewError,
                root / "outputs",
            )
