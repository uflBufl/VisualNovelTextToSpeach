"""PCM preparation and native clip playback for authoring interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import gcd
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import soundfile as sf
from numpy.typing import NDArray
from scipy.signal import resample_poly

from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.path_safety import open_regular_binary
from vntts.rust_clip_player import ClipStatus, RustClipController


class PcmPlaybackError(RuntimeError):
    pass


class _ClipController(Protocol):
    @property
    def device_changed(self) -> bool: ...

    def query_device(self) -> dict[str, Any]: ...

    def load(
        self,
        samples: NDArray[np.float32],
        sample_rate: int,
        channels: int,
        latency: float,
    ) -> None: ...

    def start(self) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> bool: ...

    def seek(self, position_frames: int) -> bool: ...

    def status(self) -> ClipStatus: ...

    def stop(self) -> None: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


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
    """Keep a native worker warm and rebuild clips when the output changes."""

    def __init__(
        self, controller: _ClipController | None = None, *, latency: float = 0.1
    ) -> None:
        if controller is None:
            from vntts.system_audio import get_system_audio_output

            try:
                # Establish the monitor baseline before starting the dedicated worker.
                get_system_audio_output().query_devices(kind="output")
                controller = RustClipController()
            except Exception as error:
                raise PcmPlaybackError(
                    f"Unable to initialize audio playback: {error}"
                ) from error
        self.controller = controller
        self._latency = latency
        try:
            self._query_device()
        except BaseException as error:
            attempt_cleanup(
                controller.close,
                description="Native player cleanup",
                primary_error=error,
            )
            raise
        self._token = 0
        self._clip: PcmClip | None = None
        self._samples: NDArray[np.float32] = np.empty(
            (0, self.channels), dtype=np.float32
        )
        self._position = self._submitted_position = self._onset_position = 0
        self._playing = self._paused = self._started = self._completed = False
        self._underflowed = False
        self._error: str | None = None
        self._closed = False
        self._loaded = self._native_started = False
        self._needs_device = False

    def _query_device(self) -> None:
        try:
            device = self.controller.query_device()
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

    def _ensure_output(self, *, restart_playback: bool = True) -> None:
        if self._closed:
            raise PcmPlaybackError("The PCM player is closed")
        if not self._needs_device and not self.controller.device_changed:
            return
        restart = restart_playback and self._playing and not self._completed
        completed = self._completed
        if self._loaded:
            try:
                self._read_status()
            except Exception:
                # Device teardown can invalidate its clock before status is read.
                pass
        position = self._logical_position(self._position)
        self._playing = False
        self._loaded = self._native_started = False
        self._needs_device = True
        self.controller.reset()
        self._query_device()
        self._needs_device = False
        self._samples = (
            self._convert_samples(self._clip.samples, self._clip.sample_rate)
            if self._clip is not None
            else np.empty((0, self.channels), dtype=np.float32)
        )
        self._position = 0 if restart else self._native_position(position)
        self._started = False
        self._completed = completed
        if restart:
            self._load_native()
            self.controller.start()
            self._native_started = self._playing = True

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

    def _load_native(self) -> None:
        self.controller.load(
            self._samples, self.sample_rate, self.channels, self._latency
        )
        self._loaded = True
        self._native_started = False
        self._submitted_position = self._onset_position = self._position
        if self._position:
            self.controller.seek(self._position)

    def _read_status(self) -> None:
        if not self._loaded:
            return
        state = self.controller.status()
        self._position = state["audible_position_frames"]
        self._submitted_position = state["position_frames"]
        self._started = (
            state["started"] and self._submitted_position > self._onset_position
        )
        self._paused = state["paused"]
        # A submitted final buffer is still playing until its native DAC drain.
        self._playing = (
            self._native_started and not state["paused"] and not state["finished"]
        )
        self._completed = state["finished"]
        self._underflowed |= bool(state["underflow_count"])
        self._error = self._error or state["error"]
        if self._completed:
            self._loaded = False

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
        self._ensure_output(restart_playback=False)
        samples = self._prepare(clip.samples, clip.sample_rate, "PCM clip").samples
        self._clip = clip
        self._samples = samples
        self._position = self._native_position(
            max(0, min(clip.frames, int(position_frames)))
        )
        self._playing = self._paused = self._started = self._completed = False
        self._underflowed = False
        self._error = None
        self._token += 1
        try:
            self._load_native()
            if self._position < len(samples):
                self.controller.start()
                self._native_started = self._playing = True
        except Exception as error:
            self._needs_device = True
            self._error = str(error)
            raise PcmPlaybackError(
                f"Unable to start native playback: {error}"
            ) from error
        return self._token

    def pause(self) -> None:
        self._read_status()
        if self._loaded and self._native_started and not self._paused:
            self.controller.pause()
            self._read_status()
        self._playing = False
        self._paused = True

    def resume(self) -> int:
        self._ensure_output()
        self._read_status()
        if self._position >= len(self._samples):
            self._position = 0
        self._token += 1
        self._started = self._completed = False
        self._onset_position = self._submitted_position
        if self._clip is not None:
            if not self._loaded:
                self._load_native()
            if self._native_started and self._paused:
                if not self.controller.resume():
                    self._position = 0
                    self._load_native()
            if not self._native_started:
                self.controller.start()
            self._native_started = self._playing = True
            self._paused = False
        return self._token

    def seek(self, position_frames: int) -> int:
        self._ensure_output()
        self._read_status()
        self._position = self._native_position(max(0, int(position_frames)))
        self._onset_position = self._position
        if self._clip is not None:
            if self._loaded and not self.controller.seek(self._position):
                self._loaded = self._playing = False
            if not self._loaded:
                self._load_native()
        self._token += 1
        self._started = self._completed = False
        return self._token

    def stop(self) -> None:
        try:
            self.controller.stop()
        finally:
            self._clip = None
            self._samples = np.empty((0, self.channels), dtype=np.float32)
            self._position = 0
            self._playing = self._paused = self._started = self._completed = False
            self._loaded = self._native_started = False
            self._underflowed = False
            self._error = None
            self._token += 1

    def snapshot(self) -> PlaybackSnapshot:
        try:
            self._ensure_output()
            try:
                self._read_status()
            except Exception:
                if not self.controller.device_changed:
                    raise
                self._ensure_output()
                self._read_status()
        except Exception as error:
            self._playing = False
            self._needs_device = True
            self._error = self._error or str(error)
        return PlaybackSnapshot(
            token=self._token,
            position_frames=self._logical_position(self._position),
            total_frames=self._clip.frames if self._clip is not None else 0,
            started=self._started,
            playing=self._playing,
            finished=self._completed,
            underflowed=self._underflowed,
            error=self._error,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with cleanup_on_exit(
            self.controller.close, description="Native player cleanup"
        ):
            self.stop()
