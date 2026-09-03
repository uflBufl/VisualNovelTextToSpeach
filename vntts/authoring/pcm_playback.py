"""Persistent PCM playback for authoring interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import gcd
from pathlib import Path
from threading import Lock

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


class PcmPlaybackError(RuntimeError):
    pass


@dataclass(frozen=True)
class PcmClip:
    samples: np.ndarray
    sample_rate: int

    @property
    def frames(self):
        return len(self.samples)

    @property
    def duration_ms(self):
        return round(self.frames * 1000 / self.sample_rate)


@dataclass(frozen=True)
class PlaybackSnapshot:
    token: int
    position_frames: int
    total_frames: int
    started: bool
    playing: bool
    finished: bool
    underflowed: bool
    error: str | None


class PersistentPcmPlayer:
    """Keep one fixed-format device stream alive and swap prepared PCM buffers."""

    def __init__(self, audio_module=None, *, latency=0.1):
        if audio_module is None:
            import sounddevice as audio_module

        self.audio_module = audio_module
        try:
            device = audio_module.query_devices(kind="output")
            self.sample_rate = int(round(float(device["default_samplerate"])))
            max_channels = int(device["max_output_channels"])
        except Exception as error:
            raise PcmPlaybackError(
                f"Unable to query the output device: {error}"
            ) from error
        if self.sample_rate <= 0 or max_channels <= 0:
            raise PcmPlaybackError("The default output device has no usable PCM format")
        self.channels = min(2, max_channels)
        self._lock = Lock()
        self._token = 0
        self._samples = np.empty((0, self.channels), dtype=np.float32)
        self._position = 0
        self._audible_origin = 0
        self._first_dac_time = None
        self._end_dac_time = None
        self._playing = False
        self._started = False
        self._underflowed = False
        self._error = None
        self._closed = False
        self.stream = None
        try:
            self.stream = audio_module.OutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                latency=latency,
                blocksize=0,
                callback=self._callback,
                prime_output_buffers_using_stream_callback=True,
            )
            self.stream.start()
        except Exception as error:
            if self.stream is not None:
                self.stream.close()
            raise PcmPlaybackError(
                f"Unable to open the output stream: {error}"
            ) from error

    def load(self, path):
        path = Path(path)
        try:
            samples, sample_rate = sf.read(path, dtype="float32", always_2d=True)
        except (OSError, RuntimeError, sf.SoundFileError) as error:
            raise PcmPlaybackError(f"Unable to decode {path.name}: {error}") from error
        return self._prepare(samples, sample_rate, path.name)

    def load_bytes(self, payload, *, name="audio"):
        try:
            samples, sample_rate = sf.read(
                BytesIO(payload), dtype="float32", always_2d=True
            )
        except (OSError, RuntimeError, sf.SoundFileError) as error:
            raise PcmPlaybackError(f"Unable to decode {name}: {error}") from error
        return self._prepare(samples, sample_rate, name)

    def _prepare(self, samples, sample_rate, name):
        if not len(samples) or sample_rate <= 0 or samples.shape[1] not in {1, 2}:
            raise PcmPlaybackError(f"Unsupported PCM layout in {name}")
        if not np.isfinite(samples).all():
            raise PcmPlaybackError(f"Non-finite PCM samples in {name}")
        if sample_rate != self.sample_rate:
            divisor = gcd(sample_rate, self.sample_rate)
            samples = resample_poly(
                samples,
                self.sample_rate // divisor,
                sample_rate // divisor,
                axis=0,
            ).astype(np.float32, copy=False)
        if samples.shape[1] == 1 and self.channels == 2:
            samples = np.repeat(samples, 2, axis=1)
        elif samples.shape[1] == 2 and self.channels == 1:
            samples = samples.mean(axis=1, keepdims=True, dtype=np.float32)
        return PcmClip(
            np.ascontiguousarray(samples, dtype=np.float32), self.sample_rate
        )

    def play(self, clip, *, position_frames=0):
        if not isinstance(clip, PcmClip) or clip.sample_rate != self.sample_rate:
            raise PcmPlaybackError("Playback received an incompatible PCM clip")
        position = max(0, min(clip.frames, int(position_frames)))
        with self._lock:
            self._token += 1
            self._samples = clip.samples
            self._position = position
            self._audible_origin = position
            self._first_dac_time = None
            self._end_dac_time = None
            self._playing = position < clip.frames
            self._started = False
            self._underflowed = False
            self._error = None
            return self._token

    def pause(self):
        with self._lock:
            self._playing = False
            self._first_dac_time = None
            self._audible_origin = self._position
            self._end_dac_time = None

    def resume(self):
        with self._lock:
            if self._position >= len(self._samples):
                self._position = 0
            self._token += 1
            self._audible_origin = self._position
            self._first_dac_time = None
            self._end_dac_time = None
            self._playing = bool(len(self._samples))
            self._started = False
            self._underflowed = False
            self._error = None
            return self._token

    def seek(self, position_frames):
        with self._lock:
            self._token += 1
            self._position = max(0, min(len(self._samples), int(position_frames)))
            self._audible_origin = self._position
            self._first_dac_time = None
            self._end_dac_time = None
            self._started = False
            self._underflowed = False
            self._error = None
            return self._token

    def stop(self):
        with self._lock:
            self._token += 1
            self._samples = np.empty((0, self.channels), dtype=np.float32)
            self._position = 0
            self._audible_origin = 0
            self._first_dac_time = None
            self._end_dac_time = None
            self._playing = False
            self._started = False
            self._underflowed = False
            self._error = None

    def snapshot(self):
        try:
            stream_time = float(self.stream.time)
        except Exception as error:
            stream_time = 0.0
            with self._lock:
                self._error = str(error)
        with self._lock:
            total = len(self._samples)
            position = self._position
            if self._first_dac_time is not None:
                audible = self._audible_origin + round(
                    max(0.0, stream_time - self._first_dac_time) * self.sample_rate
                )
                position = min(position, audible)
            finished = (
                self._end_dac_time is not None and stream_time >= self._end_dac_time
            )
            if finished:
                position = total
            return PlaybackSnapshot(
                token=self._token,
                position_frames=position,
                total_frames=total,
                started=self._started,
                playing=self._playing,
                finished=finished,
                underflowed=self._underflowed,
                error=self._error,
            )

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop()
        try:
            self.stream.abort()
        finally:
            self.stream.close()

    def _callback(self, outdata, frames, time_info, status):
        outdata.fill(0)
        try:
            with self._lock:
                if not self._playing:
                    return
                if getattr(status, "output_underflow", False):
                    self._underflowed = True
                end = min(self._position + frames, len(self._samples))
                count = end - self._position
                if count:
                    outdata[:count] = self._samples[self._position : end]
                    if self._first_dac_time is None:
                        self._first_dac_time = float(time_info.outputBufferDacTime)
                        self._audible_origin = self._position
                    self._position = end
                    self._started = True
                if self._position >= len(self._samples):
                    self._playing = False
                    self._end_dac_time = float(time_info.outputBufferDacTime) + (
                        count / self.sample_rate
                    )
        except Exception as error:
            with self._lock:
                self._playing = False
                self._error = str(error)
