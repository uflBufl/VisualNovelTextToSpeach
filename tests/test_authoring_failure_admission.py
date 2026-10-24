"""Two real-file controls for failure-plan and repair-admission contracts."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from tests.authoring_fixtures import create_carry_source_workspace
from tests.bulk_generation_fixtures import SyntheticRenderer, audio_samples
from tests.generation_state_fixtures import replace_generation_state
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    publish_generated_manifest,
    run_bulk_generation,
)
from vntts.authoring.failure_repair import FailureRepairPolicy
from vntts.authoring.generation_failures import generation_failure_repair_plan
from vntts.authoring.generation_state import provider_attempts
from vntts.authoring.workbench import AuthoringWorkbenchError, create_resume_workspace
from vntts.synthesis import SynthesisCompletion


class AuthoringFailureAdmissionTest(unittest.TestCase):
    def moss_renderer(self, outcomes=None, *, pcm=None):
        renderer = SyntheticRenderer(outcomes, diagnostics_backend="moss-tts", pcm=pcm)
        renderer.name = "moss-tts"
        renderer.model_name = "model with spaces"
        return renderer

    def generate(self, workspace, renderer, queue_id, policy=None):
        return run_bulk_generation(
            workspace / "queue.jsonl",
            workspace / "generated-audio",
            renderer,
            provider="moss-tts",
            model="model with spaces",
            generation_profile="stable",
            retries=0,
            seed=0,
            include_queue_ids=(queue_id,),
            failure_repair_policy=policy,
        )

    def failed_source(self, root):
        fixture, imported, source = create_carry_source_workspace(
            root, text="The gate is already open. We should leave before dawn."
        )
        queue = source.directory / "queue.jsonl"
        state_path = source.directory / "generated-audio/generation-state.sqlite3"
        state = load_generation_state(state_path, queue)
        old = state["items"].pop(fixture["queue_id"])
        (state_path.parent / old["path"]).unlink()
        replace_generation_state(state_path, state)
        publish_generated_manifest(state_path)
        self.generate(
            source.directory,
            self.moss_renderer([SynthesisCompletion.LIMITED]),
            fixture["queue_id"],
        )
        return fixture, imported, source

    def successor(self, fixture, imported, source, root, policy):
        return create_resume_workspace(
            imported,
            root,
            story_index=fixture["job"]["story_index"],
            voice_manifest=fixture["job"]["voice_manifest"],
            backend="moss-tts",
            model="model with spaces",
            generation_profile="stable",
            narrator_character="Rhiannon",
            failure_repair_policy=policy,
            carry_forward_from=source,
        )

    def test_explicit_provider_map_keeps_fresh_provider_budget(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = self.failed_source(root)
            queue_id = fixture["queue_id"]
            queue = source.directory / "queue.jsonl"
            state_path = source.directory / "generated-audio/generation-state.sqlite3"
            state = load_generation_state(state_path, queue)
            item = state["items"][queue_id]
            item["attempts"] = 3
            item["attempts_by_provider"] = {"legacy-unbound": 3}
            replace_generation_state(state_path, state)
            persisted = load_generation_state(state_path, queue)["items"][queue_id]
            self.assertEqual(provider_attempts(persisted, 3).get("moss-tts", 0), 0)
            policy = FailureRepairPolicy(bounded_seed_retry_queue_ids=(queue_id,))
            with self.subTest(contract="planner uses provider counts"):
                plan = generation_failure_repair_plan(state_path, queue)
                self.assertEqual(plan["records"][0]["action"], "bounded_seed_retry")

            # Two earlier attempts also must not prove a current-provider failure.
            item["attempts"] = 2
            item["attempts_by_provider"] = {"legacy-unbound": 2}
            replace_generation_state(state_path, state)
            before = state_path.read_bytes()
            target = root / "unsupported-provider-carry"
            with self.subTest(contract="successor requires a proven provider attempt"):
                with self.assertRaises(AuthoringWorkbenchError):
                    self.successor(fixture, imported, source.directory, target, policy)
                self.assertEqual(state_path.read_bytes(), before)
                self.assertEqual(list(target.iterdir()), [])

            item["attempts"] = 3
            item["attempts_by_provider"] = {"legacy-unbound": 3}
            replace_generation_state(state_path, state)
            renderer = self.moss_renderer()
            result = self.generate(source.directory, renderer, queue_id, policy)
            final = load_generation_state(result.state, queue)["items"][queue_id]
            self.assertEqual(result.generated, 1)
            self.assertEqual([request.seed for request in renderer.requests], [0])
            self.assertEqual(final["attempts"], 4)
            self.assertEqual(
                final["attempts_by_provider"], {"legacy-unbound": 3, "moss-tts": 1}
            )

    def test_carried_sentence_history_blocks_repeat_in_both_admissions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture, imported, source = self.failed_source(root)
            queue_id = fixture["queue_id"]
            sentence_policy = FailureRepairPolicy((queue_id,))
            sentence = self.successor(
                fixture, imported, source.directory, root / "sentence", sentence_policy
            )
            self.generate(
                sentence.directory,
                self.moss_renderer([SynthesisCompletion.LIMITED]),
                queue_id,
                sentence_policy,
            )
            bounded_policy = FailureRepairPolicy(
                bounded_seed_retry_queue_ids=(queue_id,)
            )
            bounded = self.successor(
                fixture, imported, sentence.directory, root / "bounded", bounded_policy
            )
            silence = np.concatenate(
                [
                    audio_samples(),
                    np.zeros(3 * 16_000, dtype=np.float32),
                    audio_samples(),
                ]
            )
            failed = self.generate(
                bounded.directory,
                self.moss_renderer(pcm=silence),
                queue_id,
                bounded_policy,
            )
            queue = bounded.directory / "queue.jsonl"
            state = load_generation_state(failed.state, queue)
            item = state["items"][queue_id]
            self.assertEqual(item["failure"]["kind"], "speech_silence")
            self.assertEqual(item["failure_repair"]["strategy"], "bounded_seed_retry")
            self.assertEqual(
                item["carry_forward"]["source_repair_strategy"],
                "sentence_boundary_segmentation",
            )
            plan = generation_failure_repair_plan(failed.state, queue)
            self.assertEqual(plan["records"][0]["action"], "reference_comparison")
            before = failed.state.read_bytes()
            target = root / "repeated-sentence"
            with self.subTest(contract="successor refuses historical repeat"):
                with self.assertRaises(AuthoringWorkbenchError):
                    self.successor(
                        fixture, imported, bounded.directory, target, sentence_policy
                    )
                self.assertEqual(failed.state.read_bytes(), before)
                self.assertEqual(list(target.iterdir()), [])
            renderer = self.moss_renderer()
            with self.subTest(contract="bulk refuses historical repeat"):
                with self.assertRaisesRegex(
                    BulkGenerationError, "Sentence repair already failed"
                ):
                    self.generate(
                        bounded.directory, renderer, queue_id, sentence_policy
                    )
                self.assertEqual(renderer.requests, [])
                self.assertEqual(failed.state.read_bytes(), before)
