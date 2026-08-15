import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from vntts.speech_backend import SpeechBackendCapabilities
from vntts.speech_backend_runtime import BoundedCache
from vntts.tts_benchmark import benchmark_backend, main, write_report, write_wav
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
        self.assertEqual(sample["duration_seconds"], 1.0)
        self.assertEqual(sample["first_audio_ms"], 125.0)

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


if __name__ == "__main__":
    unittest.main()
