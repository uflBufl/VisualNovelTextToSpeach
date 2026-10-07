import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

import vntts.authoring.model_benchmark as benchmark_module
from tests.authoring_fixtures import write_legacy_fixture
from tests.symlink_support import symlink_or_skip
from vntts.authoring.generation_lease import BulkGenerationError
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
from vntts.authoring.publication import rename_directory_no_replace
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


class ShutdownRenderBackend(FakeRenderBackend):
    def __init__(self, *arguments, shutdown_error=None, **keywords):
        super().__init__(*arguments, **keywords)
        self.shutdown_error = shutdown_error
        self.shutdown_calls = 0
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        return False

    def shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error


class AuthoringModelBenchmarkTest(unittest.TestCase):
    def test_json_readers_preserve_domain_errors_for_invalid_encoding(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_bytes(b"\xff")
            for load in (load_benchmark_corpus, load_model_variants):
                with self.subTest(reader=load.__name__):
                    with self.assertRaisesRegex(ModelBenchmarkError, "Unable to read"):
                        load(path)
            with patch.object(
                benchmark_module,
                "load_stable_generation_queue",
                return_value=(None, "1" * 64),
            ):
                with self.assertRaisesRegex(ModelBenchmarkError, "Unable to capture"):
                    benchmark_module._capture_failure_corpus_inputs(
                        path,
                        path,
                        lambda *_args: self.fail("Invalid state reached loader"),
                    )

    @staticmethod
    def _write_strict_corpus(root):
        corpus = root / "corpus.json"
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
                            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return corpus

    def test_corpus_versions_are_exact_integers_before_backend_startup(self):
        for version in (True, 1.0, "1", [], {}):
            with self.subTest(version=version), TemporaryDirectory() as directory:
                root = Path(directory)
                corpus = self._write_strict_corpus(root)
                document = json.loads(corpus.read_text())
                document["schema_version"] = version
                corpus.write_text(json.dumps(document), encoding="utf-8")
                calls = []
                with self.assertRaisesRegex(ModelBenchmarkError, "schema"):
                    load_benchmark_corpus(corpus)
                with self.assertRaisesRegex(ModelBenchmarkError, "schema"):
                    benchmark_model_variants(
                        corpus,
                        (ModelVariant("fake", "fake"),),
                        CharacterVoiceRegistry(),
                        root / "output",
                        backend_factory=lambda *args, **kwargs: calls.append(args),
                    )
                self.assertEqual(calls, [])
                self.assertFalse((root / "output").exists())

    def test_model_variant_profile_is_text_with_compatible_default(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            for value in (None, "", "expressive", False, 2, [], {}):
                with self.subTest(value=value):
                    path.write_text(
                        json.dumps(
                            [
                                {
                                    "model_id": "fake",
                                    "backend": "fake",
                                    "generation_profile": value,
                                }
                            ]
                        ),
                        encoding="utf-8",
                    )
                    if value is not None and not isinstance(value, str):
                        with self.assertRaisesRegex(
                            ModelBenchmarkError, "generation_profile"
                        ):
                            load_model_variants(path)
                    else:
                        self.assertEqual(
                            load_model_variants(path)[0].generation_profile,
                            value or "stable",
                        )

    def test_comparison_voice_projection_uses_captured_data_and_original_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            manifest = root / "manifest.json"
            document = {
                "version": 2,
                "voices": [
                    {
                        "character": "Voice",
                        "speaker": "captured",
                        "references": ["reference.wav"],
                    }
                ],
            }
            manifest.write_text(json.dumps(document), encoding="utf-8")
            payload = manifest.read_bytes()
            original_read = Path.read_bytes

            def replace_after_read(path):
                captured = original_read(path)
                if path == manifest:
                    changed = json.loads(captured)
                    changed["voices"][0]["speaker"] = "unreviewed"
                    manifest.write_text(json.dumps(changed), encoding="utf-8")
                return captured

            with patch.object(Path, "read_bytes", replace_after_read):
                context = _comparison_voice_context(manifest, None)

            self.assertEqual(context.sha256, hashlib.sha256(payload).hexdigest())
            voice = context.registry.resolve("Voice")
            self.assertEqual(voice.speaker, "captured")
            self.assertEqual(voice.reference, root / "reference.wav")

    def test_failure_corpus_projects_the_captured_queue_with_custom_loader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = write_legacy_fixture(root / "legacy")
            queue = fixture["queue"].resolve()
            original_payload = queue.read_bytes()
            state = {
                "items": {
                    fixture["queue_id"]: {"status": "failed", "provider": "moss-tts"}
                }
            }
            fixture["state"].write_text(json.dumps(state), encoding="utf-8")
            original_read = Path.read_bytes

            def replace_after_read(path):
                captured = original_read(path)
                if path == queue:
                    records = [json.loads(line) for line in captured.splitlines()]
                    records[-1]["voice_character"] = "Unreviewed voice"
                    queue.write_text(
                        "".join(json.dumps(record) + "\n" for record in records),
                        encoding="utf-8",
                    )
                return captured

            def restore_queue(_state_path, _queue_path):
                self.assertEqual(Path(_queue_path), queue)
                queue.write_bytes(original_payload)
                return state

            with patch.object(Path, "read_bytes", replace_after_read):
                corpus = build_failure_comparison_corpus(
                    queue,
                    fixture["state"],
                    root / "corpus.json",
                    state_loader=restore_queue,
                )
            self.assertEqual(corpus["samples"][0]["character"], "Rhiannon")
            self.assertEqual(
                corpus["source_queue_sha256"],
                hashlib.sha256(original_payload).hexdigest(),
            )

    def test_failure_corpus_default_loader_retains_current_and_legacy_state_validation(
        self,
    ):
        for schema in (
            "vntts.authoring-generation-state",
            "r1999.bulk-generation-state",
        ):
            with self.subTest(schema=schema), TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = write_legacy_fixture(root / "legacy")
                state = json.loads(fixture["state"].read_text(encoding="utf-8"))
                state["schema"] = schema
                state["items"] = {
                    fixture["queue_id"]: {
                        "status": "failed",
                        "attempts": 1,
                        "provider": "moss-tts",
                    }
                }
                fixture["state"].write_text(json.dumps(state), encoding="utf-8")
                corpus = build_failure_comparison_corpus(
                    fixture["queue"], fixture["state"], root / "corpus.json"
                )
                self.assertEqual(corpus["samples"][0]["character"], "Rhiannon")
                self.assertEqual(
                    corpus["source_state_sha256"],
                    hashlib.sha256(fixture["state"].read_bytes()).hexdigest(),
                )

                state["queue_sha256"] = "0" * 64
                fixture["state"].write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(BulkGenerationError, "queue changed"):
                    build_failure_comparison_corpus(
                        fixture["queue"], fixture["state"], root / "invalid.json"
                    )
                self.assertFalse((root / "invalid.json").exists())

    def test_failure_corpus_supports_manifest_without_selected_variants(self):
        variants = (
            None,
            {},
            {"selected_variants": None},
            {"selected_variants": []},
            [],
            "bad",
            {"selected_variants": "bad"},
            {"selected_variants": {}},
        )
        for bindings in variants:
            with self.subTest(bindings=bindings), TemporaryDirectory() as directory:
                root = Path(directory)
                text = "Exact failure line."
                queue_id = "line:one:" + hashlib.sha256(text.encode()).hexdigest()[:16]
                queue = root / "queue.jsonl"
                write_voice_generation_queue(
                    queue,
                    {"game": "Fixture", "language": "en"},
                    [
                        {
                            "record_type": "generation_item",
                            "queue_id": queue_id,
                            "line_id": "line:one",
                            "text": text,
                            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                            "speaker": "Voice",
                            "voice_character": "Voice",
                            "action": "generate",
                            "state": "pending",
                        }
                    ],
                )
                state = {
                    "items": {queue_id: {"status": "failed", "provider": "moss-tts"}}
                }
                state_path = root / "state.json"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                reference = root / "reference.wav"
                reference.write_bytes(b"fixture reference")
                manifest = root / "manifest.json"
                document = {
                    "version": 2,
                    "voices": [
                        {
                            "character": "Voice",
                            "speaker": "voice",
                            "references": ["reference.wav"],
                        }
                    ],
                }
                if bindings is not None:
                    document["vntts.authoring.source_reference_bindings"] = bindings
                manifest.write_text(json.dumps(document), encoding="utf-8")
                output = root / "failure-corpus.json"

                def create():
                    return build_failure_comparison_corpus(
                        queue,
                        state_path,
                        output,
                        manifest_path=manifest,
                        state_loader=lambda *_: state,
                    )

                if not isinstance(bindings, (dict, type(None))) or (
                    isinstance(bindings, dict)
                    and bindings.get("selected_variants") is not None
                    and not isinstance(bindings["selected_variants"], list)
                ):
                    with self.assertRaisesRegex(ModelBenchmarkError, "bindings"):
                        create()
                    self.assertFalse(output.exists())
                else:
                    result = create()
                    self.assertEqual(result["samples"][0]["character"], "Voice")
                    self.assertEqual(
                        result["source_voice_manifest_sha256"],
                        hashlib.sha256(manifest.read_bytes()).hexdigest(),
                    )
                    self.assertEqual(json.loads(output.read_text()), result)

    def test_model_publications_preserve_destination_created_after_precheck(self):
        for multiple in (False, True):
            with self.subTest(multiple=multiple), TemporaryDirectory() as directory:
                root = Path(directory)
                output = (root / "output").resolve()
                corpus = self._write_strict_corpus(root)
                backend = ShutdownRenderBackend()
                original_rename = os.rename
                calls = []

                def publish(source, destination):
                    destination = Path(destination)
                    if destination == output:
                        destination.mkdir()
                        calls.append(destination.stat().st_ino)
                    return rename_directory_no_replace(source, destination)

                # Also wrap the old primitive so this gate can reproduce the old race.
                def old_publish(source, destination, *args, **kwargs):
                    destination = Path(destination)
                    if destination == output:
                        destination.mkdir()
                        calls.append(destination.stat().st_ino)
                    return original_rename(source, destination, *args, **kwargs)

                with (
                    patch.object(
                        benchmark_module,
                        "rename_directory_no_replace",
                        side_effect=publish,
                    ),
                    patch("os.rename", side_effect=old_publish),
                    self.assertRaisesRegex(ModelBenchmarkError, "publish"),
                ):
                    if multiple:
                        benchmark_model_variants(
                            corpus,
                            (ModelVariant("fake", "fake"),),
                            CharacterVoiceRegistry(),
                            output,
                            backend_factory=lambda *_args, **_kw: backend,
                        )
                    else:
                        benchmark_renderer(
                            ModelVariant("fake", "fake"),
                            backend,
                            [{"id": "one", "character": "Voice", "text": "Exact."}],
                            output,
                        )
                self.assertEqual(calls, [output.stat().st_ino])
                self.assertEqual(list(output.iterdir()), [])
                self.assertEqual(list(root.glob(".output-*")), [])
                self.assertEqual(backend.shutdown_calls, 1 if multiple else 0)

    def test_mixed_render_outcomes_keep_order_groups_and_exact_sample_records(self):
        class MixedBackend(FakeRenderBackend):
            def render(self, request):
                if request.text == "error":
                    self.requests.append(request)
                    raise RuntimeError("fixture render error")
                self.completion = {
                    "complete": SynthesisCompletion.COMPLETE,
                    "limited": SynthesisCompletion.LIMITED,
                    "cancelled": SynthesisCompletion.CANCELLED,
                }[request.text]
                return super().render(request)

        with TemporaryDirectory() as directory:
            backend = MixedBackend()
            samples = [
                {
                    "id": str(index),
                    "character": "Voice",
                    "text": outcome,
                    "comparison_group": "first" if index < 2 else "second",
                }
                for index, outcome in enumerate(
                    ("complete", "error", "limited", "cancelled")
                )
            ]
            report = benchmark_renderer(
                ModelVariant("fake", "fake"), backend, samples, directory, seed=8
            )
            self.assertEqual(
                [sample["outcome"] for sample in report["samples"]],
                ["complete", "error", "limited", "cancelled"],
            )
            self.assertEqual(
                report["summary"],
                {"total": 4, "complete": 1, "error": 1, "limited": 1, "cancelled": 1},
            )
            self.assertEqual(
                report["group_summary"]["first"],
                {"total": 2, "complete": 1, "error": 1, "limited": 0, "cancelled": 0},
            )
            self.assertEqual(
                report["group_summary"]["second"],
                {"total": 2, "complete": 0, "error": 0, "limited": 1, "cancelled": 1},
            )
            self.assertEqual(len(list((Path(directory) / "audio").glob("*.wav"))), 1)
            for expected, actual in zip(samples, report["samples"], strict=True):
                self.assertEqual({key: actual[key] for key in expected}, expected)
            self.assertEqual(
                json.loads((Path(directory) / "report.json").read_text()), report
            )
            self.assertEqual(backend.play_calls, 0)

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
            root = Path(directory).resolve()
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
                        reference_root=root.resolve(),
                        source_character="Original identity",
                        reference_transcript="Exact reference line.",
                    )
                ]
            )
            received_references = []

            def factory(name, received_registry, cache, **options):
                del cache, options
                voice = received_registry.resolve("Rhiannon")
                captured_reference = voice.references[0]
                self.assertEqual(voice.reference, captured_reference)
                self.assertEqual(voice.reference_root, captured_reference.parent.parent)
                self.assertEqual(voice.source_character, "Original identity")
                self.assertEqual(voice.reference_transcript, "Exact reference line.")
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

    def test_benchmark_shutdowns_each_backend_before_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = self._write_strict_corpus(root)
            backend = ShutdownRenderBackend()

            output = root / "output"
            benchmark_model_variants(
                corpus,
                (ModelVariant("managed", "fake"),),
                CharacterVoiceRegistry(),
                output,
                backend_factory=lambda *_arguments, **_keywords: backend,
            )

            self.assertEqual(backend.shutdown_calls, 1)
            self.assertEqual(backend.stop_calls, 0)
            self.assertTrue(output.is_dir())

    def test_benchmark_shutdown_failure_blocks_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = self._write_strict_corpus(root)
            backend = ShutdownRenderBackend(shutdown_error=RuntimeError("cleanup"))

            output = root / "output"
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                benchmark_model_variants(
                    corpus,
                    (ModelVariant("managed", "fake"),),
                    CharacterVoiceRegistry(),
                    output,
                    backend_factory=lambda *_arguments, **_keywords: backend,
                )

            self.assertEqual(backend.shutdown_calls, 1)
            self.assertFalse(output.exists())

    def test_benchmark_preserves_renderer_error_when_shutdown_also_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = self._write_strict_corpus(root)
            backend = ShutdownRenderBackend(
                backend_name="different",
                shutdown_error=RuntimeError("cleanup"),
            )

            output = root / "output"
            with self.assertRaisesRegex(ModelBenchmarkError, "different request"):
                benchmark_model_variants(
                    corpus,
                    (ModelVariant("managed", "fake"),),
                    CharacterVoiceRegistry(),
                    output,
                    backend_factory=lambda *_arguments, **_keywords: backend,
                )

            self.assertEqual(backend.shutdown_calls, 1)
            self.assertFalse(output.exists())

    def test_benchmark_preserves_primary_when_cache_cleanup_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = self._write_strict_corpus(root)
            cleanup_error = OSError("benchmark cache cleanup failed")
            real_temporary_directory = tempfile.TemporaryDirectory
            backend = FakeRenderBackend(backend_name="different")

            def temporary_directory(*args, **kwargs):
                cache = real_temporary_directory(*args, **kwargs)
                if kwargs.get("prefix") is None:
                    cleanup = cache.cleanup

                    def failing_cleanup():
                        cleanup()
                        raise cleanup_error

                    cache.cleanup = failing_cleanup
                return cache

            with (
                patch(
                    "vntts.cleanup.TemporaryDirectory", side_effect=temporary_directory
                ),
                self.assertRaisesRegex(
                    ModelBenchmarkError, "different request"
                ) as caught,
            ):
                benchmark_model_variants(
                    corpus,
                    (ModelVariant("managed", "fake"),),
                    CharacterVoiceRegistry(),
                    root / "output",
                    backend_factory=lambda *_arguments, **_keywords: backend,
                )

            self.assertIn(
                "benchmark cache cleanup failed", " ".join(caught.exception.__notes__)
            )
            self.assertFalse((root / "output").exists())

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
