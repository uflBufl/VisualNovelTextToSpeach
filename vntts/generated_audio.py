"""Ahead-of-time generated audio with verified live-TTS fallback."""

from __future__ import annotations

import hashlib
import io
import wave
from dataclasses import dataclass, replace
from threading import Event, Lock
from time import monotonic

import numpy as np
from vntts_artifacts.audio import Pcm16MonoWavError
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)

from vntts.playback import PlaybackOutcome, PlaybackStatus
from vntts.services.tts_engine import match_output_sample_rate
from vntts.settings import audio_source_policies
from vntts.speech_backend_runtime import BoundedCache, validate_speed, validate_volume
from vntts.voices import synthesis_character


@dataclass(frozen=True)
class PreparedGeneratedAudio:
    line_id: str
    text_sha256: str
    samples: np.ndarray
    sample_rate: int


@dataclass(frozen=True)
class PreparedSourceAudioPassThrough:
    """A line whose original audio is already being played by the game."""

    line_id: str
    text_sha256: str
    source_audio_id: str | None = None
    completion_seconds: float | None = None
    completion_source: str | None = None


@dataclass(frozen=True)
class AudioRouteTrace:
    generation: int | None
    effective_source: str
    match_result: str
    fallback_reason: str | None
    voice_reference_id: str | None
    line_id: str | None
    artifact_preflight_state: str
    chunk_id: str | None = None
    chunk_ordinal: int | None = None
    chunk_characters: int | None = None

    def message(self):
        values = (
            ("generation", self.generation),
            ("source", self.effective_source),
            ("line", self.line_id),
            ("match", self.match_result),
            ("fallback", self.fallback_reason),
            ("voice-reference", self.voice_reference_id),
            ("artifact-preflight", self.artifact_preflight_state),
            ("chunk", self.chunk_id),
            ("chunk-ordinal", self.chunk_ordinal),
            ("chunk-characters", self.chunk_characters),
        )
        return "Audio route: " + "; ".join(
            f"{key}={value if value is not None else 'none'}" for key, value in values
        )

    def support_fields(self):
        return {
            "generation": self.generation,
            "effective_source": self.effective_source,
            "match_result": self.match_result,
            "fallback_reason": self.fallback_reason,
            "voice_reference_id": self.voice_reference_id,
            "line_id": self.line_id,
            "artifact_preflight_state": self.artifact_preflight_state,
            "chunk_id": self.chunk_id,
            "chunk_ordinal": self.chunk_ordinal,
            "chunk_characters": self.chunk_characters,
        }


@dataclass(frozen=True)
class SourceAudioRoute:
    prepared: PreparedSourceAudioPassThrough
    trace: AudioRouteTrace
    synthesis_ms: float = 0.0
    first_audio_ms: float | None = None
    cache_source: str | None = None


@dataclass(frozen=True)
class GeneratedAudioRoute:
    prepared: PreparedGeneratedAudio
    trace: AudioRouteTrace
    synthesis_ms: float = 0.0
    first_audio_ms: float | None = 0.0
    cache_source: str | None = "generated-audio"


@dataclass(frozen=True)
class LiveTTSRoute:
    prepared: object
    trace: AudioRouteTrace
    synthesis_ms: float | None
    first_audio_ms: float | None
    cache_source: str | None = None


RouteDecision = SourceAudioRoute | GeneratedAudioRoute | LiveTTSRoute


class GeneratedAudioLibrary:
    def __init__(self, index, *, warn=None, cache_size=32):
        self.index = index
        self.warn = warn or (lambda _message: None)
        self.cache = BoundedCache(cache_size)
        self.warned_entries = set()

    @classmethod
    def load_optional(cls, path, *, warn=None, cache_size=32):
        if not path:
            return None
        try:
            index = GeneratedAudioIndex.load(path)
        except GeneratedAudioManifestError as error:
            if warn is not None:
                warn(f"Generated audio disabled: {error}")
            return None
        return cls(index, warn=warn, cache_size=cache_size)

    def find(self, line_id, text_sha256):
        prepared, _state = self.find_with_preflight(line_id, text_sha256)
        return prepared

    def find_with_preflight(self, line_id, text_sha256):
        entry = self.index.find(line_id, text_sha256, verify_file=False)
        if entry is None:
            return None, "generated-audio-entry-not-found"
        try:
            payload = entry.audio.read_bytes()
        except OSError:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None, "generated-audio-entry-missing"
        if hashlib.sha256(payload).hexdigest() != entry.audio_sha256:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None, "generated-audio-checksum-failed"
        cache_key = (
            entry.line_id,
            entry.text_sha256,
            entry.audio_sha256,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached, "generated-audio-entry-verified"
        try:
            samples, sample_rate = _read_pcm16_mono_wav_bytes(payload)
        except Pcm16MonoWavError as error:
            self._warn_once(
                entry, f"Generated audio is invalid: {entry.audio}: {error}"
            )
            return None, "generated-audio-invalid-wav"
        if sample_rate != entry.sample_rate or len(samples) != entry.sample_count:
            self._warn_once(
                entry,
                f"Generated audio metadata does not match the WAV file: {entry.audio}",
            )
            return None, "generated-audio-metadata-mismatch"
        prepared = PreparedGeneratedAudio(
            line_id=entry.line_id,
            text_sha256=entry.text_sha256,
            samples=samples,
            sample_rate=sample_rate,
        )
        self.cache.put(cache_key, prepared)
        return prepared, "generated-audio-entry-verified"

    def _warn_once(self, entry, message):
        identity = entry.line_id, entry.text_sha256
        if identity in self.warned_entries:
            return
        self.warned_entries.add(identity)
        self.warn(message)


class GeneratedAudioFallbackBackend:
    """Pass through source audio, prefer local generations, then use live TTS."""

    def __init__(
        self,
        live_backend,
        library,
        line_resolver,
        *,
        volume=1.0,
        speed=1.0,
        audio_output=None,
        playback_latency="low",
        clock=monotonic,
        audio_source_policy="prefer-generated",
        require_source_audio_completion=False,
    ):
        if audio_output is None:
            import sounddevice

            audio_output = sounddevice
        self.live_backend = live_backend
        self.library = library
        self.line_resolver = line_resolver
        self.audio_output = audio_output
        self.playback_latency = playback_latency
        self.clock = clock
        if audio_source_policy not in audio_source_policies:
            raise ValueError(f"Unknown audio source policy: {audio_source_policy}")
        self.audio_source_policy = audio_source_policy
        self.require_source_audio_completion = bool(require_source_audio_completion)
        prefix = "generated-audio" if library is not None else "story-audio"
        self.name = f"{prefix}+{live_backend.name}"
        self.capabilities = live_backend.capabilities
        self.playback_lock = Lock()
        self.source_audio_completion_stop = Event()
        self.generated_audio_stop = Event()
        self.generated_reservations = BoundedCache(32)
        self.active_playback_source = None
        self.playback_active = False
        self.live_mode_active = False
        self.voice_override = None
        self.set_volume(volume, delegate=False)
        self.set_speed(speed, delegate=False)

    def will_use_source_audio(self, character, text):
        """Return whether live playback for this exact line stays in the game."""
        if not self.live_mode_active:
            return False
        if self.audio_source_policy != "prefer-game-audio":
            return False
        if self.voice_override is not None and self.voice_override(character):
            return False
        line = self.line_resolver.resolve_exact(character, text)
        return bool(
            line is not None
            and line.line_id
            and getattr(line, "source_audio_status", "unknown") == "available"
            and (
                not self.require_source_audio_completion
                or getattr(line, "source_audio_duration_seconds", None) is not None
            )
        )

    def has_generated_line(self, line):
        """Reserve a verified generation for safe early-prefix expansion."""
        if (
            self.library is None
            or self.speed != 1.0
            or not line.line_id
            or not line.text_sha256
        ):
            return False
        prepared, _state = self.library.find_with_preflight(
            line.line_id, line.text_sha256
        )
        if prepared is None:
            return False
        self.generated_reservations.put((line.line_id, line.text_sha256), prepared)
        return True

    def prepare_route(self, character, text):
        voice_overridden = self.voice_override is not None and self.voice_override(
            character
        )
        line, match_result = self._resolve_line(character, text, voice_overridden)
        fallback_reasons = []
        artifact_preflight_state = "not-applicable"
        if voice_overridden:
            fallback_reasons.append("manual-voice-override")
            artifact_preflight_state = "skipped-manual-voice-override"
        elif match_result != "exact":
            fallback_reasons.append(f"story-line-{match_result}")
        source_audio_completion = (
            getattr(line, "source_audio_duration_seconds", None)
            if line is not None
            else None
        )
        source_audio_missing_completion = bool(
            self.require_source_audio_completion
            and line is not None
            and getattr(line, "source_audio_status", "unknown") == "available"
            and source_audio_completion is None
        )
        if (
            line is not None
            and line.line_id
            and self.live_mode_active
            and self.audio_source_policy == "prefer-game-audio"
            and getattr(line, "source_audio_status", "unknown") == "available"
            and not source_audio_missing_completion
        ):
            trace = AudioRouteTrace(
                None,
                "game",
                match_result,
                None,
                None,
                line.line_id,
                "source-audio-declared-available",
            )
            return SourceAudioRoute(
                PreparedSourceAudioPassThrough(
                    line.line_id,
                    line.text_sha256,
                    getattr(line, "source_audio_id", None),
                    source_audio_completion,
                    ("story-index" if source_audio_completion is not None else None),
                ),
                trace,
            )
        if self.audio_source_policy == "prefer-game-audio" and line is not None:
            if source_audio_missing_completion:
                fallback_reasons.append("source-audio-completion-unavailable")
                artifact_preflight_state = "source-audio-completion-unavailable"
            else:
                source_status = getattr(line, "source_audio_status", "unknown")
                fallback_reasons.append(f"source-audio-{source_status}")
                artifact_preflight_state = f"source-audio-{source_status}"
        if (
            line is not None
            and line.line_id
            and self.audio_source_policy in {"prefer-generated", "prefer-game-audio"}
            and self.library is not None
            and self.speed == 1.0
        ):
            reservation_key = (line.line_id, line.text_sha256)
            prepared = self.generated_reservations.get(reservation_key)
            if prepared is not None:
                artifact_preflight_state = "generated-audio-entry-reserved"
            else:
                prepared, artifact_preflight_state = self.library.find_with_preflight(
                    line.line_id,
                    line.text_sha256,
                )
            if prepared is not None:
                trace = AudioRouteTrace(
                    None,
                    "generated",
                    match_result,
                    ";".join(fallback_reasons) or None,
                    None,
                    line.line_id,
                    artifact_preflight_state,
                )
                return GeneratedAudioRoute(prepared, trace)
            fallback_reasons.append(artifact_preflight_state)
        elif line is not None and self.audio_source_policy in {
            "prefer-generated",
            "prefer-game-audio",
        }:
            if self.library is None:
                artifact_preflight_state = "generated-audio-library-not-configured"
            elif self.speed != 1.0:
                artifact_preflight_state = "generated-audio-skipped-nondefault-speed"
            fallback_reasons.append(artifact_preflight_state)
        prepared = self.live_backend.prepare_playback(
            synthesis_character(character), text
        )
        effective_source = prepared.audio_source
        line_id = line.line_id if line is not None else None
        trace = AudioRouteTrace(
            None,
            effective_source,
            match_result,
            ";".join(dict.fromkeys(fallback_reasons)) or None,
            None,
            line_id,
            artifact_preflight_state,
        )
        return LiveTTSRoute(
            prepared,
            trace,
            prepared.synthesis_ms,
            prepared.first_audio_ms,
            prepared.cache_source,
        )

    def _resolve_line(self, character, text, voice_overridden):
        if voice_overridden:
            return None, "skipped"
        resolve = getattr(self.line_resolver, "resolve_exact_with_result", None)
        if callable(resolve):
            return resolve(character, text)
        line = self.line_resolver.resolve_exact(character, text)
        return line, "exact" if line is not None else "no-match"

    def play_route(self, route, *, playback_guard=None):
        """Play one immutable route and return metrics bound to that route."""
        if isinstance(route, SourceAudioRoute):
            return self._play_source_route(route, playback_guard)
        if isinstance(route, GeneratedAudioRoute):
            return self._play_generated_route(route, playback_guard)
        if isinstance(route, LiveTTSRoute):
            return self._play_live_route(route, playback_guard)
        raise TypeError(f"Unsupported audio route: {type(route).__name__}")

    def _play_source_route(self, route, playback_guard):
        prepared = route.prepared
        if playback_guard is not None and not playback_guard():
            return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
        if prepared.completion_seconds is None:
            return _route_outcome(route, PlaybackStatus.PASSTHROUGH_UNOBSERVED, None)
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
            started = self.clock()
            self.source_audio_completion_stop.clear()
            try:
                self.playback_active = True
                self.active_playback_source = "game"
                interrupted = self.source_audio_completion_stop.wait(
                    prepared.completion_seconds
                )
                playable = playback_guard is None or bool(playback_guard())
                status = (
                    PlaybackStatus.INTERRUPTED
                    if interrupted or not playable
                    else PlaybackStatus.COMPLETED
                )
                return _route_outcome(
                    route,
                    status,
                    (self.clock() - started) * 1000,
                )
            finally:
                self.playback_active = False
                self.active_playback_source = None

    def _play_generated_route(self, route, playback_guard):
        if playback_guard is not None and not playback_guard():
            return _route_outcome(
                route,
                PlaybackStatus.INTERRUPTED,
                None,
            )
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return _route_outcome(
                    route,
                    PlaybackStatus.INTERRUPTED,
                    None,
                )
            started = self.clock()
            self.generated_audio_stop.clear()
            try:
                self.playback_active = True
                self.active_playback_source = "generated"
                samples = (
                    np.asarray(route.prepared.samples, dtype=np.float32) * self.volume
                )
                samples, sample_rate = match_output_sample_rate(
                    self.audio_output,
                    samples,
                    route.prepared.sample_rate,
                )
                self.audio_output.play(
                    samples,
                    sample_rate,
                    latency=self.playback_latency,
                )
                status = self.audio_output.wait()
                underflowed = bool(getattr(status, "output_underflow", False))
                playable = playback_guard is None or bool(playback_guard())
                playback_status = (
                    PlaybackStatus.INTERRUPTED
                    if self.generated_audio_stop.is_set() or not playable
                    else PlaybackStatus.COMPLETED
                )
                return _route_outcome(
                    route,
                    playback_status,
                    (self.clock() - started) * 1000,
                    underflowed=underflowed,
                    first_audio_ms=route.first_audio_ms,
                )
            except Exception as error:
                return _route_outcome(
                    route,
                    PlaybackStatus.FAILED,
                    (self.clock() - started) * 1000,
                    first_audio_ms=route.first_audio_ms,
                    error=str(error),
                )
            finally:
                self.playback_active = False
                self.active_playback_source = None

    def _play_live_route(self, route, playback_guard):
        if playback_guard is not None and not playback_guard():
            return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
        try:
            outcome = self.live_backend.play_prepared(
                route.prepared,
                playback_guard=playback_guard,
            )
        except Exception as error:
            return _route_outcome(
                route,
                PlaybackStatus.FAILED,
                None,
                first_audio_ms=route.first_audio_ms,
                error=str(error),
            )
        return replace(
            outcome,
            audio_source=route.trace.effective_source,
        )

    def prime(self, character):
        prime = getattr(self.live_backend, "prime", None)
        return prime(character) if callable(prime) else False

    def set_live_mode_active(self, active):
        self.live_mode_active = bool(active)
        configure = getattr(self.live_backend, "set_live_mode_active", None)
        return configure(active) if callable(configure) else self.live_mode_active

    def set_volume(self, volume, *, delegate=True):
        self.volume = validate_volume(volume)
        configure = getattr(self.live_backend, "set_volume", None)
        if delegate and callable(configure):
            configure(self.volume)
        return self.volume

    def set_speed(self, speed, *, delegate=True):
        self.speed = validate_speed(speed)
        configure = getattr(self.live_backend, "set_speed", None)
        if delegate and callable(configure):
            configure(self.speed)
        return self.speed

    def stop(self):
        was_playing = self.playback_active
        if self.active_playback_source == "game":
            self.source_audio_completion_stop.set()
        elif self.active_playback_source == "generated":
            self.generated_audio_stop.set()
            self.audio_output.stop()
        return bool(self.live_backend.stop()) or was_playing


def _read_pcm16_mono_wav_bytes(payload):
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getnchannels() != 1:
                raise Pcm16MonoWavError("WAV must be mono")
            if source.getsampwidth() != 2:
                raise Pcm16MonoWavError("WAV must contain 16-bit PCM")
            if source.getcomptype() != "NONE":
                raise Pcm16MonoWavError("WAV must contain uncompressed PCM")
            sample_rate = source.getframerate()
            sample_count = source.getnframes()
            if sample_rate <= 0:
                raise Pcm16MonoWavError("WAV sample rate must be positive")
            frames = source.readframes(sample_count)
    except (EOFError, wave.Error) as error:
        raise Pcm16MonoWavError(str(error)) from error
    if len(frames) != sample_count * 2:
        raise Pcm16MonoWavError("WAV frame data is truncated")
    pcm = np.frombuffer(frames, dtype="<i2")
    samples = np.asarray(pcm, dtype=np.float32) / 32768.0
    return samples, sample_rate


def _numeric_metric(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _route_outcome(
    route,
    status,
    playback_ms,
    *,
    underflowed=False,
    generation_limited=False,
    first_audio_ms=None,
    error=None,
):
    return PlaybackOutcome(
        status,
        playback_ms,
        underflowed=underflowed,
        generation_limited=generation_limited,
        first_audio_ms=first_audio_ms,
        error=error,
        synthesis_ms=route.synthesis_ms,
        cache_source=route.cache_source,
        audio_source=route.trace.effective_source,
    )
