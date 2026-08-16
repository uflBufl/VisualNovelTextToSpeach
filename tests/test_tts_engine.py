import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import numpy as np

from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSEngine,
    TTSSynthesisError,
    get_tts_profile,
    prepare_speech_text,
)
from vntts.synthesis import SynthesisCachePolicy

_default_audio_output = object()


class TTSEngineTest(unittest.TestCase):
    def create_engine(
        self,
        *,
        is_multi_speaker=False,
        speakers=None,
        is_multi_lingual=False,
        languages=None,
        sample_rate=24000,
        speaker=None,
        language=None,
        speaker_wav=None,
        synthesis_options=None,
        model_name="tts_models/en/vctk/vits",
        volume=1.0,
        clock=None,
        audio_cache_size=32,
        audio_output=_default_audio_output,
    ):
        tts = Mock()
        tts.is_multi_speaker = is_multi_speaker
        tts.speakers = speakers
        tts.is_multi_lingual = is_multi_lingual
        tts.languages = languages
        tts.synthesizer.output_sample_rate = sample_rate
        tts.tts.return_value = [0.0, 0.5, 0.0]

        tts_factory = Mock()
        tts_factory.return_value.to.return_value = tts

        torch_module = Mock()
        torch_module.cuda.is_available.return_value = False
        torch_module.device.return_value = "cpu"

        if audio_output is _default_audio_output:
            audio_output = Mock()
        options = {}
        if clock is not None:
            options["clock"] = clock
        engine = TTSEngine(
            model_name=model_name,
            speaker=speaker,
            language=language,
            speaker_wav=speaker_wav,
            synthesis_options=synthesis_options,
            volume=volume,
            tts_factory=tts_factory,
            torch_module=torch_module,
            audio_output=audio_output,
            audio_cache_size=audio_cache_size,
            **options,
        )
        return engine, tts, audio_output

    def test_xtts_uses_stable_profile_by_default(self):
        engine, _, _ = self.create_engine(model_name="xtts_v2")

        self.assertEqual(engine.synthesis_options, get_tts_profile("stable"))
        self.assertTrue(engine.synthesis_options["split_sentences"])
        self.assertEqual(engine.synthesis_options["repetition_penalty"], 10.0)

    def test_terminal_ellipsis_is_closed_before_synthesis(self):
        text = (
            "Paddle out to Itiiti, circle around to Miti and Vaipuna, "
            "then come back to Meli ..."
        )

        self.assertEqual(
            prepare_speech_text(text),
            "Paddle out to Itiiti, circle around to Miti and Vaipuna, "
            "then come back to Meli.",
        )
        self.assertEqual(
            prepare_speech_text("I ... don't know ... yet."),
            "I ... don't know ... yet.",
        )
        self.assertEqual(
            prepare_speech_text("Wait, this is still appearing,"),
            "Wait, this is still appearing.",
        )

    def test_synthesize_normalizes_only_the_spoken_copy(self):
        engine, tts, _ = self.create_engine()

        engine.synthesize("Come back to Meli ...")

        tts.tts.assert_called_once_with(text="Come back to Meli.")

    def test_non_xtts_model_has_no_profile_by_default(self):
        engine, _, _ = self.create_engine()

        self.assertEqual(engine.synthesis_options, {})

    def test_speak_uses_model_sample_rate_and_first_available_speaker(self):
        engine, tts, audio_output = self.create_engine(
            is_multi_speaker=True,
            speakers=["p225", "p227"],
            sample_rate=48000,
        )

        engine.speak("Hello")

        tts.tts.assert_called_once_with(text="Hello", speaker="p225")
        audio_output.play.assert_called_once_with(
            tts.tts.return_value,
            48000,
            latency="high",
        )
        audio_output.wait.assert_called_once_with()

    def test_speak_records_synthesis_and_playback_latency(self):
        clock = iter((0.0, 0.15, 1.0, 1.4)).__next__
        engine, _, _ = self.create_engine(clock=clock)

        engine.speak("Hello")

        self.assertAlmostEqual(engine.last_synthesis_ms, 150.0)
        self.assertAlmostEqual(engine.last_playback_ms, 400.0)

    def test_synthesize_warms_model_without_playing_audio(self):
        engine, tts, audio_output = self.create_engine()

        audio = engine.synthesize("Voice ready.")

        self.assertIs(audio, tts.tts.return_value)
        tts.tts.assert_called_once_with(text="Voice ready.")
        audio_output.play.assert_not_called()
        audio_output.wait.assert_not_called()

    def test_repeated_line_reuses_generated_audio(self):
        engine, tts, audio_output = self.create_engine()

        engine.speak("Same line")
        engine.speak("Same line")

        tts.tts.assert_called_once_with(text="Same line")
        self.assertEqual(audio_output.play.call_count, 2)
        self.assertEqual(engine.last_synthesis_ms, 0.0)
        self.assertEqual(engine.last_cache_source, "memory-cache")

    def test_synthesis_cache_policy_can_refresh_or_bypass_audio(self):
        engine, tts, _audio_output = self.create_engine()

        engine.synthesize("Same line")
        engine.synthesize("Same line")
        engine.synthesize("Same line", cache_policy=SynthesisCachePolicy.REFRESH)
        engine.synthesize("Same line", cache_policy=SynthesisCachePolicy.BYPASS)

        self.assertEqual(tts.tts.call_count, 3)
        self.assertEqual(engine.last_cache_source, "fresh-generation")

    def test_cancelled_synthesis_is_not_returned_from_audio_cache(self):
        cancelled = Event()
        engine, tts, _audio_output = self.create_engine()

        def synthesize(**_arguments):
            cancelled.set()
            return [0.0, 0.5, 0.0]

        tts.tts.side_effect = synthesize
        cancelled_audio = engine.synthesize(
            "Cancel this",
            cancellation=cancelled.is_set,
        )
        cancelled.clear()
        engine.synthesize("Cancel this")

        self.assertEqual(cancelled_audio, [0.0, 0.5, 0.0])
        self.assertEqual(tts.tts.call_count, 2)
        self.assertFalse(engine.last_synthesis_cancelled)

    def test_render_only_engine_does_not_import_sounddevice(self):
        original_import = __import__

        def reject_sounddevice(name, *args, **kwargs):
            if name == "sounddevice":
                raise AssertionError("render-only construction imported sounddevice")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=reject_sounddevice):
            engine, _tts, audio_output = self.create_engine(audio_output=None)
            audio = engine.synthesize("Offline XTTS render")

        self.assertIsNone(audio_output)
        self.assertEqual(audio, [0.0, 0.5, 0.0])

    def test_audio_cache_keeps_speakers_separate(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Alice", "Bob"],
        )

        engine.synthesize("Same line", speaker="Alice")
        engine.synthesize("Same line", speaker="Bob")

        self.assertEqual(tts.tts.call_count, 2)

    def test_audio_cache_discards_least_recently_used_line(self):
        engine, tts, _ = self.create_engine(audio_cache_size=1)

        engine.synthesize("First")
        engine.synthesize("Second")
        engine.synthesize("First")

        self.assertEqual(tts.tts.call_count, 3)

    def test_named_clone_result_is_cached_without_reference_path(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Preset"],
            is_multi_lingual=True,
            languages=["en"],
            language="en",
        )

        engine.synthesize(
            "Same line",
            speaker="Cloned",
            speaker_wav="reference.wav",
        )
        engine.synthesize("Same line", speaker="Cloned")

        tts.tts.assert_called_once_with(
            text="Same line",
            speaker="Cloned",
            language="en",
            speaker_wav="reference.wav",
        )

    def test_synthesize_suppresses_only_torchaudio_load_deprecation(self):
        engine, tts, _ = self.create_engine()

        def synthesize(**_arguments):
            warnings.warn(
                "In 2.9, this function's implementation will be changed to use "
                "torchaudio.load_with_torchcodec` under the hood.",
                UserWarning,
            )
            warnings.warn("A useful model warning", RuntimeWarning)
            return [0.0]

        tts.tts.side_effect = synthesize

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            engine.synthesize("Voice ready.")

        self.assertEqual(
            [str(item.message) for item in captured], ["A useful model warning"]
        )

    def test_output_volume_scales_audio_before_playback(self):
        engine, tts, audio_output = self.create_engine(volume=0.4)

        engine.speak("Hello")

        audio_output.play.assert_called_once_with(
            [0.0, 0.2, 0.0],
            24000,
            latency="high",
        )
        self.assertEqual(tts.tts.call_count, 1)

    def test_playback_fades_audio_edges_to_avoid_clicks(self):
        engine, tts, audio_output = self.create_engine(sample_rate=1000)
        tts.tts.return_value = np.ones(100, dtype=np.float32)

        engine.speak("Hello")

        prepared = audio_output.play.call_args.args[0]
        self.assertEqual(prepared[0], 0.0)
        self.assertEqual(prepared[-1], 0.0)
        self.assertEqual(prepared[10], 1.0)

    def test_playback_reports_and_resets_output_underflow(self):
        engine, _tts, audio_output = self.create_engine()
        audio_output.wait.return_value.output_underflow = True

        engine.play([0.0, 0.5, 0.0])
        self.assertTrue(engine.last_playback_underrun)

        audio_output.wait.return_value.output_underflow = False
        engine.play([0.0, 0.5, 0.0])
        self.assertFalse(engine.last_playback_underrun)

    def test_runtime_volume_and_speed_are_validated_and_updated(self):
        engine, tts, _ = self.create_engine()

        engine.set_volume(0.25)
        engine.set_speed(1.2)
        engine.speak("Hello")

        self.assertEqual(engine.volume, 0.25)
        tts.tts.assert_called_once_with(text="Hello", speed=1.2)
        with self.assertRaisesRegex(ValueError, "Volume"):
            engine.set_volume(2)
        with self.assertRaisesRegex(ValueError, "speed"):
            engine.set_speed(0.2)

    def test_speak_omits_speaker_and_language_for_single_speaker_model(self):
        engine, tts, _ = self.create_engine()

        engine.speak("Hello")

        tts.tts.assert_called_once_with(text="Hello")

    def test_speak_passes_configured_speaker_and_language(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Alice", "Bob"],
            is_multi_lingual=True,
            languages=["en", "de"],
            speaker="Bob",
            language="en",
        )

        engine.speak("Hello")

        tts.tts.assert_called_once_with(
            text="Hello",
            speaker="Bob",
            language="en",
        )

    def test_speak_passes_profile_options_to_synthesis(self):
        profile = get_tts_profile("natural")
        engine, tts, _ = self.create_engine(synthesis_options=profile)

        engine.speak("Hello")

        tts.tts.assert_called_once_with(text="Hello", **profile)

    def test_call_options_override_profile(self):
        engine, tts, _ = self.create_engine(
            synthesis_options={"temperature": 0.85, "speed": 0.98}
        )

        engine.speak("Hello", synthesis_options={"temperature": 0.7})

        tts.tts.assert_called_once_with(
            text="Hello",
            temperature=0.7,
            speed=0.98,
        )

    def test_unknown_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown TTS profile"):
            get_tts_profile("cinematic")

    def test_speak_clones_custom_speaker_then_reuses_cached_voice(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Preset"],
            is_multi_lingual=True,
            languages=["en"],
            speaker="cloned-voice",
            language="en",
            speaker_wav="reference.ogg",
        )

        engine.speak("First phrase")
        engine.speak("Second phrase")

        self.assertEqual(
            tts.tts.call_args_list,
            [
                unittest.mock.call(
                    text="First phrase",
                    speaker="cloned-voice",
                    language="en",
                    speaker_wav="reference.ogg",
                ),
                unittest.mock.call(
                    text="Second phrase",
                    speaker="cloned-voice",
                    language="en",
                ),
            ],
        )

    def test_speak_can_clone_without_caching_under_a_speaker_id(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Preset"],
            is_multi_lingual=True,
            languages=["en"],
            language="en",
        )

        engine.speak("Hello", speaker_wav="reference.ogg")

        tts.tts.assert_called_once_with(
            text="Hello",
            language="en",
            speaker_wav="reference.ogg",
        )

    def test_multilingual_model_requires_language_selection(self):
        engine, tts, audio_output = self.create_engine(
            is_multi_lingual=True,
            languages=["en", "de"],
        )

        with self.assertRaisesRegex(ValueError, "requires a language"):
            engine.speak("Hello")

        tts.tts.assert_not_called()
        audio_output.play.assert_not_called()

    def test_unsupported_speaker_is_rejected_before_synthesis(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Alice"],
        )

        with self.assertRaisesRegex(ValueError, "Speaker 'Bob' is not supported"):
            engine.speak("Hello", speaker="Bob")

        tts.tts.assert_not_called()

    def test_synthesis_failure_identifies_tts_stage(self):
        engine, tts, audio_output = self.create_engine()
        tts.tts.side_effect = RuntimeError("model crashed")

        with self.assertRaisesRegex(TTSSynthesisError, "model crashed"):
            engine.speak("Hello")

        audio_output.play.assert_not_called()

    def test_audio_failure_identifies_playback_stage(self):
        engine, _, audio_output = self.create_engine()
        audio_output.play.side_effect = RuntimeError("device unavailable")

        with self.assertRaisesRegex(AudioPlaybackError, "device unavailable"):
            engine.speak("Hello")

    def test_stale_synthesis_is_discarded_before_playback(self):
        engine, tts, audio_output = self.create_engine()

        result = engine.speak("Old line", playback_guard=Mock(return_value=False))

        tts.tts.assert_called_once_with(text="Old line")
        audio_output.play.assert_not_called()
        self.assertFalse(result)

    def test_stop_delegates_to_audio_output(self):
        engine, _, audio_output = self.create_engine()

        result = engine.stop()

        audio_output.stop.assert_called_once_with()
        self.assertFalse(result)

    def test_has_speaker_checks_model_and_newly_cached_voices(self):
        engine, _, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Preset"],
        )
        engine.cached_speakers.add("Cloned")

        self.assertTrue(engine.has_speaker("Preset"))
        self.assertTrue(engine.has_speaker("Cloned"))
        self.assertFalse(engine.has_speaker("Unknown"))

    def test_has_speaker_recognizes_voice_persisted_by_coqui(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Preset"],
        )
        with TemporaryDirectory() as directory:
            voice_path = Path(directory) / "Saved-clone.pth"
            tts.synthesizer.voice_dir = directory
            tts.synthesizer.tts_model.get_voices.return_value = {
                "Saved-clone": voice_path
            }

            self.assertTrue(engine.has_speaker("Saved-clone"))
            self.assertIn("Saved-clone", engine.cached_speakers)

    def test_persisted_voice_is_reused_without_reference_audio(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=["Preset"],
            is_multi_lingual=True,
            languages=["en"],
            language="en",
        )
        with TemporaryDirectory() as directory:
            voice_path = Path(directory) / "Saved-clone.pth"
            tts.synthesizer.voice_dir = directory
            tts.synthesizer.tts_model.get_voices.return_value = {
                "Saved-clone": voice_path
            }

            engine.speak("Hello", speaker="Saved-clone")

        tts.tts.assert_called_once_with(
            text="Hello",
            speaker="Saved-clone",
            language="en",
        )


if __name__ == "__main__":
    unittest.main()
