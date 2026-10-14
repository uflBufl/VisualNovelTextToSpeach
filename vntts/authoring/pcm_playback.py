"""Persistent PCM playback for authoring interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import gcd
from pathlib import Path
from threading import Lock
from typing import NotRequired, Protocol, TypedDict

import numpy as np
import soundfile as sf
from numpy.typing import NDArray
from scipy.signal import resample_poly

from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.path_safety import open_regular_binary
from vntts.system_audio import AudioDeviceChangedError


class PcmPlaybackError(RuntimeError):
    pass


class _AudioStream(Protocol):
    @property
    def time(self) -> float: ...

    def start(self) -> None: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


class _OutputDevice(TypedDict):
    default_samplerate: float
    max_output_channels: int
    name: NotRequired[str]


class _AudioModule(Protocol):
    def query_devices(self, *, kind: str) -> _OutputDevice: ...

    def OutputStream(self, **options: object) -> _AudioStream: ...


class _CallbackTimeInfo(Protocol):
    outputBufferDacTime: float


@dataclass(frozen=True)
class PcmClip:
    samples: NDArray[np.float32]
    sample_rate: int

    @property
    def frames(self) -> int:
        return len(self.samples)

    @property
    def duration_ms(self) -> int:
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
    """Keep a warm device stream and rebuild it when the system output changes."""

    def __init__(
        self, audio_module: _AudioModule | None = None, *, latency: float = 0.1
    ) -> None:
        if (
            audio_module is None
            or getattr(audio_module, "__name__", None) == "sounddevice"
        ):
            from vntts.system_audio import get_system_audio_output

            audio_module = get_system_audio_output()
        assert audio_module is not None
        self.audio_module: _AudioModule = audio_module
        self._latency = latency
        self._query_device()
        self._lock = Lock()
        self._token = 0
        self._clip: PcmClip | None = None
        self._samples: NDArray[np.float32] = np.empty(
            (0, self.channels), dtype=np.float32
        )
        self._position = 0
        self._audible_origin = 0
        self._first_dac_time: float | None = None
        self._end_dac_time: float | None = None
        self._playing = False
        self._started = False
        self._completed = False
        self._underflowed = False
        self._error: str | None = None
        self._closed = False
        self.stream: _AudioStream | None = None
        self._open_stream()

    def _query_device(self) -> None:
        try:
            device = self.audio_module.query_devices(kind="output")
            self.sample_rate = round(float(device["default_samplerate"]))
            max_channels = int(device["max_output_channels"])
            self.device_name = str(device.get("name", ""))
        except Exception as error:
            raise PcmPlaybackError(
                f"Unable to query the output device: {error}"
            ) from error
        if self.sample_rate <= 0 or max_channels <= 0:
            raise PcmPlaybackError("The default output device has no usable PCM format")
        self.channels = min(2, max_channels)

    def _open_stream(self) -> None:
        stream: _AudioStream | None = None
        try:
            stream = self.audio_module.OutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                latency=self._latency,
                blocksize=0,
                callback=self._callback,
                prime_output_buffers_using_stream_callback=True,
            )
            stream.start()
        except Exception as error:
            with self._lock:
                self._playing = False
            if stream is not None:
                attempt_cleanup(
                    stream.close,
                    description="Output stream cleanup",
                    primary_error=error,
                )
            raise PcmPlaybackError(
                f"Unable to open the output stream: {error}"
            ) from error
        assert stream is not None
        self.stream = stream

    def _ensure_stream(self, *, restart_playback: bool = True) -> None:
        if self._closed:
            raise PcmPlaybackError("The PCM player is closed")
        stream = self.stream
        if stream is not None and not getattr(stream, "device_changed", False):
            return
        with self._lock:
            restart = restart_playback and (
                self._playing
                or (self._end_dac_time is not None and not self._completed)
            )
            completed = self._completed
            position = self._logical_position(self._position)
            self._playing = False
        if stream is not None:
            # Native teardown can wait for the callback, which takes our lock.
            with cleanup_on_exit(stream.close, description="Output stream cleanup"):
                stream.abort()
        self.stream = None
        self._query_device()
        samples = (
            self._convert_samples(self._clip.samples, self._clip.sample_rate)
            if self._clip is not None
            else np.empty((0, self.channels), dtype=np.float32)
        )
        with self._lock:
            self._samples = samples
            self._position = 0 if restart else self._native_position(position)
            self._reset_timeline(new_token=False)
            self._completed = completed
            self._playing = restart and bool(len(samples))
        self._open_stream()

    def _logical_position(self, position: int) -> int:
        if self._clip is None:
            return 0
        return min(
            self._clip.frames,
            round(position * self._clip.sample_rate / self.sample_rate),
        )

    def _native_position(self, position: int) -> int:
        if self._clip is None:
            return 0
        return min(
            len(self._samples),
            round(position * self.sample_rate / self._clip.sample_rate),
        )

    def load(self, path: str | Path) -> PcmClip:
        path = Path(path)
        try:
            with open_regular_binary(path) as source:
                samples, sample_rate = sf.read(source, dtype="float32", always_2d=True)
        except (OSError, RuntimeError, sf.SoundFileError) as error:
            raise PcmPlaybackError(f"Unable to decode {path.name}: {error}") from error
        return self._prepare(samples, sample_rate, path.name)

    def load_bytes(self, payload: bytes, *, name: str = "audio") -> PcmClip:
        try:
            samples, sample_rate = sf.read(
                BytesIO(payload), dtype="float32", always_2d=True
            )
        except (OSError, RuntimeError, sf.SoundFileError) as error:
            raise PcmPlaybackError(f"Unable to decode {name}: {error}") from error
        return self._prepare(samples, sample_rate, name)

    def _prepare(
        self, samples: NDArray[np.float32], sample_rate: int, name: str
    ) -> PcmClip:
        if (
            samples.ndim != 2
            or not len(samples)
            or sample_rate <= 0
            or samples.shape[1] not in {1, 2}
        ):
            raise PcmPlaybackError(f"Unsupported PCM layout in {name}")
        if not np.isfinite(samples).all():
            raise PcmPlaybackError(f"Non-finite PCM samples in {name}")
        return PcmClip(self._convert_samples(samples, sample_rate), self.sample_rate)

    def _convert_samples(
        self, samples: NDArray[np.float32], sample_rate: int
    ) -> NDArray[np.float32]:
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
        return np.ascontiguousarray(samples, dtype=np.float32)

    def play(self, clip: PcmClip, *, position_frames: int = 0) -> int:
        if not isinstance(clip, PcmClip) or clip.sample_rate <= 0:
            raise PcmPlaybackError("Playback received an incompatible PCM clip")
        self._ensure_stream(restart_playback=False)
        samples = self._prepare(clip.samples, clip.sample_rate, "PCM clip").samples
        position = max(0, min(clip.frames, int(position_frames)))
        with self._lock:
            self._clip = clip
            self._samples = samples
            self._position = self._native_position(position)
            self._playing = position < clip.frames
            self._underflowed = False
            self._error = None
            return self._reset_timeline()

    def pause(self) -> None:
        with self._lock:
            self._playing = False
            self._first_dac_time = None
            self._audible_origin = self._position
            self._end_dac_time = None

    def resume(self) -> int:
        self._ensure_stream()
        with self._lock:
            if self._position >= len(self._samples):
                self._position = 0
            self._playing = bool(len(self._samples))
            return self._reset_timeline()

    def seek(self, position_frames: int) -> int:
        with self._lock:
            self._position = self._native_position(max(0, int(position_frames)))
            return self._reset_timeline()

    def stop(self) -> None:
        with self._lock:
            self._clip = None
            self._samples = np.empty((0, self.channels), dtype=np.float32)
            self._position = 0
            self._playing = False
            self._underflowed = False
            self._error = None
            self._reset_timeline()

    def _reset_timeline(self, *, new_token: bool = True) -> int:
        # Called under the lock; only a new play or stop clears attempt failures.
        if new_token:
            self._token += 1
        self._audible_origin = self._position
        self._first_dac_time = None
        self._end_dac_time = None
        self._started = False
        self._completed = False
        return self._token

    def snapshot(self) -> PlaybackSnapshot:
        try:
            if self.stream is None:
                raise PcmPlaybackError("Audio output is unavailable; replay the sample")
            self._ensure_stream()
            assert self.stream is not None
            try:
                stream_time = float(self.stream.time)
            except AudioDeviceChangedError:
                self._ensure_stream()
                assert self.stream is not None
                stream_time = float(self.stream.time)
        except Exception as error:
            stream_time = 0.0
            with self._lock:
                self._error = self._error or str(error)
        with self._lock:
            total = self._clip.frames if self._clip is not None else 0
            position = self._position
            if self._first_dac_time is not None:
                audible = self._audible_origin + round(
                    max(0.0, stream_time - self._first_dac_time) * self.sample_rate
                )
                position = min(position, audible)
            finished = self._completed or (
                self._end_dac_time is not None and stream_time >= self._end_dac_time
            )
            if finished:
                self._completed = True
                position = len(self._samples)
            return PlaybackSnapshot(
                token=self._token,
                position_frames=self._logical_position(position),
                total_frames=total,
                started=self._started,
                playing=self._playing,
                finished=finished,
                underflowed=self._underflowed,
                error=self._error,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.stream is None:
            self.stop()
            return
        with (
            cleanup_on_exit(self.stream.close, description="Output stream cleanup"),
            cleanup_on_exit(self.stream.abort, description="Output stream abort"),
        ):
            self.stop()

    def _callback(
        self,
        outdata: NDArray[np.float32],
        frames: int,
        time_info: _CallbackTimeInfo,
        status: object,
    ) -> None:
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
                self._error = self._error or str(error)
