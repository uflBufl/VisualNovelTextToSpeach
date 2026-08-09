class TTSError(Exception):
    pass


class TTSConfigurationError(TTSError, ValueError):
    pass


class TTSSynthesisError(TTSError, RuntimeError):
    pass


class AudioPlaybackError(RuntimeError):
    pass


tts_profiles = {
    "stable": {
        "temperature": 0.70,
        "top_p": 0.80,
        "top_k": 40,
        "repetition_penalty": 2.0,
        "speed": 1.0,
        "split_sentences": False,
        "sound_norm_refs": True,
    },
    "natural": {
        "temperature": 0.85,
        "top_p": 0.90,
        "top_k": 50,
        "repetition_penalty": 2.0,
        "speed": 0.98,
        "split_sentences": False,
        "sound_norm_refs": True,
    },
    "expressive": {
        "temperature": 0.95,
        "top_p": 0.95,
        "top_k": 60,
        "repetition_penalty": 2.0,
        "speed": 0.98,
        "split_sentences": False,
        "sound_norm_refs": True,
    },
}
default_tts_profile = "stable"


def get_tts_profile(name):
    try:
        return dict(tts_profiles[name])
    except KeyError as error:
        available = ", ".join(sorted(tts_profiles))
        raise TTSConfigurationError(
            f"Unknown TTS profile {name!r}; available profiles: {available}"
        ) from error


class TTSEngine:
    def __init__(
        self,
        model_name="tts_models/en/vctk/vits",
        speaker=None,
        language=None,
        speaker_wav=None,
        synthesis_options=None,
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
            "cuda" if torch_module.cuda.is_available() else "cpu"
        )
        print(f"TTS will be executed on {device}")

        self.tts = tts_factory(model_name=model_name).to(device)
        self.audio_output = audio_output
        self.default_speaker = speaker
        self.default_language = language
        self.default_speaker_wav = speaker_wav
        if synthesis_options is None and "xtts" in model_name.casefold():
            synthesis_options = get_tts_profile(default_tts_profile)
        self.synthesis_options = dict(synthesis_options or {})
        self.cached_speakers = set()
        self.playback_active = False
        self.sample_rate = self.tts.synthesizer.output_sample_rate
        if not self.sample_rate:
            raise RuntimeError("Loaded TTS model does not define an output sample rate")

    def speak(
        self,
        text,
        speaker=None,
        language=None,
        speaker_wav=None,
        synthesis_options=None,
        playback_guard=None,
    ):
        uses_default_reference = speaker_wav is None
        if uses_default_reference:
            speaker_wav = self.default_speaker_wav

        speaker = self._resolve_speaker(speaker, speaker_wav)
        language = self._resolve_language(language)

        arguments = dict(self.synthesis_options)
        if synthesis_options:
            arguments.update(synthesis_options)
        if speaker is not None:
            arguments["speaker"] = speaker
        if language is not None:
            arguments["language"] = language
        if speaker_wav is not None:
            arguments["speaker_wav"] = speaker_wav

        try:
            audio = self.tts.tts(text=text, **arguments)
        except Exception as error:
            raise TTSSynthesisError(str(error)) from error

        if speaker_wav is not None and speaker is not None:
            self.cached_speakers.add(speaker)
            if uses_default_reference:
                # Coqui caches a cloned voice when speaker_wav and a custom
                # speaker ID are passed together. Later phrases can reuse it.
                self.default_speaker_wav = None

        if playback_guard is not None and not playback_guard():
            return False

        try:
            self.playback_active = True
            self.audio_output.play(audio, self.sample_rate)
            self.audio_output.wait()
        except Exception as error:
            raise AudioPlaybackError(str(error)) from error
        finally:
            self.playback_active = False
        return True

    def _resolve_speaker(self, speaker, speaker_wav=None):
        speaker = speaker if speaker is not None else self.default_speaker
        if speaker_wav is not None:
            return speaker

        if not self.tts.is_multi_speaker:
            if speaker is not None:
                raise TTSConfigurationError(
                    "Loaded TTS model does not support speaker selection"
                )
            return None

        available_speakers = self.tts.speakers or []
        if speaker is None:
            if not available_speakers:
                raise TTSConfigurationError("Loaded TTS model requires a speaker")
            return available_speakers[0]
        if (
            available_speakers
            and speaker not in available_speakers
            and speaker not in self.cached_speakers
        ):
            raise TTSConfigurationError(
                f"Speaker {speaker!r} is not supported; "
                f"available speakers: {available_speakers}"
            )
        return speaker

    def _resolve_language(self, language):
        language = language if language is not None else self.default_language
        if not self.tts.is_multi_lingual:
            if language is not None:
                raise TTSConfigurationError(
                    "Loaded TTS model does not support language selection"
                )
            return None

        available_languages = self.tts.languages or []
        if language is None:
            if len(available_languages) == 1:
                return available_languages[0]
            raise TTSConfigurationError(
                "Loaded multilingual TTS model requires a language; "
                f"available languages: {available_languages}"
            )
        if available_languages and language not in available_languages:
            raise TTSConfigurationError(
                f"Language {language!r} is not supported; "
                f"available languages: {available_languages}"
            )
        return language

    def has_speaker(self, speaker):
        return speaker in (self.tts.speakers or []) or speaker in self.cached_speakers

    def stop(self):
        was_playing = self.playback_active
        self.audio_output.stop()
        return was_playing
