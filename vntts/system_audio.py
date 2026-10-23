"""System-output conversion and refresh facade over the Rust device owner."""

from __future__ import annotations

import atexit
from collections.abc import Callable
from threading import Event, Lock, RLock, Thread, current_thread, main_thread
from time import monotonic
from typing import Any

import numpy as np

from vntts.audio_lifecycle import audio_lifecycle_context, record_audio_lifecycle
from vntts.cleanup import attempt_cleanup, cleanup_on_exit


class AudioDeviceChangedError(RuntimeError):
    pass


class _DefaultOutputMonitor:
    """Probe outside playback locks; native audio-service calls can be slow."""

    def __init__(
        self,
        output: SystemAudioOutput,
        probe_factory: Callable[[], Callable[[], object]],
    ) -> None:
        self.output = output
        self.probe_factory = probe_factory
        self.probe: Callable[[], object] | None = None
        self.identity: object = object()
        self.failed = False
        self.ready = Event()
        self.stopping = Event()
        self.thread = Thread(
            target=self._run, name="vntts-default-audio-device", daemon=True
        )

    def check(self) -> None:
        if self.stopping.is_set():
            return
        try:
            if self.probe is None:
                self.probe = self.probe_factory()
            identity = self.probe()
        except Exception as error:
            if self.stopping.is_set():
                return
            if not self.failed:
                record_audio_lifecycle(
                    "device-probe", outcome="failed", reason=type(error).__name__
                )
            self.failed = True
            return
        if self.stopping.is_set():
            return
        recovered = self.failed
        if recovered:
            record_audio_lifecycle(
                "device-probe", outcome="complete", reason="probe-recovered"
            )
            self.failed = False
        if recovered or identity != self.identity:
            self.identity = identity
            self.output.request_refresh()

    def _run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.check()
            finally:
                self.ready.set()
            self.stopping.wait(0.5)

    def close(self) -> None:
        self.stopping.set()
        if self.thread.ident is not None and current_thread() is not self.thread:
            self.thread.join(timeout=2)


class SystemAudioOutput:
    """Refresh PortAudio only after every owned native stream has been closed."""

    def __init__(self, module: Any) -> None:
        self.module = module
        self.lock = RLock()
        self.pending = Event()
        self.streams: set[_SystemStream] = set()
        self.initialized = True
        self.device_monitor: _DefaultOutputMonitor | None = None

    def request_refresh(self) -> None:
        # The Qt/main thread must never wait for a blocking device write.
        self.pending.set()
        from vntts.rust_audio import notify_audio_device_changed

        notify_audio_device_changed()

    def _refresh(self) -> None:
        # Establish the first baseline before opening streams, avoiding a late
        # initial observation invalidating a newly constructed stream.
        if self.device_monitor is not None and not self.device_monitor.ready.wait(3):
            raise RuntimeError("Audio device detection timed out; retry playback")
        if not self.pending.is_set():
            return
        self.pending.clear()
        try:
            self.module.stop()
            for stream in tuple(self.streams):
                stream._release()
            if self.initialized:
                self.module._terminate()
                self.initialized = False
            self.module._initialize()
            self.initialized = True
            record_audio_lifecycle(
                "device-refresh", outcome="complete", reason="system-output-changed"
            )
        except BaseException:
            self.pending.set()
            record_audio_lifecycle(
                "device-refresh", outcome="failed", reason="system-output-changed"
            )
            raise

    def query_devices(self, *, kind: str) -> Any:
        with self.lock:
            self._refresh()
            return self.module.query_devices(kind=kind)

    def query_hostapis(self, index: int) -> Any:
        with self.lock:
            self._refresh()
            return self.module.query_hostapis(index)

    def OutputStream(self, **options: Any) -> _SystemStream:
        with self.lock:
            self._refresh()
            stream = _SystemStream(self, options)
            self.streams.add(stream)
            return stream

    def play(self, audio: object, sample_rate: int, *, latency: object) -> Any:
        with self.lock:
            self._refresh()
            return self.module.play(audio, sample_rate, latency=latency)

    def wait(self) -> Any:
        return self.module.wait()

    def stop(self) -> Any:
        with self.lock:
            return self.module.stop()


class _SystemStream:
    def __init__(self, output: SystemAudioOutput, options: dict[str, Any]) -> None:
        self.output = output
        self.options = options
        self.write_lock = Lock()
        self.native: Any = None
        self.started = False
        self.closed = False
        self.resampler: Any = None
        self.write_context = dict(audio_lifecycle_context.get() or {})
        self.write_metrics: dict[str, float | int] = {
            "system_write_attempts": 0,
            "system_write_failures": 0,
            "device_reopens": 0,
            "max_output_lock_ms": 0.0,
            "max_stream_lock_ms": 0.0,
            "max_refresh_ms": 0.0,
            "max_conversion_ms": 0.0,
            "max_native_write_ms": 0.0,
        }
        self.write_index = self.input_frames = 0
        self.max_native_block_seconds = -1.0
        self.final_underflowed = False
        self.write_blocks: dict[str, float | int] = {}
        self._open()

    def _open(self) -> None:
        if self.closed:
            raise RuntimeError("The output stream is closed")
        options = self.options.copy()
        # Callback owners prepare their buffers and reopen in the new format.
        if "callback" not in options:
            try:
                self.output.module.check_output_settings(
                    samplerate=options["samplerate"],
                    channels=options["channels"],
                    dtype=options["dtype"],
                )
            except self.output.module.PortAudioError as error:
                # Preserve the host's continuous converter when the format works.
                if len(error.args) < 2 or error.args[1] not in (-9997, -9998):
                    raise
                device = self.output.module.query_devices(kind="output")
                options["samplerate"] = round(float(device["default_samplerate"]))
                options["channels"] = min(
                    options["channels"], int(device["max_output_channels"])
                )
        self.sample_rate = options["samplerate"]
        self.channels = options["channels"]
        self.resampler = None
        if "callback" not in options and self.options["samplerate"] != self.sample_rate:
            from soxr import ResampleStream

            self.resampler = ResampleStream(
                self.options["samplerate"], self.sample_rate, self.channels
            )
        self.native = self.output.module.OutputStream(**options)

    @property
    def device_changed(self) -> bool:
        with self.output.lock:
            self.output._refresh()
            return self.native is None

    @property
    def time(self) -> float:
        with self.output.lock:
            self.output._refresh()
            if self.native is None:
                raise AudioDeviceChangedError("The system output device changed")
            return float(self.native.time)

    def start(self) -> None:
        with self.output.lock:
            self.output._refresh()
            if self.native is None:
                if "callback" in self.options:
                    raise AudioDeviceChangedError("The system output device changed")
                self._open()
            self.native.start()
            self.started = True

    def __enter__(self) -> _SystemStream:
        try:
            self.start()
        except BaseException as error:
            attempt_cleanup(
                self.close,
                description="Output stream startup cleanup",
                primary_error=error,
            )
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        with cleanup_on_exit(self.close, description="Output stream cleanup"):
            if _args and _args[0] is not None:
                self.abort()
            else:
                self.stop()

    def write(self, audio: object) -> Any:
        self.write_index += 1
        block: dict[str, float | int] = {"write_index": self.write_index}
        for attempt in range(2):
            try:
                return self._write(audio, block, attempt + 1)
            except Exception:
                self.write_metrics["system_write_failures"] += 1
                with self.output.lock:
                    if (
                        attempt
                        or self.native is not None
                        or not self.started
                        or self.closed
                    ):
                        raise
        raise AssertionError("Unreachable playback retry")

    def _record_write_phase(
        self, field: str, started: float, phases: dict[str, float | int]
    ) -> float:
        ended = monotonic()
        duration_ms = (ended - started) * 1000
        self.write_metrics[field] = max(self.write_metrics[field], duration_ms)
        phases[field.removeprefix("max_")] = round(duration_ms, 3)
        return ended

    def _record_native_block(
        self,
        block: dict[str, float | int],
        phases: dict[str, float | int],
        started: float,
        ended: float,
        native_frames: int,
        native_sample_rate: int,
        underflow: object,
    ) -> None:
        snapshot = {
            **block,
            **phases,
            "native_frames": native_frames,
            "native_sample_rate": native_sample_rate,
            "started_monotonic": started,
            "ended_monotonic": ended,
        }
        if ended - started > self.max_native_block_seconds:
            self.max_native_block_seconds = ended - started
            self.write_blocks.update(
                {f"max_native_write_{key}": value for key, value in snapshot.items()}
            )
        if (
            isinstance(underflow, (bool, np.bool_))
            and underflow
            and "first_underflow_write_index" not in self.write_blocks
        ):
            self.write_blocks.update(
                {f"first_underflow_{key}": value for key, value in snapshot.items()}
            )

    def _convert_write_samples(
        self, audio: object, block: dict[str, float | int]
    ) -> Any:
        samples = np.asarray(audio, dtype=np.float32)
        if "input_frame_offset" not in block:
            block.update(
                input_frame_offset=self.input_frames,
                input_frames=len(samples),
                sample_rate=self.options["samplerate"],
            )
            self.input_frames += len(samples)
        if self.options["channels"] != self.channels:
            samples = samples.mean(axis=1, keepdims=True, dtype=np.float32)
        if self.resampler is not None:
            samples = self.resampler.resample_chunk(samples)
            np.clip(samples, -0.95, 0.95, out=samples)
        return samples

    def _write(self, audio: object, block: dict[str, float | int], attempt: int) -> Any:
        self.write_metrics["system_write_attempts"] += 1
        phases: dict[str, float | int] = {"attempt": attempt}
        started = monotonic()
        with self.output.lock:
            self._record_write_phase("max_output_lock_ms", started, phases)
            started = monotonic()
            try:
                self.output._refresh()
                if self.native is None:
                    self._open()
                    self.write_metrics["device_reopens"] += 1
                    assert self.native is not None
                    if self.started:
                        try:
                            native = self.native
                            native.start()
                        except BaseException as error:
                            attempt_cleanup(
                                self._release,
                                description="Changed output startup cleanup",
                                primary_error=error,
                            )
                            raise
            finally:
                self._record_write_phase("max_refresh_ms", started, phases)
            started = monotonic()
            try:
                samples = self._convert_write_samples(audio, block)
            finally:
                self._record_write_phase("max_conversion_ms", started, phases)
            if not len(samples):
                return False
            native = self.native
            native_sample_rate = self.sample_rate
            started = monotonic()
            self.write_lock.acquire()
            self._record_write_phase("max_stream_lock_ms", started, phases)
        # Cancellation must be able to abort a native write on another thread.
        started = monotonic()
        underflow: object = None
        try:
            underflow = native.write(samples)
            return underflow
        finally:
            ended = self._record_write_phase("max_native_write_ms", started, phases)
            try:
                self._record_native_block(
                    block,
                    phases,
                    started,
                    ended,
                    len(samples),
                    native_sample_rate,
                    underflow,
                )
            finally:
                self.write_lock.release()

    def abort(self) -> None:
        with self.output.lock:
            if self.native is not None:
                self.native.abort()
            if self.resampler is not None:
                self.resampler.clear()
            self.started = False

    @property
    def underflowed(self) -> bool:
        value = getattr(self.native, "underflowed", False)
        return self.final_underflowed or (isinstance(value, bool) and value)

    def play_buffer(
        self,
        samples: np.ndarray,
        cancelled: Callable[[], bool],
        first_write: Callable[[], None] | None,
        stop_requested: Event | None,
    ) -> tuple[bool, bool]:
        from vntts.rust_output import RustOutputStream

        with self.output.lock:
            self.output._refresh()
            native = self.native
            if not isinstance(native, RustOutputStream):
                raise RuntimeError("Prepared output no longer owns a Rust device")
            converted = self._convert_write_samples(samples, {})
            if self.resampler is not None:
                tail = self.resampler.resample_chunk(
                    np.empty((0, self.channels), np.float32), last=True
                )
                self.resampler.clear()
                converted = np.concatenate((converted, tail), axis=0)
                np.clip(converted, -0.95, 0.95, out=converted)
        with self.write_lock:
            return native.play_buffer(converted, cancelled, first_write, stop_requested)

    def stop(self) -> None:
        with self.output.lock:
            native = self.native
            tail = None
            if native is not None and self.started and self.resampler is not None:
                tail = self.resampler.resample_chunk(
                    np.empty((0, self.channels), np.float32), last=True
                )
                self.resampler.clear()
            self.started = False
        # Drain without the refresh lock so cancellation can still abort it.
        if native is not None:
            with self.write_lock:
                if tail is not None and len(tail):
                    np.clip(tail, -0.95, 0.95, out=tail)
                    if native.write(tail):
                        raise RuntimeError(
                            "Audio output underflowed while draining converted PCM"
                        )
                native.stop()
                value = getattr(native, "underflowed", False)
                self.final_underflowed = isinstance(value, bool) and value

    def _release(self) -> None:
        if self.native is None:
            return
        native = self.native

        def close() -> None:
            with self.write_lock:
                native.close()
                self.native = None

        with cleanup_on_exit(close, description="Changed output stream cleanup"):
            native.abort()

    def close(self) -> None:
        with self.output.lock:
            if self.closed:
                return
            if self.native is not None:
                if self.started:
                    self._release()
                else:
                    with self.write_lock:
                        self.native.close()
                        self.native = None
            self.closed = True
            self.output.streams.discard(self)
        if self.write_metrics["system_write_attempts"]:
            record_audio_lifecycle(
                "system-write-summary",
                **self.write_context,
                **self.write_metrics,
                **self.write_blocks,
                outcome="closed",
                reason="blocking-system-output",
            )


_system_output: SystemAudioOutput | None = None
_media_devices: Any = None
_system_lock = RLock()


def get_system_audio_output() -> SystemAudioOutput:
    global _system_output
    with _system_lock:
        if _system_output is None:
            from vntts.rust_output import RustAudioModule

            output = SystemAudioOutput(RustAudioModule())
            # Earlier read-only probes may have initialized a stale device list.
            output.request_refresh()
            from vntts.system_audio_devices import create_default_output_probe

            monitor = _DefaultOutputMonitor(output, create_default_output_probe)
            output.device_monitor = monitor
            atexit.register(monitor.close)
            monitor.thread.start()
            _system_output = output
    return _system_output


def initialize_audio_device_monitor() -> None:
    """Initialize Qt's native device notifications on the application's thread."""
    global _media_devices
    if _media_devices is not None or current_thread() is not main_thread():
        return
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtMultimedia import QMediaDevices

    application = QCoreApplication.instance()
    if application is None:
        return
    _media_devices = QMediaDevices(application)
    _media_devices.audioOutputsChanged.connect(_request_system_refresh)
    QMediaDevices.defaultAudioOutput()


def _request_system_refresh() -> None:
    if _system_output is not None:
        _system_output.request_refresh()


__all__ = [
    "AudioDeviceChangedError",
    "get_system_audio_output",
    "initialize_audio_device_monitor",
]
