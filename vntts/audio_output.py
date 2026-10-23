"""Small shared helpers for lazy audio output and playback status."""

from collections.abc import Callable, Mapping
from threading import Event, Lock, current_thread
from time import monotonic
from types import ModuleType
from typing import Protocol, TypeAlias, TypeGuard, cast, overload
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray
from scipy.signal import resample_poly

from vntts.audio_lifecycle import audio_lifecycle_context, record_audio_lifecycle
from vntts.playback import (
    PlaybackOutcome,
    PlaybackStatus,
    PreparedPlayback,
    outcome_for_prepared,
)

AudioData: TypeAlias = NDArray[np.float32]


def sanitize_playback_audio(samples: AudioData, volume: float) -> None:
    """Sanitize, scale and clip an owned playback buffer in place."""
    np.nan_to_num(samples, copy=False)
    samples *= volume
    np.clip(samples, -0.95, 0.95, out=samples)


class StreamingAudioStream(Protocol):
    def write(self, audio: object) -> object: ...

    def abort(self) -> object: ...


class _StreamingAudioContext(StreamingAudioStream, Protocol):
    def __enter__(self) -> _StreamingAudioContext: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> object: ...

    def stop(self) -> object: ...

    def close(self) -> object: ...


class PlaybackAudioOutput(Protocol):
    def query_devices(self, *, kind: str) -> object: ...

    def play(self, audio: object, sample_rate: int, *, latency: object) -> object: ...

    def wait(self) -> object: ...

    def stop(self) -> object: ...


class AudioOutput(PlaybackAudioOutput, Protocol):
    def OutputStream(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        latency: object,
    ) -> _StreamingAudioContext: ...


def _supports_streaming_output(value: object) -> TypeGuard[AudioOutput]:
    return all(
        callable(getattr(value, name, None))
        for name in (
            "query_devices",
            "play",
            "wait",
            "stop",
            "OutputStream",
        )
    )


@overload
def resolve_audio_output(audio_output: None) -> AudioOutput: ...


@overload
def resolve_audio_output(audio_output: AudioOutput) -> AudioOutput: ...


@overload
def resolve_audio_output(
    audio_output: PlaybackAudioOutput,
) -> PlaybackAudioOutput: ...


def resolve_audio_output(
    audio_output: PlaybackAudioOutput | None,
) -> PlaybackAudioOutput:
    """Return an injected output or the Rust-owned system output."""
    if audio_output is None:
        from vntts.system_audio import get_system_audio_output

        return _LoggedAudioOutput(get_system_audio_output())
    if isinstance(audio_output, ModuleType) and audio_output.__name__ == "sounddevice":
        from vntts.system_audio import get_system_audio_output

        return _LoggedAudioOutput(get_system_audio_output())
    if isinstance(audio_output, _LoggedAudioOutput):
        return audio_output
    if getattr(
        audio_output, "__name__", None
    ) == "sounddevice" and _supports_streaming_output(audio_output):
        return _LoggedAudioOutput(audio_output)
    return audio_output


def prepared_output_stream(
    audio_output: PlaybackAudioOutput,
    *,
    samplerate: int,
    channels: int,
    dtype: str,
    latency: object,
) -> _StreamingAudioContext:
    """Open prepared audio on the same Rust owner as streaming output."""
    return cast(AudioOutput, audio_output).OutputStream(
        samplerate=samplerate, channels=channels, dtype=dtype, latency=latency
    )


class _LoggedOutputStream:
    def __init__(
        self,
        stream: _StreamingAudioContext,
        stream_id: str,
        fields: dict[str, object],
        context: dict[str, object],
    ) -> None:
        self.context_stream = stream
        self.stream = stream
        self.stream_id = stream_id
        self.fields = fields
        self.context = context

    def _record(self, operation: str, outcome: str, reason: str) -> None:
        record_audio_lifecycle(
            operation,
            **self.context,
            **self.fields,
            stream_id=self.stream_id,
            outcome=outcome,
            reason=reason,
            owner=current_thread().name,
        )

    def __enter__(self) -> _LoggedOutputStream:
        try:
            entered = self.stream.__enter__()
        except Exception:
            self._record("start", "failed", "context-enter")
            raise
        if entered is not None:
            self.stream = entered
        self._record("start", "complete", "context-enter")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> object:
        self._record("stop", "requested", "context-exit")
        try:
            result = self.context_stream.__exit__(exc_type, exc_value, traceback)
        except Exception:
            self._record("close", "failed", "context-exit")
            raise
        self._record("stop", "complete", "context-exit")
        self._record("close", "complete", "context-exit")
        return result

    def write(self, audio: object) -> object:
        return self.stream.write(audio)

    def abort(self) -> object:
        self._record("abort", "requested", "caller-request")
        try:
            result = self.stream.abort()
        except Exception:
            self._record("abort", "failed", "caller-request")
            raise
        self._record("abort", "complete", "caller-request")
        return result

    def stop(self) -> object:
        self._record("stop", "requested", "caller-request")
        try:
            result = self.stream.stop()
        except Exception:
            self._record("stop", "failed", "caller-request")
            raise
        self._record("stop", "complete", "caller-request")
        return result

    def close(self) -> object:
        try:
            result = self.stream.close()
        except Exception:
            self._record("close", "failed", "caller-request")
            raise
        self._record("close", "complete", "caller-request")
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self.stream, name)


class _LoggedAudioOutput:
    def __init__(self, output: AudioOutput) -> None:
        self.output = output
        self.lock = Lock()
        self.convenience: tuple[str, dict[str, object], dict[str, object]] | None = None

    def query_devices(self, *, kind: str) -> object:
        return self.output.query_devices(kind=kind)

    def _device_fields(self) -> dict[str, object]:
        fields: dict[str, object] = {}
        try:
            device = self.output.query_devices(kind="output")
            if isinstance(device, Mapping):
                fields["device_name"] = device.get("name")
                host_api_index = device.get("hostapi")
                query_hostapis = getattr(self.output, "query_hostapis", None)
                if callable(query_hostapis) and host_api_index is not None:
                    host_api = query_hostapis(host_api_index)
                    if isinstance(host_api, Mapping):
                        fields["host_api"] = host_api.get("name")
        except Exception:
            pass
        return fields

    def OutputStream(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        latency: object,
    ) -> _LoggedOutputStream:
        stream_id = uuid4().hex
        fields = {
            **self._device_fields(),
            "sample_rate": samplerate,
            "channels": channels,
            "dtype": dtype,
            "latency": latency,
        }
        context = dict(audio_lifecycle_context.get() or {})
        try:
            token = audio_lifecycle_context.set({**context, "stream_id": stream_id})
            try:
                stream = self.output.OutputStream(
                    samplerate=samplerate,
                    channels=channels,
                    dtype=dtype,
                    latency=latency,
                )
            finally:
                audio_lifecycle_context.reset(token)
        except Exception:
            record_audio_lifecycle(
                "open",
                **context,
                **fields,
                stream_id=stream_id,
                outcome="failed",
                reason="output-stream-construction",
                owner=current_thread().name,
            )
            raise
        record_audio_lifecycle(
            "open",
            **context,
            **fields,
            stream_id=stream_id,
            outcome="complete",
            reason="output-stream-construction",
            owner=current_thread().name,
        )
        return _LoggedOutputStream(stream, stream_id, fields, context)

    def play(self, audio: object, sample_rate: int, *, latency: object) -> object:
        stream_id = uuid4().hex
        context = dict(audio_lifecycle_context.get() or {})
        fields = {
            **self._device_fields(),
            "sample_rate": sample_rate,
            "channels": 1,
            "dtype": str(getattr(audio, "dtype", "unknown")),
            "latency": latency,
        }
        try:
            result = self.output.play(audio, sample_rate, latency=latency)
        except Exception:
            record_audio_lifecycle(
                "open",
                **context,
                **fields,
                stream_id=stream_id,
                outcome="failed",
                reason="convenience-play",
                owner=current_thread().name,
            )
            raise
        self._finish_convenience("abort", "convenience-replaced", "complete")
        with self.lock:
            self.convenience = stream_id, fields, context
        record_audio_lifecycle(
            "open",
            **context,
            **fields,
            stream_id=stream_id,
            outcome="complete",
            reason="convenience-play",
            owner=current_thread().name,
        )
        return result

    def wait(self) -> object:
        outcome = "failed"
        try:
            result = self.output.wait()
            outcome = "complete"
            return result
        finally:
            self._finish_convenience("close", "convenience-wait", outcome)

    def stop(self) -> object:
        outcome = "failed"
        try:
            result = self.output.stop()
            outcome = "complete"
            return result
        finally:
            self._finish_convenience("abort", "convenience-stop", outcome)

    def _finish_convenience(self, operation: str, reason: str, outcome: str) -> None:
        with self.lock:
            active, self.convenience = self.convenience, None
        if active is None:
            return
        stream_id, fields, context = active
        record_audio_lifecycle(
            operation,
            **context,
            **fields,
            stream_id=stream_id,
            outcome=outcome,
            reason=reason,
            owner=current_thread().name,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self.output, name)


def match_output_sample_rate(
    audio_output: PlaybackAudioOutput | None,
    audio: AudioData,
    source_sample_rate: int,
) -> tuple[AudioData, int]:
    """Resample once in Python instead of relying on a live device converter."""
    query_devices = getattr(audio_output, "query_devices", None)
    if not callable(query_devices):
        return audio, source_sample_rate
    try:
        device = query_devices(kind="output")
        if not isinstance(device, Mapping):
            return audio, source_sample_rate
        default_samplerate = device["default_samplerate"]
        target_sample_rate = round(float(default_samplerate))
    except Exception:
        return audio, source_sample_rate
    if target_sample_rate <= 0 or target_sample_rate == source_sample_rate:
        return audio, source_sample_rate

    divisor = np.gcd(source_sample_rate, target_sample_rate)
    resampled = resample_poly(
        np.asarray(audio, dtype=np.float32),
        target_sample_rate // divisor,
        source_sample_rate // divisor,
        axis=0,
    ).astype(np.float32, copy=False)
    peak = float(np.max(np.abs(resampled))) if resampled.size else 0.0
    if peak > 0.95:
        resampled *= 0.95 / peak
    return resampled, target_sample_rate


def playback_cancelled(
    stop_requested: Event, playback_guard: Callable[[], bool] | None
) -> bool:
    if stop_requested.is_set():
        return True
    playable = playback_guard is None or bool(playback_guard())
    # The guard may wait for resume while Stop remains nonblocking.
    return stop_requested.is_set() or not playable


def stream_underflowed(stream: object) -> bool:
    value = getattr(stream, "underflowed", False)
    return isinstance(value, (bool, np.bool_)) and bool(value)


class _PcmWriteDiagnostics:
    """Keep bounded block associations beside the existing stream aggregates."""

    def __init__(self) -> None:
        self.written_frames = self.write_count = self.underflow_count = 0
        self.first_underflow_frame: int | None = None
        self.max_guard_ms = self.max_write_ms = self.max_write_gap_ms = 0.0
        self.guard_ms = self.gap_ms = 0.0
        self.last_write_end: float | None = None
        self.blocks: dict[str, float | int] = {}

    def before_write(self, started: float) -> None:
        self.gap_ms = (
            0.0
            if self.last_write_end is None
            else (started - self.last_write_end) * 1000
        )
        self.max_write_gap_ms = max(self.max_write_gap_ms, self.gap_ms)

    def record_write(
        self,
        offset: int,
        frames: int,
        sample_rate: int,
        started: float,
        ended: float,
        underflow: bool,
    ) -> None:
        self.last_write_end = ended
        self.write_count += 1
        self.written_frames += frames
        write_ms = (ended - started) * 1000
        block = {
            "write_index": self.write_count,
            "input_frame_offset": offset,
            "input_frames": frames,
            "sample_rate": sample_rate,
            "started_monotonic": started,
            "ended_monotonic": ended,
            "guard_ms": round(self.guard_ms, 3),
            "write_gap_ms": round(self.gap_ms, 3),
            "write_ms": round(write_ms, 3),
        }
        if "max_write_write_index" not in self.blocks or write_ms > self.max_write_ms:
            self.blocks.update(
                {f"max_write_{key}": value for key, value in block.items()}
            )
        self.max_write_ms = max(self.max_write_ms, write_ms)
        if underflow:
            self.underflow_count += 1
            if self.first_underflow_frame is None:
                self.first_underflow_frame = offset
                self.blocks.update(
                    {f"first_underflow_{key}": value for key, value in block.items()}
                )

    def summary(self) -> dict[str, object]:
        return {
            "written_frames": self.written_frames,
            "write_count": self.write_count,
            "underflow_count": self.underflow_count,
            "first_underflow_frame": self.first_underflow_frame,
            "max_guard_ms": round(self.max_guard_ms, 3),
            "max_write_ms": round(self.max_write_ms, 3),
            "max_write_gap_ms": round(self.max_write_gap_ms, 3),
            **self.blocks,
        }


def write_pcm_chunks(
    stream: StreamingAudioStream,
    audio: AudioData,
    sample_rate: int,
    cancelled: Callable[[], bool],
    *,
    first_write: Callable[[], None] | None = None,
    stop_requested: Event | None = None,
) -> tuple[bool, bool]:
    """Admit prepared buffers to Rust; preserve injected output compatibility."""
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)
    from vntts.rust_output import RustOutputStream
    from vntts.system_audio import _SystemStream

    owner = stream.stream if isinstance(stream, _LoggedOutputStream) else stream
    if isinstance(owner, RustOutputStream) or (
        isinstance(owner, _SystemStream) and isinstance(owner.native, RustOutputStream)
    ):
        return owner.play_buffer(samples, cancelled, first_write, stop_requested)
    block_frames = max(1, sample_rate // 20)
    underflowed = False
    wrote = False
    diagnostics = _PcmWriteDiagnostics()
    outcome = "failed"

    def is_cancelled() -> bool:
        started = monotonic()
        result = cancelled()
        diagnostics.guard_ms = (monotonic() - started) * 1000
        diagnostics.max_guard_ms = max(diagnostics.max_guard_ms, diagnostics.guard_ms)
        return result

    try:
        for offset in range(0, len(samples), block_frames):
            if is_cancelled():
                outcome = "interrupted"
                return False, underflowed
            started = monotonic()
            diagnostics.before_write(started)
            block = samples[offset : offset + block_frames]
            underrun = bool(stream.write(block))
            diagnostics.record_write(
                offset, len(block), sample_rate, started, monotonic(), underrun
            )
            underflowed = underrun or underflowed
            if not wrote and first_write is not None:
                first_write()
            wrote = True
        completed = wrote and not is_cancelled()
        outcome = "complete" if completed else "interrupted"
        return completed, underflowed
    finally:
        record_audio_lifecycle(
            "pcm-write-summary",
            outcome=outcome,
            reason="blocking-pcm-loop",
            sample_rate=sample_rate,
            stream_id=getattr(stream, "stream_id", None),
            **diagnostics.summary(),
        )


class SynchronousPcmPlaybackMixin:
    """Shared locking, cancellation and metrics for blocking PCM output."""

    playback_configuration_error = ValueError
    invalid_playback_message = "Playback received an invalid payload"
    audio_output: AudioOutput | None
    playback_latency: object
    sample_rate: int
    playback_lock: Lock
    playback_state_lock: Lock
    playback_active: bool
    active_playback_stop: Event | None
    clock: Callable[[], float]

    def _prepare_audio(self, payload: object) -> AudioData:
        raise NotImplementedError

    def play_prepared(
        self,
        prepared: object,
        *,
        playback_guard: Callable[[], bool] | None = None,
    ) -> PlaybackOutcome:
        if not isinstance(prepared, PreparedPlayback):
            raise self.playback_configuration_error(self.invalid_playback_message)
        if not prepared.generation_completed:
            return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
        if playback_guard is not None and not playback_guard():
            return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
            stop_requested = Event()
            started = self.clock()
            underflowed = False
            first_audio_ms = None
            first_audio_at = None

            def first_write() -> None:
                nonlocal first_audio_ms, first_audio_at
                first_audio_at = self.clock()
                first_audio_ms = (first_audio_at - started) * 1000
                record_audio_lifecycle(
                    "first-pcm",
                    occurred_at=first_audio_at,
                    timing_provenance="measured",
                    reason="first-pcm-write-returned",
                )

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
                with prepared_output_stream(
                    audio_output,
                    samplerate=playback_sample_rate,
                    channels=1,
                    dtype="float32",
                    latency=self.playback_latency,
                ) as stream:

                    def cancelled() -> bool:
                        return playback_cancelled(stop_requested, playback_guard)

                    completed, underflowed = write_pcm_chunks(
                        stream,
                        audio,
                        playback_sample_rate,
                        cancelled,
                        first_write=first_write,
                        stop_requested=stop_requested,
                    )
                interrupted = not completed
            except Exception as error:
                if stop_requested.is_set():
                    return outcome_for_prepared(
                        prepared,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        first_audio_ms=first_audio_ms,
                        first_audio_at=first_audio_at,
                    )
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.FAILED,
                    (self.clock() - started) * 1000,
                    first_audio_ms=first_audio_ms,
                    first_audio_at=first_audio_at,
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
            first_audio_at=first_audio_at,
        )

    def stop(self) -> bool:
        with self.playback_state_lock:
            was_playing = self.playback_active
            stop_requested = self.active_playback_stop
            if was_playing and stop_requested is not None:
                stop_requested.set()
        return was_playing

    def _resolve_audio_output(self) -> AudioOutput:
        audio_output = resolve_audio_output(self.audio_output)
        self.audio_output = audio_output
        return audio_output


__all__ = [
    "AudioOutput",
    "PlaybackAudioOutput",
    "StreamingAudioStream",
    "SynchronousPcmPlaybackMixin",
    "match_output_sample_rate",
    "playback_cancelled",
    "prepared_output_stream",
    "resolve_audio_output",
    "sanitize_playback_audio",
    "write_pcm_chunks",
]
