import unittest
from unittest.mock import Mock

from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSEngine,
    TTSSynthesisError,
    get_tts_profile,
)


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
        clock=None,
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
            tts_factory=tts_factory,
            torch_module=torch_module,
            audio_output=audio_output,
            **options,
        )
        return engine, tts, audio_output

    def test_xtts_uses_stable_profile_by_default(self):
        engine, _, _ = self.create_engine(model_name="xtts_v2")

        self.assertEqual(engine.synthesis_options, get_tts_profile("stable"))

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
        audio_output.play.assert_called_once_with(tts.tts.return_value, 48000)
        audio_output.wait.assert_called_once_with()

    def test_speak_records_synthesis_and_playback_latency(self):
        clock = iter((0.0, 0.15, 1.0, 1.4)).__next__
        engine, _, _ = self.create_engine(clock=clock)

        engine.speak("Hello")

        self.assertAlmostEqual(engine.last_synthesis_ms, 150.0)
        self.assertAlmostEqual(engine.last_playback_ms, 400.0)

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


if __name__ == "__main__":
    unittest.main()
