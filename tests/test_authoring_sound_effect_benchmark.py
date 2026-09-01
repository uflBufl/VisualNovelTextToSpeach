import hashlib
import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from vntts.authoring.sound_effect_benchmark import (
    SoundEffectBenchmarkError,
    benchmark_sound_effects,
    load_sound_effect_corpus,
)
from vntts.cuda_probe import CudaProbeError


class FakeCuda:
    def __init__(self, available=True, bf16=True):
        self.available = available
        self.bf16 = bf16
        self.reset_calls = 0
        self.sync_calls = 0

    def is_available(self):
        return self.available

    def current_device(self):
        return 0

    def get_device_properties(self, _index):
        return type("Properties", (), {"name": "Fixture CUDA"})()

    def mem_get_info(self, _index):
        return 8_000, 16_000

    def get_device_capability(self, _index):
        return 8, 0

    def is_bf16_supported(self):
        return self.bf16

    def reset_peak_memory_stats(self):
        self.reset_calls += 1

    def max_memory_allocated(self):
        return 4_000

    def synchronize(self):
        self.sync_calls += 1


class FakeTorch:
    __version__ = "2.9.0+cu128"
    bfloat16 = "bfloat16"

    def __init__(self, available=True, bf16=True):
        self.cuda = FakeCuda(available, bf16)
        self.version = type("Version", (), {"cuda": "12.8"})()
        cudnn = type("Cudnn", (), {"version": staticmethod(lambda: 91002)})()
        self.backends = type("Backends", (), {"cudnn": cudnn})()


class FakePipeline:
    sample_rate = 8_000

    def __init__(self):
        self.calls = []

    def __call__(self, **values):
        self.calls.append(values)
        count = round(values["seconds"] * self.sample_rate)
        return np.full((1, 1, count), 0.25, dtype=np.float32)


def write_corpus(path, *, samples=None):
    document = {
        "schema": "vntts.sound-effect-benchmark-corpus",
        "schema_version": 1,
        "name": "Fixture events",
        "samples": samples
        or [
            {
                "id": "gasp",
                "kind": "human-gasp",
                "prompt": "One isolated gasp, no speech.",
                "seconds": 1.5,
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")


class SoundEffectBenchmarkTest(unittest.TestCase):
    def test_publishes_exact_multiseed_report_and_pcm(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus.json"
            output = root / "output"
            write_corpus(corpus)
            corpus_sha256 = hashlib.sha256(corpus.read_bytes()).hexdigest()
            torch = FakeTorch()
            pipeline = FakePipeline()
            loader_calls = []

            def load_pipeline(*args, **kwargs):
                loader_calls.append((args, kwargs))
                return pipeline

            ticks = iter((10.0, 11.5, 20.0, 22.0))
            report = benchmark_sound_effects(
                corpus,
                output,
                model="fixture/model",
                model_revision="a" * 40,
                seeds=(3, 7),
                torch_module=torch,
                pipeline_factory=load_pipeline,
                clock=lambda: next(ticks),
            )

            saved = json.loads((output / "report.json").read_text())
            wavs = sorted((output / "audio").glob("*.wav"))
            with wave.open(str(wavs[0]), "rb") as audio:
                wav_shape = (
                    audio.getnchannels(),
                    audio.getframerate(),
                    audio.getnframes(),
                )

        self.assertEqual(report, saved)
        self.assertEqual(report["corpus_sha256"], corpus_sha256)
        self.assertEqual(report["controls"]["seeds"], [3, 7])
        self.assertEqual(report["cuda"]["device_name"], "Fixture CUDA")
        self.assertFalse(report["speaker_identity_claim"])
        self.assertTrue(report["manual_review_required"])
        self.assertEqual(len(report["samples"]), 2)
        self.assertEqual(report["samples"][0]["render_seconds"], 1.5)
        self.assertEqual(report["samples"][1]["render_seconds"], 2.0)
        self.assertEqual(
            report["samples"][0]["human_review"],
            {
                "adherence": "pending",
                "unwanted_speech": "pending",
                "artifacts": "pending",
            },
        )
        self.assertEqual(wav_shape, (1, 8_000, 12_000))
        self.assertEqual(len(wavs), 2)
        self.assertEqual(torch.cuda.reset_calls, 2)
        self.assertEqual(torch.cuda.sync_calls, 2)
        self.assertEqual(
            loader_calls,
            [
                (
                    ("fixture/model",),
                    {
                        "revision": "a" * 40,
                        "torch_dtype": "bfloat16",
                        "device": "cuda",
                    },
                )
            ],
        )

    def test_cpu_host_fails_before_pipeline_load_or_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus.json"
            output = root / "output"
            write_corpus(corpus)
            called = []

            with self.assertRaises(CudaProbeError):
                benchmark_sound_effects(
                    corpus,
                    output,
                    torch_module=FakeTorch(available=False),
                    pipeline_factory=lambda *args, **kwargs: called.append(
                        (args, kwargs)
                    ),
                )

            self.assertEqual(called, [])
            self.assertFalse(output.exists())

    def test_floating_model_revision_fails_before_cuda_or_pipeline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus.json"
            write_corpus(corpus)

            with self.assertRaisesRegex(SoundEffectBenchmarkError, "exact commit"):
                benchmark_sound_effects(
                    corpus,
                    root / "output",
                    model_revision="main",
                    torch_module=FakeTorch(),
                    pipeline_factory=lambda *args, **kwargs: FakePipeline(),
                )

    def test_unsupported_bf16_fails_before_pipeline_load(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus.json"
            output = root / "output"
            write_corpus(corpus)
            called = []

            with self.assertRaisesRegex(SoundEffectBenchmarkError, "BF16"):
                benchmark_sound_effects(
                    corpus,
                    output,
                    torch_module=FakeTorch(bf16=False),
                    pipeline_factory=lambda *args, **kwargs: called.append(
                        (args, kwargs)
                    ),
                )

            self.assertEqual(called, [])
            self.assertFalse(output.exists())

    def test_failed_generation_publishes_no_partial_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus.json"
            output = root / "output"
            write_corpus(corpus)

            class FailingPipeline(FakePipeline):
                def __call__(self, **values):
                    raise RuntimeError("CUDA kernel failed")

            with self.assertRaisesRegex(SoundEffectBenchmarkError, "kernel failed"):
                benchmark_sound_effects(
                    corpus,
                    output,
                    seeds=(0,),
                    torch_module=FakeTorch(),
                    pipeline_factory=lambda *args, **kwargs: FailingPipeline(),
                )

            self.assertFalse(output.exists())

    def test_rejects_duplicate_and_unsafe_sample_ids(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            for samples, message in (
                (
                    [
                        {"id": "same", "kind": "a", "prompt": "a", "seconds": 1},
                        {"id": "same", "kind": "b", "prompt": "b", "seconds": 1},
                    ],
                    "IDs must be unique",
                ),
                (
                    [{"id": "../escape", "kind": "a", "prompt": "a", "seconds": 1}],
                    "ID is invalid",
                ),
            ):
                with self.subTest(message=message):
                    write_corpus(path, samples=samples)
                    with self.assertRaisesRegex(SoundEffectBenchmarkError, message):
                        load_sound_effect_corpus(path)


if __name__ == "__main__":
    unittest.main()
