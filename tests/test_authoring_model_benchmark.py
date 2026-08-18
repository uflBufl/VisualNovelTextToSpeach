import hashlib
import json
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
    load_benchmark_corpus,
    load_model_variants,
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
            {
                "queue_id": "warm-1",
                "action": "generate",
                "emotion": {"primary": "warm"},
            },
            {
                "queue_id": "warm-2",
                "action": "generate",
                "emotion": {"primary": "warm"},
            },
            {
                "queue_id": "angry",
                "action": "generate",
                "emotion": {"primary": "angry"},
            },
            {"queue_id": "manual", "action": "manual_review"},
        ]

        selected = select_representative_items(items, 3)

        self.assertEqual(
            [item["queue_id"] for item in selected], ["angry", "warm-1", "warm-2"]
        )

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
        self.assertEqual(corpus["samples"][0]["text_sha256"], text_hash)

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
        self.assertRegex(report["samples"][0]["audio_sha256"], r"^[0-9a-f]{64}$")

    def test_renderer_applies_explicit_variant_voice_without_changing_corpus_identity(
        self,
    ):
        backend = FakeRenderBackend()
        variant = ModelVariant("narrator/paper-heron", "fake", voice="Paper Heron")
        with TemporaryDirectory() as directory:
            report = benchmark_renderer(
                variant,
                backend,
                [
                    {
                        "id": "narration-line",
                        "line_id": "line:narration",
                        "character": "Narrator",
                        "text": "A fixed narration line.",
                    }
                ],
                directory,
                seed=7,
            )

        self.assertEqual(backend.requests[0].voice, "Paper Heron")
        self.assertEqual(report["voice_override"], "Paper Heron")
        self.assertEqual(report["samples"][0]["character"], "Narrator")
        self.assertEqual(report["samples"][0]["synthesis_voice"], "Paper Heron")
        self.assertEqual(report["samples"][0]["seed"], 7)

    def test_model_variants_validate_optional_voice_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "model_id": "narrator/centurion",
                            "backend": "fake",
                            "voice": "Centurion",
                        },
                        {
                            "model_id": "narrator/paper-heron",
                            "backend": "fake",
                            "voice": "Paper Heron",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            variants = load_model_variants(path)
            self.assertEqual(
                [variant.voice for variant in variants], ["Centurion", "Paper Heron"]
            )
            for invalid in (None, "", "   ", 3141):
                with self.subTest(invalid=invalid):
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document[0]["voice"] = invalid
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaisesRegex(ModelBenchmarkError, "voice"):
                        load_model_variants(path)

    def test_limited_render_is_not_published_as_benchmark_sample(self):
        with (
            TemporaryDirectory() as directory,
            self.assertRaisesRegex(ModelBenchmarkError, "limited"),
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
            text_hash = hashlib.sha256(b" Same line. ").hexdigest()
            corpus.write_text(
                '{"schema":"vntts.tts-benchmark-corpus","schema_version":1,'
                '"name":"Shared","samples":['
                '{"id":"one","line_id":"line-one","character":"Voice",'
                f'"text":" Same line. ","text_sha256":"{text_hash}"}}]}}',
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
        self.assertTrue(
            all(backend.requests[0].text == " Same line. " for backend in backends)
        )

    def test_strict_corpus_preserves_identity_and_rejects_drift_or_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "corpus.json"
            text = "  Exact text stays padded.  "
            text_hash = hashlib.sha256(text.encode()).hexdigest()
            document = {
                "schema": "vntts.tts-benchmark-corpus",
                "schema_version": 1,
                "samples": [
                    {
                        "id": "stable-id",
                        "line_id": "opaque-line",
                        "character": "Voice",
                        "text": text,
                        "text_sha256": text_hash,
                    }
                ],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_benchmark_corpus(path)
            self.assertEqual(loaded["samples"][0]["text"], text)
            self.assertEqual(loaded["samples"][0]["line_id"], "opaque-line")
            document["samples"][0]["text_sha256"] = "0" * 64
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ModelBenchmarkError, "exact text"):
                load_benchmark_corpus(path)
            document["samples"][0]["text_sha256"] = text_hash
            document["samples"].append(dict(document["samples"][0]))
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ModelBenchmarkError, "Duplicate"):
                load_benchmark_corpus(path)

    def test_rejects_unsafe_and_casefold_colliding_model_destinations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text = "Exact."
            corpus = root / "corpus.json"
            corpus.write_text(
                json.dumps(
                    {
                        "schema": "vntts.tts-benchmark-corpus",
                        "schema_version": 1,
                        "samples": [
                            {
                                "id": "one",
                                "line_id": "line-one",
                                "character": "Voice",
                                "text": text,
                                "text_sha256": hashlib.sha256(
                                    text.encode()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            for variants, pattern in (
                ((ModelVariant("..", "fake"), ModelVariant("safe", "fake")), "safe"),
                (
                    (ModelVariant("Model", "fake"), ModelVariant("model", "fake")),
                    "collide",
                ),
            ):
                with (
                    self.subTest(pattern=pattern),
                    self.assertRaisesRegex(ModelBenchmarkError, pattern),
                ):
                    benchmark_model_variants(
                        corpus,
                        variants,
                        CharacterVoiceRegistry(),
                        root / f"output-{pattern}",
                    )

    def test_rejects_diagnostics_that_do_not_match_request(self):
        class WrongDiagnostics(FakeRenderBackend):
            def render(self, request):
                stream = super().render(request)

                def produce():
                    chunks = []
                    for chunk in stream:
                        chunks.append(chunk)
                        yield chunk
                    result = stream.result
                    return SynthesisResult(
                        pcm=result.pcm,
                        sample_rate=result.sample_rate,
                        completion=result.completion,
                        limits=result.limits,
                        timing=result.timing,
                        diagnostics=SynthesisDiagnostics(
                            backend=result.diagnostics.backend,
                            cache_source=result.diagnostics.cache_source,
                            generation_profile="different",
                            seed=999,
                            chunk_count=len(chunks),
                            sample_count=result.diagnostics.sample_count,
                        ),
                    )

                return SynthesisChunkStream(produce())

        with (
            TemporaryDirectory() as directory,
            self.assertRaisesRegex(ModelBenchmarkError, "different request"),
        ):
            benchmark_renderer(
                ModelVariant("fake/wrong", "fake"),
                WrongDiagnostics(),
                [{"id": "one", "character": "Voice", "text": "Exact."}],
                directory,
                seed=12,
            )


if __name__ == "__main__":
    unittest.main()
