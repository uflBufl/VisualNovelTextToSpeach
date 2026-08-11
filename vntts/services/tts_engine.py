import re
import warnings
from collections import OrderedDict
from os import PathLike
from threading import Lock
from time import monotonic

import numpy as np


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
        "repetition_penalty": 10.0,
        "speed": 1.0,
        "split_sentences": True,
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
torchaudio_load_deprecation = (
    r"In 2\.9, this function's implementation will be changed to use "
    r"torchaudio\.load_with_torchcodec"
)
terminal_incomplete_punctuation = re.compile(r"\s*(?:\.{2,}|…+|[,;:])\s*$")


def prepare_speech_text(text):
    text = text.strip()
    return terminal_incomplete_punctuation.sub(".", text)


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
        volume=1.0,
        *,
        tts_factory=None,
        torch_module=None,
        audio_output=None,
        clock=monotonic,
        audio_cache_size=32,
        playback_latency="high",
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
        self.playback_latency = playback_latency
        self.clock = clock
        self.default_speaker = speaker
        self.default_language = language
        self.default_speaker_wav = speaker_wav
        if synthesis_options is None and "xtts" in model_name.casefold():
            synthesis_options = get_tts_profile(default_tts_profile)
        self.synthesis_options = dict(synthesis_options or {})
        self.set_volume(volume)
        self.cached_speakers = set()
        self.audio_cache_size = max(0, int(audio_cache_size))
        self.audio_cache = OrderedDict()
        # Coqui model inference and the audio device are independent resources.
        # Separate locks let live mode prepare the next sentence while the
        # current sentence is playing, without allowing two model inferences or
        # two PortAudio streams to overlap.
        self.synthesis_lock = Lock()
        self.playback_lock = Lock()
        self.playback_active = False
        self.last_synthesis_ms = None
        self.last_playback_ms = None
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
        audio = self.synthesize(
            text,
            speaker=speaker,
            language=language,
            speaker_wav=speaker_wav,
            synthesis_options=synthesis_options,
        )

        return self.play(audio, playback_guard=playback_guard)

    def play(self, audio, *, playback_guard=None):
        """Play already-synthesized audio.

        Live mode uses this separately from ``synthesize`` so sentence N+1 can
        be prepared while sentence N is using the output device.
        """
        self.last_playback_ms = None
        if playback_guard is not None and not playback_guard():
            return False

        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return False
            playback_started = self.clock()
            try:
                self.playback_active = True
                self.audio_output.play(
                    self._prepare_audio(audio),
                    self.sample_rate,
                    latency=self.playback_latency,
                )
                self.audio_output.wait()
            except Exception as error:
                raise AudioPlaybackError(str(error)) from error
            finally:
                self.playback_active = False
                self.last_playback_ms = (self.clock() - playback_started) * 1000
        return True

    def synthesize(
        self,
        text,
        speaker=None,
        language=None,
        speaker_wav=None,
        synthesis_options=None,
    ):
        with self.synthesis_lock:
            return self._synthesize_locked(
                text,
                speaker=speaker,
                language=language,
                speaker_wav=speaker_wav,
                synthesis_options=synthesis_options,
            )

    def _synthesize_locked(
        self,
        text,
        speaker=None,
        language=None,
        speaker_wav=None,
        synthesis_options=None,
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

        spoken_text = prepare_speech_text(text)
        cache_key = self._audio_cache_key(spoken_text, arguments)
        can_reuse_cached_audio = speaker_wav is None or speaker is None
        if can_reuse_cached_audio and cache_key in self.audio_cache:
            audio = self.audio_cache.pop(cache_key)
            self.audio_cache[cache_key] = audio
            self.last_synthesis_ms = 0.0
            return audio

        synthesis_started = self.clock()
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=torchaudio_load_deprecation,
                    category=UserWarning,
                )
                audio = self.tts.tts(text=spoken_text, **arguments)
        except Exception as error:
            raise TTSSynthesisError(str(error)) from error
        finally:
            self.last_synthesis_ms = (self.clock() - synthesis_started) * 1000

        if speaker_wav is not None and speaker is not None:
            self.cached_speakers.add(speaker)
            if uses_default_reference:
                # Coqui caches a cloned voice when speaker_wav and a custom
                # speaker ID are passed together. Later phrases can reuse it.
                self.default_speaker_wav = None

        self._cache_audio(cache_key, audio)
        return audio

    def _audio_cache_key(self, text, arguments):
        reusable_arguments = dict(arguments)
        if reusable_arguments.get("speaker") is not None:
            # Once a named voice has been cloned, Coqui only needs its ID. This
            # lets the first reference-backed result be reused by later calls
            # that correctly omit speaker_wav.
            reusable_arguments.pop("speaker_wav", None)
        return text, tuple(
            sorted(
                (name, self._cache_value(value))
                for name, value in reusable_arguments.items()
            )
        )

    def _cache_value(self, value):
        if isinstance(value, dict):
            return tuple(
                sorted((name, self._cache_value(item)) for name, item in value.items())
            )
        if isinstance(value, (list, tuple)):
            return tuple(self._cache_value(item) for item in value)
        if isinstance(value, PathLike):
            return str(value)
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    def _cache_audio(self, cache_key, audio):
        if self.audio_cache_size == 0:
            return
        self.audio_cache.pop(cache_key, None)
        self.audio_cache[cache_key] = audio
        while len(self.audio_cache) > self.audio_cache_size:
            self.audio_cache.popitem(last=False)

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
            and not self.has_speaker(speaker)
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
        if speaker in (self.tts.speakers or []) or speaker in self.cached_speakers:
            return True

        synthesizer = getattr(self.tts, "synthesizer", None)
        model = getattr(synthesizer, "tts_model", None)
        voice_dir = getattr(synthesizer, "voice_dir", None)
        get_voices = getattr(model, "get_voices", None)
        if not isinstance(voice_dir, (str, PathLike)) or not callable(get_voices):
            return False

        try:
            stored_speakers = get_voices(voice_dir)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        if speaker not in stored_speakers:
            return False

        # Coqui persists cloned XTTS embeddings in its voice directory. Keep
        # recognizing those voices after an app restart so the router does not
        # analyze the reference recordings again on the first spoken line.
        self.cached_speakers.add(speaker)
        return True

    def set_volume(self, volume):
        if isinstance(volume, bool) or not isinstance(volume, (int, float)):
            raise TTSConfigurationError("Volume must be a number from 0 to 1")
        if not 0 <= volume <= 1:
            raise TTSConfigurationError("Volume must be between 0 and 1")
        self.volume = float(volume)

    def set_speed(self, speed):
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise TTSConfigurationError("Speech speed must be a number")
        if not 0.5 <= speed <= 1.5:
            raise TTSConfigurationError("Speech speed must be between 0.5 and 1.5")
        self.synthesis_options["speed"] = float(speed)

    def _scaled_audio(self, audio):
        if self.volume == 1:
            return audio
        try:
            return audio * self.volume
        except TypeError:
            return [sample * self.volume for sample in audio]

    def _prepare_audio(self, audio, fade_seconds=0.01):
        audio = self._scaled_audio(audio)
        samples = np.asarray(audio)
        if samples.ndim == 0 or len(samples) < 4:
            return audio

        fade_samples = min(round(self.sample_rate * fade_seconds), len(samples) // 2)
        if fade_samples < 2:
            return audio

        prepared = samples.astype(np.float32, copy=True)
        fade = np.linspace(0.0, 1.0, fade_samples, dtype=prepared.dtype)
        shape = (fade_samples,) + (1,) * (prepared.ndim - 1)
        prepared[:fade_samples] *= fade.reshape(shape)
        prepared[-fade_samples:] *= fade[::-1].reshape(shape)
        return prepared

    def stop(self):
        was_playing = self.playback_active
        self.audio_output.stop()
        return was_playing
