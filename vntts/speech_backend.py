import os
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol

import numpy as np

from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSConfigurationError,
    TTSSynthesisError,
)
from vntts.voices import is_narrator, normalize_character_name


@dataclass(frozen=True)
class SpeechBackendCapabilities:
    voice_cloning: bool
    streaming: bool
    concurrent_prepare_and_play: bool


class SpeechBackend(Protocol):
    name: str
    capabilities: SpeechBackendCapabilities

    def prepare(self, character: str, text: str) -> Any: ...

    def play(self, prepared: Any, *, playback_guard=None) -> bool: ...

    def stop(self) -> bool: ...


class XTTSVoiceRouterBackend:
    """Compatibility adapter around the existing Coqui voice router."""

    name = "coqui-xtts"
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=False,
        concurrent_prepare_and_play=True,
    )

    def __init__(self, voice_router):
        self.voice_router = voice_router

    def prepare(self, character, text):
        return self.voice_router.synthesize(character, text)

    def play(self, prepared, *, playback_guard=None):
        return self.voice_router.play(
            prepared,
            playback_guard=playback_guard,
        )

    def stop(self):
        return self.voice_router.tts.stop()


class ChatterboxNanoVoiceRouterBackend:
    """Low-latency English voice cloning with persistent voice conditioning."""

    name = "chatterbox-nano"
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=False,
        concurrent_prepare_and_play=True,
    )

    def __init__(
        self,
        registry,
        *,
        narrator_reference=None,
        volume=1.0,
        model_factory=None,
        torch_module=None,
        audio_output=None,
        clock=monotonic,
        audio_cache_size=32,
        playback_latency="high",
        runtime_directory=None,
    ):
        if model_factory is None:
            runtime_site_packages = activate_chatterbox_runtime(runtime_directory)
            try:
                from chatterbox.tts_turbo import ChatterboxTurboTTS
            except ImportError as error:
                raise TTSConfigurationError(
                    "Chatterbox Nano could not be imported from "
                    f"{runtime_site_packages}. Reinstall it with "
                    "`uv sync --project backends/chatterbox-nano`."
                ) from error
            model_factory = ChatterboxTurboTTS.from_pretrained
        if torch_module is None:
            import torch

            torch_module = torch
        if audio_output is None:
            import sounddevice

            audio_output = sounddevice

        device = "cuda" if torch_module.cuda.is_available() else "cpu"
        self.model = model_factory(device=device, nano=True)
        self.registry = registry
        self.narrator_speaker = "Chatterbox default"
        self.narrator_reference = narrator_reference
        self.audio_output = audio_output
        self.clock = clock
        self.playback_latency = playback_latency
        self.sample_rate = int(self.model.sr)
        self.default_conditionals = getattr(self.model, "conds", None)
        self.conditionals = {}
        self.synthesis_lock = Lock()
        self.playback_lock = Lock()
        self.playback_active = False
        self.last_synthesis_ms = None
        self.last_playback_ms = None
        self.audio_cache_size = max(0, int(audio_cache_size))
        self.audio_cache = OrderedDict()
        self.set_volume(volume)

    def prepare(self, character, text):
        return self.synthesize(character, text)

    def synthesize(self, character, text):
        normalized_character = normalize_character_name(character) or "narrator"
        cache_key = normalized_character, " ".join((text or "").split())
        with self.synthesis_lock:
            if cache_key in self.audio_cache:
                audio = self.audio_cache.pop(cache_key)
                self.audio_cache[cache_key] = audio
                self.last_synthesis_ms = 0.0
                return audio

            started = self.clock()
            try:
                self.model.conds = self._resolve_conditionals(character)
                audio = self.model.generate(cache_key[1]).detach().cpu().numpy()
            except Exception as error:
                raise TTSSynthesisError(str(error)) from error
            finally:
                self.last_synthesis_ms = (self.clock() - started) * 1000

            audio = np.asarray(audio, dtype=np.float32).squeeze()
            self._cache_audio(cache_key, audio)
            return audio

    def speak(self, character, text, *, playback_guard=None):
        return self.play(
            self.prepare(character, text),
            playback_guard=playback_guard,
        )

    def play(self, prepared, *, playback_guard=None):
        self.last_playback_ms = None
        if playback_guard is not None and not playback_guard():
            return False
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return False
            started = self.clock()
            try:
                self.playback_active = True
                self.audio_output.play(
                    self._prepare_audio(prepared),
                    self.sample_rate,
                    latency=self.playback_latency,
                )
                self.audio_output.wait()
            except Exception as error:
                raise AudioPlaybackError(str(error)) from error
            finally:
                self.playback_active = False
                self.last_playback_ms = (self.clock() - started) * 1000
        return True

    def warm_up(self, *, progress=None, text="Voice ready."):
        progress = progress or (lambda _current, _total, _character: None)
        voices = sorted(
            {id(voice): voice for voice in self.registry.voices.values()}.values(),
            key=lambda voice: voice.character.casefold(),
        )
        characters = ["Narrator", *(voice.character for voice in voices)]
        for current, character in enumerate(characters, start=1):
            progress(current, len(characters), character)
            self.synthesize(character, text)
        return len(characters)

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
        # Nano does not currently expose a pitch-preserving speed control.
        self.speed = float(speed)

    def stop(self):
        was_playing = self.playback_active
        self.audio_output.stop()
        return was_playing

    def _resolve_conditionals(self, character):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            if self.default_conditionals is not None:
                return self.default_conditionals
            if self.narrator_reference:
                return self._prepare_conditionals(
                    "narrator",
                    self.narrator_reference,
                )
            raise TTSConfigurationError(
                "This Chatterbox model has no default narrator voice; configure "
                "a narrator reference in TTS speaker WAV."
            )

        cached = self.conditionals.get(voice.speaker)
        if cached is not None:
            return cached
        if not voice.references:
            raise TTSConfigurationError(
                f"Voice {voice.character!r} has no reference recording"
            )
        return self._prepare_conditionals(voice.speaker, voice.references[0])

    def _prepare_conditionals(self, key, reference):
        self.model.prepare_conditionals(str(reference))
        conditionals = self.model.conds
        self.conditionals[key] = conditionals
        return conditionals

    def _cache_audio(self, key, audio):
        if self.audio_cache_size == 0:
            return
        self.audio_cache.pop(key, None)
        self.audio_cache[key] = audio
        while len(self.audio_cache) > self.audio_cache_size:
            self.audio_cache.popitem(last=False)

    def _prepare_audio(self, audio, fade_seconds=0.01):
        prepared = np.asarray(audio, dtype=np.float32).squeeze().copy()
        prepared *= self.volume
        if prepared.ndim != 1 or len(prepared) < 4:
            return prepared
        fade_samples = min(round(self.sample_rate * fade_seconds), len(prepared) // 2)
        if fade_samples >= 2:
            fade = np.linspace(0.0, 1.0, fade_samples, dtype=prepared.dtype)
            prepared[:fade_samples] *= fade
            prepared[-fade_samples:] *= fade[::-1]
        return prepared


def activate_chatterbox_runtime(runtime_directory=None):
    runtime_directory = Path(
        runtime_directory
        or os.environ.get("VNTTS_CHATTERBOX_RUNTIME", "")
        or Path(__file__).resolve().parents[1] / "backends" / "chatterbox-nano" / ".venv"
    ).expanduser().resolve()
    if sys.platform == "win32":
        site_packages = runtime_directory / "Lib" / "site-packages"
    else:
        site_packages = (
            runtime_directory
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    if not site_packages.is_dir():
        raise TTSConfigurationError(
            "Chatterbox Nano is not installed. Run "
            "`uv sync --project backends/chatterbox-nano`, then restart the app."
        )
    site_packages_text = str(site_packages)
    if site_packages_text not in sys.path:
        sys.path.insert(0, site_packages_text)
    return site_packages
