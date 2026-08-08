class TTSEngine:
    def __init__(
        self,
        model_name='tts_models/en/vctk/vits',
        speaker=None,
        language=None,
        *,
        tts_factory=None,
        torch_module=None,
        audio_output=None,
    ):
        if tts_factory is None:
            from TTS.api import TTS

            tts_factory = TTS
        if torch_module is None:
            import torch

            torch_module = torch
        if audio_output is None:
            import sounddevice

            audio_output = sounddevice

        device = torch_module.device(
            'cuda' if torch_module.cuda.is_available() else 'cpu'
        )
        print(f'TTS will be executed on {device}')

        self.tts = tts_factory(model_name=model_name).to(device)
        self.audio_output = audio_output
        self.default_speaker = speaker
        self.default_language = language
        self.sample_rate = self.tts.synthesizer.output_sample_rate
        if not self.sample_rate:
            raise RuntimeError('Loaded TTS model does not define an output sample rate')

    def speak(self, text, speaker=None, language=None):
        speaker = self._resolve_speaker(speaker)
        language = self._resolve_language(language)

        arguments = {}
        if speaker is not None:
            arguments['speaker'] = speaker
        if language is not None:
            arguments['language'] = language

        audio = self.tts.tts(text=text, **arguments)
        self.audio_output.play(audio, self.sample_rate)
        self.audio_output.wait()

    def _resolve_speaker(self, speaker):
        speaker = speaker if speaker is not None else self.default_speaker
        if not self.tts.is_multi_speaker:
            if speaker is not None:
                raise ValueError('Loaded TTS model does not support speaker selection')
            return None

        available_speakers = self.tts.speakers or []
        if speaker is None:
            if not available_speakers:
                raise ValueError('Loaded TTS model requires a speaker')
            return available_speakers[0]
        if available_speakers and speaker not in available_speakers:
            raise ValueError(
                f'Speaker {speaker!r} is not supported; '
                f'available speakers: {available_speakers}'
            )
        return speaker

    def _resolve_language(self, language):
        language = language if language is not None else self.default_language
        if not self.tts.is_multi_lingual:
            if language is not None:
                raise ValueError('Loaded TTS model does not support language selection')
            return None

        available_languages = self.tts.languages or []
        if language is None:
            if len(available_languages) == 1:
                return available_languages[0]
            raise ValueError(
                'Loaded multilingual TTS model requires a language; '
                f'available languages: {available_languages}'
            )
        if available_languages and language not in available_languages:
            raise ValueError(
                f'Language {language!r} is not supported; '
                f'available languages: {available_languages}'
            )
        return language

    def stop(self):
        self.audio_output.stop()
