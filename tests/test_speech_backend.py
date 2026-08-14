import queue
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

import numpy as np
import torch

from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    XTTSVoiceRouterBackend,
    activate_chatterbox_runtime,
    activate_pocket_tts_runtime,
    configure_cpu_synthesis_threads,
    select_torch_device,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FakeTensor:
    def __init__(self, audio):
        self.audio = np.asarray(audio, dtype=np.float32)

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.audio


class FakeChatterboxModel:
    sr = 24_000

    def __init__(self):
        self.conds = "default narrator"
        self.prepared_references = []
        self.generated = []

    def prepare_conditionals(self, reference):
        self.prepared_references.append(reference)
        self.conds = f"conditioned:{reference}"

    def generate(self, text):
        self.generated.append((self.conds, text))
        return FakeTensor([[0.0, 0.25, -0.25, 0.0]])


class CacheableConditionals:
    def __init__(self, value):
        self.value = value

    def save(self, path):
        Path(path).write_text(self.value, encoding="utf-8")

    @classmethod
    def load(cls, path, map_location="cpu"):
        return cls(f"{Path(path).read_text(encoding='utf-8')}@{map_location}")


class CacheableChatterboxModel(FakeChatterboxModel):
    def __init__(self):
        super().__init__()
        self.conds = CacheableConditionals("default narrator")

    def prepare_conditionals(self, reference):
        self.prepared_references.append(reference)
        self.conds = CacheableConditionals(f"conditioned:{reference}")


class FakePocketModel:
    sample_rate = 24_000

    def __init__(self):
        self.prompt_calls = []
        self.stream_calls = []

    def get_state_for_audio_prompt(self, source):
        self.prompt_calls.append(source)
        return f"state:{source}"

    def generate_audio_stream(self, state, text):
        self.stream_calls.append((state, text))
        yield FakeTensor([0.0, 0.25, -0.25])
        yield FakeTensor([0.1, -0.1, 0.0])


class FakePrivatePocketModel(FakePocketModel):
    class FlowModel:
        ldim = 2
        dtype = torch.float32

        @staticmethod
        def parameters():
            return iter((torch.zeros(1),))

    def __init__(self):
        super().__init__()
        self.flow_lm = self.FlowModel()
        self.generation_calls = 0

    def _autoregressive_generation(self, *_arguments):
        raise AssertionError("Pocket TTS loop was not patched")

    def _run_flow_lm_and_increment_step(self, **_arguments):
        self.generation_calls += 1
        return torch.zeros((1, 1, 2)), torch.tensor(False)


class FakeOutputStream:
    def __init__(self, owner, **options):
        self.owner = owner
        self.options = options
        self.writes = []
        self.aborted = False

    def __enter__(self):
        self.owner.streams.append(self)
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def write(self, audio):
        self.writes.append(np.asarray(audio))
        return False

    def abort(self):
        self.aborted = True


class FakeStreamingAudioOutput:
    def __init__(self):
        self.streams = []

    def OutputStream(self, **options):
        return FakeOutputStream(self, **options)


class ChatterboxNanoBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_runtime_activation_requires_an_installed_private_environment(self):
        with TemporaryDirectory() as temporary_directory:
            missing_runtime = Path(temporary_directory) / "missing"

            with self.assertRaisesRegex(TTSConfigurationError, "uv sync --project"):
                activate_chatterbox_runtime(missing_runtime)

    def test_runtime_activation_prepends_private_site_packages(self):
        with TemporaryDirectory() as temporary_directory:
            runtime = Path(temporary_directory)
            if sys.platform == "win32":
                site_packages = runtime / "Lib" / "site-packages"
            else:
                site_packages = (
                    runtime
                    / "lib"
                    / f"python{sys.version_info.major}.{sys.version_info.minor}"
                    / "site-packages"
                )
            site_packages.mkdir(parents=True)

            try:
                result = activate_chatterbox_runtime(runtime)
                self.assertEqual(result, site_packages.resolve())
                self.assertEqual(sys.path[0], str(site_packages.resolve()))
            finally:
                if str(site_packages.resolve()) in sys.path:
                    sys.path.remove(str(site_packages.resolve()))

    def create_backend(
        self,
        registry,
        model=None,
        audio_output=None,
        conditioning_cache_directory=None,
    ):
        model = model or FakeChatterboxModel()
        model_factory = Mock(return_value=model)
        torch_module = Mock()
        torch_module.cuda.is_available.return_value = False
        torch_module.backends.mps.is_available.return_value = False
        backend = ChatterboxNanoVoiceRouterBackend(
            registry,
            model_factory=model_factory,
            torch_module=torch_module,
            audio_output=audio_output or Mock(),
            conditioning_cache_directory=conditioning_cache_directory,
            persistent_audio_cache_directory=(
                Path(self.temporary_directory.name) / "audio-cache"
            ),
        )
        model_factory.assert_called_once_with(device="cpu", nano=True)
        return backend, model

    def test_uses_measured_cpu_path_when_only_apple_metal_is_available(self):
        torch_module = Mock()
        torch_module.cuda.is_available.return_value = False
        torch_module.backends.mps.is_available.return_value = True

        self.assertEqual(select_torch_device(torch_module), "cpu")

    def test_cpu_backend_disables_synthesis_during_playback(self):
        backend, _model = self.create_backend(CharacterVoiceRegistry())

        self.assertFalse(backend.capabilities.concurrent_prepare_and_play)

    def test_configured_narrator_reference_overrides_embedded_default(self):
        with TemporaryDirectory() as temporary_directory:
            reference = Path(temporary_directory) / "narrator.wav"
            reference.touch()
            model = FakeChatterboxModel()
            backend, model = self.create_backend(CharacterVoiceRegistry(), model=model)
            backend.narrator_reference = reference

            backend.prepare("Narrator", "Once upon a time.")

        self.assertEqual(model.prepared_references, [str(reference)])

    def test_reserves_cpu_threads_for_ocr_and_playback(self):
        torch_module = Mock()
        torch_module.get_num_threads.return_value = 6

        self.assertTrue(configure_cpu_synthesis_threads(torch_module))
        torch_module.set_num_threads.assert_called_once_with(4)

    def test_live_mode_reduces_cpu_synthesis_to_two_threads(self):
        torch_module = Mock()
        torch_module.cuda.is_available.return_value = False
        torch_module.get_num_threads.return_value = 6
        backend = ChatterboxNanoVoiceRouterBackend(
            CharacterVoiceRegistry(),
            model_factory=Mock(return_value=FakeChatterboxModel()),
            torch_module=torch_module,
            audio_output=Mock(),
        )

        backend.set_live_mode_active(True)
        backend.set_live_mode_active(False)

        self.assertEqual(
            [call.args[0] for call in torch_module.set_num_threads.call_args_list],
            [4, 2, 6],
        )

    def test_output_underflow_disables_concurrent_prefetch(self):
        audio_output = Mock()
        audio_output.get_stream.return_value.status.output_underflow = True
        backend, _model = self.create_backend(
            CharacterVoiceRegistry(),
            audio_output=audio_output,
        )

        backend.play(np.zeros(100, dtype=np.float32))

        self.assertTrue(backend.last_playback_underrun)
        self.assertFalse(backend.capabilities.concurrent_prepare_and_play)

    def test_reuses_persistent_conditioning_when_returning_to_a_voice(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            alice_reference = root / "alice.wav"
            bob_reference = root / "bob.wav"
            alice_reference.touch()
            bob_reference.touch()
            registry = CharacterVoiceRegistry(
                [
                    CharacterVoice("Alice", "alice", alice_reference),
                    CharacterVoice("Bob", "bob", bob_reference),
                ]
            )
            backend, model = self.create_backend(registry)

            backend.prepare("Alice", "First line.")
            backend.prepare("Bob", "Second line.")
            backend.prepare("Alice", "Third line.")

        self.assertEqual(
            model.prepared_references,
            [str(alice_reference), str(bob_reference)],
        )
        self.assertEqual(model.generated[0][0], model.generated[2][0])
        self.assertNotEqual(model.generated[0][0], model.generated[1][0])

    def test_reuses_conditioning_after_backend_restart(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference = root / "alice.wav"
            reference.touch()
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Alice", "alice", reference)]
            )
            first_model = CacheableChatterboxModel()
            first_backend, _model = self.create_backend(
                registry,
                model=first_model,
                conditioning_cache_directory=root / "cache",
            )
            self.assertTrue(first_backend.prime("Alice"))

            second_model = CacheableChatterboxModel()
            second_backend, _model = self.create_backend(
                registry,
                model=second_model,
                conditioning_cache_directory=root / "cache",
            )
            self.assertTrue(second_backend.prime("Alice"))

        self.assertEqual(first_model.prepared_references, [str(reference)])
        self.assertEqual(second_model.prepared_references, [])
        self.assertTrue(second_backend.conditionals["alice"].value.endswith("@cpu"))

    def test_repeated_line_uses_audio_cache(self):
        backend, model = self.create_backend(CharacterVoiceRegistry())

        first = backend.prepare("Narrator", "Same line.")
        second = backend.prepare("Narrator", "Same line.")

        self.assertIs(first, second)
        self.assertEqual(len(model.generated), 1)
        self.assertEqual(backend.last_synthesis_ms, 0.0)

    def test_audio_cache_survives_backend_restart(self):
        registry = CharacterVoiceRegistry()
        first_backend, first_model = self.create_backend(registry)
        first_backend.prepare("Narrator", "Persistent line.")
        second_model = FakeChatterboxModel()
        second_backend, _model = self.create_backend(registry, model=second_model)

        second_backend.prepare("Narrator", "Persistent line.")

        self.assertEqual(len(first_model.generated), 1)
        self.assertEqual(second_model.generated, [])

    def test_playback_guard_prevents_stale_audio(self):
        audio_output = Mock()
        backend, _model = self.create_backend(
            CharacterVoiceRegistry(),
            audio_output=audio_output,
        )

        self.assertFalse(
            backend.play(np.zeros(10, dtype=np.float32), playback_guard=lambda: False)
        )
        audio_output.play.assert_not_called()

    def test_play_uses_backend_sample_rate_and_volume(self):
        audio_output = Mock()
        backend, _model = self.create_backend(
            CharacterVoiceRegistry(),
            audio_output=audio_output,
        )
        backend.set_volume(0.5)

        self.assertTrue(backend.play(np.array([0.0, 1.0, 1.0, 0.0])))

        played_audio, sample_rate = audio_output.play.call_args.args
        self.assertEqual(sample_rate, 24_000)
        self.assertLessEqual(float(np.max(played_audio)), 0.5)
        audio_output.wait.assert_called_once_with()

    def test_play_resamples_nano_audio_to_the_native_output_rate(self):
        audio_output = Mock()
        audio_output.query_devices.return_value = {"default_samplerate": 48_000.0}
        backend, _model = self.create_backend(
            CharacterVoiceRegistry(),
            audio_output=audio_output,
        )

        backend.play(np.linspace(-0.5, 0.5, 24_000, dtype=np.float32))

        played_audio, sample_rate = audio_output.play.call_args.args
        self.assertEqual(sample_rate, 48_000)
        self.assertGreaterEqual(len(played_audio), 47_999)
        self.assertLessEqual(float(np.max(np.abs(played_audio))), 0.95)

    def test_playback_removes_invalid_samples_dc_offset_and_clipping(self):
        backend, _model = self.create_backend(CharacterVoiceRegistry())

        prepared = backend._prepare_audio(
            np.array([np.nan, 2.0, 2.0, 2.0, np.inf, -2.0, -2.0, -2.0])
        )

        self.assertTrue(np.all(np.isfinite(prepared)))
        self.assertLessEqual(float(np.max(np.abs(prepared))), 0.95)


class XTTSBackendTest(unittest.TestCase):
    def test_exposes_the_wrapped_engine_underflow_status(self):
        voice_router = Mock()
        voice_router.tts.last_playback_underrun = True
        backend = XTTSVoiceRouterBackend(voice_router)

        self.assertTrue(backend.last_playback_underrun)


class PocketTTSBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_backend(
        self,
        registry=None,
        *,
        model=None,
        audio_output=None,
        cache_directory=None,
    ):
        model = model or FakePocketModel()
        audio_output = audio_output or FakeStreamingAudioOutput()

        def export_state(state, path):
            Path(path).write_text(state, encoding="utf-8")

        backend = PocketTTSVoiceRouterBackend(
            registry or CharacterVoiceRegistry(),
            model_factory=Mock(return_value=model),
            state_exporter=export_state,
            audio_output=audio_output,
            voice_state_cache_directory=(
                cache_directory
                or Path(self.temporary_directory.name) / "voice-state-cache"
            ),
            persistent_audio_cache_directory=(
                Path(self.temporary_directory.name) / "audio-cache"
            ),
        )
        return backend, model, audio_output

    def test_runtime_activation_requires_an_installed_private_environment(self):
        with TemporaryDirectory() as temporary_directory:
            missing_runtime = Path(temporary_directory) / "missing"

            with self.assertRaisesRegex(TTSConfigurationError, "uv sync --project"):
                activate_pocket_tts_runtime(missing_runtime)

    def test_private_pocket_latent_loop_stops_before_next_generation_step(self):
        model = FakePrivatePocketModel()
        backend, _model, _output = self.create_backend(model=model)
        cancellation = Event()
        cancellation.set()
        backend.active_generation_cancel = cancellation
        latents = queue.Queue()

        model._autoregressive_generation({}, 10, 2, latents)

        self.assertTrue(backend.cooperative_generation_cancellation)
        self.assertEqual(model.generation_calls, 0)
        self.assertIsNone(latents.get_nowait())

    def test_streams_generated_chunks_without_waiting_for_full_waveform(self):
        backend, model, audio_output = self.create_backend()

        prepared = backend.prepare("Narrator", "  Hello   world. ")
        self.assertTrue(backend.play(prepared))

        self.assertEqual(model.stream_calls, [("state:alba", "Hello world.")])
        self.assertEqual(len(audio_output.streams), 1)
        self.assertEqual(len(audio_output.streams[0].writes), 2)
        self.assertEqual(audio_output.streams[0].options["samplerate"], 24_000)
        self.assertIsNotNone(backend.last_first_audio_ms)

    def test_repeated_line_reuses_complete_audio_cache(self):
        backend, model, audio_output = self.create_backend()

        self.assertTrue(backend.speak("Narrator", "Same line."))
        self.assertTrue(backend.speak("Narrator", "Same line."))

        self.assertEqual(len(model.stream_calls), 1)
        self.assertEqual(len(audio_output.streams), 2)
        self.assertEqual(backend.last_synthesis_ms, 0.0)

    def test_complete_audio_cache_survives_backend_restart(self):
        backend, first_model, _output = self.create_backend()
        self.assertTrue(backend.speak("Narrator", "Persistent line."))
        second_model = FakePocketModel()
        second_backend, _model, _output = self.create_backend(model=second_model)

        self.assertTrue(second_backend.speak("Narrator", "Persistent line."))

        self.assertEqual(len(first_model.stream_calls), 1)
        self.assertEqual(second_model.prompt_calls, [])
        self.assertEqual(second_model.stream_calls, [])

    def test_voice_state_is_exported_and_reloaded_after_restart(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference = root / "selone.wav"
            reference.touch()
            registry = CharacterVoiceRegistry(
                [CharacterVoice("Selone", "selone", reference)]
            )
            first_backend, first_model, _output = self.create_backend(
                registry,
                cache_directory=root / "cache",
            )
            self.assertTrue(first_backend.prime("Selone"))

            second_model = FakePocketModel()
            second_backend, _model, _output = self.create_backend(
                registry,
                model=second_model,
                cache_directory=root / "cache",
            )
            self.assertTrue(second_backend.prime("Selone"))

        self.assertEqual(first_model.prompt_calls, [str(reference)])
        self.assertEqual(len(second_model.prompt_calls), 1)
        self.assertTrue(second_model.prompt_calls[0].endswith(".safetensors"))

    def test_playback_guard_prevents_opening_stream(self):
        backend, _model, audio_output = self.create_backend()
        prepared = backend.prepare("Narrator", "Stale line.")

        self.assertFalse(backend.play(prepared, playback_guard=lambda: False))

        self.assertEqual(audio_output.streams, [])

    def test_locked_voice_cloning_reports_actionable_setup(self):
        model = FakePocketModel()
        model.get_state_for_audio_prompt = Mock(
            side_effect=ValueError("accept the terms before using voice cloning")
        )
        backend, _model, _output = self.create_backend(model=model)

        with self.assertRaisesRegex(
            TTSConfigurationError,
            "huggingface.co/kyutai/pocket-tts",
        ):
            backend.prepare("Narrator", "Test.")

    def test_output_is_scaled_and_clipped_per_stream_chunk(self):
        backend, _model, _output = self.create_backend()
        backend.set_volume(0.5)

        prepared = backend._prepare_audio(np.array([np.nan, 3.0, -3.0]))

        self.assertTrue(np.all(np.isfinite(prepared)))
        self.assertLessEqual(float(np.max(np.abs(prepared))), 0.95)

    def test_stop_cancels_active_generation_and_aborts_stream(self):
        backend, _model, audio_output = self.create_backend()
        backend.playback_active = True
        backend.active_generation_cancel = Event()
        stream = audio_output.OutputStream()
        backend.active_stream = stream

        self.assertTrue(backend.stop())

        self.assertTrue(backend.active_generation_cancel.is_set())
        self.assertTrue(stream.aborted)

    def test_cancelled_stream_is_drained_without_writing_later_chunks(self):
        backend, _model, audio_output = self.create_backend()
        stream = audio_output.OutputStream()
        generated = []

        def chunks():
            generated.append(1)
            yield FakeTensor([0.1, -0.1])
            backend.playback_stop.set()
            generated.append(2)
            yield FakeTensor([0.2, -0.2])
            generated.append(3)

        self.assertFalse(backend._write_chunks(stream, chunks(), None))

        self.assertEqual(generated, [1, 2, 3])
        self.assertEqual(len(stream.writes), 1)


if __name__ == "__main__":
    unittest.main()
