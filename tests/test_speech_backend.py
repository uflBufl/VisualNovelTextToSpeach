import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import numpy as np

from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    activate_chatterbox_runtime,
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


class ChatterboxNanoBackendTest(unittest.TestCase):
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

    def create_backend(self, registry, model=None, audio_output=None):
        model = model or FakeChatterboxModel()
        model_factory = Mock(return_value=model)
        torch_module = Mock()
        torch_module.cuda.is_available.return_value = False
        backend = ChatterboxNanoVoiceRouterBackend(
            registry,
            model_factory=model_factory,
            torch_module=torch_module,
            audio_output=audio_output or Mock(),
        )
        model_factory.assert_called_once_with(device="cpu", nano=True)
        return backend, model

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

    def test_repeated_line_uses_audio_cache(self):
        backend, model = self.create_backend(CharacterVoiceRegistry())

        first = backend.prepare("Narrator", "Same line.")
        second = backend.prepare("Narrator", "Same line.")

        self.assertIs(first, second)
        self.assertEqual(len(model.generated), 1)
        self.assertEqual(backend.last_synthesis_ms, 0.0)

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


if __name__ == "__main__":
    unittest.main()
