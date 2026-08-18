import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from vntts.playback import PlaybackStatus
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.speech_backend import (
    MossTTSVoiceRouterBackend,
    activate_moss_tts_runtime,
    moss_generation_limits,
    normalize_moss_language,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry

_default_audio_output = object()


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


class EndlessMossModel(FakeMossModel):
    sample_rate = 10

    def generate(self, **arguments):
        self.generate_calls.append(arguments)
        while True:
            yield FakeMossResult(np.full(10, 0.1, dtype=np.float32))


class ErrorAfterFirstMossModel(FakeMossModel):
    def generate(self, **arguments):
        self.generate_calls.append(arguments)
        yield FakeMossResult([[0.0, 0.2], [0.0, -0.2]])
        raise RuntimeError("generation was closed")


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


class BlockingOutputStream(FakeOutputStream):
    def write(self, audio):
        self.owner.write_started.set()
        self.owner.release_write.wait(timeout=1)
        return super().write(audio)

    def abort(self):
        super().abort()
        if self.owner.abort_unblocks:
            self.owner.release_write.set()


class BlockingAudioOutput(FakeAudioOutput):
    def __init__(self, *, abort_unblocks=True):
        super().__init__()
        self.abort_unblocks = abort_unblocks
        self.write_started = Event()
        self.release_write = Event()

    def OutputStream(self, **options):
        return BlockingOutputStream(self, **options)


class FailingOutputStream(FakeOutputStream):
    def __enter__(self):
        if self.owner.failure_stage == "open":
            raise RuntimeError("device open failed")
        return super().__enter__()

    def write(self, audio):
        if self.owner.failure_stage == "write":
            raise RuntimeError("device write failed")
        return super().write(audio)


class FailingAudioOutput(FakeAudioOutput):
    def __init__(self, failure_stage):
        super().__init__()
        self.failure_stage = failure_stage

    def OutputStream(self, **options):
        return FailingOutputStream(self, **options)


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
        audio_output=_default_audio_output,
        generation_profile="stable",
        playback_consumer_join_timeout=5.0,
    ):
        model = model or FakeMossModel()
        model_factory = Mock(return_value=model)
        if audio_output is _default_audio_output:
            audio_output = FakeAudioOutput()
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
            generation_profile=generation_profile,
            playback_consumer_join_timeout=playback_consumer_join_timeout,
        )
        model_factory.assert_called_once_with(backend.model_name, lazy=False)
        return backend, model, audio_output

    def test_streams_stereo_audio_with_realtime_buffering(self):
        backend, model, output = self.create_backend()

        prepared = backend.prepare("Narrator", "  Hello   Timekeeper. ")
        self.assertEqual(prepared.cache_source, "fresh-generation")
        self.assertTrue(backend.play(prepared))

        self.assertEqual(len(model.generate_calls), 1)
        call = model.generate_calls[0]
        self.assertEqual(call["text"], "Hello Timekeeper.")
        self.assertEqual(call["language"], "English")
        self.assertEqual(call["mode"], "generation")
        self.assertTrue(call["do_sample"])
        self.assertEqual(call["audio_temperature"], 0.8)
        self.assertEqual(call["audio_top_p"], 0.8)
        self.assertEqual(call["audio_top_k"], 25)
        self.assertEqual(call["audio_repetition_penalty"], 1.0)
        self.assertTrue(call["stream"])
        self.assertEqual(call["streaming_first_chunk_frames"], 4)
        self.assertEqual(call["streaming_interval"], 0.25)
        self.assertEqual(call["max_tokens"], prepared.max_tokens)
        self.assertEqual(output.streams[0].options["samplerate"], 48_000)
        self.assertEqual(output.streams[0].options["channels"], 2)
        self.assertEqual(output.streams[0].writes[0].shape, (3, 2))
        self.assertIsNotNone(backend.last_first_audio_ms)

    def test_render_returns_typed_pcm_without_opening_an_audio_device(self):
        backend, model, output = self.create_backend()

        stream = backend.render(
            SynthesisRequest(
                voice="Narrator",
                text="Render this line.",
                seed=7,
                generation_profile="natural",
            )
        )
        chunks = list(stream)
        result = stream.result

        self.assertEqual(output.streams, [])
        self.assertEqual(result.completion, SynthesisCompletion.COMPLETE)
        self.assertEqual(result.sample_rate, 48_000)
        self.assertEqual(result.pcm.shape, (5, 2))
        self.assertEqual([chunk.index for chunk in chunks], [0, 1])
        self.assertTrue(all(chunk.sample_rate == 48_000 for chunk in chunks))
        self.assertEqual(result.diagnostics.cache_source, "fresh-generation")
        self.assertEqual(result.diagnostics.generation_profile, "natural")
        self.assertEqual(result.diagnostics.seed, 7)
        self.assertEqual(
            result.limits,
            backend.render(
                SynthesisRequest(
                    voice="Narrator",
                    text="Render this line.",
                    seed=7,
                    generation_profile="natural",
                )
            )
            .collect()
            .limits,
        )
        self.assertEqual(len(model.generate_calls), 1)

    def test_render_only_backend_does_not_import_sounddevice(self):
        original_import = __import__

        def reject_sounddevice(name, *args, **kwargs):
            if name == "sounddevice":
                raise AssertionError("render-only construction imported sounddevice")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=reject_sounddevice):
            backend, _model, audio_output = self.create_backend(audio_output=None)
            result = backend.render(
                SynthesisRequest(
                    voice="Narrator",
                    text="Offline MOSS render.",
                )
            ).collect()

        self.assertIsNone(audio_output)
        self.assertEqual(result.completion, SynthesisCompletion.COMPLETE)

    def test_render_cancellation_returns_partial_pcm_without_caching_it(self):
        backend, model, output = self.create_backend()
        cancelled = Event()
        stream = backend.render(
            SynthesisRequest(
                voice="Narrator",
                text="Cancel this line.",
                cancellation=cancelled,
            )
        )

        first = next(stream)
        cancelled.set()
        remaining = list(stream)

        self.assertEqual(output.streams, [])
        self.assertEqual(remaining, [])
        self.assertEqual(stream.result.completion, SynthesisCompletion.CANCELLED)
        np.testing.assert_array_equal(stream.result.pcm, first.pcm)
        self.assertEqual(len(model.generate_calls), 1)
        self.assertEqual(
            backend.prepare("Narrator", "Cancel this line.").cache_source,
            "fresh-generation",
        )

    def test_render_cancellation_keeps_typed_result_when_generator_raises(self):
        backend, _model, _output = self.create_backend(model=ErrorAfterFirstMossModel())
        cancelled = Event()
        stream = backend.render(
            SynthesisRequest(
                voice="Narrator",
                text="Cancel while closing.",
                cancellation=cancelled,
            )
        )

        next(stream)
        cancelled.set()
        list(stream)

        self.assertEqual(stream.result.completion, SynthesisCompletion.CANCELLED)
        self.assertEqual(stream.result.diagnostics.chunk_count, 1)

    def test_bypass_cache_renders_each_seeded_request_fresh(self):
        backend, model, _output = self.create_backend()
        random = Mock()
        backend._mlx = SimpleNamespace(random=random)
        request = SynthesisRequest(
            voice="Narrator",
            text="Retry this line.",
            seed=11,
            cache_policy=SynthesisCachePolicy.BYPASS,
        )

        first = backend.render(request).collect()
        second = backend.render(request).collect()

        self.assertEqual(first.diagnostics.cache_source, "fresh-generation")
        self.assertEqual(second.diagnostics.cache_source, "fresh-generation")
        self.assertEqual(len(model.generate_calls), 2)
        self.assertEqual(random.seed.call_args_list, [unittest.mock.call(11)] * 2)

    def test_refresh_cache_skips_read_then_replaces_reusable_audio(self):
        backend, model, _output = self.create_backend()
        base = {
            "voice": "Narrator",
            "text": "Refresh this line.",
            "seed": 5,
        }

        first = backend.render(SynthesisRequest(**base)).collect()
        refreshed = backend.render(
            SynthesisRequest(**base, cache_policy=SynthesisCachePolicy.REFRESH)
        ).collect()
        reused = backend.render(SynthesisRequest(**base)).collect()

        self.assertEqual(first.diagnostics.cache_source, "fresh-generation")
        self.assertEqual(refreshed.diagnostics.cache_source, "fresh-generation")
        self.assertEqual(reused.diagnostics.cache_source, "memory-cache")
        self.assertEqual(len(model.generate_calls), 2)

    def test_render_reports_missed_eos_limit_without_caching_partial_audio(self):
        model = EndlessMossModel()
        backend, _model, output = self.create_backend(model=model)

        result = backend.render(
            SynthesisRequest(voice="Narrator", text="I, erhm ...")
        ).collect()

        self.assertEqual(output.streams, [])
        self.assertEqual(result.completion, SynthesisCompletion.LIMITED)
        self.assertLessEqual(
            len(result.pcm),
            round(model.sample_rate * result.limits.max_audio_seconds),
        )
        self.assertEqual(
            backend.prepare("Narrator", "I, erhm ...").cache_source,
            "fresh-generation",
        )

    def test_short_text_has_a_bounded_generation_budget_in_cache_identity(self):
        backend, _model, _output = self.create_backend()
        backend.persistent_cache_keys.key = Mock(return_value="bounded-key")

        prepared = backend.prepare("Narrator", "I, erhm ...")

        self.assertEqual(
            (prepared.max_tokens, prepared.max_audio_seconds),
            moss_generation_limits("I, erhm ..."),
        )
        self.assertEqual(prepared.max_audio_seconds, 3.0)
        self.assertLess(prepared.max_tokens, 4096)
        self.assertEqual(prepared.persistent_cache_key, "bounded-key")
        cache_settings = backend.persistent_cache_keys.key.call_args.kwargs
        self.assertEqual(cache_settings["max_tokens"], prepared.max_tokens)
        self.assertEqual(
            cache_settings["max_audio_seconds"],
            prepared.max_audio_seconds,
        )

    def test_natural_sentences_have_bounded_slow_cadence_reserve(self):
        self.assertEqual(
            moss_generation_limits("Wait for me now.")[1], 5.166666666666666
        )
        self.assertEqual(
            moss_generation_limits("The poachers shove her forward, and they set off."),
            (850, 8.5),
        )
        self.assertEqual(moss_generation_limits("word " * 100), (2000, 20.0))

    def test_missed_eos_is_cut_at_the_text_length_audio_budget(self):
        model = EndlessMossModel()
        backend, _model, output = self.create_backend(model=model)

        prepared = backend.prepare("Narrator", "I, erhm ...")
        self.assertTrue(backend.play(prepared))

        written_samples = sum(len(chunk) for chunk in output.streams[0].writes)
        self.assertLessEqual(
            written_samples,
            round(model.sample_rate * prepared.max_audio_seconds),
        )
        self.assertTrue(backend.last_generation_limited)
        self.assertEqual(
            backend.prepare("Narrator", "I, erhm ...").cache_source,
            "fresh-generation",
        )

    def test_expressive_profile_uses_upstream_moss_sampling_temperature(self):
        backend, model, _output = self.create_backend(generation_profile="expressive")

        self.assertTrue(backend.speak("Narrator", "An expressive line."))

        self.assertEqual(model.generate_calls[0]["audio_temperature"], 1.7)

    def test_switching_profile_invalidates_in_memory_generated_audio(self):
        backend, model, _output = self.create_backend()
        self.assertTrue(backend.speak("Narrator", "The same line."))

        self.assertTrue(backend.set_generation_profile("natural"))
        self.assertTrue(backend.speak("Narrator", "The same line."))

        self.assertEqual(len(model.generate_calls), 2)
        self.assertEqual(model.generate_calls[-1]["audio_temperature"], 1.2)

    def test_invalid_generation_profile_is_actionable(self):
        with self.assertRaisesRegex(
            TTSConfigurationError,
            "Unknown MOSS-TTS voice profile",
        ):
            self.create_backend(generation_profile="chaotic")

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

        first = backend.prepare("Narrator", "Same line.")
        self.assertEqual(first.cache_source, "fresh-generation")
        self.assertTrue(backend.play(first))
        second = backend.prepare("Narrator", "Same line.")
        self.assertEqual(second.cache_source, "memory-cache")
        self.assertTrue(backend.play(second))

        self.assertEqual(len(model.generate_calls), 1)
        self.assertEqual(len(output.streams), 2)
        self.assertEqual(backend.last_synthesis_ms, 0.0)

    def test_complete_audio_cache_survives_backend_restart(self):
        first_backend, first_model, _output = self.create_backend()
        first = first_backend.prepare("Narrator", "Persistent line.")
        self.assertTrue(first_backend.play(first))

        second_model = FakeMossModel()
        second_backend, _model, _output = self.create_backend(model=second_model)
        second = second_backend.prepare("Narrator", "Persistent line.")

        self.assertEqual(first.cache_source, "fresh-generation")
        self.assertEqual(second.cache_source, "persistent-cache")
        self.assertTrue(second_backend.play(second))
        self.assertEqual(len(first_model.generate_calls), 1)
        self.assertEqual(second_model.generate_calls, [])

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

    def test_unknown_character_falls_back_to_narrator_reference(self):
        backend, model, _output = self.create_backend()

        backend.prepare("Hotelier", "Welcome to the hotel.")

        self.assertEqual(
            model.encoded_references,
            [str(self.narrator_reference.resolve())],
        )

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

    def test_stop_aborts_and_joins_the_owned_blocked_stream(self):
        output = BlockingAudioOutput()
        backend, _model, _output = self.create_backend(audio_output=output)
        prepared = backend.prepare_playback("Narrator", "Stop this stream.")
        outcomes = []
        thread = Thread(target=lambda: outcomes.append(backend.play_prepared(prepared)))
        thread.start()
        self.assertTrue(output.write_started.wait(timeout=1))

        self.assertTrue(backend.stop())
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0].status, PlaybackStatus.INTERRUPTED)
        self.assertTrue(output.streams[0].aborted)
        self.assertIsNone(backend.active_stream)
        self.assertFalse(backend.playback_active)

    def test_ignored_abort_fails_but_retains_stream_ownership(self):
        output = BlockingAudioOutput(abort_unblocks=False)
        backend, _model, _output = self.create_backend(
            audio_output=output,
            playback_consumer_join_timeout=0.01,
        )
        prepared = backend.prepare_playback("Narrator", "Blocked stream.")

        outcome = backend.play_prepared(prepared)

        self.assertEqual(outcome.status, PlaybackStatus.FAILED)
        self.assertIn("remains active", outcome.error)
        self.assertIsNotNone(backend.active_stream)
        self.assertTrue(backend.playback_active)
        retry = backend.play_prepared(prepared)
        self.assertEqual(retry.status, PlaybackStatus.FAILED)
        output.release_write.set()
        for _index in range(100):
            if backend.active_stream is None:
                break
            Event().wait(0.001)
        self.assertIsNone(backend.active_stream)

    def test_legacy_moss_play_preserves_synthesis_error_type(self):
        backend, _model, _output = self.create_backend(model=ErrorAfterFirstMossModel())
        prepared = backend.prepare("Narrator", "Broken generation.")

        with self.assertRaisesRegex(TTSSynthesisError, "generation was closed"):
            backend.play(prepared)

    def test_device_open_and_write_errors_are_failed_not_interrupted(self):
        for stage in ("open", "write"):
            with self.subTest(stage=stage):
                backend, _model, _output = self.create_backend(
                    audio_output=FailingAudioOutput(stage)
                )
                prepared = backend.prepare_playback("Narrator", "Device error.")

                outcome = backend.play_prepared(prepared)

                self.assertEqual(outcome.status, PlaybackStatus.FAILED)
                self.assertIn(f"device {stage} failed", outcome.error)

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
