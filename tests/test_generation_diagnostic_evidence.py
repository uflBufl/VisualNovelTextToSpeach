import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.file_integrity import sha256_file

from tests.bulk_generation_fixtures import SyntheticRenderer, queue_item, write_queue
from vntts.authoring import bulk_generation
from vntts.pregeneration_generation import OfflineGenerationWorker
from vntts.synthesis import SynthesisChunkStream, SynthesisCompletion


class DiagnosticRenderer(SyntheticRenderer):
    name = "moss-tts"
    model_name = "synthetic-moss-model"

    def __init__(self, *, cache_source="fresh-generation", **options):
        super().__init__(**options)
        self.cache_source = cache_source

    def render(self, request):
        stream = super().render(request)

        def produce():
            yield from stream
            result = stream.result
            return replace(
                result,
                diagnostics=replace(result.diagnostics, cache_source=self.cache_source),
            )

        return SynthesisChunkStream(produce())


class GenerationDiagnosticEvidenceTest(unittest.TestCase):
    def generate(self, root, renderer, **options):
        item = queue_item(text="PRIVATE dialogue for the synthetic generation test.")
        queue = write_queue(root / "queue.jsonl", [item])
        result = bulk_generation.run_bulk_generation(
            queue,
            root / "output",
            renderer,
            provider=renderer.name,
            model=renderer.model_name,
            generation_profile="stable",
            retries=0,
            seed=17,
            **options,
        )
        return item, result

    def test_typed_fresh_render_and_atomic_publication_have_matching_evidence(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(bulk_generation, "record_diagnostic_event") as record,
        ):
            root = Path(directory)
            item, result = self.generate(root, DiagnosticRenderer())
            state = json.loads(result.state.read_text())
            published = state["items"][item["queue_id"]]
            audio_sha256 = sha256_file(root / "output" / published["path"])
        self.assertEqual(result.generated, 1)
        self.assertEqual(len(record.call_args_list), 2)
        render, publication = record.call_args_list
        self.assertEqual(render.args, ("generation", "generation-render-result"))
        self.assertEqual(render.kwargs["line_id"], item["line_id"])
        self.assertEqual(render.kwargs["process_id"], os.getpid())
        self.assertIsNone(render.kwargs["worker_pid"])
        self.assertEqual(render.kwargs["provider"], "moss-tts")
        self.assertEqual(render.kwargs["cache_source"], "fresh-generation")
        self.assertEqual(render.kwargs["completion"], "complete")
        self.assertEqual(render.kwargs["seed"], 17)
        self.assertEqual(render.kwargs["synthesis_ms"], 2.0)
        self.assertEqual(render.kwargs["first_chunk_ms"], 1.0)
        self.assertEqual(render.kwargs["sample_count"], 4000)
        self.assertEqual(render.kwargs["chunk_count"], 1)
        self.assertEqual(publication.args, ("generation", "generation-published"))
        self.assertEqual(publication.kwargs["source_sha256"], audio_sha256)
        self.assertEqual(publication.kwargs["sample_count"], 4000)
        self.assertEqual(publication.kwargs["sample_rate"], 16000)
        self.assertNotIn("PRIVATE", repr(record.call_args_list))
        self.assertNotIn("path", publication.kwargs)

    def test_cache_source_comes_from_typed_result_even_when_bypass_was_requested(self):
        for cache_source in ("memory-cache", "persistent-cache"):
            with (
                self.subTest(cache_source=cache_source),
                TemporaryDirectory() as directory,
            ):
                renderer = DiagnosticRenderer(cache_source=cache_source)
                with patch.object(bulk_generation, "record_diagnostic_event") as record:
                    self.generate(Path(directory), renderer)
                self.assertEqual(renderer.requests[0].cache_policy.value, "bypass")
                self.assertEqual(
                    record.call_args_list[0].kwargs["cache_source"], cache_source
                )

    def test_incomplete_typed_result_is_recorded_without_claiming_publication(self):
        for completion in (SynthesisCompletion.CANCELLED, SynthesisCompletion.LIMITED):
            with self.subTest(completion=completion), TemporaryDirectory() as directory:
                renderer = DiagnosticRenderer(outcomes=[completion])
                with patch.object(bulk_generation, "record_diagnostic_event") as record:
                    _item, result = self.generate(Path(directory), renderer)
                self.assertEqual(result.generated, 0)
                self.assertEqual(record.call_count, 1)
                self.assertEqual(
                    record.call_args.args, ("generation", "generation-render-result")
                )
                self.assertEqual(
                    record.call_args.kwargs["completion"], completion.value
                )

    def test_failed_output_validation_does_not_claim_publication(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(bulk_generation, "record_diagnostic_event") as record,
            patch.object(
                bulk_generation,
                "write_pcm16_wav",
                side_effect=OSError("output unavailable"),
            ),
        ):
            _item, result = self.generate(Path(directory), DiagnosticRenderer())
        self.assertEqual(result.generated, 0)
        self.assertEqual(record.call_count, 1)
        self.assertEqual(record.call_args.args[1], "generation-render-result")

    def test_generation_owner_is_distinct_from_concrete_retained_worker_pid(self):
        for attribute in ("process", "server"):
            with self.subTest(attribute=attribute), TemporaryDirectory() as directory:
                root = Path(directory)
                observed = {}

                def observe(_request):
                    state = json.loads(
                        (root / "output" / "generation-state.json").read_text()
                    )
                    observed.update(state["active"])

                renderer = DiagnosticRenderer(inspect_state=observe)
                setattr(
                    renderer, attribute, Mock(pid=87654, poll=Mock(return_value=None))
                )
                with patch.object(bulk_generation, "record_diagnostic_event") as record:
                    self.generate(root, renderer)
                self.assertEqual(observed["generation_owner_pid"], os.getpid())
                self.assertEqual(observed["runtime_worker_pid"], 87654)
                self.assertEqual(record.call_args_list[0].kwargs["worker_pid"], 87654)

    def test_runtime_pid_is_unknown_for_in_process_and_finished_backends(self):
        backend = DiagnosticRenderer()
        self.assertIsNone(bulk_generation._runtime_worker_pid(backend))
        backend.process = Mock(pid=87654, poll=Mock(return_value=0))
        self.assertIsNone(bulk_generation._runtime_worker_pid(backend))
        backend.process = Mock(pid=True, poll=Mock(return_value=None))
        self.assertIsNone(bulk_generation._runtime_worker_pid(backend))

    def test_progress_uses_owner_identity_and_keeps_legacy_state_compatible(self):
        worker = OfflineGenerationWorker()
        worker._in_process_active = True
        inputs = SimpleNamespace(
            queue_sha256="queue-sha",
            story_index="unused-story.jsonl",
            story_index_sha256="story-sha",
        )
        for identity, expected in (
            (
                {"generation_owner_pid": os.getpid(), "runtime_worker_pid": 87654},
                "GPU runtime",
            ),
            ({"runtime_worker_pid": os.getpid()}, "GPU runtime"),
            ({"generation_owner_pid": None, "runtime_worker_pid": os.getpid()}, None),
            ({"generation_owner_pid": 87654, "runtime_worker_pid": os.getpid()}, None),
        ):
            with (
                self.subTest(identity=identity),
                patch(
                    "vntts.pregeneration_generation._ready_line_ids", return_value=()
                ),
            ):
                progress = worker._progress_from_state(
                    {
                        "queue_sha256": "queue-sha",
                        "items": {},
                        "active": {
                            "phase": "generating",
                            "runtime_status": "GPU runtime",
                            **identity,
                        },
                    },
                    inputs,
                )
                self.assertEqual(progress.runtime_status, expected)
