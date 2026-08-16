"""Ahead-of-time generated audio with verified live-TTS fallback."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic

import numpy as np
from vntts_artifacts.audio import Pcm16MonoWavError, read_pcm16_mono_wav
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)

from vntts.services.tts_engine import AudioPlaybackError, match_output_sample_rate
from vntts.settings import audio_source_policies
from vntts.speech_backend_runtime import BoundedCache, validate_speed, validate_volume


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
            stat = entry.audio.stat()
        except OSError:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None, "generated-audio-entry-missing"
        cache_key = (
            entry.line_id,
            entry.text_sha256,
            entry.audio_sha256,
            stat.st_size,
            stat.st_mtime_ns,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached, "generated-audio-entry-verified"
        if self.index.find(line_id, text_sha256) is None:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None, "generated-audio-checksum-failed"
        try:
            samples, sample_rate = _read_pcm16_mono_wav(entry.audio)
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
        self.playback_active = False
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None
        self.last_playback_ms = None
        self.last_playback_underrun = False
        self.last_audio_source = None
        self.last_line_id = None
        self.last_route_trace = None
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
        """Return whether a full indexed line has a declared local generation."""
        if (
            self.library is None
            or self.speed != 1.0
            or not line.line_id
            or not line.text_sha256
        ):
            return False
        return (
            self.library.index.find(
                line.line_id,
                line.text_sha256,
                verify_file=False,
            )
            is not None
        )

    def prepare(self, character, text):
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
            self.last_synthesis_ms = 0.0
            self.last_first_audio_ms = None
            self.last_audio_source = "game"
            self.last_line_id = line.line_id
            self.last_route_trace = AudioRouteTrace(
                None,
                "game",
                match_result,
                None,
                None,
                line.line_id,
                "source-audio-declared-available",
            )
            return PreparedSourceAudioPassThrough(
                line.line_id,
                line.text_sha256,
                getattr(line, "source_audio_id", None),
                source_audio_completion,
                ("story-index" if source_audio_completion is not None else None),
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
            prepared, artifact_preflight_state = self.library.find_with_preflight(
                line.line_id,
                line.text_sha256,
            )
            if prepared is not None:
                self.last_synthesis_ms = 0.0
                self.last_first_audio_ms = 0.0
                self.last_audio_source = "generated"
                self.last_line_id = line.line_id
                self.last_route_trace = AudioRouteTrace(
                    None,
                    "generated",
                    match_result,
                    ";".join(fallback_reasons) or None,
                    None,
                    line.line_id,
                    artifact_preflight_state,
                )
                return prepared
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
        prepared = self.live_backend.prepare(character, text)
        live_source = getattr(self.live_backend, "last_audio_source", None)
        self.last_audio_source = (
            live_source
            if isinstance(live_source, str) and live_source
            else f"live:{self.live_backend.name}"
        )
        self.last_line_id = line.line_id if line is not None else None
        self.last_synthesis_ms = getattr(self.live_backend, "last_synthesis_ms", None)
        self.last_first_audio_ms = getattr(
            self.live_backend, "last_first_audio_ms", None
        )
        self.last_route_trace = AudioRouteTrace(
            None,
            self.last_audio_source,
            match_result,
            ";".join(dict.fromkeys(fallback_reasons)) or None,
            None,
            self.last_line_id,
            artifact_preflight_state,
        )
        return prepared

    def _resolve_line(self, character, text, voice_overridden):
        if voice_overridden:
            return None, "skipped"
        resolve = getattr(self.line_resolver, "resolve_exact_with_result", None)
        if callable(resolve):
            return resolve(character, text)
        line = self.line_resolver.resolve_exact(character, text)
        return line, "exact" if line is not None else "no-match"

    def play(self, prepared, *, playback_guard=None):
        if isinstance(prepared, PreparedSourceAudioPassThrough):
            self.last_playback_ms = None
            self.last_playback_underrun = False
            if playback_guard is not None and not playback_guard():
                return False
            if prepared.completion_seconds is None:
                return True
            with self.playback_lock:
                if playback_guard is not None and not playback_guard():
                    return False
                started = self.clock()
                self.source_audio_completion_stop.clear()
                try:
                    self.playback_active = True
                    interrupted = self.source_audio_completion_stop.wait(
                        prepared.completion_seconds
                    )
                    if interrupted:
                        return False
                    return playback_guard is None or bool(playback_guard())
                finally:
                    self.playback_active = False
                    self.last_playback_ms = (self.clock() - started) * 1000
        if not isinstance(prepared, PreparedGeneratedAudio):
            result = self.live_backend.play(prepared, playback_guard=playback_guard)
            self._copy_live_metrics()
            return result
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
                samples = np.asarray(prepared.samples, dtype=np.float32) * self.volume
                samples, sample_rate = match_output_sample_rate(
                    self.audio_output,
                    samples,
                    prepared.sample_rate,
                )
                self.audio_output.play(
                    samples,
                    sample_rate,
                    latency=self.playback_latency,
                )
                status = self.audio_output.wait()
                self.last_playback_underrun = bool(
                    getattr(status, "output_underflow", False)
                )
            except Exception as error:
                raise AudioPlaybackError(str(error)) from error
            finally:
                self.playback_active = False
                self.last_playback_ms = (self.clock() - started) * 1000
        return True

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
        if was_playing:
            self.source_audio_completion_stop.set()
            self.audio_output.stop()
        return bool(self.live_backend.stop()) or was_playing

    def _copy_live_metrics(self):
        for name in (
            "last_synthesis_ms",
            "last_first_audio_ms",
            "last_playback_ms",
            "last_playback_underrun",
        ):
            if hasattr(self.live_backend, name):
                setattr(self, name, getattr(self.live_backend, name))


def _read_pcm16_mono_wav(path):
    pcm, info = read_pcm16_mono_wav(path)
    samples = np.asarray(pcm, dtype=np.float32) / 32768.0
    return samples, info.sample_rate
