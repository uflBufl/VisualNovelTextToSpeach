import unittest
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

    def prime(self, character):
        self.primed.append(character)

    def prepare(self, character, text):
        del character, text
        return np.array([0.0, 0.5, -0.5, 0.0], dtype=np.float32)

    def stop(self):
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

    def __init__(self):
        super().__init__()
        self.render_requests = []

    def render(self, request):
        self.render_requests.append(request)
        cache_source = (
            "fresh-generation" if len(self.render_requests) == 1 else "memory-cache"
        )
        audio = np.array([[0.0], [0.5], [-0.5], [0.0]], dtype=np.float32)

        def produce():
            yield SynthesisChunk(audio, self.sample_rate, 0, 25.0)
            return SynthesisResult(
                pcm=audio,
                sample_rate=self.sample_rate,
                completion=SynthesisCompletion.COMPLETE,
                limits=SynthesisLimits(256, 3.0),
                timing=SynthesisTiming(25.0, 50.0),
                diagnostics=SynthesisDiagnostics(
                    backend="fake",
                    cache_source=cache_source,
                    generation_profile=request.generation_profile,
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
            [{"id": "short", "character": "Rhiannon", "text": "I, erhm ..."}],
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
