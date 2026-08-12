import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from types import MethodType
from typing import Any, Protocol

import numpy as np

from vntts.audio_cache import PersistentAudioCache
from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSConfigurationError,
    TTSSynthesisError,
    match_output_sample_rate,
)
from vntts.settings import get_local_data_directory
from vntts.voices import is_narrator, normalize_character_name


@dataclass(frozen=True)
class SpeechBackendCapabilities:
    voice_cloning: bool
    streaming: bool
    concurrent_prepare_and_play: bool
    interrupt_on_dialog_replacement: bool = False


class SpeechBackend(Protocol):
    name: str
    capabilities: SpeechBackendCapabilities

    def prepare(self, character: str, text: str) -> Any: ...

    def play(self, prepared: Any, *, playback_guard=None) -> bool: ...

    def stop(self) -> bool: ...


@dataclass(frozen=True)
class PocketTTSPreparedSpeech:
    voice_key: str
    voice_state: Any
    text: str
    cache_key: tuple[str, str]
    persistent_cache_key: str
    cached_audio: np.ndarray | None = None


def _voice_source_identity(voice_key, source):
    source_path = Path(str(source)).expanduser()
    try:
        stat = source_path.stat()
        source_identity = f"{source_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        source_identity = str(source)
    return f"{voice_key}:{source_identity}"


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

    @property
    def last_playback_underrun(self):
        return bool(getattr(self.voice_router.tts, "last_playback_underrun", False))


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
        conditioning_cache_directory=None,
        persistent_audio_cache_directory=None,
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
        self.playback_active = False
        self.last_playback_underrun = False
        self.last_synthesis_ms = None
        self.last_playback_ms = None
        self.audio_cache_size = max(0, int(audio_cache_size))
        self.audio_cache = OrderedDict()
        self.persistent_audio_cache = PersistentAudioCache(
            persistent_audio_cache_directory
            or get_local_data_directory() / "audio-cache" / self.name,
            max_entries=max(64, self.audio_cache_size * 8),
        )
        self.set_volume(volume)
        self.set_speed(1.0)

    def prepare(self, character, text):
        return self.synthesize(character, text)

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
        normalized_character = normalize_character_name(character) or "narrator"
        cache_key = normalized_character, " ".join((text or "").split())
        with self.synthesis_lock:
            if cache_key in self.audio_cache:
                audio = self.audio_cache.pop(cache_key)
                self.audio_cache[cache_key] = audio
                self.last_synthesis_ms = 0.0
                return audio
            persistent_key = self._persistent_cache_key(character, cache_key[1])
            persistent_audio = self.persistent_audio_cache.get(persistent_key)
            if persistent_audio is not None:
                self._cache_audio(cache_key, persistent_audio)
                self.last_synthesis_ms = 0.0
                return persistent_audio

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
            self.persistent_audio_cache.put(persistent_key, audio)
            return audio

    def speak(self, character, text, *, playback_guard=None):
        return self.play(
            self.prepare(character, text),
            playback_guard=playback_guard,
        )

    def play(self, prepared, *, playback_guard=None):
        self.last_playback_ms = None
        self.last_playback_underrun = False
        if playback_guard is not None and not playback_guard():
            return False
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return False
            started = self.clock()
            try:
                self.playback_active = True
                prepared, playback_sample_rate = match_output_sample_rate(
                    self.audio_output,
                    self._prepare_audio(prepared),
                    self.sample_rate,
                )
                self.audio_output.play(
                    prepared,
                    playback_sample_rate,
                    latency=self.playback_latency,
                )
                playback_status = self.audio_output.wait()
                self.last_playback_underrun = self._playback_underflowed(
                    playback_status
                )
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
        was_playing = self.playback_active
        self.audio_output.stop()
        return was_playing

    def _playback_underflowed(self, playback_status=None):
        value = getattr(playback_status, "output_underflow", None)
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        get_stream = getattr(self.audio_output, "get_stream", None)
        if not callable(get_stream):
            return False
        try:
            value = get_stream().status.output_underflow
        except (AttributeError, RuntimeError):
            return False
        return bool(value) if isinstance(value, (bool, np.bool_)) else False

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
        try:
            stat = reference.stat()
            identity = f"{reference}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            identity = str(reference)
        model_identity = (
            f"{type(self.model).__module__}.{type(self.model).__qualname__}:"
            f"{getattr(self.model, 'model_label', 'nano')}:{self.sample_rate}"
        )
        digest = blake2b(
            f"{model_identity}:{identity}".encode(),
            digest_size=12,
        ).hexdigest()
        safe_key = re.sub(r"[^a-z0-9]+", "-", str(key).casefold()).strip("-")
        return self.conditioning_cache_directory / f"{safe_key or 'voice'}-{digest}.pt"

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
        temporary_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            save(temporary_path)
            temporary_path.replace(cache_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _cache_audio(self, key, audio):
        if self.audio_cache_size == 0:
            return
        self.audio_cache.pop(key, None)
        self.audio_cache[key] = audio
        while len(self.audio_cache) > self.audio_cache_size:
            self.audio_cache.popitem(last=False)

    def _persistent_cache_key(self, character, text):
        model = f"{self.model.__class__.__module__}.{self.model.__class__.__qualname__}"
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            voice_key = "narrator"
            source = self.narrator_reference or "embedded-default"
        else:
            voice_key = voice.speaker
            source = voice.references[0] if voice.references else "missing-reference"
        return self.persistent_audio_cache.key(
            backend=self.name,
            model=model,
            voice=_voice_source_identity(voice_key, source),
            text=text,
            settings={"sample_rate": self.sample_rate, "speed": self.speed},
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
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=True,
        # Voice-state preparation and streaming use the same model instance.
        concurrent_prepare_and_play=False,
        interrupt_on_dialog_replacement=True,
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
        cached_stream_chunk_seconds=0.2,
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
        if audio_output is None:
            import sounddevice

            audio_output = sounddevice

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
        self.audio_cache_size = max(0, int(audio_cache_size))
        self.audio_cache = OrderedDict()
        self.persistent_audio_cache = PersistentAudioCache(
            persistent_audio_cache_directory
            or get_local_data_directory() / "audio-cache" / self.name,
            max_entries=max(64, self.audio_cache_size * 8),
        )
        self.cached_stream_chunk_samples = max(
            1,
            round(self.sample_rate * float(cached_stream_chunk_seconds)),
        )
        self.set_volume(volume)
        self.set_speed(1.0)

    def prepare(self, character, text):
        spoken_text = " ".join((text or "").split())
        voice_key, source = self._resolve_voice_source(character)
        cache_key = voice_key, spoken_text
        persistent_key = self._persistent_cache_key(voice_key, spoken_text, source)
        cached_audio = self.audio_cache.get(cache_key)
        if cached_audio is not None:
            self.audio_cache.move_to_end(cache_key)
        else:
            cached_audio = self.persistent_audio_cache.get(persistent_key)
            if cached_audio is not None:
                self._cache_audio(cache_key, cached_audio)
        voice_state = None
        if cached_audio is None:
            with self.model_lock:
                resolved_voice_key, voice_state = self._resolve_voice_state(character)
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
        )

    def prime(self, character):
        voice_key, _source = self._resolve_voice_source(character)
        with self.model_lock:
            if voice_key in self.voice_states:
                return False
            self._resolve_voice_state(character)
        return True

    def speak(self, character, text, *, playback_guard=None):
        return self.play(
            self.prepare(character, text),
            playback_guard=playback_guard,
        )

    def play(self, prepared, *, playback_guard=None):
        if playback_guard is not None and not playback_guard():
            return False
        if not isinstance(prepared, PocketTTSPreparedSpeech):
            raise TTSConfigurationError("Pocket TTS received invalid prepared speech")

        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return False
            self.playback_stop.clear()
            generation_cancel = Event()
            self.active_generation_cancel = generation_cancel
            self.last_playback_underrun = False
            self.last_synthesis_ms = 0.0 if prepared.cached_audio is not None else None
            self.last_first_audio_ms = self.last_synthesis_ms
            started = self.clock()
            raw_chunks = []
            completed = False
            try:
                self.playback_active = True
                with self.audio_output.OutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    latency=self.playback_latency,
                ) as stream:
                    with self.active_stream_lock:
                        self.active_stream = stream
                    if prepared.cached_audio is not None:
                        chunks = self._cached_chunks(prepared.cached_audio)
                        completed = self._write_chunks(
                            stream,
                            chunks,
                            playback_guard,
                        )
                    else:
                        with self.model_lock:
                            chunks = self.model.generate_audio_stream(
                                prepared.voice_state,
                                prepared.text,
                            )
                            completed = self._write_chunks(
                                stream,
                                chunks,
                                playback_guard,
                                started=started,
                                raw_chunks=raw_chunks,
                            )
                if completed and raw_chunks:
                    complete_audio = np.concatenate(raw_chunks)
                    self._cache_audio(prepared.cache_key, complete_audio)
                    self.persistent_audio_cache.put(
                        prepared.persistent_cache_key,
                        complete_audio,
                    )
                return completed
            except Exception as error:
                if self.playback_stop.is_set():
                    return False
                raise AudioPlaybackError(str(error)) from error
            finally:
                if self.active_generation_cancel is generation_cancel:
                    self.active_generation_cancel = None
                with self.active_stream_lock:
                    self.active_stream = None
                self.playback_active = False
                self.last_playback_ms = (self.clock() - started) * 1000

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
        # Pocket TTS 2.1 has no pitch-preserving speed control.
        self.speed = float(speed)

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
                self.last_synthesis_ms = first_audio_ms
                self.last_first_audio_ms = first_audio_ms
            underflowed = stream.write(self._prepare_audio(raw).reshape(-1, 1))
            self.last_playback_underrun = self.last_playback_underrun or bool(
                underflowed
            )
            wrote_audio = True
        if cancelled:
            return False
        if not wrote_audio:
            raise TTSSynthesisError("Pocket TTS generated no audio")
        return True

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
            raise TTSConfigurationError(
                f"Voice {voice.character!r} has no reference recording"
            )
        return voice.speaker, voice.references[0]

    def _voice_state_cache_path(self, voice_key, source):
        source_path = Path(str(source)).expanduser()
        try:
            stat = source_path.stat()
            identity = f"{source_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            identity = str(source)
        model_identity = (
            f"{type(self.model).__module__}.{type(self.model).__qualname__}:"
            f"{self.sample_rate}"
        )
        digest = blake2b(
            f"{model_identity}:{identity}".encode(),
            digest_size=12,
        ).hexdigest()
        safe_key = re.sub(r"[^a-z0-9]+", "-", voice_key.casefold()).strip("-")
        return self.voice_state_cache_directory / (
            f"{safe_key or 'voice'}-{digest}.safetensors"
        )

    def _save_voice_state(self, state, cache_path):
        if not callable(self.state_exporter):
            return False
        temporary_path = cache_path.with_name(
            f"{cache_path.stem}.tmp{cache_path.suffix}"
        )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_exporter(state, temporary_path)
            temporary_path.replace(cache_path)
        except (OSError, RuntimeError, TypeError, ValueError):
            return False
        return True

    def _cache_audio(self, key, audio):
        if self.audio_cache_size == 0:
            return
        self.audio_cache.pop(key, None)
        self.audio_cache[key] = np.asarray(audio, dtype=np.float32)
        while len(self.audio_cache) > self.audio_cache_size:
            self.audio_cache.popitem(last=False)

    def _persistent_cache_key(self, voice_key, text, source):
        model = f"{self.model.__class__.__module__}.{self.model.__class__.__qualname__}"
        return self.persistent_audio_cache.key(
            backend=self.name,
            model=model,
            voice=_voice_source_identity(voice_key, source),
            text=text,
            settings={"sample_rate": self.sample_rate, "speed": self.speed},
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
    runtime_directory = (
        Path(
            runtime_directory
            or os.environ.get("VNTTS_CHATTERBOX_RUNTIME", "")
            or Path(__file__).resolve().parents[1]
            / "backends"
            / "chatterbox-nano"
            / ".venv"
        )
        .expanduser()
        .resolve()
    )
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


def activate_pocket_tts_runtime(runtime_directory=None):
    runtime_directory = (
        Path(
            runtime_directory
            or os.environ.get("VNTTS_POCKET_TTS_RUNTIME", "")
            or Path(__file__).resolve().parents[1] / "backends" / "pocket-tts" / ".venv"
        )
        .expanduser()
        .resolve()
    )
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
            "Pocket TTS is not installed. Run "
            "`uv sync --project backends/pocket-tts`, then restart the app."
        )
    site_packages_text = str(site_packages)
    if site_packages_text not in sys.path:
        sys.path.insert(0, site_packages_text)
    return site_packages
