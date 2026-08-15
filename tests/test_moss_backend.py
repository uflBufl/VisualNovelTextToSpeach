import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    MossTTSVoiceRouterBackend,
    activate_moss_tts_runtime,
    normalize_moss_language,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FakeMossResult:
    def __init__(self, audio):
        self.audio = np.asarray(audio, dtype=np.float32)


class FakeMossModel:
    sample_rate = 48_000

    def __init__(self):
        self.encoded_references = []
        self.generate_calls = []

    def encode_reference_audio(self, reference):
        self.encoded_references.append(reference)
        return f"codes:{reference}"

    def generate(self, **arguments):
        self.generate_calls.append(arguments)
        yield FakeMossResult([[0.0, 0.2, -0.2], [0.0, -0.2, 0.2]])
        yield FakeMossResult([[0.1, 0.0], [-0.1, 0.0]])


class CoordinatedMossModel(FakeMossModel):
    def __init__(self):
        super().__init__()
        self.second_chunk_ready = Event()

    def generate(self, **arguments):
        self.generate_calls.append(arguments)
        yield FakeMossResult([[0.0, 0.2, -0.2], [0.0, -0.2, 0.2]])
        self.second_chunk_ready.set()
        yield FakeMossResult([[0.1, 0.0], [-0.1, 0.0]])


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


class FakeAudioOutput:
    def __init__(self):
        self.streams = []

    def OutputStream(self, **options):
        return FakeOutputStream(self, **options)


class CoordinatedOutputStream(FakeOutputStream):
    def write(self, audio):
        if not self.writes:
            self.owner.generated_while_first_chunk_played = (
                self.owner.model.second_chunk_ready.wait(0.5)
            )
        return super().write(audio)


class CoordinatedAudioOutput(FakeAudioOutput):
    def __init__(self, model):
        super().__init__()
        self.model = model
        self.generated_while_first_chunk_played = False

    def OutputStream(self, **options):
        return CoordinatedOutputStream(self, **options)


class MossTTSBackendTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.narrator_reference = self.root / "narrator.wav"
        self.narrator_reference.touch()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_backend(
        self,
        *,
        model=None,
        registry=None,
        narrator_reference=None,
        prompt_cache_directory=None,
        audio_output=None,
    ):
        model = model or FakeMossModel()
        model_factory = Mock(return_value=model)
        audio_output = audio_output or FakeAudioOutput()
        resolved_narrator_reference = (
            None
            if narrator_reference is False
            else self.narrator_reference
            if narrator_reference is None
            else narrator_reference
        )

        def save_codes(codes, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(codes, encoding="utf-8")

        backend = MossTTSVoiceRouterBackend(
            registry or CharacterVoiceRegistry(),
            narrator_reference=resolved_narrator_reference,
            language="en",
            model_factory=model_factory,
            audio_output=audio_output,
            prompt_cache_directory=(
                prompt_cache_directory or self.root / "prompt-cache"
            ),
            persistent_audio_cache_directory=self.root / "audio-cache",
            prompt_code_loader=lambda path: path.read_text(encoding="utf-8"),
            prompt_code_saver=save_codes,
            array_evaluator=lambda _value: None,
        )
        model_factory.assert_called_once_with(backend.model_name, lazy=True)
        return backend, model, audio_output

    def test_streams_stereo_audio_with_realtime_buffering(self):
        backend, model, output = self.create_backend()

        prepared = backend.prepare("Narrator", "  Hello   Timekeeper. ")
        self.assertTrue(backend.play(prepared))

        self.assertEqual(len(model.generate_calls), 1)
        call = model.generate_calls[0]
        self.assertEqual(call["text"], "Hello Timekeeper.")
        self.assertEqual(call["language"], "English")
        self.assertTrue(call["stream"])
        self.assertEqual(call["streaming_first_chunk_frames"], 16)
        self.assertEqual(call["streaming_interval"], 1.0)
        self.assertEqual(output.streams[0].options["samplerate"], 48_000)
        self.assertEqual(output.streams[0].options["channels"], 2)
        self.assertEqual(output.streams[0].writes[0].shape, (3, 2))
        self.assertIsNotNone(backend.last_first_audio_ms)

    def test_generates_next_chunk_while_current_chunk_is_playing(self):
        model = CoordinatedMossModel()
        output = CoordinatedAudioOutput(model)
        backend, _model, _output = self.create_backend(
            model=model,
            audio_output=output,
        )

        self.assertTrue(backend.speak("Narrator", "Keep streaming."))

        self.assertTrue(output.generated_while_first_chunk_played)

    def test_repeated_line_reuses_complete_stereo_audio_cache(self):
        backend, model, output = self.create_backend()

        self.assertTrue(backend.speak("Narrator", "Same line."))
        self.assertTrue(backend.speak("Narrator", "Same line."))

        self.assertEqual(len(model.generate_calls), 1)
        self.assertEqual(len(output.streams), 2)
        self.assertEqual(backend.last_synthesis_ms, 0.0)

    def test_prompt_codes_survive_backend_restart(self):
        first_backend, first_model, _output = self.create_backend()
        self.assertTrue(first_backend.prime("Narrator"))

        second_model = FakeMossModel()
        second_backend, _model, _output = self.create_backend(model=second_model)
        self.assertTrue(second_backend.prime("Narrator"))

        self.assertEqual(
            first_model.encoded_references,
            [str(self.narrator_reference.resolve())],
        )
        self.assertEqual(second_model.encoded_references, [])
        self.assertEqual(
            second_backend.prompt_audio_codes["narrator"],
            f"codes:{self.narrator_reference.resolve()}",
        )

    def test_character_reference_is_encoded_instead_of_narrator(self):
        reference = self.root / "matilda.wav"
        reference.touch()
        registry = CharacterVoiceRegistry(
            [CharacterVoice("Matilda", "matilda", reference)]
        )
        backend, model, _output = self.create_backend(registry=registry)

        backend.prepare("Matilda", "Bonjour!")

        self.assertEqual(model.encoded_references, [str(reference.resolve())])

    def test_missing_narrator_reference_is_actionable(self):
        backend, _model, _output = self.create_backend(narrator_reference=False)

        with self.assertRaisesRegex(
            TTSConfigurationError,
            "requires a narrator reference",
        ):
            backend.prepare("Narrator", "Once upon a time.")

    def test_stop_aborts_active_stream(self):
        backend, _model, output = self.create_backend()
        backend.playback_active = True
        stream = output.OutputStream()
        backend.active_stream = stream

        self.assertTrue(backend.stop())

        self.assertTrue(stream.aborted)

    def test_runtime_activation_requires_private_environment(self):
        with TemporaryDirectory() as temporary_directory:
            missing_runtime = Path(temporary_directory) / "missing"
            machine = SimpleNamespace(machine="arm64")
            with (
                patch("vntts.speech_backend.sys.platform", "darwin"),
                patch("vntts.speech_backend.os.uname", return_value=machine),
                self.assertRaisesRegex(TTSConfigurationError, "uv sync --project"),
            ):
                activate_moss_tts_runtime(missing_runtime)

    def test_runtime_reports_unsupported_platform(self):
        unsupported = "win32" if sys.platform != "win32" else "linux"
        with (
            patch("vntts.speech_backend.sys.platform", unsupported),
            self.assertRaisesRegex(TTSConfigurationError, "Apple Silicon"),
        ):
            activate_moss_tts_runtime()

    def test_language_codes_are_expanded_for_v15(self):
        self.assertEqual(normalize_moss_language("en"), "English")
        self.assertEqual(normalize_moss_language("fr"), "French")
        self.assertEqual(normalize_moss_language(None), "English")


if __name__ == "__main__":
    unittest.main()
