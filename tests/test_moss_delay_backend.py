import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from vntts.moss_delay_backend import MossTTSDelayVoiceRouterBackend
from vntts.speech_backend import TTSConfigurationError
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FakeTensor:
    def __init__(self, shape):
        self.shape = shape

    def to(self, _device):
        return self


class FakeCuda:
    def __init__(self, available=False, bf16=True):
        self.available = available
        self.bf16 = bf16
        self.seeds = []

    def is_available(self):
        return self.available

    def manual_seed_all(self, seed):
        self.seeds.append(seed)

    def is_bf16_supported(self):
        return self.bf16


class FakeTorch:
    bfloat16 = "bfloat16"
    float32 = "float32"

    def __init__(self, cuda=False, bf16=True):
        self.cuda = FakeCuda(cuda, bf16)
        self.seeds = []

    def manual_seed(self, seed):
        self.seeds.append(seed)

    def no_grad(self):
        return nullcontext()


class FakeAudioTokenizer:
    def __init__(self):
        self.device = None

    def to(self, device):
        self.device = device
        return self


class FakeMessage:
    def __init__(self, audio):
        self.audio_codes_list = [audio]


class FakeProcessor:
    def __init__(self, generated_tokens=10):
        self.audio_tokenizer = FakeAudioTokenizer()
        self.model_config = type("Config", (), {"sampling_rate": 24_000})()
        self.generated_tokens = generated_tokens
        self.messages = []
        self.batches = []

    def build_user_message(self, **values):
        self.messages.append(values)
        return values

    def __call__(self, conversations, *, mode):
        self.batches.append((conversations, mode))
        return {
            "input_ids": FakeTensor((1, 20)),
            "attention_mask": FakeTensor((1, 20)),
        }

    def decode(self, _outputs):
        return [FakeMessage(np.array([0.0, 0.25, -0.25], dtype=np.float32))]


class FakeModel:
    def __init__(self, generated_tokens=10):
        self.generated_tokens = generated_tokens
        self.calls = []

    def generate(self, **values):
        self.calls.append(values)
        return FakeTensor((1, 20 + self.generated_tokens))


class FakeAutoLoader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def from_pretrained(self, name, **options):
        self.calls.append((name, options))
        return self.value


class LoadableFakeModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.device = None
        self.evaluated = False

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        self.evaluated = True
        return self


class MossDelayBackendTest(unittest.TestCase):
    def create_backend(self, reference, *, generated_tokens=10, cuda=False):
        processor = FakeProcessor(generated_tokens)
        model = FakeModel(generated_tokens)
        torch = FakeTorch(cuda)
        registry = CharacterVoiceRegistry(
            [
                CharacterVoice(
                    "Rhiannon",
                    "Rhiannon",
                    references=(reference,),
                )
            ]
        )
        backend = MossTTSDelayVoiceRouterBackend(
            registry,
            processor=processor,
            model=model,
            torch_module=torch,
        )
        return backend, processor, model, torch

    def test_upstream_loader_is_trusted_and_cpu_uses_float32_eager(self):
        processor = FakeProcessor()
        model = LoadableFakeModel()
        processor_loader = FakeAutoLoader(processor)
        model_loader = FakeAutoLoader(model)

        backend = MossTTSDelayVoiceRouterBackend(
            CharacterVoiceRegistry(),
            torch_module=FakeTorch(),
            auto_processor=processor_loader,
            auto_model=model_loader,
        )

        self.assertEqual(backend.device, "cpu")
        self.assertEqual(processor.audio_tokenizer.device, "cpu")
        self.assertEqual(processor_loader.calls[0][1], {"trust_remote_code": True})
        self.assertEqual(
            model_loader.calls[0][1],
            {
                "trust_remote_code": True,
                "attn_implementation": "eager",
                "torch_dtype": "float32",
            },
        )
        self.assertEqual(model.device, "cpu")
        self.assertTrue(model.evaluated)

    def test_exact_model_revision_is_forwarded_to_both_upstream_loaders(self):
        processor_loader = FakeAutoLoader(FakeProcessor())
        model_loader = FakeAutoLoader(LoadableFakeModel())
        revision = "c" * 40

        MossTTSDelayVoiceRouterBackend(
            CharacterVoiceRegistry(),
            torch_module=FakeTorch(),
            auto_processor=processor_loader,
            auto_model=model_loader,
            model_revision=revision,
        )

        self.assertEqual(processor_loader.calls[0][1]["revision"], revision)
        self.assertEqual(model_loader.calls[0][1]["revision"], revision)

    def test_required_cuda_rejects_before_model_loading(self):
        processor_loader = FakeAutoLoader(FakeProcessor())
        model_loader = FakeAutoLoader(LoadableFakeModel())

        with self.assertRaisesRegex(TTSConfigurationError, "requires CUDA"):
            MossTTSDelayVoiceRouterBackend(
                CharacterVoiceRegistry(),
                torch_module=FakeTorch(cuda=False),
                auto_processor=processor_loader,
                auto_model=model_loader,
                require_cuda=True,
            )

        self.assertEqual(processor_loader.calls, [])
        self.assertEqual(model_loader.calls, [])

    def test_required_cuda_rejects_unsupported_bf16_before_model_loading(self):
        processor_loader = FakeAutoLoader(FakeProcessor())
        model_loader = FakeAutoLoader(LoadableFakeModel())

        with self.assertRaisesRegex(TTSConfigurationError, "BF16"):
            MossTTSDelayVoiceRouterBackend(
                CharacterVoiceRegistry(),
                torch_module=FakeTorch(cuda=True, bf16=False),
                auto_processor=processor_loader,
                auto_model=model_loader,
                require_cuda=True,
            )

        self.assertEqual(processor_loader.calls, [])
        self.assertEqual(model_loader.calls, [])

    def test_renders_one_reference_with_distinct_backend_and_seed(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "rhiannon.wav"
            reference.write_bytes(b"reference")
            backend, processor, model, torch = self.create_backend(reference)
            request = SynthesisRequest(
                "Rhiannon",
                "A short comparison line.",
                seed=17,
                generation_profile="expressive",
                cache_policy=SynthesisCachePolicy.BYPASS,
            )

            result = backend.render(request).collect()

        self.assertIs(result.completion, SynthesisCompletion.COMPLETE)
        self.assertEqual(result.sample_rate, 24_000)
        self.assertEqual(result.pcm.shape, (3, 1))
        self.assertEqual(result.diagnostics.backend, "moss-tts-delay")
        self.assertEqual(result.diagnostics.seed, 17)
        self.assertEqual(torch.seeds, [17])
        self.assertEqual(processor.messages[0]["reference"], [str(reference.resolve())])
        self.assertEqual(processor.messages[0]["language"], "English")
        self.assertEqual(model.calls[0]["audio_temperature"], 1.7)
        self.assertLess(result.limits.max_tokens, 256)

    def test_marks_exact_token_cap_limited_and_does_not_claim_local_tokens(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "rhiannon.wav"
            reference.write_bytes(b"reference")
            backend, _processor, model, _torch = self.create_backend(
                reference, generated_tokens=10_000
            )
            request = SynthesisRequest(
                "Rhiannon",
                "A short comparison line.",
                generation_profile="stable",
                cache_policy=SynthesisCachePolicy.BYPASS,
            )
            result = backend.render(request).collect()

        self.assertIs(result.completion, SynthesisCompletion.LIMITED)
        self.assertEqual(model.calls[-1]["max_new_tokens"], result.limits.max_tokens)

    def test_rejects_cache_reuse_so_local_and_delay_caches_cannot_mix(self):
        with TemporaryDirectory() as directory:
            reference = Path(directory) / "rhiannon.wav"
            reference.write_bytes(b"reference")
            backend, _processor, _model, _torch = self.create_backend(reference)
            with self.assertRaisesRegex(ValueError, "BYPASS"):
                backend.render(
                    SynthesisRequest(
                        "Rhiannon",
                        "A short comparison line.",
                        cache_policy=SynthesisCachePolicy.USE,
                    )
                ).collect()


if __name__ == "__main__":
    unittest.main()
