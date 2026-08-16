import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from vntts.authoring.model_benchmark import (
    ModelBenchmarkError,
    ModelVariant,
    benchmark_model_variants,
    benchmark_renderer,
    build_benchmark_corpus,
    select_representative_items,
)
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoiceRegistry


class FakeRenderBackend:
    def __init__(self, completion=SynthesisCompletion.COMPLETE):
        self.completion = completion
        self.requests = []
        self.play_calls = 0

    def render(self, request):
        self.requests.append(request)
        pcm = np.array([[0.0], [0.25], [-0.25], [0.0]], dtype=np.float32)

        def produce():
            yield SynthesisChunk(pcm, 16_000, 0, 5.0)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=16_000,
                completion=self.completion,
                limits=SynthesisLimits(256, 3.0),
                timing=SynthesisTiming(5.0, 10.0),
                diagnostics=SynthesisDiagnostics(
                    backend="fake",
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=4,
                ),
            )

        return SynthesisChunkStream(produce())

    def play(self, _prepared):
        self.play_calls += 1
        raise AssertionError("authoring benchmark must not open playback")

    def stop(self):
        return False


class AuthoringModelBenchmarkTest(unittest.TestCase):
    def test_selects_emotion_buckets_round_robin_and_skips_review(self):
        items = [
            {"queue_id": "warm-1", "action": "generate", "emotion": {"primary": "warm"}},
            {"queue_id": "warm-2", "action": "generate", "emotion": {"primary": "warm"}},
            {"queue_id": "angry", "action": "generate", "emotion": {"primary": "angry"}},
            {"queue_id": "manual", "action": "manual_review"},
        ]

        selected = select_representative_items(items, 3)

        self.assertEqual([item["queue_id"] for item in selected], ["angry", "warm-1", "warm-2"])

    def test_builds_generic_corpus_from_shared_queue_without_game_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text = "A generic corpus line."
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            queue = root / "queue.jsonl"
            write_voice_generation_queue(
                queue,
                {"game": "Any Game", "language": "en"},
                [
                    {
                        "record_type": "generation_item",
                        "queue_id": f"line:any:{text_hash[:16]}",
                        "line_id": "line:any",
                        "text_sha256": text_hash,
                        "text": text,
                        "speaker": "Speaker",
                        "voice_character": "Voice",
                        "action": "generate",
                        "state": "pending",
                    }
                ],
            )

            corpus = build_benchmark_corpus(queue, root / "corpus.json")

        self.assertEqual(corpus["samples"][0]["character"], "Voice")
        self.assertEqual(corpus["samples"][0]["line_id"], "line:any")

    def test_renderer_uses_typed_bypass_request_and_never_plays(self):
        backend = FakeRenderBackend()
        variant = ModelVariant("fake/one", "fake", generation_profile="expressive")
        with TemporaryDirectory() as directory:
            report = benchmark_renderer(
                variant,
                backend,
                [{"id": "sample", "character": "Voice", "text": "A line."}],
                directory,
                seed=11,
            )

        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0].cache_policy.value, "bypass")
        self.assertEqual(backend.requests[0].seed, 11)
        self.assertEqual(backend.play_calls, 0)
        self.assertEqual(report["samples"][0]["sample_rate"], 16_000)

    def test_limited_render_is_not_published_as_benchmark_sample(self):
        with TemporaryDirectory() as directory, self.assertRaisesRegex(
            ModelBenchmarkError, "limited"
        ):
            benchmark_renderer(
                ModelVariant("fake/limited", "fake"),
                FakeRenderBackend(SynthesisCompletion.LIMITED),
                [{"id": "sample", "character": "Voice", "text": "A line."}],
                directory,
            )

    def test_multi_model_benchmark_uses_one_exact_corpus(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus.json"
            corpus.write_text(
                '{"schema_version":1,"name":"Shared","samples":['
                '{"id":"one","character":"Voice","text":"Same line."}]}',
                encoding="utf-8",
            )
            backends = []

            def factory(name, registry, cache, *, model_name=None):
                del name, registry, cache, model_name
                backend = FakeRenderBackend()
                backends.append(backend)
                return backend

            aggregate = benchmark_model_variants(
                corpus,
                (ModelVariant("fake/one", "fake"), ModelVariant("fake/two", "fake")),
                CharacterVoiceRegistry(),
                root / "output",
                backend_factory=factory,
            )

        self.assertEqual(aggregate["sample_count"], 1)
        self.assertEqual(len(aggregate["reports"]), 2)
        self.assertTrue(all(len(backend.requests) == 1 for backend in backends))


if __name__ == "__main__":
    unittest.main()
