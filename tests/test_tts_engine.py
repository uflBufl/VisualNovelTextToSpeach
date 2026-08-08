import unittest
from unittest.mock import Mock

from vntts.services.tts_engine import TTSEngine


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
        torch_module.device.return_value = 'cpu'

        audio_output = Mock()
        engine = TTSEngine(
            speaker=speaker,
            language=language,
            tts_factory=tts_factory,
            torch_module=torch_module,
            audio_output=audio_output,
        )
        return engine, tts, audio_output

    def test_speak_uses_model_sample_rate_and_first_available_speaker(self):
        engine, tts, audio_output = self.create_engine(
            is_multi_speaker=True,
            speakers=['p225', 'p227'],
            sample_rate=48000,
        )

        engine.speak('Hello')

        tts.tts.assert_called_once_with(text='Hello', speaker='p225')
        audio_output.play.assert_called_once_with(tts.tts.return_value, 48000)
        audio_output.wait.assert_called_once_with()

    def test_speak_omits_speaker_and_language_for_single_speaker_model(self):
        engine, tts, _ = self.create_engine()

        engine.speak('Hello')

        tts.tts.assert_called_once_with(text='Hello')

    def test_speak_passes_configured_speaker_and_language(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=['Alice', 'Bob'],
            is_multi_lingual=True,
            languages=['en', 'de'],
            speaker='Bob',
            language='en',
        )

        engine.speak('Hello')

        tts.tts.assert_called_once_with(
            text='Hello',
            speaker='Bob',
            language='en',
        )

    def test_multilingual_model_requires_language_selection(self):
        engine, tts, audio_output = self.create_engine(
            is_multi_lingual=True,
            languages=['en', 'de'],
        )

        with self.assertRaisesRegex(ValueError, 'requires a language'):
            engine.speak('Hello')

        tts.tts.assert_not_called()
        audio_output.play.assert_not_called()

    def test_unsupported_speaker_is_rejected_before_synthesis(self):
        engine, tts, _ = self.create_engine(
            is_multi_speaker=True,
            speakers=['Alice'],
        )

        with self.assertRaisesRegex(ValueError, "Speaker 'Bob' is not supported"):
            engine.speak('Hello', speaker='Bob')

        tts.tts.assert_not_called()

    def test_stop_delegates_to_audio_output(self):
        engine, _, audio_output = self.create_engine()

        engine.stop()

        audio_output.stop.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
