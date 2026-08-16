import hashlib
import json
import unittest
import wave
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from vntts.speech_backend import SpeechBackendCapabilities
from vntts.speech_backend_runtime import BoundedCache
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.tts_benchmark import (
    benchmark_backend,
    load_tts_benchmark_corpus,
    main,
    write_report,
    write_wav,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FakeBackend:
    capabilities = SpeechBackendCapabilities(True, False, True)
    sample_rate = 4

    def __init__(self):
        self.primed = []
        self.stop_calls = 0

    def prime(self, character):
        self.primed.append(character)

    def prepare(self, character, text):
        del character, text
        return np.array([0.0, 0.5, -0.5, 0.0], dtype=np.float32)

    def stop(self):
        self.stop_calls += 1
        return False


class FakeStreamingBackend(FakeBackend):
    capabilities = SpeechBackendCapabilities(True, True, True)

    def __init__(self):
        super().__init__()
        self.audio_cache = BoundedCache(1)
        self.last_first_audio_ms = 125.0

    def prepare(self, character, text):
        return character, text

    def play(self, prepared):
        character, text = prepared
        self.audio_cache.put(
            (character.casefold(), " ".join(text.split())),
            np.array([0.0, 0.5, -0.5, 0.0], dtype=np.float32),
        )
        return True


class FakeRenderingBackend(FakeBackend):
    capabilities = SpeechBackendCapabilities(True, True, True)
    generation_profile = "stable"

    def __init__(
        self,
        *,
        completions=(SynthesisCompletion.COMPLETE,),
        result_sample_rate=None,
        diagnostics_profile=None,
    ):
        super().__init__()
        self.render_requests = []
        self.completions = tuple(completions)
        self.result_sample_rate = result_sample_rate or self.sample_rate
        self.diagnostics_profile = diagnostics_profile

    def render(self, request):
        self.render_requests.append(request)
        call_index = len(self.render_requests) - 1
        cache_source = (
            "fresh-generation" if len(self.render_requests) == 1 else "memory-cache"
        )
        completion = self.completions[min(call_index, len(self.completions) - 1)]
        audio = np.array([[0.0], [0.5], [-0.5], [0.0]], dtype=np.float32)

        def produce():
            yield SynthesisChunk(audio, self.sample_rate, 0, 25.0)
            return SynthesisResult(
                pcm=audio,
                sample_rate=self.result_sample_rate,
                completion=completion,
                limits=SynthesisLimits(256, 3.0),
                timing=SynthesisTiming(25.0, 50.0),
                diagnostics=SynthesisDiagnostics(
                    backend="fake",
                    cache_source=cache_source,
                    generation_profile=(
                        self.diagnostics_profile or request.generation_profile
                    ),
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=4,
                ),
            )

        return SynthesisChunkStream(produce())

    def play(self, _prepared):
        raise AssertionError("benchmark must not open playback for renderable backends")


class TTSBenchmarkTest(unittest.TestCase):
    def test_cli_reports_missing_manifest_to_stderr(self):
        errors = StringIO()
        with (
            patch("vntts.tts_benchmark.find_default_voice_manifest", return_value=None),
            redirect_stderr(errors),
        ):
            exit_code = main(["--backend", "pocket-tts"])

        self.assertEqual(exit_code, 1)
        self.assertIn("No complete voice manifest", errors.getvalue())

    def test_reads_streamed_audio_through_bounded_cache_interface(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        backend = FakeStreamingBackend()

        def create_fake_backend(name, registry, cache):
            del name, registry, cache
            return backend

        with TemporaryDirectory() as temporary_directory:
            report = benchmark_backend(
                "fake",
                registry,
                ["Kamuta"],
                "A line.",
                temporary_directory,
                backend_factory=create_fake_backend,
            )

        sample = report["samples"][0]
        self.assertEqual(report["schema"], "vntts.tts-benchmark-report")
        self.assertEqual(report["model_id"], "fake")
        self.assertEqual(sample["duration_seconds"], 1.0)
        self.assertEqual(sample["first_audio_ms"], 125.0)
        self.assertEqual(sample["line_id"], "Kamuta")
        self.assertRegex(sample["text_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(sample["audio_sha256"], r"^[0-9a-f]{64}$")

    def test_benchmark_uses_device_independent_rendering_when_available(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        backend = FakeRenderingBackend()

        with TemporaryDirectory() as temporary_directory:
            report = benchmark_backend(
                "fake",
                registry,
                ["Kamuta"],
                "A line.",
                temporary_directory,
                backend_factory=lambda _name, _registry, _cache: backend,
            )

        sample = report["samples"][0]
        self.assertEqual(len(backend.render_requests), 2)
        self.assertEqual(backend.render_requests[0].voice, "Kamuta")
        self.assertEqual(sample["fresh"]["cache_source"], "fresh-generation")
        self.assertEqual(sample["memory_cache"]["cache_source"], "memory-cache")
        self.assertEqual(sample["first_audio_ms"], 25.0)

    def test_rejects_incomplete_or_mismatched_render_before_publishing_wav(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        cases = (
            FakeRenderingBackend(completions=(SynthesisCompletion.LIMITED,)),
            FakeRenderingBackend(
                completions=(
                    SynthesisCompletion.COMPLETE,
                    SynthesisCompletion.CANCELLED,
                )
            ),
            FakeRenderingBackend(diagnostics_profile="different"),
        )
        for backend in cases:
            with self.subTest(backend=backend), TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    benchmark_backend(
                        "fake",
                        registry,
                        ["Kamuta"],
                        "A line.",
                        directory,
                        backend_factory=lambda _name, _registry, _cache: backend,
                    )
                self.assertEqual(list(Path(directory).glob("*.wav")), [])
                self.assertEqual(backend.stop_calls, 1)

    def test_late_sample_failure_publishes_nothing_and_stops_backend(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        backend = FakeRenderingBackend(
            completions=(
                SynthesisCompletion.COMPLETE,
                SynthesisCompletion.COMPLETE,
                SynthesisCompletion.LIMITED,
            )
        )
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            sentinel = output / "existing.txt"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "limited"):
                benchmark_backend(
                    "fake",
                    registry,
                    [],
                    "unused",
                    output,
                    benchmark_samples=[
                        {"id": "one", "character": "Kamuta", "text": "One"},
                        {"id": "two", "character": "Kamuta", "text": "Two"},
                    ],
                    backend_factory=lambda _name, _registry, _cache: backend,
                )

            self.assertEqual(list(output.glob("*.wav")), [])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertEqual(backend.stop_calls, 1)

    def test_uses_typed_render_sample_rate_for_published_wav(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        backend = FakeRenderingBackend(result_sample_rate=8)
        with TemporaryDirectory() as directory:
            report = benchmark_backend(
                "fake",
                registry,
                ["Kamuta"],
                "A line.",
                directory,
                backend_factory=lambda _name, _registry, _cache: backend,
            )
            with wave.open(report["samples"][0]["audio"], "rb") as stream:
                self.assertEqual(stream.getframerate(), 8)
        self.assertEqual(report["samples"][0]["duration_seconds"], 0.5)

    def test_records_cold_generation_cache_and_audio(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        backend = FakeBackend()
        wall_times = iter([0.0, 0.1, 1.0, 1.2, 2.0, 2.4, 3.0, 3.01, 4.01])
        cpu_times = iter([0.0, 0.05, 1.0, 1.1])

        def create_fake_backend(name, registry, cache):
            del name, registry, cache
            return backend

        with TemporaryDirectory() as temporary_directory:
            report = benchmark_backend(
                "fake",
                registry,
                ["Kamuta"],
                "A line.",
                temporary_directory,
                backend_factory=create_fake_backend,
                clock=lambda: next(wall_times),
                cpu_clock=lambda: next(cpu_times),
            )

            sample = report["samples"][0]
            self.assertTrue(Path(sample["audio"]).is_file())
            self.assertEqual(sample["duration_seconds"], 1.0)
            self.assertAlmostEqual(sample["first_audio_ms"], 400.0)
            self.assertAlmostEqual(sample["realtime_factor"], 1.0)
            self.assertEqual(backend.primed, ["Kamuta"])

    def test_writes_valid_wave_and_json_report(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audio = write_wav(root / "sample.wav", [0.0, 0.5], 24_000)
            report = write_report({"backend": "fake"}, root)

            self.assertTrue(audio.is_file())
            self.assertIn('"backend": "fake"', report.read_text(encoding="utf-8"))

    def test_loads_versioned_per_line_corpus(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "corpus.json"
            path.write_text(
                '{"schema_version": 1, "name": "Rhiannon", "samples": ['
                '{"id": "short", "character": "Rhiannon", '
                '"text": "I,  erhm ..."}]}',
                encoding="utf-8",
            )

            corpus = load_tts_benchmark_corpus(path)

        self.assertEqual(corpus["name"], "Rhiannon")
        self.assertEqual(
            corpus["samples"],
            [
                {
                    "id": "short",
                    "line_id": "short",
                    "character": "Rhiannon",
                    "text": "I, erhm ...",
                    "text_sha256": hashlib.sha256(b"I, erhm ...").hexdigest(),
                }
            ],
        )

    def test_strict_corpus_preserves_exact_text_and_identity(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            text = "I,  erhm ...\nStill exact."
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            path.write_text(
                json.dumps(
                    {
                        "schema": "vntts.tts-benchmark-corpus",
                        "schema_version": 1,
                        "samples": [
                            {
                                "id": "queue:1",
                                "line_id": "line:1",
                                "character": "Rhiannon",
                                "text": text,
                                "text_sha256": digest,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            corpus = load_tts_benchmark_corpus(path)

        self.assertEqual(corpus["samples"][0]["text"], text)
        self.assertEqual(corpus["samples"][0]["line_id"], "line:1")
        self.assertEqual(corpus["samples"][0]["text_sha256"], digest)

    def test_rejects_conflicting_schema_identity_and_duplicate_ids(self):
        documents = (
            {
                "schema": "unrelated.corpus",
                "schema_version": 1,
                "samples": [{"id": "one", "text": "Text"}],
            },
            {
                "schema_version": 1,
                "samples": [{"id": "one", "line_id": "line", "text": "Text"}],
            },
            {
                "schema_version": 1,
                "samples": [
                    {"id": "one", "text": "First"},
                    {"id": "one", "text": "Second"},
                ],
            },
        )
        for document in documents:
            with self.subTest(document=document), TemporaryDirectory() as directory:
                path = Path(directory) / "corpus.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_tts_benchmark_corpus(path)

    def test_strict_corpus_rejects_coerced_or_blank_identity_fields(self):
        text = "Text"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        valid = {
            "id": "one",
            "line_id": "line",
            "character": "Rhiannon",
            "text": text,
            "text_sha256": digest,
        }
        for field, invalid in (
            ("id", 1),
            ("line_id", 2),
            ("character", 3),
            ("text", 4),
            ("id", "  "),
        ):
            sample = {**valid, field: invalid}
            if field == "text" and isinstance(invalid, str):
                sample["text_sha256"] = hashlib.sha256(invalid.encode()).hexdigest()
            document = {
                "schema": "vntts.tts-benchmark-corpus",
                "schema_version": 1,
                "samples": [sample],
            }
            with (
                self.subTest(field=field, invalid=invalid),
                TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "corpus.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, field):
                    load_tts_benchmark_corpus(path)

    def test_output_names_are_contained_and_collisions_rejected(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("../Kamuta", "kamuta", references=(Path("voice.wav"),))]
        )
        backend = FakeRenderingBackend()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = benchmark_backend(
                "../escaped",
                registry,
                [],
                "unused",
                root,
                benchmark_samples=[
                    {"id": "../sample", "character": "../Kamuta", "text": "Text"}
                ],
                backend_factory=lambda _name, _registry, _cache: backend,
            )
            audio = Path(report["samples"][0]["audio"])
            self.assertEqual(audio.parent, root.resolve())
            report_path = write_report(report, root)
            self.assertEqual(report_path.parent, root.resolve())
            self.assertFalse((root.parent / "escaped.json").exists())

            with self.assertRaisesRegex(ValueError, "collide"):
                benchmark_backend(
                    "fake",
                    registry,
                    [],
                    "unused",
                    root,
                    benchmark_samples=[
                        {"id": "A/B", "character": "../Kamuta", "text": "One"},
                        {"id": "A B", "character": "../Kamuta", "text": "Two"},
                    ],
                    backend_factory=lambda _name, _registry, _cache: backend,
                )

    def test_benchmarks_each_corpus_line_with_cache_stage_fields(self):
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Rhiannon", "rhiannon", references=(Path("voice.wav"),))]
        )
        backend = FakeStreamingBackend()

        with TemporaryDirectory() as temporary_directory:
            report = benchmark_backend(
                "fake",
                registry,
                [],
                "unused",
                temporary_directory,
                benchmark_samples=[
                    {
                        "id": "short-line",
                        "character": "Rhiannon",
                        "text": "I, erhm ...",
                    }
                ],
                corpus_name="Rhiannon regression",
                backend_factory=lambda _name, _registry, _cache: backend,
            )

        sample = report["samples"][0]
        self.assertEqual(report["corpus"], "Rhiannon regression")
        self.assertEqual(sample["id"], "short-line")
        self.assertEqual(sample["fresh"]["first_pcm_ms"], 125.0)
        self.assertEqual(sample["memory_cache"]["first_pcm_ms"], 125.0)
        self.assertIsNone(sample["persistent_cache"]["wall_ms"])


if __name__ == "__main__":
    unittest.main()
