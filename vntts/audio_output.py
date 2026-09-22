"""Small shared helpers for lazy audio output and playback status."""

from collections.abc import Callable, Mapping
from threading import Event, Lock, current_thread
from typing import Protocol, TypeAlias, TypeGuard, overload
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


class _AudioStatus(Protocol):
    output_underflow: object


class _AudioStream(Protocol):
    status: _AudioStatus


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
    def get_stream(self) -> _AudioStream: ...

    def OutputStream(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        latency: object,
    ) -> _StreamingAudioContext: ...


def _supports_streaming_output(value: object) -> TypeGuard[AudioOutput]:
    return callable(getattr(value, "query_devices", None)) and callable(
        getattr(value, "OutputStream", None)
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
    """Return an injected output module or lazily import sounddevice."""
    if audio_output is None:
        import sounddevice

        audio_output = sounddevice
    if isinstance(audio_output, _LoggedAudioOutput):
        return audio_output
    if (
        getattr(audio_output, "__name__", None) == "sounddevice"
        and _supports_streaming_output(audio_output)
    ):
        return _LoggedAudioOutput(audio_output)
    return audio_output


class _LoggedOutputStream:
    def __init__(
        self,
        stream: _StreamingAudioContext,
        stream_id: str,
        fields: dict[str, object],
        context: dict[str, object],
    ) -> None:
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
            result = self.stream.__exit__(exc_type, exc_value, traceback)
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

    def get_stream(self) -> _AudioStream:
        return self.output.get_stream()

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
            stream = self.output.OutputStream(
                samplerate=samplerate,
                channels=channels,
                dtype=dtype,
                latency=latency,
            )
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
        try:
            return self.output.wait()
        finally:
            self._finish_convenience("close", "convenience-wait")

    def stop(self) -> object:
        try:
            return self.output.stop()
        finally:
            self._finish_convenience("abort", "convenience-stop")

    def _finish_convenience(self, operation: str, reason: str) -> None:
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
            outcome="complete",
            reason=reason,
            owner=current_thread().name,
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self.output, name)


def playback_underflowed(
    audio_output: AudioOutput | None, playback_status: object = None
) -> bool:
    """Read a reliable output-underflow flag without requiring a live stream."""
    value = getattr(playback_status, "output_underflow", None)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    get_stream = getattr(audio_output, "get_stream", None)
    if not callable(get_stream):
        return False
    try:
        value = get_stream().status.output_underflow
    except AttributeError, RuntimeError:
        return False
    return bool(value) if isinstance(value, (bool, np.bool_)) else False


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
        target_sample_rate = int(round(float(default_samplerate)))
    except KeyError, TypeError, ValueError, RuntimeError:
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


def write_pcm_chunks(
    stream: StreamingAudioStream,
    audio: AudioData,
    sample_rate: int,
    cancelled: Callable[[], bool],
) -> tuple[bool, bool]:
    """Write short blocks so only the owner thread ever stops the device."""
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)
    block_frames = max(1, sample_rate // 20)
    underflowed = False
    wrote = False
    for offset in range(0, len(samples), block_frames):
        if cancelled():
            return False, underflowed
        underflowed = (
            bool(stream.write(samples[offset : offset + block_frames])) or underflowed
        )
        wrote = True
    return wrote and not cancelled(), underflowed


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
                with audio_output.OutputStream(
                    samplerate=playback_sample_rate,
                    channels=1,
                    dtype="float32",
                    latency=self.playback_latency,
                ) as stream:
                    def cancelled() -> bool:
                        return stop_requested.is_set() or (
                            playback_guard is not None and not playback_guard()
                        )

                    if len(audio) and not cancelled():
                        first_audio_ms = (self.clock() - started) * 1000
                    completed, underflowed = write_pcm_chunks(
                        stream,
                        audio,
                        playback_sample_rate,
                        cancelled,
                    )
                interrupted = not completed
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

    def _playback_underflowed(self, playback_status: object = None) -> bool:
        return playback_underflowed(self.audio_output, playback_status)


__all__ = [
    "AudioOutput",
    "PlaybackAudioOutput",
    "StreamingAudioStream",
    "SynchronousPcmPlaybackMixin",
    "match_output_sample_rate",
    "playback_underflowed",
    "resolve_audio_output",
    "write_pcm_chunks",
]
