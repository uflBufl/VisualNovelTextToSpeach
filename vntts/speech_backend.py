import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from types import MethodType
from typing import Any

import numpy as np
from vntts_artifacts.atomic_io import atomic_output_path

from vntts.application_directories import get_local_data_directory
from vntts.audio_cache import PersistentAudioCache
from vntts.audio_output import playback_underflowed, resolve_audio_output
from vntts.playback import (
    PlaybackStatus,
    PreparedPlayback,
    outcome_for_prepared,
)
from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSConfigurationError,
    TTSSynthesisError,
    get_tts_profile,
    match_output_sample_rate,
)
from vntts.speech_backend_contract import SpeechBackend, SpeechBackendCapabilities
from vntts.speech_backend_runtime import (
    BoundedCache,
    SpeechCacheKeyFactory,
    activate_backend_runtime,
    validate_speed,
    validate_volume,
    voice_artifact_cache_path,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
    moss_generation_limits,
    normalize_short_trailing_ellipsis,
)
from vntts.voices import (
    is_narrator,
    normalize_character_name,
    pocket_tts_preset_voices,
)

__all__ = ["SpeechBackend", "SpeechBackendCapabilities"]


def _raise_playback_failure(outcome, default_message):
    message = outcome.error or default_message
    error_type = outcome.error_type
    if isinstance(error_type, type) and issubclass(
        error_type, (TTSConfigurationError, TTSSynthesisError)
    ):
        raise error_type(message)
    raise AudioPlaybackError(message)


@dataclass(frozen=True)
class PocketTTSPreparedSpeech:
    voice_key: str
    voice_state: Any
    text: str
    cache_key: tuple[str, str]
    persistent_cache_key: str
    cached_audio: np.ndarray | None = None
    cache_source: str = "fresh-generation"
    generation_profile: str = "default"
    cache_policy: SynthesisCachePolicy = SynthesisCachePolicy.USE


@dataclass(frozen=True)
class MossTTSPreparedSpeech:
    voice_key: str
    prompt_audio_codes: Any
    text: str
    cache_key: tuple[Any, ...]
    persistent_cache_key: str
    max_tokens: int
    max_audio_seconds: float
    cached_audio: np.ndarray | None = None
    cache_source: str = "fresh-generation"
    seed: int | None = None
    generation_profile: str = "stable"
    generation_options: tuple[tuple[str, Any], ...] = ()
    cache_policy: SynthesisCachePolicy = SynthesisCachePolicy.USE


default_moss_tts_model = "shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8"

moss_tts_generation_profiles = {
    "stable": {
        "audio_temperature": 0.8,
        "audio_top_p": 0.8,
        "audio_top_k": 25,
        "audio_repetition_penalty": 1.0,
    },
    "natural": {
        "audio_temperature": 1.2,
        "audio_top_p": 0.8,
        "audio_top_k": 25,
        "audio_repetition_penalty": 1.0,
    },
    "expressive": {
        "audio_temperature": 1.7,
        "audio_top_p": 0.8,
        "audio_top_k": 25,
        "audio_repetition_penalty": 1.0,
    },
}

moss_language_names = {
    "ar": "Arabic",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fa": "Persian",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "mk": "Macedonian",
    "ms": "Malay",
    "nl": "Dutch",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sv": "Swedish",
    "sw": "Swahili",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "yue": "Cantonese",
    "zh": "Chinese",
}


def normalize_moss_language(language):
    value = str(language or "English").strip()
    return moss_language_names.get(value.casefold().replace("_", "-"), value)


def get_moss_tts_generation_profile(name):
    profile_name = str(name or "stable").strip().casefold()
    try:
        return profile_name, dict(moss_tts_generation_profiles[profile_name])
    except KeyError as error:
        available = ", ".join(sorted(moss_tts_generation_profiles))
        raise TTSConfigurationError(
            f"Unknown MOSS-TTS voice profile {name!r}; available profiles: {available}"
        ) from error


class XTTSVoiceRouterBackend:
    """Compatibility adapter around the existing Coqui voice router."""

    name = "coqui-xtts"
    generation_profile = "configured"
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=False,
        concurrent_prepare_and_play=True,
    )

    def __init__(self, voice_router, *, clock=monotonic):
        self.voice_router = voice_router
        self.clock = clock
        sample_rate = getattr(voice_router.tts, "sample_rate", 24_000)
        self.sample_rate = (
            int(sample_rate)
            if isinstance(sample_rate, (int, float))
            and not isinstance(sample_rate, bool)
            else 24_000
        )
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None

    def prepare(self, character, text):
        prepared = self.prepare_playback(character, text)
        self.last_synthesis_ms = prepared.synthesis_ms
        self.last_first_audio_ms = prepared.first_audio_ms
        return prepared.payload

    def prepare_playback(self, character, text):
        rendered = self.render(
            SynthesisRequest(
                voice=character,
                text=text,
                generation_profile=self.generation_profile,
            )
        ).collect()
        return PreparedPlayback(
            rendered.pcm.reshape(-1),
            rendered.timing.first_chunk_ms,
            None,
            rendered.diagnostics.cache_source,
            f"live:{self.name}",
        )

    def render(self, request):
        """Render Coqui/XTTS PCM through the configured voice router."""
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError("XTTS received an invalid render request")
        spoken_text = " ".join((request.text or "").split())
        if not spoken_text:
            raise TTSSynthesisError("XTTS received empty text")
        if request.seed is not None:
            raise TTSConfigurationError(
                "XTTS does not expose deterministic seeded generation"
            )
        profile = str(request.generation_profile or "configured").strip().casefold()
        synthesis_options = None
        if profile != self.generation_profile:
            synthesis_options = get_tts_profile(profile)
        try:
            cache_policy = SynthesisCachePolicy(request.cache_policy)
        except ValueError as error:
            raise TTSConfigurationError(
                f"Unknown synthesis cache policy {request.cache_policy!r}"
            ) from error
        return SynthesisChunkStream(
            self._render_chunks(
                request,
                spoken_text,
                profile,
                synthesis_options,
                cache_policy,
            )
        )

    def _render_chunks(
        self,
        request,
        spoken_text,
        profile,
        synthesis_options,
        cache_policy,
    ):
        started = self.clock()
        prepared = self.voice_router.prepare_playback(
            request.voice,
            spoken_text,
            synthesis_options=synthesis_options,
            cache_policy=cache_policy,
            cancellation=request.cancellation_requested,
        )
        elapsed_ms = (self.clock() - started) * 1000
        cancelled = bool(
            request.cancellation_requested() or not prepared.generation_completed
        )
        completion = (
            SynthesisCompletion.CANCELLED if cancelled else SynthesisCompletion.COMPLETE
        )
        pcm = (
            np.asarray(prepared.payload, dtype=np.float32).squeeze().reshape(-1, 1)
            if not cancelled
            else np.empty((0, 1), dtype=np.float32)
        )
        cache_source = str(prepared.cache_source or "fresh-generation")
        first_chunk_ms = prepared.synthesis_ms
        self.last_synthesis_ms = first_chunk_ms
        self.last_first_audio_ms = first_chunk_ms if pcm.size else None
        if pcm.size:
            yield SynthesisChunk(
                pcm=pcm,
                sample_rate=self.sample_rate,
                index=0,
                elapsed_ms=elapsed_ms,
            )
        return SynthesisResult(
            pcm=pcm,
            sample_rate=self.sample_rate,
            completion=completion,
            limits=SynthesisLimits(max_tokens=None, max_audio_seconds=None),
            timing=SynthesisTiming(
                first_chunk_ms=self.last_first_audio_ms,
                total_ms=elapsed_ms,
            ),
            diagnostics=SynthesisDiagnostics(
                backend=self.name,
                cache_source=cache_source,
                generation_profile=profile,
                seed=None,
                chunk_count=1 if pcm.size else 0,
                sample_count=len(pcm),
            ),
        )

    def speak(self, character, text, *, playback_guard=None):
        outcome = self.play_prepared(
            self.prepare_playback(character, text), playback_guard=playback_guard
        )
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "XTTS playback failed")
        return outcome.successful

    def play(self, prepared, *, playback_guard=None):
        typed = PreparedPlayback(
            prepared,
            self.last_synthesis_ms,
            self.last_first_audio_ms,
            getattr(self.voice_router.tts, "last_cache_source", None),
            f"live:{self.name}",
        )
        outcome = self.play_prepared(typed, playback_guard=playback_guard)
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "XTTS playback failed")
        return outcome.successful

    def play_prepared(self, prepared, *, playback_guard=None):
        return self.voice_router.play_prepared(prepared, playback_guard=playback_guard)

    def stop(self):
        return self.voice_router.tts.stop()

    @property
    def last_playback_underrun(self):
        return bool(getattr(self.voice_router.tts, "last_playback_underrun", False))


class ChatterboxNanoVoiceRouterBackend:
    """Low-latency English voice cloning with persistent voice conditioning."""

    name = "chatterbox-nano"
    generation_profile = "default"
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
        conditioning_cache_directory=None,
        persistent_audio_cache_directory=None,
        persistent_audio_cache_max_entries=None,
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
        self.torch_module = torch_module
        device = select_torch_device(torch_module)
        cpu_playback_headroom = (
            configure_cpu_synthesis_threads(torch_module) if device == "cpu" else True
        )
        self.cpu_normal_threads = get_torch_thread_count(torch_module)
        self.cpu_live_threads = (
            min(2, self.cpu_normal_threads) if self.cpu_normal_threads else None
        )
        self.live_mode_active = False
        self.model = model_factory(device=device, nano=True)
        self.device = device
        self.capabilities = SpeechBackendCapabilities(
            voice_cloning=True,
            streaming=False,
            # CPU inference can starve PortAudio and produce a continuous buzz.
            # Overlap is safe only when the runtime successfully reserves CPU
            # threads for OCR and the audio callback.
            concurrent_prepare_and_play=cpu_playback_headroom,
        )
        self.registry = registry
        self.narrator_speaker = "Chatterbox default"
        self.narrator_reference = narrator_reference
        self.audio_output = audio_output
        self.clock = clock
        self.playback_latency = playback_latency
        self.sample_rate = int(self.model.sr)
        self.default_conditionals = getattr(self.model, "conds", None)
        self.conditionals = {}
        self.conditioning_cache_directory = Path(
            conditioning_cache_directory
            or get_local_data_directory()
            / "models"
            / "chatterbox-nano"
            / "conditionals"
        ).expanduser()
        self.synthesis_lock = Lock()
        self.playback_lock = Lock()
        self.playback_state_lock = Lock()
        self.playback_active = False
        self.active_playback_stop = None
        self.last_playback_underrun = False
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None
        self.last_playback_ms = None
        self.audio_cache = BoundedCache(audio_cache_size)
        self.persistent_audio_cache = PersistentAudioCache(
            persistent_audio_cache_directory
            or get_local_data_directory() / "audio-cache" / self.name,
            max_entries=(
                max(64, self.audio_cache.max_entries * 8)
                if persistent_audio_cache_max_entries is None
                else persistent_audio_cache_max_entries
            ),
        )
        self.persistent_cache_keys = SpeechCacheKeyFactory(
            self.persistent_audio_cache,
            backend=self.name,
            model=self.model,
            sample_rate=self.sample_rate,
        )
        self.set_volume(volume)
        self.set_speed(1.0)

    def prepare(self, character, text):
        prepared = self.prepare_playback(character, text)
        self.last_synthesis_ms = prepared.synthesis_ms
        self.last_first_audio_ms = prepared.first_audio_ms
        return prepared.payload

    def prepare_playback(self, character, text):
        rendered = self.render(
            SynthesisRequest(
                voice=character,
                text=text,
                generation_profile=self.generation_profile,
            )
        ).collect()
        return PreparedPlayback(
            rendered.pcm.reshape(-1),
            rendered.timing.first_chunk_ms,
            None,
            rendered.diagnostics.cache_source,
            f"live:{self.name}",
        )

    def render(self, request):
        """Render Chatterbox PCM without importing or opening an audio device."""
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError(
                "Chatterbox Nano received an invalid render request"
            )
        spoken_text, profile, cache_policy = self._validate_render_request(request)
        return SynthesisChunkStream(
            self._render_chunks(request, spoken_text, profile, cache_policy)
        )

    def prime(self, character):
        """Load or create a speaker embedding before dialogue is complete."""
        with self.synthesis_lock:
            normalized_character = normalize_character_name(character) or "narrator"
            if normalized_character == "narrator":
                return False
            voice = self.registry.resolve(character)
            if voice is None or voice.speaker in self.conditionals:
                return False
            self._resolve_conditionals(character)
            return True

    def synthesize(self, character, text):
        return (
            self.render(
                SynthesisRequest(
                    voice=character,
                    text=text,
                    generation_profile=self.generation_profile,
                )
            )
            .collect()
            .pcm.reshape(-1)
        )

    def _validate_render_request(self, request):
        spoken_text = " ".join((request.text or "").split())
        if not spoken_text:
            raise TTSSynthesisError("Chatterbox Nano received empty text")
        profile = str(request.generation_profile or "default").strip().casefold()
        if profile != self.generation_profile:
            raise TTSConfigurationError(
                "Chatterbox Nano supports only the 'default' generation profile"
            )
        if request.seed is not None:
            raise TTSConfigurationError(
                "Chatterbox Nano does not expose deterministic seeded generation"
            )
        try:
            cache_policy = SynthesisCachePolicy(request.cache_policy)
        except ValueError as error:
            raise TTSConfigurationError(
                f"Unknown synthesis cache policy {request.cache_policy!r}"
            ) from error
        return spoken_text, profile, cache_policy

    def _render_chunks(self, request, spoken_text, profile, cache_policy):
        normalized_character = normalize_character_name(request.voice) or "narrator"
        cache_key = normalized_character, spoken_text
        persistent_key = self._persistent_cache_key(request.voice, spoken_text)
        started = self.clock()
        cache_source = "fresh-generation"
        audio = None
        with self.synthesis_lock:
            if cache_policy is SynthesisCachePolicy.USE:
                audio = self.audio_cache.get(cache_key)
                if audio is not None:
                    cache_source = "memory-cache"
                else:
                    audio = self.persistent_audio_cache.get(persistent_key)
                    if audio is not None:
                        cache_source = "persistent-cache"
                        self.audio_cache.put(cache_key, audio)
            if request.cancellation_requested():
                completion = SynthesisCompletion.CANCELLED
                audio = None
            else:
                completion = SynthesisCompletion.COMPLETE
            if audio is None and completion is SynthesisCompletion.COMPLETE:
                try:
                    self.model.conds = self._resolve_conditionals(request.voice)
                    generated = self.model.generate(spoken_text)
                    audio = generated.detach().cpu().numpy()
                except Exception as error:
                    raise TTSSynthesisError(str(error)) from error
                audio = np.asarray(audio, dtype=np.float32).squeeze()
                if request.cancellation_requested():
                    completion = SynthesisCompletion.CANCELLED
                    audio = None
                elif cache_policy is not SynthesisCachePolicy.BYPASS:
                    self.audio_cache.put(cache_key, audio)
                    self.persistent_audio_cache.put(persistent_key, audio)

        elapsed_ms = (self.clock() - started) * 1000
        first_chunk_ms = 0.0 if cache_source != "fresh-generation" else elapsed_ms
        self.last_synthesis_ms = first_chunk_ms
        self.last_first_audio_ms = first_chunk_ms
        pcm = (
            np.asarray(audio, dtype=np.float32).reshape(-1, 1)
            if audio is not None
            else np.empty((0, 1), dtype=np.float32)
        )
        if pcm.size:
            yield SynthesisChunk(
                pcm=pcm,
                sample_rate=self.sample_rate,
                index=0,
                elapsed_ms=elapsed_ms,
            )
        return SynthesisResult(
            pcm=pcm,
            sample_rate=self.sample_rate,
            completion=completion,
            limits=SynthesisLimits(max_tokens=None, max_audio_seconds=None),
            timing=SynthesisTiming(
                first_chunk_ms=first_chunk_ms if pcm.size else None,
                total_ms=elapsed_ms,
            ),
            diagnostics=SynthesisDiagnostics(
                backend=self.name,
                cache_source=cache_source,
                generation_profile=profile,
                seed=None,
                chunk_count=1 if pcm.size else 0,
                sample_count=len(pcm),
            ),
        )

    def speak(self, character, text, *, playback_guard=None):
        outcome = self.play_prepared(
            self.prepare_playback(character, text), playback_guard=playback_guard
        )
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "Chatterbox playback failed")
        return outcome.successful

    def play(self, prepared, *, playback_guard=None):
        typed = PreparedPlayback(
            prepared,
            self.last_synthesis_ms,
            self.last_first_audio_ms,
            None,
            f"live:{self.name}",
        )
        outcome = self.play_prepared(typed, playback_guard=playback_guard)
        self.last_playback_ms = outcome.playback_ms
        self.last_playback_underrun = outcome.underflowed
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "Chatterbox playback failed")
        return outcome.successful

    def play_prepared(self, prepared, *, playback_guard=None):
        if not isinstance(prepared, PreparedPlayback):
            raise TTSConfigurationError("Chatterbox received invalid playback")
        if playback_guard is not None and not playback_guard():
            return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
            stop_requested = Event()
            started = self.clock()
            underflowed = False
            first_audio_ms = None
            try:
                with self.playback_state_lock:
                    self.playback_active = True
                    self.active_playback_stop = stop_requested
                audio_output = self._resolve_audio_output()
                audio, playback_sample_rate = match_output_sample_rate(
                    audio_output,
                    self._prepare_audio(prepared.payload),
                    self.sample_rate,
                )
                with self.playback_state_lock:
                    interrupted = stop_requested.is_set() or (
                        playback_guard is not None and not playback_guard()
                    )
                    if not interrupted:
                        audio_output.play(
                            audio,
                            playback_sample_rate,
                            latency=self.playback_latency,
                        )
                        first_audio_ms = (self.clock() - started) * 1000
                if not interrupted:
                    playback_status = audio_output.wait()
                    underflowed = self._playback_underflowed(playback_status)
                    interrupted = stop_requested.is_set() or (
                        playback_guard is not None and not playback_guard()
                    )
            except Exception as error:
                if stop_requested.is_set():
                    return outcome_for_prepared(
                        prepared,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        first_audio_ms=first_audio_ms,
                    )
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.FAILED,
                    (self.clock() - started) * 1000,
                    error=str(error),
                    error_type=type(error),
                )
            finally:
                with self.playback_state_lock:
                    self.playback_active = False
                    if self.active_playback_stop is stop_requested:
                        self.active_playback_stop = None
        return outcome_for_prepared(
            prepared,
            PlaybackStatus.INTERRUPTED if interrupted else PlaybackStatus.COMPLETED,
            (self.clock() - started) * 1000,
            underflowed=underflowed,
            first_audio_ms=first_audio_ms,
        )

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
        self.volume = validate_volume(volume)

    def set_speed(self, speed):
        # Nano does not currently expose a pitch-preserving speed control.
        self.speed = validate_speed(speed)

    def set_live_mode_active(self, active):
        self.live_mode_active = bool(active)
        if self.device != "cpu":
            return self.live_mode_active
        target = (
            self.cpu_live_threads if self.live_mode_active else self.cpu_normal_threads
        )
        if target is not None:
            self.torch_module.set_num_threads(target)
        return self.live_mode_active

    def stop(self):
        with self.playback_state_lock:
            was_playing = self.playback_active
            stop_requested = self.active_playback_stop
            if was_playing and stop_requested is not None:
                stop_requested.set()
            if was_playing and self.audio_output is not None:
                self.audio_output.stop()
        return was_playing

    def _resolve_audio_output(self):
        self.audio_output = resolve_audio_output(self.audio_output)
        return self.audio_output

    def _playback_underflowed(self, playback_status=None):
        return playback_underflowed(self.audio_output, playback_status)

    def _resolve_conditionals(self, character):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            if self.narrator_reference:
                return self._prepare_conditionals(
                    "narrator",
                    self.narrator_reference,
                )
            if self.default_conditionals is not None:
                return self.default_conditionals
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
        cache_path = self._conditioning_cache_path(key, reference)
        cached = self._load_conditionals(cache_path)
        if cached is not None:
            self.conditionals[key] = cached
            return cached

        self.model.prepare_conditionals(str(reference))
        conditionals = self.model.conds
        self.conditionals[key] = conditionals
        self._save_conditionals(conditionals, cache_path)
        return conditionals

    def _conditioning_cache_path(self, key, reference):
        reference = Path(reference).expanduser().resolve()
        model_identity = (
            f"{type(self.model).__module__}.{type(self.model).__qualname__}:"
            f"{getattr(self.model, 'model_label', 'nano')}:{self.sample_rate}"
        )
        return voice_artifact_cache_path(
            self.conditioning_cache_directory,
            voice_key=key,
            source=reference,
            model_identity=model_identity,
            suffix=".pt",
        )

    def _load_conditionals(self, cache_path):
        if not cache_path.is_file() or self.default_conditionals is None:
            return None
        loader = getattr(type(self.default_conditionals), "load", None)
        if not callable(loader):
            return None
        try:
            return loader(cache_path, map_location=self.device)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def _save_conditionals(self, conditionals, cache_path):
        save = getattr(conditionals, "save", None)
        if not callable(save):
            return False
        try:
            with atomic_output_path(cache_path) as temporary_path:
                save(temporary_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _persistent_cache_key(self, character, text):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            voice_key = "narrator"
            source = self.narrator_reference or "embedded-default"
        else:
            voice_key = voice.speaker
            source = voice.references[0] if voice.references else "missing-reference"
        return self.persistent_cache_keys.key(
            voice_key=voice_key,
            source=source,
            text=text,
            speed=self.speed,
        )

    def _prepare_audio(self, audio, fade_seconds=0.01):
        prepared = np.asarray(audio, dtype=np.float32).squeeze().copy()
        if prepared.ndim != 1 or len(prepared) < 4:
            prepared *= self.volume
            return prepared

        # Model output can contain a small DC offset, non-finite samples, or
        # peaks outside the float audio range. At full application volume those
        # become audible hum or clipping distortion in CoreAudio/PortAudio.
        np.nan_to_num(prepared, copy=False)
        dc_offset = float(np.mean(prepared, dtype=np.float64))
        if abs(dc_offset) > 1e-4:
            prepared -= dc_offset
        prepared *= self.volume
        peak = float(np.max(np.abs(prepared)))
        if peak > 0.95:
            prepared *= 0.95 / peak

        fade_samples = min(round(self.sample_rate * fade_seconds), len(prepared) // 2)
        if fade_samples >= 2:
            fade = np.linspace(0.0, 1.0, fade_samples, dtype=prepared.dtype)
            prepared[:fade_samples] *= fade
            prepared[-fade_samples:] *= fade[::-1]
        return prepared


def _install_pocket_generation_cancellation(model, cancel_event_provider):
    """Patch Pocket TTS 2.1's private latent loop with cooperative cancellation."""
    required = (
        "_autoregressive_generation",
        "_run_flow_lm_and_increment_step",
        "flow_lm",
    )
    if not all(hasattr(model, name) for name in required):
        return False
    try:
        import torch
    except ImportError:
        return False

    def cancellable_generation(
        pocket_model,
        model_state,
        max_gen_len,
        frames_after_eos,
        latents_queue,
    ):
        cancel_event = cancel_event_provider()
        backbone_input = torch.full(
            (1, 1, pocket_model.flow_lm.ldim),
            fill_value=float("NaN"),
            device=next(iter(pocket_model.flow_lm.parameters())).device,
            dtype=pocket_model.flow_lm.dtype,
        )
        eos_step = None
        with torch.no_grad():
            for generation_step in range(max_gen_len):
                if cancel_event is not None and cancel_event.is_set():
                    break
                next_latent, is_eos = pocket_model._run_flow_lm_and_increment_step(
                    model_state=model_state,
                    backbone_input_latents=backbone_input,
                )
                if cancel_event is not None and cancel_event.is_set():
                    break
                if is_eos.item() and eos_step is None:
                    eos_step = generation_step
                if (
                    eos_step is not None
                    and generation_step >= eos_step + frames_after_eos
                ):
                    break
                latents_queue.put(next_latent)
                backbone_input = next_latent
        latents_queue.put(None)

    model._autoregressive_generation = MethodType(cancellable_generation, model)
    return True


class PocketTTSVoiceRouterBackend:
    """Experimental CPU voice cloning with first-chunk streaming playback."""

    name = "pocket-tts"
    generation_profile = "default"
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=True,
        # Voice-state preparation and streaming use the same model instance.
        concurrent_prepare_and_play=False,
        # OCR may briefly replace a stable dialogue while the game is still
        # drawing it. Finish audio that already started; Skip/Pause/Emergency
        # Stop remain explicit interruption paths.
        interrupt_on_dialog_replacement=False,
    )

    def __init__(
        self,
        registry,
        *,
        narrator_reference=None,
        volume=1.0,
        model_factory=None,
        state_exporter=None,
        audio_output=None,
        clock=monotonic,
        audio_cache_size=32,
        playback_latency="low",
        runtime_directory=None,
        voice_state_cache_directory=None,
        persistent_audio_cache_directory=None,
        persistent_audio_cache_max_entries=None,
        cached_stream_chunk_seconds=0.2,
        stream_prefill_seconds=0.25,
    ):
        if model_factory is None:
            runtime_site_packages = activate_pocket_tts_runtime(runtime_directory)
            try:
                from pocket_tts import TTSModel, export_model_state
            except ImportError as error:
                raise TTSConfigurationError(
                    "Pocket TTS could not be imported from "
                    f"{runtime_site_packages}. Reinstall it with "
                    "`uv sync --project backends/pocket-tts`."
                ) from error
            model_factory = TTSModel.load_model
            state_exporter = export_model_state
        self.model = model_factory()
        self.registry = registry
        self.narrator_speaker = "Pocket TTS default"
        self.narrator_reference = narrator_reference or "alba"
        self.state_exporter = state_exporter
        self.audio_output = audio_output
        self.clock = clock
        self.playback_latency = playback_latency
        self.sample_rate = int(self.model.sample_rate)
        self.voice_states = {}
        self.voice_state_cache_directory = Path(
            voice_state_cache_directory
            or get_local_data_directory() / "models" / "pocket-tts" / "voices"
        ).expanduser()
        self.model_lock = Lock()
        self.playback_lock = Lock()
        self.active_stream_lock = Lock()
        self.active_stream = None
        self.active_generation_cancel = None
        self.cooperative_generation_cancellation = (
            _install_pocket_generation_cancellation(
                self.model, lambda: self.active_generation_cancel
            )
        )
        self.playback_stop = Event()
        self.playback_active = False
        self.last_playback_underrun = False
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None
        self.last_playback_ms = None
        self.audio_cache = BoundedCache(audio_cache_size)
        self.persistent_audio_cache = PersistentAudioCache(
            persistent_audio_cache_directory
            or get_local_data_directory() / "audio-cache" / self.name,
            max_entries=(
                max(64, self.audio_cache.max_entries * 8)
                if persistent_audio_cache_max_entries is None
                else persistent_audio_cache_max_entries
            ),
        )
        self.persistent_cache_keys = SpeechCacheKeyFactory(
            self.persistent_audio_cache,
            backend=self.name,
            model=self.model,
            sample_rate=self.sample_rate,
        )
        self.cached_stream_chunk_samples = max(
            1,
            round(self.sample_rate * float(cached_stream_chunk_seconds)),
        )
        self.stream_prefill_samples = max(
            1,
            round(self.sample_rate * float(stream_prefill_seconds)),
        )
        self.set_volume(volume)
        self.set_speed(1.0)

    def prepare(self, character, text):
        prepared = self.prepare_playback(character, text)
        self.last_synthesis_ms = prepared.synthesis_ms
        self.last_first_audio_ms = prepared.first_audio_ms
        return prepared.payload

    def prepare_playback(self, character, text):
        payload = self._prepare_request(
            SynthesisRequest(
                voice=character,
                text=text,
                generation_profile=self.generation_profile,
            )
        )
        first_audio_ms = 0.0 if payload.cached_audio is not None else None
        return PreparedPlayback(
            payload,
            first_audio_ms,
            None,
            payload.cache_source,
            f"live:{self.name}",
        )

    def render(self, request):
        """Render Pocket TTS PCM without opening an audio device."""
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError("Pocket TTS received an invalid render request")
        prepared = self._prepare_request(request)
        self.playback_stop.clear()
        return self._render_prepared(prepared, request)

    def _prepare_request(self, request):
        spoken_text = " ".join((request.text or "").split())
        if not spoken_text:
            raise TTSSynthesisError("Pocket TTS received empty text")
        profile = str(request.generation_profile or "default").strip().casefold()
        if profile != self.generation_profile:
            raise TTSConfigurationError(
                "Pocket TTS supports only the 'default' generation profile"
            )
        if request.seed is not None:
            raise TTSConfigurationError(
                "Pocket TTS does not expose deterministic seeded generation"
            )
        try:
            cache_policy = SynthesisCachePolicy(request.cache_policy)
        except ValueError as error:
            raise TTSConfigurationError(
                f"Unknown synthesis cache policy {request.cache_policy!r}"
            ) from error
        voice_key, source = self._resolve_voice_source(request.voice)
        cache_key = voice_key, spoken_text
        persistent_key = self._persistent_cache_key(voice_key, spoken_text, source)
        may_read_cache = cache_policy is SynthesisCachePolicy.USE
        cached_audio = self.audio_cache.get(cache_key) if may_read_cache else None
        cache_source = "memory-cache" if cached_audio is not None else None
        if cached_audio is None and may_read_cache:
            cached_audio = self.persistent_audio_cache.get(persistent_key)
            if cached_audio is not None:
                cache_source = "persistent-cache"
                self.audio_cache.put(cache_key, cached_audio)
        voice_state = None
        if cached_audio is None:
            cache_source = "fresh-generation"
            with self.model_lock:
                resolved_voice_key, voice_state = self._resolve_voice_state(
                    request.voice
                )
            if resolved_voice_key != voice_key:
                raise TTSConfigurationError(
                    "Pocket TTS resolved inconsistent voice state"
                )
        return PocketTTSPreparedSpeech(
            voice_key,
            voice_state,
            spoken_text,
            cache_key,
            persistent_key,
            cached_audio,
            cache_source,
            profile,
            cache_policy,
        )

    def _render_prepared(self, prepared, request):
        return SynthesisChunkStream(self._render_chunks(prepared, request))

    def _render_chunks(self, prepared, request):
        started = self.clock()
        first_chunk_ms = None
        chunks = []
        completion = SynthesisCompletion.COMPLETE
        render_finished = False
        generation_cancel = (
            request.cancellation
            if callable(getattr(request.cancellation, "set", None))
            and callable(getattr(request.cancellation, "is_set", None))
            else Event()
        )
        self.active_generation_cancel = generation_cancel
        self.last_synthesis_ms = 0.0 if prepared.cached_audio is not None else None
        self.last_first_audio_ms = self.last_synthesis_ms

        def cancelled():
            return self.playback_stop.is_set() or request.cancellation_requested()

        def finish(final_completion):
            pcm = (
                np.concatenate(chunks, axis=0)
                if chunks
                else np.empty((0, 1), dtype=np.float32)
            )
            if (
                final_completion is SynthesisCompletion.COMPLETE
                and prepared.cached_audio is None
                and prepared.cache_policy is not SynthesisCachePolicy.BYPASS
            ):
                cached_audio = pcm.reshape(-1)
                self.audio_cache.put(prepared.cache_key, cached_audio)
                self.persistent_audio_cache.put(
                    prepared.persistent_cache_key,
                    cached_audio,
                )
            return SynthesisResult(
                pcm=pcm,
                sample_rate=self.sample_rate,
                completion=final_completion,
                limits=SynthesisLimits(
                    max_tokens=None,
                    max_audio_seconds=None,
                ),
                timing=SynthesisTiming(
                    first_chunk_ms=first_chunk_ms,
                    total_ms=(self.clock() - started) * 1000,
                ),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source=prepared.cache_source,
                    generation_profile=prepared.generation_profile,
                    seed=None,
                    chunk_count=len(chunks),
                    sample_count=len(pcm),
                ),
            )

        try:
            if prepared.cached_audio is not None:
                source_chunks = self._cached_chunks(prepared.cached_audio)
                first_chunk_ms = 0.0
            else:

                def generated_chunks():
                    with self.model_lock:
                        yield from self.model.generate_audio_stream(
                            prepared.voice_state,
                            prepared.text,
                        )

                source_chunks = generated_chunks()

            for raw in source_chunks:
                if cancelled():
                    generation_cancel.set()
                    completion = SynthesisCompletion.CANCELLED
                    break
                audio = self._to_numpy(raw).reshape(-1, 1)
                if not audio.size:
                    continue
                if first_chunk_ms is None:
                    first_chunk_ms = (self.clock() - started) * 1000
                    self.last_synthesis_ms = first_chunk_ms
                    self.last_first_audio_ms = first_chunk_ms
                chunks.append(audio)
                yield SynthesisChunk(
                    pcm=audio,
                    sample_rate=self.sample_rate,
                    index=len(chunks) - 1,
                    elapsed_ms=(self.clock() - started) * 1000,
                )
            if cancelled() and completion is SynthesisCompletion.COMPLETE:
                generation_cancel.set()
                completion = SynthesisCompletion.CANCELLED
            if not chunks and completion is SynthesisCompletion.COMPLETE:
                raise TTSSynthesisError("Pocket TTS generated no audio")
            result = finish(completion)
            render_finished = True
            return result
        except (TTSConfigurationError, TTSSynthesisError):
            raise
        except Exception as error:
            if cancelled():
                generation_cancel.set()
                result = finish(SynthesisCompletion.CANCELLED)
                render_finished = True
                return result
            raise TTSSynthesisError(str(error)) from error
        finally:
            if not render_finished:
                generation_cancel.set()
            if self.active_generation_cancel is generation_cancel:
                self.active_generation_cancel = None

    def prime(self, character):
        voice_key, _source = self._resolve_voice_source(character)
        with self.model_lock:
            if voice_key in self.voice_states:
                return False
            self._resolve_voice_state(character)
        return True

    def speak(self, character, text, *, playback_guard=None):
        outcome = self.play_prepared(
            self.prepare_playback(character, text), playback_guard=playback_guard
        )
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "Pocket TTS playback failed")
        return outcome.successful

    def play(self, prepared, *, playback_guard=None):
        typed = PreparedPlayback(
            prepared,
            0.0 if getattr(prepared, "cached_audio", None) is not None else None,
            0.0 if getattr(prepared, "cached_audio", None) is not None else None,
            getattr(prepared, "cache_source", None),
            f"live:{self.name}",
        )
        outcome = self.play_prepared(typed, playback_guard=playback_guard)
        self.last_playback_ms = outcome.playback_ms
        self.last_playback_underrun = outcome.underflowed
        self.last_synthesis_ms = outcome.synthesis_ms
        self.last_first_audio_ms = outcome.first_audio_ms
        if outcome.status is PlaybackStatus.FAILED:
            raise AudioPlaybackError(outcome.error or "Pocket TTS playback failed")
        return outcome.successful

    def play_prepared(self, prepared, *, playback_guard=None):
        if playback_guard is not None and not playback_guard():
            return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
        if not isinstance(prepared, PreparedPlayback) or not isinstance(
            prepared.payload, PocketTTSPreparedSpeech
        ):
            raise TTSConfigurationError("Pocket TTS received invalid prepared speech")
        payload = prepared.payload

        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
            self.playback_stop.clear()
            started = self.clock()
            rendered = None
            underflowed = False
            try:
                self.playback_active = True
                request = SynthesisRequest(
                    voice=payload.voice_key,
                    text=payload.text,
                    generation_profile=payload.generation_profile,
                    cancellation=(
                        None if playback_guard is None else lambda: not playback_guard()
                    ),
                    cache_policy=payload.cache_policy,
                )
                playback_prepared = payload
                if (
                    payload.cached_audio is None
                    and payload.cache_policy is not SynthesisCachePolicy.BYPASS
                ):
                    playback_prepared = replace(
                        payload,
                        cache_policy=SynthesisCachePolicy.BYPASS,
                    )
                rendered = self._render_prepared(playback_prepared, request)
                with self._resolve_audio_output().OutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    latency=self.playback_latency,
                ) as stream:
                    with self.active_stream_lock:
                        self.active_stream = stream
                    chunks = rendered
                    if payload.cached_audio is None:
                        chunks = self._prefill_rendered_chunks(rendered)
                    completed, underflowed, _first_write_ms = self._write_chunks(
                        stream,
                        (chunk.pcm for chunk in chunks),
                        playback_guard,
                        started=started,
                    )
                result = rendered.result
                completed = completed and result.completion is not (
                    SynthesisCompletion.CANCELLED
                )
                if (
                    completed
                    and result.completion is SynthesisCompletion.COMPLETE
                    and payload.cached_audio is None
                    and payload.cache_policy is not SynthesisCachePolicy.BYPASS
                ):
                    cached_audio = result.pcm.reshape(-1)
                    self.audio_cache.put(payload.cache_key, cached_audio)
                    self.persistent_audio_cache.put(
                        payload.persistent_cache_key,
                        cached_audio,
                    )
                resolved = replace(
                    prepared,
                    synthesis_ms=result.timing.first_chunk_ms,
                    first_audio_ms=_first_write_ms,
                    cache_source=result.diagnostics.cache_source,
                )
                return outcome_for_prepared(
                    resolved,
                    (
                        PlaybackStatus.COMPLETED
                        if completed
                        else PlaybackStatus.INTERRUPTED
                    ),
                    (self.clock() - started) * 1000,
                    underflowed=underflowed,
                    first_audio_ms=_first_write_ms,
                )
            except Exception as error:
                if self.playback_stop.is_set():
                    return outcome_for_prepared(
                        prepared,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        underflowed=underflowed,
                    )
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.FAILED,
                    (self.clock() - started) * 1000,
                    underflowed=underflowed,
                    error=str(error),
                    error_type=type(error),
                )
            finally:
                if rendered is not None:
                    rendered.close()
                with self.active_stream_lock:
                    self.active_stream = None
                self.playback_active = False

    def _prefill_rendered_chunks(self, chunks):
        buffered = []
        buffered_samples = 0
        for chunk in chunks:
            buffered.append(chunk)
            buffered_samples += len(chunk.pcm)
            if buffered_samples >= self.stream_prefill_samples:
                first = buffered[0]
                yield SynthesisChunk(
                    pcm=np.concatenate([item.pcm for item in buffered], axis=0),
                    sample_rate=self.sample_rate,
                    index=first.index,
                    elapsed_ms=chunk.elapsed_ms,
                )
                buffered.clear()
                break
        if buffered:
            first = buffered[0]
            yield SynthesisChunk(
                pcm=np.concatenate([item.pcm for item in buffered], axis=0),
                sample_rate=self.sample_rate,
                index=first.index,
                elapsed_ms=buffered[-1].elapsed_ms,
            )
        for chunk in chunks:
            yield chunk

    def _resolve_audio_output(self):
        self.audio_output = resolve_audio_output(self.audio_output)
        return self.audio_output

    def warm_up(self, *, progress=None, text=None):
        del text
        progress = progress or (lambda _current, _total, _character: None)
        voices = sorted(
            {id(voice): voice for voice in self.registry.voices.values()}.values(),
            key=lambda voice: voice.character.casefold(),
        )
        characters = ["Narrator", *(voice.character for voice in voices)]
        for current, character in enumerate(characters, start=1):
            progress(current, len(characters), character)
            self.prime(character)
        return len(characters)

    def set_volume(self, volume):
        self.volume = validate_volume(volume)

    def set_speed(self, speed):
        # Pocket TTS 2.1 has no pitch-preserving speed control.
        self.speed = validate_speed(speed)

    def set_live_mode_active(self, active):
        return bool(active)

    def stop(self):
        was_playing = self.playback_active
        self.playback_stop.set()
        generation_cancel = self.active_generation_cancel
        if generation_cancel is not None:
            generation_cancel.set()
        with self.active_stream_lock:
            stream = self.active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        return was_playing

    def _write_chunks(
        self,
        stream,
        chunks,
        playback_guard,
        *,
        started=None,
        raw_chunks=None,
    ):
        wrote_audio = False
        cancelled = False
        underflowed_any = False
        first_audio_ms = None
        for chunk in chunks:
            if self.playback_stop.is_set():
                cancelled = True
                continue
            if playback_guard is not None and not playback_guard():
                generation_cancel = self.active_generation_cancel
                if generation_cancel is not None:
                    generation_cancel.set()
                cancelled = True
                continue
            raw = self._to_numpy(chunk)
            if raw.size == 0:
                continue
            if raw_chunks is not None:
                raw_chunks.append(raw)
            if not wrote_audio and started is not None:
                first_audio_ms = (self.clock() - started) * 1000
            underflowed = stream.write(self._prepare_audio(raw).reshape(-1, 1))
            underflowed_any = underflowed_any or bool(underflowed)
            wrote_audio = True
        if cancelled:
            return False, underflowed_any, first_audio_ms
        if not wrote_audio:
            raise TTSSynthesisError("Pocket TTS generated no audio")
        return True, underflowed_any, first_audio_ms

    def _resolve_voice_state(self, character):
        voice_key, source = self._resolve_voice_source(character)
        cached = self.voice_states.get(voice_key)
        if cached is not None:
            return voice_key, cached

        cache_path = self._voice_state_cache_path(voice_key, source)
        if cache_path.is_file():
            try:
                state = self.model.get_state_for_audio_prompt(str(cache_path))
            except (OSError, RuntimeError, TypeError, ValueError):
                state = None
            if state is not None:
                self.voice_states[voice_key] = state
                return voice_key, state

        try:
            state = self.model.get_state_for_audio_prompt(str(source))
        except Exception as error:
            message = str(error)
            if "accept the terms" in message and "voice cloning" in message:
                raise TTSConfigurationError(
                    "Pocket TTS voice cloning is locked. Accept the model terms at "
                    "https://huggingface.co/kyutai/pocket-tts, authenticate with "
                    "`uvx hf auth login`, and restart the app."
                ) from error
            raise TTSConfigurationError(
                f"Pocket TTS could not prepare voice {voice_key!r}: {message}"
            ) from error
        self.voice_states[voice_key] = state
        self._save_voice_state(state, cache_path)
        return voice_key, state

    def _resolve_voice_source(self, character):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            return "narrator", self.narrator_reference
        if not voice.references:
            if voice.speaker in pocket_tts_preset_voices:
                return voice.speaker, voice.speaker
            raise TTSConfigurationError(
                f"Voice {voice.character!r} has no reference recording"
            )
        return voice.speaker, voice.references[0]

    def _voice_state_cache_path(self, voice_key, source):
        model_identity = (
            f"{type(self.model).__module__}.{type(self.model).__qualname__}:"
            f"{self.sample_rate}"
        )
        return voice_artifact_cache_path(
            self.voice_state_cache_directory,
            voice_key=voice_key,
            source=source,
            model_identity=model_identity,
            suffix=".safetensors",
        )

    def _save_voice_state(self, state, cache_path):
        if not callable(self.state_exporter):
            return False
        try:
            with atomic_output_path(cache_path) as temporary_path:
                self.state_exporter(state, temporary_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _persistent_cache_key(self, voice_key, text, source):
        return self.persistent_cache_keys.key(
            voice_key=voice_key,
            source=source,
            text=text,
            speed=self.speed,
        )

    def _cached_chunks(self, audio):
        for start in range(0, len(audio), self.cached_stream_chunk_samples):
            yield audio[start : start + self.cached_stream_chunk_samples]

    def _to_numpy(self, chunk):
        value = chunk
        detach = getattr(value, "detach", None)
        if callable(detach):
            value = detach()
        cpu = getattr(value, "cpu", None)
        if callable(cpu):
            value = cpu()
        to_numpy = getattr(value, "numpy", None)
        if callable(to_numpy):
            value = to_numpy()
        return np.asarray(value, dtype=np.float32).squeeze()

    def _prepare_audio(self, audio):
        prepared = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
        np.nan_to_num(prepared, copy=False)
        prepared *= self.volume
        np.clip(prepared, -0.95, 0.95, out=prepared)
        return prepared


class MossTTSVoiceRouterBackend:
    """High-fidelity Apple Silicon voice cloning with streaming playback."""

    name = "moss-tts"
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=True,
        concurrent_prepare_and_play=False,
        interrupt_on_dialog_replacement=True,
    )

    def __init__(
        self,
        registry,
        *,
        narrator_reference=None,
        language="English",
        model_name=None,
        volume=1.0,
        model_factory=None,
        audio_output=None,
        clock=monotonic,
        audio_cache_size=32,
        playback_latency="low",
        runtime_directory=None,
        prompt_cache_directory=None,
        persistent_audio_cache_directory=None,
        persistent_audio_cache_max_entries=None,
        prompt_code_loader=None,
        prompt_code_saver=None,
        array_evaluator=None,
        cached_stream_chunk_seconds=0.2,
        streaming_first_chunk_frames=4,
        streaming_interval=0.25,
        generation_profile="stable",
        playback_consumer_join_timeout=5.0,
    ):
        self.model_name = str(
            model_name
            or os.environ.get("VNTTS_MOSS_MODEL", "")
            or default_moss_tts_model
        )
        self._mlx = None
        if model_factory is None:
            runtime_site_packages = activate_moss_tts_runtime(runtime_directory)
            try:
                import mlx.core as mx
                from mlx_audio.tts import load
            except ImportError as error:
                raise TTSConfigurationError(
                    "MOSS-TTS could not be imported from "
                    f"{runtime_site_packages}. Reinstall it with "
                    "`uv sync --project backends/moss-tts`."
                ) from error
            from vntts.moss_compat import install_moss_quantized_codec_compat

            install_moss_quantized_codec_compat()
            self._mlx = mx
            model_factory = load
        try:
            # VNTTS constructs the backend on its startup worker and performs
            # live generation on a playback worker. MLX lazy parameters retain
            # the thread-local stream that created their graph, then fail on
            # first use in the other worker with "There is no Stream(...) in
            # current thread". Materialize the model before it crosses that
            # boundary; streamed audio generation remains lazy and incremental.
            self.model = model_factory(self.model_name, lazy=False)
        except Exception as error:
            raise TTSConfigurationError(
                f"MOSS-TTS could not load {self.model_name!r}: {error}"
            ) from error
        self.registry = registry
        self.narrator_speaker = "MOSS reference voice"
        self.narrator_reference = narrator_reference
        self.language = normalize_moss_language(language)
        (
            self.generation_profile,
            self.generation_options,
        ) = get_moss_tts_generation_profile(generation_profile)
        self.audio_output = audio_output
        self.clock = clock
        self.playback_latency = playback_latency
        self.sample_rate = int(getattr(self.model, "sample_rate", 48_000))
        self.prompt_audio_codes = {}
        self.prompt_cache_directory = Path(
            prompt_cache_directory
            or get_local_data_directory() / "models" / "moss-tts" / "voices"
        ).expanduser()
        self.prompt_code_loader = prompt_code_loader or self._load_prompt_codes
        self.prompt_code_saver = prompt_code_saver or self._save_prompt_codes
        self.array_evaluator = array_evaluator or self._evaluate_array
        self.model_lock = Lock()
        self.playback_lock = Lock()
        self.active_stream_lock = Lock()
        self.active_stream = None
        self.active_generation = None
        self.playback_stop = Event()
        self.playback_cancel_requested = Event()
        self.playback_active = False
        self.last_playback_underrun = False
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None
        self.last_playback_ms = None
        self.last_audio_source = None
        self.last_generation_limited = False
        self.last_generated_audio = None
        self.audio_cache = BoundedCache(audio_cache_size)
        self.persistent_audio_cache = PersistentAudioCache(
            persistent_audio_cache_directory
            or get_local_data_directory() / "audio-cache" / self.name,
            max_entries=(
                max(64, self.audio_cache.max_entries * 8)
                if persistent_audio_cache_max_entries is None
                else persistent_audio_cache_max_entries
            ),
        )
        self.persistent_cache_keys = SpeechCacheKeyFactory(
            self.persistent_audio_cache,
            backend=self.name,
            model=self.model,
            model_identity=self.model_name,
            sample_rate=self.sample_rate,
        )
        self.cached_stream_chunk_samples = max(
            1,
            round(self.sample_rate * float(cached_stream_chunk_seconds)),
        )
        self.streaming_first_chunk_frames = max(1, int(streaming_first_chunk_frames))
        self.streaming_interval = max(0.08, float(streaming_interval))
        self.playback_consumer_join_timeout = max(
            0.01, float(playback_consumer_join_timeout)
        )
        self.set_volume(volume)
        self.set_speed(1.0)

    def prepare(self, character, text):
        prepared = self.prepare_playback(character, text)
        self.last_synthesis_ms = prepared.synthesis_ms
        self.last_first_audio_ms = prepared.first_audio_ms
        self.last_audio_source = prepared.audio_source
        return prepared.payload

    def prepare_playback(self, character, text):
        payload = self._prepare_request(
            SynthesisRequest(
                voice=character,
                text=text,
                generation_profile=self.generation_profile,
            )
        )
        first_audio_ms = 0.0 if payload.cached_audio is not None else None
        return PreparedPlayback(
            payload,
            first_audio_ms,
            None,
            payload.cache_source,
            f"moss-tts:{payload.cache_source}",
        )

    def render(self, request):
        """Render PCM without opening an audio device."""
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError("MOSS-TTS received an invalid render request")
        prepared = self._prepare_request(request)
        self.playback_stop.clear()
        return self._render_prepared(prepared, request)

    def _prepare_request(self, request):
        spoken_text = normalize_short_trailing_ellipsis(
            " ".join((request.text or "").split())
        )
        if not spoken_text:
            raise TTSSynthesisError("MOSS-TTS received empty text")
        profile, generation_options = get_moss_tts_generation_profile(
            request.generation_profile
        )
        try:
            cache_policy = SynthesisCachePolicy(request.cache_policy)
        except ValueError as error:
            raise TTSConfigurationError(
                f"Unknown synthesis cache policy {request.cache_policy!r}"
            ) from error
        voice_key, source = self._resolve_voice_source(request.voice)
        max_tokens, max_audio_seconds = moss_generation_limits(spoken_text)
        cache_key = voice_key, spoken_text, profile, request.seed
        persistent_key = self._persistent_cache_key(
            voice_key,
            spoken_text,
            source,
            seed=request.seed,
            generation_profile=profile,
            generation_options=generation_options,
        )
        may_read_cache = cache_policy is SynthesisCachePolicy.USE
        cached_audio = self.audio_cache.get(cache_key) if may_read_cache else None
        cache_source = "memory-cache" if cached_audio is not None else None
        if cached_audio is None and may_read_cache:
            cached_audio = self.persistent_audio_cache.get(persistent_key)
            if cached_audio is not None:
                cache_source = "persistent-cache"
                self.audio_cache.put(cache_key, cached_audio)
        prompt_audio_codes = None
        if cached_audio is None:
            cache_source = "fresh-generation"
            with self.model_lock:
                resolved_voice_key, prompt_audio_codes = self._resolve_prompt_codes(
                    request.voice
                )
            if resolved_voice_key != voice_key:
                raise TTSConfigurationError(
                    "MOSS-TTS resolved inconsistent voice conditioning"
                )
        self.last_audio_source = f"moss-tts:{cache_source}"
        return MossTTSPreparedSpeech(
            voice_key,
            prompt_audio_codes,
            spoken_text,
            cache_key,
            persistent_key,
            max_tokens,
            max_audio_seconds,
            cached_audio,
            cache_source,
            request.seed,
            profile,
            tuple(sorted(generation_options.items())),
            cache_policy,
        )

    def _render_prepared(self, prepared, request):
        return SynthesisChunkStream(self._render_chunks(prepared, request))

    def _render_chunks(self, prepared, request):
        started = self.clock()
        first_chunk_ms = None
        chunks = []
        completion = SynthesisCompletion.COMPLETE
        emitted_samples = 0
        max_samples = max(1, round(self.sample_rate * prepared.max_audio_seconds))
        generation = None
        cache_source = prepared.cache_source
        self.last_generation_limited = False
        self.last_generated_audio = prepared.cached_audio
        self.last_synthesis_ms = 0.0 if prepared.cached_audio is not None else None
        self.last_first_audio_ms = self.last_synthesis_ms

        def cancelled():
            return self.playback_stop.is_set() or request.cancellation_requested()

        def bounded(audio):
            nonlocal emitted_samples, completion
            remaining = max_samples - emitted_samples
            if remaining <= 0:
                completion = SynthesisCompletion.LIMITED
                return None
            if len(audio) > remaining:
                audio = audio[:remaining]
                completion = SynthesisCompletion.LIMITED
            emitted_samples += len(audio)
            return audio

        def finish(final_completion):
            channels = chunks[0].shape[1] if chunks else 1
            pcm = (
                np.concatenate(chunks, axis=0)
                if chunks
                else np.empty((0, channels), dtype=np.float32)
            )
            self.last_generation_limited = (
                final_completion is SynthesisCompletion.LIMITED
            )
            self.last_generated_audio = pcm if chunks else None
            if (
                final_completion is SynthesisCompletion.COMPLETE
                and prepared.cached_audio is None
                and prepared.cache_policy is not SynthesisCachePolicy.BYPASS
            ):
                self.audio_cache.put(prepared.cache_key, pcm)
                self.persistent_audio_cache.put(prepared.persistent_cache_key, pcm)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=self.sample_rate,
                completion=final_completion,
                limits=SynthesisLimits(
                    max_tokens=prepared.max_tokens,
                    max_audio_seconds=prepared.max_audio_seconds,
                ),
                timing=SynthesisTiming(
                    first_chunk_ms=first_chunk_ms,
                    total_ms=(self.clock() - started) * 1000,
                ),
                diagnostics=SynthesisDiagnostics(
                    backend=self.name,
                    cache_source=cache_source,
                    generation_profile=prepared.generation_profile,
                    seed=prepared.seed,
                    chunk_count=len(chunks),
                    sample_count=len(pcm),
                ),
            )

        try:
            if prepared.cached_audio is not None:
                source_chunks = self._cached_chunks(prepared.cached_audio)
                first_chunk_ms = 0.0
            else:
                cache_source = "fresh-generation"

                def generated_chunks():
                    nonlocal generation
                    with self.model_lock:
                        if prepared.seed is not None and self._mlx is not None:
                            self._mlx.random.seed(prepared.seed)
                        generation = self.model.generate(
                            text=prepared.text,
                            prompt_audio_codes=prepared.prompt_audio_codes,
                            language=self.language,
                            mode="generation",
                            max_tokens=prepared.max_tokens,
                            stream=True,
                            do_sample=True,
                            streaming_first_chunk_frames=(
                                self.streaming_first_chunk_frames
                            ),
                            streaming_interval=self.streaming_interval,
                            **dict(prepared.generation_options),
                        )
                        self.active_generation = generation
                        for generated in generation:
                            yield self._to_numpy_audio(generated.audio)

                source_chunks = generated_chunks()

            for audio in source_chunks:
                if cancelled():
                    completion = SynthesisCompletion.CANCELLED
                    break
                audio = self._to_numpy_audio(audio)
                if not audio.size:
                    continue
                if prepared.cached_audio is None:
                    audio = bounded(audio)
                    if audio is None:
                        break
                if first_chunk_ms is None:
                    first_chunk_ms = (self.clock() - started) * 1000
                    self.last_synthesis_ms = first_chunk_ms
                    self.last_first_audio_ms = first_chunk_ms
                chunks.append(audio)
                yield SynthesisChunk(
                    pcm=audio,
                    sample_rate=self.sample_rate,
                    index=len(chunks) - 1,
                    elapsed_ms=(self.clock() - started) * 1000,
                )
                if completion is SynthesisCompletion.LIMITED:
                    break

            if cancelled() and completion is SynthesisCompletion.COMPLETE:
                completion = SynthesisCompletion.CANCELLED
            if not chunks and completion is SynthesisCompletion.COMPLETE:
                raise TTSSynthesisError("MOSS-TTS generated no audio")
            return finish(completion)
        except (TTSConfigurationError, TTSSynthesisError):
            raise
        except Exception as error:
            if cancelled():
                return finish(SynthesisCompletion.CANCELLED)
            raise TTSSynthesisError(str(error)) from error
        finally:
            if generation is not None:
                self._close_active_generation()

    def prime(self, character):
        voice_key, _source = self._resolve_voice_source(character)
        with self.model_lock:
            if voice_key in self.prompt_audio_codes:
                return False
            self._resolve_prompt_codes(character)
        return True

    def speak(self, character, text, *, playback_guard=None):
        outcome = self.play_prepared(
            self.prepare_playback(character, text), playback_guard=playback_guard
        )
        if outcome.status is PlaybackStatus.FAILED:
            _raise_playback_failure(outcome, "MOSS-TTS playback failed")
        return outcome.successful

    def play(self, prepared, *, playback_guard=None):
        typed = PreparedPlayback(
            prepared,
            0.0 if getattr(prepared, "cached_audio", None) is not None else None,
            0.0 if getattr(prepared, "cached_audio", None) is not None else None,
            getattr(prepared, "cache_source", None),
            f"moss-tts:{getattr(prepared, 'cache_source', 'unknown')}",
        )
        outcome = self.play_prepared(typed, playback_guard=playback_guard)
        self.last_playback_ms = outcome.playback_ms
        self.last_playback_underrun = outcome.underflowed
        self.last_generation_limited = outcome.generation_limited
        self.last_synthesis_ms = outcome.synthesis_ms
        self.last_first_audio_ms = outcome.first_audio_ms
        self.last_audio_source = outcome.audio_source
        if outcome.status is PlaybackStatus.FAILED:
            _raise_playback_failure(outcome, "MOSS-TTS playback failed")
        return outcome.successful

    def play_prepared(self, prepared, *, playback_guard=None):
        if not isinstance(prepared, PreparedPlayback) or not isinstance(
            prepared.payload, MossTTSPreparedSpeech
        ):
            raise TTSConfigurationError("MOSS-TTS received invalid prepared speech")
        if playback_guard is not None and not playback_guard():
            return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
        payload = prepared.payload

        with self.playback_lock:
            with self.active_stream_lock:
                previous_stream = self.active_stream
            if previous_stream is not None:
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.FAILED,
                    None,
                    error="A previous MOSS playback stream did not stop",
                    error_type=AudioPlaybackError,
                )
            if playback_guard is not None and not playback_guard():
                return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
            self.playback_stop.clear()
            self.playback_cancel_requested.clear()
            started = self.clock()
            underflowed = False
            rendered = None
            try:
                self.playback_active = True
                request = SynthesisRequest(
                    voice=payload.voice_key,
                    text=payload.text,
                    seed=payload.seed,
                    generation_profile=payload.generation_profile,
                    cancellation=(
                        None if playback_guard is None else lambda: not playback_guard()
                    ),
                    cache_policy=payload.cache_policy,
                )
                playback_prepared = payload
                if (
                    payload.cached_audio is None
                    and payload.cache_policy is not SynthesisCachePolicy.BYPASS
                ):
                    playback_prepared = replace(
                        payload,
                        cache_policy=SynthesisCachePolicy.BYPASS,
                    )
                rendered = self._render_prepared(playback_prepared, request)
                played, underflowed, first_write_ms = self._play_rendered_stream(
                    rendered, playback_guard, started=started
                )
                if not played:
                    try:
                        result = rendered.result
                    except RuntimeError:
                        result = None
                    resolved = (
                        replace(
                            prepared,
                            synthesis_ms=result.timing.first_chunk_ms,
                            first_audio_ms=first_write_ms,
                            cache_source=result.diagnostics.cache_source,
                            audio_source=(
                                f"moss-tts:{result.diagnostics.cache_source}"
                            ),
                        )
                        if result is not None
                        else replace(prepared, first_audio_ms=first_write_ms)
                    )
                    return outcome_for_prepared(
                        resolved,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        underflowed=underflowed,
                        generation_limited=(
                            result is not None
                            and result.completion is SynthesisCompletion.LIMITED
                        ),
                        first_audio_ms=first_write_ms,
                    )
                result = rendered.result
                completed = result.completion is not SynthesisCompletion.CANCELLED
                if (
                    completed
                    and result.completion is SynthesisCompletion.COMPLETE
                    and payload.cached_audio is None
                    and payload.cache_policy is not SynthesisCachePolicy.BYPASS
                ):
                    self.audio_cache.put(payload.cache_key, result.pcm)
                    self.persistent_audio_cache.put(
                        payload.persistent_cache_key,
                        result.pcm,
                    )
                resolved = replace(
                    prepared,
                    synthesis_ms=result.timing.first_chunk_ms,
                    first_audio_ms=first_write_ms,
                    cache_source=result.diagnostics.cache_source,
                    audio_source=f"moss-tts:{result.diagnostics.cache_source}",
                )
                return outcome_for_prepared(
                    resolved,
                    (
                        PlaybackStatus.COMPLETED
                        if completed
                        else PlaybackStatus.INTERRUPTED
                    ),
                    (self.clock() - started) * 1000,
                    underflowed=underflowed,
                    generation_limited=(
                        result.completion is SynthesisCompletion.LIMITED
                    ),
                    first_audio_ms=first_write_ms,
                )
            except Exception as error:
                with self.active_stream_lock:
                    stream_still_owned = self.active_stream is not None
                explicitly_cancelled = self.playback_cancel_requested.is_set() or (
                    playback_guard is not None and not playback_guard()
                )
                if explicitly_cancelled and not stream_still_owned:
                    return outcome_for_prepared(
                        prepared,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        underflowed=underflowed,
                    )
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.FAILED,
                    (self.clock() - started) * 1000,
                    underflowed=underflowed,
                    error=str(error),
                    error_type=type(error),
                )
            finally:
                if rendered is not None:
                    rendered.close()
                self._close_active_generation()
                with self.active_stream_lock:
                    self.playback_active = self.active_stream is not None

    def _play_rendered_stream(self, rendered, playback_guard, *, started):
        try:
            first_chunk = next(rendered)
        except StopIteration:
            return False, False, None

        chunk_queue = Queue(maxsize=4)
        playback_finished = object()
        playback_result = {
            "completed": False,
            "error": None,
            "underflowed": False,
            "first_audio_ms": None,
        }
        audio_output = self._resolve_audio_output()

        def enqueue(value):
            while not self.playback_stop.is_set():
                try:
                    chunk_queue.put(value, timeout=0.1)
                    return True
                except Full:
                    continue
            return False

        def consume():
            try:
                with audio_output.OutputStream(
                    samplerate=self.sample_rate,
                    channels=first_chunk.pcm.shape[1],
                    dtype="float32",
                    latency=self.playback_latency,
                ) as stream:
                    with self.active_stream_lock:
                        self.active_stream = stream
                    wrote_audio = False
                    while not self._cancelled(playback_guard):
                        try:
                            item = chunk_queue.get(timeout=0.1)
                        except Empty:
                            continue
                        if item is playback_finished:
                            playback_result["completed"] = wrote_audio
                            return
                        if not wrote_audio:
                            playback_result["first_audio_ms"] = (
                                self.clock() - started
                            ) * 1000
                        playback_result["underflowed"] = bool(
                            playback_result["underflowed"]
                            or self._write_stream_chunk(stream, item.pcm)
                        )
                        wrote_audio = True
            except Exception as error:
                playback_result["error"] = error
                self.playback_stop.set()
            finally:
                with self.active_stream_lock:
                    self.active_stream = None
                    self.playback_active = False

        enqueue(first_chunk)
        consumer = Thread(target=consume, name="vntts-moss-playback", daemon=True)
        consumer.start()
        render_exhausted = False
        try:
            for chunk in rendered:
                if not enqueue(chunk):
                    break
            else:
                render_exhausted = True
        finally:
            if not render_exhausted:
                rendered.close()
            if not self.playback_stop.is_set():
                enqueue(playback_finished)
            consumer.join(timeout=self.playback_consumer_join_timeout)
            if consumer.is_alive():
                self.playback_stop.set()
                with self.active_stream_lock:
                    stream = self.active_stream
                if stream is not None:
                    try:
                        stream.abort()
                    except Exception:
                        pass
                consumer.join(timeout=self.playback_consumer_join_timeout)
                if consumer.is_alive():
                    raise AudioPlaybackError(
                        "MOSS playback stream ignored abort and remains active"
                    )
        if playback_result["error"] is not None:
            raise playback_result["error"]
        return (
            bool(playback_result["completed"]),
            bool(playback_result["underflowed"]),
            playback_result["first_audio_ms"],
        )

    def _resolve_audio_output(self):
        self.audio_output = resolve_audio_output(self.audio_output)
        return self.audio_output

    def warm_up(self, *, progress=None, text="Voice ready."):
        progress = progress or (lambda _current, _total, _character: None)
        voices = sorted(
            {id(voice): voice for voice in self.registry.voices.values()}.values(),
            key=lambda voice: voice.character.casefold(),
        )
        characters = ["Narrator", *(voice.character for voice in voices)]
        for current, character in enumerate(characters, start=1):
            progress(current, len(characters), character)
            self.prime(character)

        # Compile the MLX generation path before live reading begins. The
        # generated warm-up is cached and never sent to the output device.
        warmup_request = SynthesisRequest(
            voice="Narrator",
            text=text,
            generation_profile=self.generation_profile,
        )
        prepared = self._prepare_request(warmup_request)
        if prepared.cached_audio is None:
            self.playback_stop.clear()
            self._render_prepared(
                replace(prepared, max_tokens=128),
                warmup_request,
            ).collect()
        return len(characters)

    def set_volume(self, volume):
        self.volume = validate_volume(volume)

    def set_speed(self, speed):
        # MOSS-TTS does not expose pitch-preserving speed control.
        self.speed = validate_speed(speed)

    def set_generation_profile(self, profile):
        profile_name, options = get_moss_tts_generation_profile(profile)
        if (
            profile_name == self.generation_profile
            and options == self.generation_options
        ):
            return False
        self.generation_profile = profile_name
        self.generation_options = options
        self.audio_cache.clear()
        return True

    def set_live_mode_active(self, active):
        return bool(active)

    def stop(self):
        was_playing = self.playback_active
        self.playback_cancel_requested.set()
        self.playback_stop.set()
        self._close_active_generation()
        with self.active_stream_lock:
            stream = self.active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        return was_playing

    def _cancelled(self, playback_guard):
        if self.playback_stop.is_set():
            return True
        if playback_guard is not None and not playback_guard():
            self.playback_stop.set()
            return True
        return False

    def _write_stream_chunk(self, stream, audio):
        underflowed = stream.write(self._prepare_audio(audio))
        return bool(underflowed)

    def _resolve_prompt_codes(self, character):
        voice_key, source = self._resolve_voice_source(character)
        cached = self.prompt_audio_codes.get(voice_key)
        if cached is not None:
            return voice_key, cached

        cache_path = self._prompt_cache_path(voice_key, source)
        if cache_path.is_file():
            try:
                codes = self.prompt_code_loader(cache_path)
                self.array_evaluator(codes)
            except (OSError, RuntimeError, TypeError, ValueError, KeyError):
                codes = None
            if codes is not None:
                self.prompt_audio_codes[voice_key] = codes
                return voice_key, codes

        try:
            codes = self.model.encode_reference_audio(str(source))
            self.array_evaluator(codes)
        except Exception as error:
            raise TTSConfigurationError(
                f"MOSS-TTS could not prepare voice {voice_key!r}: {error}"
            ) from error
        self.prompt_audio_codes[voice_key] = codes
        try:
            self.prompt_code_saver(codes, cache_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass
        return voice_key, codes

    def _resolve_voice_source(self, character):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            voice_key = "narrator"
            source = self.narrator_reference
        else:
            voice_key = voice.speaker
            source = voice.references[0] if voice.references else None
        if source is None:
            raise TTSConfigurationError(
                "MOSS-TTS requires a narrator reference recording. Assign an "
                "imported character voice to Narrator or configure TTS speaker WAV."
            )
        source_path = Path(source).expanduser()
        if not source_path.is_file():
            raise TTSConfigurationError(
                f"MOSS-TTS voice reference does not exist: {source_path}"
            )
        return voice_key, source_path.resolve()

    def _prompt_cache_path(self, voice_key, source):
        return voice_artifact_cache_path(
            self.prompt_cache_directory,
            voice_key=voice_key,
            source=source,
            model_identity=f"{self.model_name}:{self.sample_rate}",
            suffix=".safetensors",
        )

    def _persistent_cache_key(
        self,
        voice_key,
        text,
        source,
        *,
        seed=None,
        generation_profile=None,
        generation_options=None,
    ):
        profile = generation_profile or self.generation_profile
        options = generation_options or self.generation_options
        seed_identity = {} if seed is None else {"seed": seed}
        return self.persistent_cache_keys.key(
            voice_key=voice_key,
            source=source,
            text=text,
            speed=self.speed,
            language=self.language,
            generation_profile=profile,
            **options,
            **seed_identity,
            max_tokens=moss_generation_limits(text)[0],
            max_audio_seconds=moss_generation_limits(text)[1],
            streaming_first_chunk_frames=self.streaming_first_chunk_frames,
            streaming_interval=self.streaming_interval,
        )

    def _cached_chunks(self, audio):
        prepared = self._to_numpy_audio(audio)
        for start in range(0, len(prepared), self.cached_stream_chunk_samples):
            yield prepared[start : start + self.cached_stream_chunk_samples]

    @staticmethod
    def _to_numpy_audio(audio):
        value = audio
        to_numpy = getattr(value, "numpy", None)
        if callable(to_numpy):
            value = to_numpy()
        prepared = np.asarray(value, dtype=np.float32).squeeze()
        if prepared.ndim == 1:
            return prepared.reshape(-1, 1)
        if prepared.ndim != 2:
            raise TTSSynthesisError(
                f"MOSS-TTS returned unsupported audio shape {prepared.shape}"
            )
        if prepared.shape[0] in {1, 2} and prepared.shape[1] > 2:
            prepared = prepared.T
        if prepared.shape[1] not in {1, 2}:
            raise TTSSynthesisError(
                f"MOSS-TTS returned unsupported audio shape {prepared.shape}"
            )
        return prepared

    def _prepare_audio(self, audio):
        prepared = self._to_numpy_audio(audio).copy()
        np.nan_to_num(prepared, copy=False)
        prepared *= self.volume
        np.clip(prepared, -0.95, 0.95, out=prepared)
        return prepared

    def _evaluate_array(self, value):
        if self._mlx is not None:
            self._mlx.eval(value)

    def _load_prompt_codes(self, path):
        if self._mlx is None:
            raise RuntimeError("MLX is not available")
        values = self._mlx.load(path)
        return values["prompt_audio_codes"]

    def _save_prompt_codes(self, codes, path):
        if self._mlx is None:
            return False
        with atomic_output_path(path) as temporary_path:
            self._mlx.save_safetensors(
                temporary_path,
                {"prompt_audio_codes": codes},
            )
        return True

    def _close_active_generation(self):
        generation = self.active_generation
        self.active_generation = None
        close = getattr(generation, "close", None)
        if callable(close):
            try:
                close()
            except (RuntimeError, ValueError):
                pass


def select_torch_device(torch_module):
    if torch_module.cuda.is_available():
        return "cuda"
    # Chatterbox Nano's autoregressive T3 stage is currently much slower on
    # Apple Metal than on CPU (measured at 18s versus about 2s per test line).
    # Keep CPU as the measured macOS path until the upstream MPS kernels improve.
    return "cpu"


def configure_cpu_synthesis_threads(torch_module, reserved_threads=2):
    """Reserve CPU capacity for OCR and uninterrupted audio callbacks."""
    get_num_threads = getattr(torch_module, "get_num_threads", None)
    set_num_threads = getattr(torch_module, "set_num_threads", None)
    if not callable(get_num_threads) or not callable(set_num_threads):
        return False
    try:
        current = int(get_num_threads())
    except (TypeError, ValueError):
        return False
    target = max(1, current - max(1, int(reserved_threads)))
    if target >= current:
        return False
    set_num_threads(target)
    return True


def get_torch_thread_count(torch_module):
    get_num_threads = getattr(torch_module, "get_num_threads", None)
    if not callable(get_num_threads):
        return None
    try:
        count = int(get_num_threads())
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def activate_chatterbox_runtime(runtime_directory=None):
    return activate_backend_runtime(
        runtime_directory,
        environment_variable="VNTTS_CHATTERBOX_RUNTIME",
        backend_directory="chatterbox-nano",
        missing_message=(
            "Chatterbox Nano is not installed. Run "
            "`uv sync --project backends/chatterbox-nano`, then restart the app."
        ),
    )


def activate_pocket_tts_runtime(runtime_directory=None):
    return activate_backend_runtime(
        runtime_directory,
        environment_variable="VNTTS_POCKET_TTS_RUNTIME",
        backend_directory="pocket-tts",
        missing_message=(
            "Pocket TTS is not installed. Run "
            "`uv sync --project backends/pocket-tts`, then restart the app."
        ),
    )


def activate_moss_tts_runtime(runtime_directory=None):
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise TTSConfigurationError(
            "MOSS-TTS with MLX requires macOS on Apple Silicon."
        )
    return activate_backend_runtime(
        runtime_directory,
        environment_variable="VNTTS_MOSS_RUNTIME",
        backend_directory="moss-tts",
        missing_message=(
            "MOSS-TTS is not installed. Run "
            "`uv sync --project backends/moss-tts`, then restart the app."
        ),
    )
