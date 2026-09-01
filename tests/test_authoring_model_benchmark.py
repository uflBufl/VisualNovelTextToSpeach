import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from tests.symlink_support import symlink_or_skip
from vntts.authoring.model_benchmark import (
    ModelBenchmarkError,
    ModelVariant,
    _comparison_voice_context,
    benchmark_model_variants,
    benchmark_renderer,
    build_benchmark_corpus,
    build_failure_comparison_corpus,
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
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FakeRenderBackend:
    def __init__(
        self,
        completion=SynthesisCompletion.COMPLETE,
        *,
        backend_name="fake",
    ):
        self.completion = completion
        self.backend_name = backend_name
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
                    backend=self.backend_name,
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
    def test_comparison_manifest_rejects_reference_symlink_outside_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.wav"
            outside.write_bytes(b"must-not-be-published")
            pack = root / "pack"
            pack.mkdir()
            symlink_or_skip(pack / "reference.wav", outside)
            manifest = pack / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {
                                "character": "Rhiannon",
                                "speaker": "rhiannon",
                                "references": ["reference.wav"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ModelBenchmarkError, "must not use symlinks"):
                _comparison_voice_context(manifest, "Rhiannon")

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

    def test_builds_exact_failure_recovery_and_control_comparison_corpus(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            items = []
            for index, character in enumerate(
                ("Rhiannon", "Narrator", "Rhiannon", "Narrator", "Rhiannon"),
                start=1,
            ):
                text = f"Exact comparison line {index}."
                text_hash = hashlib.sha256(text.encode()).hexdigest()
                items.append(
                    {
                        "record_type": "generation_item",
                        "queue_id": f"line:{index}:{text_hash[:16]}",
                        "line_id": f"line:{index}",
                        "text_sha256": text_hash,
                        "text": text,
                        "speaker": character,
                        "voice_character": character,
                        "action": "generate",
                        "state": "pending",
                    }
                )
            write_voice_generation_queue(
                queue,
                {"game": "Any Game", "language": "en"},
                items,
            )
            states = {
                items[0]["queue_id"]: {
                    "status": "failed",
                    "provider": "moss-tts",
                    "failure": {"kind": "missed_eos_audio_limit"},
                    "attempts_by_provider": {"moss-tts": 3},
                },
                items[1]["queue_id"]: {
                    "status": "failed",
                    "provider": "moss-tts",
                    "failure": {"kind": "speech_silence"},
                    "attempts_by_provider": {"moss-tts": 3},
                },
                items[2]["queue_id"]: {
                    "status": "approved",
                    "provider": "pocket-tts",
                    "attempts_by_provider": {"moss-tts": 2, "pocket-tts": 1},
                    "source_reference_binding": {
                        "synthesis_voice_character": "Bound Rhiannon reference"
                    },
                },
                items[3]["queue_id"]: {
                    "status": "approved",
                    "provider": "moss-tts",
                    "attempts_by_provider": {"moss-tts": 1},
                },
                items[4]["queue_id"]: {
                    "status": "generated",
                    "provider": "moss-tts",
                    "attempts_by_provider": {"moss-tts": 1},
                },
            }
            state_document = {"items": states}
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state_document), encoding="utf-8")

            corpus = build_failure_comparison_corpus(
                queue,
                state_path,
                root / "corpus.json",
                pocket_sample_size=1,
                control_sample_size=1,
                state_loader=lambda _state, _queue: state_document,
            )

        self.assertEqual(
            corpus["selection"],
            {
                "unresolved_moss_failures": 2,
                "moss_to_pocket_recoveries": 1,
                "moss_controls": 1,
            },
        )
        self.assertEqual(
            [sample["comparison_group"] for sample in corpus["samples"]],
            [
                "unresolved_moss_failure",
                "unresolved_moss_failure",
                "moss_to_pocket_recovery",
                "moss_control",
            ],
        )
        self.assertEqual(corpus["samples"][2]["character"], "Bound Rhiannon reference")
        self.assertRegex(corpus["source_state_sha256"], r"^[0-9a-f]{64}$")

    def test_failure_corpus_resolves_narrator_and_source_bindings_from_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            items = []
            for index, character in enumerate(("Narrator", "Aderyn"), start=1):
                text = f"Bound comparison line {index}."
                text_hash = hashlib.sha256(text.encode()).hexdigest()
                items.append(
                    {
                        "record_type": "generation_item",
                        "queue_id": f"line:{index}:{text_hash[:16]}",
                        "line_id": f"line:{index}",
                        "text_sha256": text_hash,
                        "text": text,
                        "speaker": character,
                        "voice_character": character,
                        "action": "generate",
                        "state": "pending",
                    }
                )
            write_voice_generation_queue(
                queue,
                {"game": "Any Game", "language": "en"},
                items,
            )
            state_document = {
                "items": {
                    items[0]["queue_id"]: {
                        "status": "failed",
                        "provider": "moss-tts",
                        "attempts_by_provider": {"moss-tts": 3},
                        "source_reference_binding": {
                            "source_voice_character": "Narrator",
                            "synthesis_voice_character": "Stale selected narrator",
                        },
                    },
                    items[1]["queue_id"]: {
                        "status": "failed",
                        "provider": "moss-tts",
                        "attempts_by_provider": {"moss-tts": 3},
                        "source_reference_binding": {
                            "source_voice_character": "Aderyn",
                            "synthesis_voice_character": "Stale selected Aderyn",
                        },
                    },
                }
            }
            state_path = root / "state.json"
            state_path.write_text(json.dumps(state_document), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "voices": [
                            {"character": "Centurion", "speaker": "centurion"},
                            {
                                "character": "Bound Aderyn",
                                "speaker": "aderyn",
                            },
                        ],
                        "vntts.authoring.source_reference_bindings": {
                            "selected_variants": [
                                {
                                    "voice_character": "Bound Aderyn",
                                    "queue_ids": [items[1]["queue_id"]],
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            corpus = build_failure_comparison_corpus(
                queue,
                state_path,
                root / "corpus.json",
                pocket_sample_size=0,
                control_sample_size=0,
                manifest_path=manifest,
                narrator_character="Centurion",
                state_loader=lambda _state, _queue: state_document,
            )

        self.assertEqual(
            [sample["character"] for sample in corpus["samples"]],
            ["Centurion", "Bound Aderyn"],
        )
        self.assertEqual(
            [sample["prior_synthesis_voice"] for sample in corpus["samples"]],
            ["Stale selected narrator", "Stale selected Aderyn"],
        )
        self.assertEqual(corpus["narrator_character"], "Centurion")
        self.assertRegex(corpus["source_voice_manifest_sha256"], r"^[0-9a-f]{64}$")

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

    def test_pocket_renderer_does_not_request_unsupported_seed(self):
        backend = FakeRenderBackend(backend_name="pocket-tts")
        variant = ModelVariant(
            "pocket/fallback", "pocket-tts", generation_profile="default"
        )
        with TemporaryDirectory() as directory:
            report = benchmark_renderer(
                variant,
                backend,
                [{"id": "sample", "character": "Voice", "text": "A line."}],
                directory,
                seed=11,
            )

        self.assertIsNone(backend.requests[0].seed)
        self.assertEqual(report["seed_policy"], "unsupported")
        self.assertEqual(report["samples"][0]["requested_shared_seed"], 11)
        self.assertEqual(report["samples"][0]["seed_policy"], "unsupported")

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

    def test_xtts_requires_explicit_terms_and_records_unsupported_seed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text = "A shared exact line."
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
                                "character": "Rhiannon",
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
            rejected = (
                ModelVariant("xtts", "coqui-xtts", voice="Rhiannon"),
                ModelVariant("moss", "fake"),
            )
            with self.assertRaisesRegex(ModelBenchmarkError, "CPML"):
                benchmark_model_variants(
                    corpus,
                    rejected,
                    CharacterVoiceRegistry(),
                    root / "rejected",
                )

            captured = []

            def factory(name, registry, cache, **options):
                del registry, cache
                captured.append((name, options))
                return FakeRenderBackend(backend_name=name)

            reference = root / "rhiannon.wav"
            reference.write_bytes(b"exact-reference")
            registry = CharacterVoiceRegistry(
                [
                    CharacterVoice(
                        "Rhiannon",
                        "rhiannon",
                        references=(reference,),
                    )
                ]
            )
            aggregate = benchmark_model_variants(
                corpus,
                (
                    ModelVariant(
                        "xtts",
                        "coqui-xtts",
                        voice="Rhiannon",
                        terms_accepted=True,
                    ),
                    ModelVariant("moss", "fake"),
                ),
                registry,
                root / "accepted",
                seed=23,
                backend_factory=factory,
            )
            xtts_report = json.loads(Path(aggregate["reports"][0]).read_text())

        self.assertEqual(captured[0][1]["terms_accepted"], True)
        self.assertEqual(xtts_report["seed_policy"], "unsupported")
        self.assertEqual(xtts_report["samples"][0]["requested_shared_seed"], 23)
        self.assertIsNone(xtts_report["samples"][0]["seed"])
        self.assertEqual(xtts_report["samples"][0]["seed_policy"], "unsupported")

    def test_voice_controls_are_snapshotted_once_for_every_real_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text = "A shared exact line."
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
                                "character": "Rhiannon",
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
            reference = root / "rhiannon.wav"
            reference_bytes = b"immutable-reference-bytes"
            reference.write_bytes(reference_bytes)
            registry = CharacterVoiceRegistry(
                [
                    CharacterVoice(
                        "Rhiannon",
                        "rhiannon",
                        references=(reference,),
                    )
                ]
            )
            received_references = []

            def factory(name, received_registry, cache, **options):
                del cache, options
                captured_reference = received_registry.resolve("Rhiannon").references[0]
                received_references.append(
                    (captured_reference, captured_reference.read_bytes())
                )
                return FakeRenderBackend(backend_name=name)

            output = root / "comparison"
            aggregate = benchmark_model_variants(
                corpus,
                (
                    ModelVariant("moss", "moss-tts"),
                    ModelVariant(
                        "xtts",
                        "coqui-xtts",
                        terms_accepted=True,
                    ),
                ),
                registry,
                output,
                backend_factory=factory,
            )

            snapshot = output / "voice-controls/voice-001/reference-001.wav"
            reports = [
                json.loads(Path(path).read_text()) for path in aggregate["reports"]
            ]
            expected_reference_sha = hashlib.sha256(reference_bytes).hexdigest()
            self.assertEqual(snapshot.read_bytes(), reference_bytes)
            self.assertEqual(received_references[0][0], received_references[1][0])
            self.assertEqual(
                [payload for _, payload in received_references],
                [reference_bytes, reference_bytes],
            )
            self.assertEqual(
                aggregate["voice_controls"],
                [
                    {
                        "character": "Rhiannon",
                        "speaker": "rhiannon",
                        "reference_index": 1,
                        "source": str(reference.resolve()),
                        "audio": str(snapshot.resolve()),
                        "sha256": expected_reference_sha,
                        "size": len(reference_bytes),
                    }
                ],
            )
            self.assertTrue(aggregate["voice_controls_sha256"])
            self.assertTrue(aggregate["voice_controls_content_sha256"])
            self.assertEqual(
                {report["voice_controls_sha256"] for report in reports},
                {aggregate["voice_controls_sha256"]},
            )
            self.assertEqual(
                {report["voice_controls_content_sha256"] for report in reports},
                {aggregate["voice_controls_content_sha256"]},
            )

    def test_voice_cloning_model_rejects_unresolved_corpus_voice(self):
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
                                "character": "Missing voice",
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

            with self.assertRaisesRegex(ModelBenchmarkError, "unresolved"):
                benchmark_model_variants(
                    corpus,
                    (
                        ModelVariant(
                            "xtts",
                            "coqui-xtts",
                            terms_accepted=True,
                        ),
                        ModelVariant("control", "fake"),
                    ),
                    CharacterVoiceRegistry(),
                    root / "output",
                )

    def test_model_variant_terms_flag_must_be_boolean(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "model_id": "xtts",
                            "backend": "coqui-xtts",
                            "terms_accepted": "yes",
                        },
                        {"model_id": "moss", "backend": "moss-tts"},
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ModelBenchmarkError, "terms_accepted"):
                load_model_variants(path)

    def test_delay_variant_cuda_requirement_is_explicit_and_scoped(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "model_id": "delay",
                            "backend": "moss-tts-delay",
                            "model_revision": "c" * 40,
                            "require_cuda": True,
                        },
                        {"model_id": "control", "backend": "fake"},
                    ]
                ),
                encoding="utf-8",
            )
            variant = load_model_variants(path)[0]
            self.assertTrue(variant.require_cuda)
            self.assertEqual(variant.model_revision, "c" * 40)

            document = json.loads(path.read_text(encoding="utf-8"))
            document[0]["require_cuda"] = "yes"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ModelBenchmarkError, "require_cuda"):
                load_model_variants(path)

            document[0] = {
                "model_id": "local",
                "backend": "moss-tts",
                "require_cuda": True,
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ModelBenchmarkError, "only"):
                load_model_variants(path)

            document[0] = {
                "model_id": "delay",
                "backend": "moss-tts-delay",
                "model_revision": "latest",
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ModelBenchmarkError, "exact commit"):
                load_model_variants(path)

    def test_single_cuda_candidate_is_a_render_batch_not_a_comparison(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            text = "One exact CUDA candidate line."
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
                                "character": "Rhiannon",
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
            reference = root / "rhiannon.wav"
            reference.write_bytes(b"exact-reference")
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Rhiannon", "rhiannon", references=(reference,))]
            )
            received = []

            def factory(name, received_registry, cache, **options):
                del received_registry, cache
                received.append((name, options))
                return FakeRenderBackend(backend_name=name)

            aggregate = benchmark_model_variants(
                corpus,
                (
                    ModelVariant(
                        "moss-delay-8b",
                        "moss-tts-delay",
                        require_cuda=True,
                    ),
                ),
                registry,
                root / "output",
                backend_factory=factory,
            )
            repeated = benchmark_model_variants(
                corpus,
                (
                    ModelVariant(
                        "moss-delay-8b",
                        "moss-tts-delay",
                        require_cuda=True,
                    ),
                ),
                registry,
                root / "repeated-output",
                backend_factory=factory,
            )

        self.assertFalse(aggregate["comparison_ready"])
        self.assertTrue(aggregate["manual_review_required"])
        self.assertNotEqual(
            aggregate["voice_controls_sha256"],
            repeated["voice_controls_sha256"],
        )
        self.assertEqual(
            aggregate["voice_controls_content_sha256"],
            repeated["voice_controls_content_sha256"],
        )
        self.assertEqual(
            received,
            [
                ("moss-tts-delay", {"model_name": None, "require_cuda": True}),
                ("moss-tts-delay", {"model_name": None, "require_cuda": True}),
            ],
        )

    def test_limited_render_is_reported_without_publishing_partial_wav(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "model-output"
            report = benchmark_renderer(
                ModelVariant("fake/limited", "fake"),
                FakeRenderBackend(SynthesisCompletion.LIMITED),
                [{"id": "sample", "character": "Voice", "text": "A line."}],
                output,
            )
            self.assertEqual(report["summary"]["limited"], 1)
            self.assertEqual(report["summary"]["complete"], 0)
            self.assertEqual(report["samples"][0]["outcome"], "limited")
            self.assertNotIn("audio", report["samples"][0])
            self.assertEqual(list((output / "audio").glob("*.wav")), [])

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

    def test_multi_model_benchmark_publishes_its_validated_corpus(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "source-corpus.json"
            text = "Exact shared line."
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

            def factory(name, registry, cache, **options):
                del registry, cache, options
                return FakeRenderBackend(backend_name=name)

            output = root / "output"
            aggregate = benchmark_model_variants(
                corpus,
                (ModelVariant("first", "first"), ModelVariant("second", "second")),
                CharacterVoiceRegistry(),
                output,
                backend_factory=factory,
            )
            published_corpus = (output / "benchmark-corpus.json").resolve()

            self.assertEqual(Path(aggregate["corpus"]), published_corpus)
            self.assertEqual(
                aggregate["corpus_sha256"],
                hashlib.sha256(published_corpus.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                load_benchmark_corpus(published_corpus)["samples"][0]["text"], text
            )

    def test_late_backend_start_failure_publishes_no_partial_bakeoff(self):
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
            calls = 0

            def factory(name, registry, cache, **options):
                nonlocal calls
                del name, registry, cache, options
                calls += 1
                if calls == 2:
                    raise ValueError("second backend is unavailable")
                return FakeRenderBackend()

            output = root / "comparison"
            with self.assertRaisesRegex(ModelBenchmarkError, "unavailable"):
                benchmark_model_variants(
                    corpus,
                    (
                        ModelVariant("first", "fake"),
                        ModelVariant("second", "fake"),
                    ),
                    CharacterVoiceRegistry(),
                    output,
                    backend_factory=factory,
                )

            self.assertFalse(output.exists())

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
