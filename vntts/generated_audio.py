"""Ahead-of-time generated audio with verified live-TTS fallback."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from threading import Lock
from time import monotonic

import numpy as np
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)

from vntts.services.tts_engine import AudioPlaybackError, match_output_sample_rate
from vntts.speech_backend_runtime import BoundedCache, validate_speed, validate_volume


@dataclass(frozen=True)
class PreparedGeneratedAudio:
    line_id: str
    text_sha256: str
    samples: np.ndarray
    sample_rate: int


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
        entry = self.index.find(line_id, text_sha256, verify_file=False)
        if entry is None:
            return None
        try:
            stat = entry.audio.stat()
        except OSError:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None
        cache_key = (
            entry.line_id,
            entry.text_sha256,
            entry.audio_sha256,
            stat.st_size,
            stat.st_mtime_ns,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        if self.index.find(line_id, text_sha256) is None:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None
        try:
            samples, sample_rate = _read_pcm16_mono_wav(entry.audio)
        except (OSError, ValueError, wave.Error) as error:
            self._warn_once(
                entry, f"Generated audio is invalid: {entry.audio}: {error}"
            )
            return None
        if sample_rate != entry.sample_rate or len(samples) != entry.sample_count:
            self._warn_once(
                entry,
                f"Generated audio metadata does not match the WAV file: {entry.audio}",
            )
            return None
        prepared = PreparedGeneratedAudio(
            line_id=entry.line_id,
            text_sha256=entry.text_sha256,
            samples=samples,
            sample_rate=sample_rate,
        )
        self.cache.put(cache_key, prepared)
        return prepared

    def _warn_once(self, entry, message):
        identity = entry.line_id, entry.text_sha256
        if identity in self.warned_entries:
            return
        self.warned_entries.add(identity)
        self.warn(message)


class GeneratedAudioFallbackBackend:
    """Prefer exact local generations and delegate every miss to live TTS."""

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
        self.name = f"generated-audio+{live_backend.name}"
        self.capabilities = live_backend.capabilities
        self.playback_lock = Lock()
        self.playback_active = False
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None
        self.last_playback_ms = None
        self.last_playback_underrun = False
        self.set_volume(volume, delegate=False)
        self.set_speed(speed, delegate=False)

    def prepare(self, character, text):
        line = self.line_resolver.resolve_exact(character, text)
        if line is not None and line.line_id and self.speed == 1.0:
            prepared = self.library.find(line.line_id, line.text_sha256)
            if prepared is not None:
                self.last_synthesis_ms = 0.0
                self.last_first_audio_ms = 0.0
                return prepared
        prepared = self.live_backend.prepare(character, text)
        self.last_synthesis_ms = getattr(self.live_backend, "last_synthesis_ms", None)
        self.last_first_audio_ms = getattr(
            self.live_backend, "last_first_audio_ms", None
        )
        return prepared

    def play(self, prepared, *, playback_guard=None):
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
        configure = getattr(self.live_backend, "set_live_mode_active", None)
        return configure(active) if callable(configure) else bool(active)

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
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("compressed WAV is not supported")
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("expected mono 16-bit PCM WAV")
        sample_rate = source.getframerate()
        frame_count = source.getnframes()
        frames = source.readframes(frame_count)
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if len(samples) != frame_count or not np.all(np.isfinite(samples)):
        raise ValueError("WAV sample data is incomplete or invalid")
    return samples, sample_rate
