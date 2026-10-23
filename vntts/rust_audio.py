"""Control protocol for the locally built Rust audio owner."""

from __future__ import annotations

import ctypes.util
import importlib.util
import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
from contextvars import copy_context
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from time import monotonic
from typing import Any
from uuid import uuid4
from weakref import WeakSet

import numpy as np

from vntts.audio_lifecycle import audio_lifecycle_context, record_audio_lifecycle

MAX_PCM_BYTES = 128 * 1024 * 1024
MAX_EVENT_BYTES = 65536
_workers: WeakSet[RustAudioWorker] = WeakSet()
_workers_lock = Lock()


def portaudio_library() -> str:
    """Resolve the installed public library, never sounddevice's CFFI internals."""
    explicit = os.environ.get("VNTTS_PORTAUDIO_LIBRARY")
    if explicit:
        return explicit
    found = ctypes.util.find_library("portaudio")
    if found:
        return found
    spec = importlib.util.find_spec("_sounddevice_data")
    if spec is not None and spec.submodule_search_locations:
        name = (
            "libportaudio.dylib"
            if sys.platform == "darwin"
            else f"libportaudio{struct.calcsize('P') * 8}bit.dll"
        )
        for location in spec.submodule_search_locations:
            candidates = list((Path(location) / "portaudio-binaries").rglob(name))
            if len(candidates) == 1:
                return str(candidates[0])
    raise RuntimeError("PortAudio not found; set VNTTS_PORTAUDIO_LIBRARY explicitly")


def worker_executable() -> Path:
    explicit = os.environ.get("VNTTS_AUDIO_WORKER")
    path = (
        Path(explicit).expanduser()
        if explicit
        else Path(__file__).resolve().parents[1]
        / "native/audio-worker/target/release"
        / (
            "vntts-audio-worker.exe"
            if sys.platform == "win32"
            else "vntts-audio-worker"
        )
    )
    if not path.is_file():
        raise RuntimeError(
            "Rust audio worker is missing. Run cargo build --release --locked "
            "--manifest-path native/audio-worker/Cargo.toml, or set VNTTS_AUDIO_WORKER"
        )
    return path


class RustAudioWorker:
    """One serialized clip owner; protocol reads never run on an audio callback."""

    def __init__(self, command: list[str] | None = None) -> None:
        self.lock = RLock()
        self.send_lock = Lock()
        self.state_lock = RLock()
        self.close_lock = Lock()
        self.events: queue.Queue[dict[str, Any] | BaseException] = queue.Queue(64)
        self.device_changed = Event()
        self.closed = Event()
        self.next_id = 0
        self._active_id: int | None = None
        self._diagnostic_clip: dict[str, Any] = {}
        self.diagnostic_worker_id = uuid4().hex
        self._audio_used = False
        self.stderr = tempfile.TemporaryFile()
        started = monotonic()
        try:
            command = command or [
                str(worker_executable()),
                "--portaudio",
                portaudio_library(),
            ]
            self.executable_path = str(
                Path(shutil.which(command[0]) or command[0]).resolve()
            )
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                bufsize=0,
            )
        except BaseException:
            self.stderr.close()
            raise
        self.reader = Thread(
            target=self._read_events, name="vntts-rust-events", daemon=True
        )
        self.reader.start()
        try:
            ready = self.receive(10)
            if ready.get("event") != "ready" or ready.get("protocol") != 2:
                raise RuntimeError(
                    "Rust audio worker protocol mismatch; rebuild native/audio-worker with Cargo"
                )
            self.startup_ms = (monotonic() - started) * 1000
            self.ready = ready
            with _workers_lock:
                _workers.add(self)
        except BaseException:
            self.close()
            raise

    def _read_events(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    raise RuntimeError("Rust audio worker exited before completion")
                if len(line) > MAX_EVENT_BYTES or not line.endswith(b"\n"):
                    raise RuntimeError("Rust audio worker returned an oversized event")
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(
                    event.get("event"), str
                ):
                    raise RuntimeError("Rust audio worker returned an invalid event")
                self.events.put(event, timeout=1)
        except (Exception, queue.Full) as error:
            if not self.closed.is_set():
                try:
                    self.events.put_nowait(error)
                except queue.Full:
                    pass
                if self.process.poll() is None:
                    self.process.kill()

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            if self.closed.is_set() or self.process.poll() is not None:
                raise RuntimeError("Rust audio worker is unavailable") from None
            raise
        if isinstance(value, BaseException):
            raise RuntimeError(str(value)) from value
        if value.get("event") in {"opened", "started", "first_callback"}:
            with self.state_lock:
                self._audio_used = True
        if value.get("event") == "finished":
            self._record_finished_diagnostics(value)
        return value

    @property
    def active_id(self) -> int | None:
        with self.state_lock:
            return self._active_id

    @active_id.setter
    def active_id(self, value: int | None) -> None:
        with self.state_lock:
            if value is not None and self.closed.is_set():
                raise RuntimeError("Rust audio worker is closed")
            self._active_id = value

    def _arm_recording(self, interrupted: Any = None) -> bool:
        # Publish the real owner before any PCM can reach its output callback.
        with self.state_lock:
            self._audio_used = True
            identity = {
                "worker_pid": self.process.pid,
                "worker_path": self.executable_path,
                "worker_id": self.diagnostic_worker_id,
            }
        from vntts.reading_diagnostics import arm_audio_worker_recording

        return arm_audio_worker_recording(
            identity,
            lambda: (
                self.closed.is_set()
                or self.device_changed.is_set()
                or (interrupted is not None and interrupted())
            ),
        )

    def send(
        self,
        message: dict[str, Any],
        payload: bytes = b"",
        *,
        only_active: int | None = None,
    ) -> None:
        with self.send_lock:
            if only_active is not None and self.active_id != only_active:
                return
            if self.closed.is_set():
                raise RuntimeError("Rust audio worker is closed")
            assert self.process.stdin is not None
            data = json.dumps(message, allow_nan=False).encode() + b"\n" + payload
            remaining = memoryview(data)
            while remaining:
                written = self.process.stdin.write(remaining)
                if written is None or written <= 0:
                    raise RuntimeError("Rust audio worker input closed")
                remaining = remaining[written:]
            if message.get("op") in {"load", "open_stream"}:
                with self.state_lock:
                    self._diagnostic_clip = {
                        "id": message.get("id"),
                        "sample_rate": message.get("sample_rate"),
                        "channels": message.get("channels"),
                        "source_frames": message.get("frames", 0),
                        "context": dict(audio_lifecycle_context.get() or {}),
                    }
            elif message.get("op") == "append":
                with self.state_lock:
                    if self._diagnostic_clip.get("id") == message.get("id"):
                        self._diagnostic_clip["source_frames"] += message.get(
                            "frames", 0
                        )

    def _record_finished_diagnostics(self, event: dict[str, Any]) -> None:
        _validate_timing_diagnostics(event)
        with self.state_lock:
            source = self._diagnostic_clip.copy()
        if not source or event.get("id") != source["id"]:
            return
        if event.get("outcome") not in {"complete", "interrupted", "failed"}:
            return
        if (event.get("sample_rate"), event.get("channels")) != (
            source["sample_rate"],
            source["channels"],
        ):
            return
        if any(
            type(event.get(name)) is not int or event[name] < 0
            for name in ("frames_played", "underflow_count", "callback_count")
        ):
            return
        details = {
            **source["context"],
            **{key: value for key, value in event.items() if key != "event"},
            "source_samples": source["source_frames"] * source["channels"],
            "sample_rate": source["sample_rate"],
            "channels": source["channels"],
            "worker_pid": self.process.pid,
            "worker_path": self.executable_path,
            "worker_id": self.diagnostic_worker_id,
        }
        record_audio_lifecycle("rust-worker-finished", **details)
        with self.state_lock:
            if self._diagnostic_clip.get("id") == source["id"]:
                self._diagnostic_clip = {}

    def _send_bounded(
        self, message: dict[str, Any], payload: bytes, stop_requested: Event | None
    ) -> None:
        done = Event()
        failure: list[BaseException] = []

        def write() -> None:
            try:
                self.send(message, payload)
            except BaseException as error:
                failure.append(error)
            finally:
                done.set()

        context = copy_context()
        writer = Thread(
            target=context.run, args=(write,), name="vntts-rust-upload", daemon=True
        )
        writer.start()
        deadline = monotonic() + 10
        while not done.wait(0.02):
            if monotonic() >= deadline or (
                stop_requested is not None and stop_requested.is_set()
            ):
                self.close()
                writer.join(timeout=1)
                raise RuntimeError("Rust PCM upload interrupted or timed out")
        if failure:
            raise RuntimeError("Rust PCM upload failed") from failure[0]

    def cancel_active(self) -> None:
        """Stop outside the playback/ingress locks, with a bounded watchdog."""
        clip_id = self.active_id
        if clip_id is None or self.closed.is_set():
            return

        def cancel() -> None:
            try:
                with self.state_lock:
                    admitted = self._diagnostic_clip.get("id") == clip_id
                if admitted:
                    self.send({"op": "stop", "id": clip_id}, only_active=clip_id)
            except Exception:
                self.close()

        Thread(target=cancel, name="vntts-rust-cancel", daemon=True).start()

        def watchdog() -> None:
            deadline = monotonic() + 3
            while self.active_id == clip_id and not self.closed.wait(0.02):
                if monotonic() >= deadline:
                    with self.state_lock:
                        if self.active_id == clip_id:
                            self.close()
                    return

        Thread(target=watchdog, name="vntts-rust-cancel-watch", daemon=True).start()

    def close(self) -> None:
        with self.close_lock:
            self._close()

    def _close(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        # Closing a pipe can wait for a concurrent writer; terminate its reader first.
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                pipe.close()
        if self.reader is not current_thread():
            self.reader.join(timeout=1)
        self.stderr.close()

    def play_buffer(
        self,
        samples: np.ndarray,
        sample_rate: int,
        channels: int,
        latency: object,
        cancelled: Any,
        first_write: Any,
        stop_requested: Event | None,
    ) -> tuple[bool, bool]:
        pcm = np.ascontiguousarray(samples, dtype="<f4")
        if (
            pcm.ndim != 2
            or pcm.shape[1] != channels
            or channels not in (1, 2)
            or not len(pcm)
            or pcm.nbytes > MAX_PCM_BYTES
            or not np.isfinite(pcm).all()
            or not 8000 <= sample_rate <= 384000
        ):
            raise ValueError(
                "Rust playback requires bounded finite mono/stereo float32 PCM"
            )
        with self.lock:
            if self.device_changed.is_set():
                raise RuntimeError("System audio device changed; retry playback")
            if cancelled():
                return False, False
            return _ClipPlayback(
                self,
                pcm,
                sample_rate,
                channels,
                latency,
                cancelled,
                first_write,
                stop_requested,
            ).run()

    def _expect(self, event: str, clip_id: int, timeout: float) -> dict[str, Any]:
        try:
            value = self.receive(timeout)
        except queue.Empty:
            raise RuntimeError(f"Rust audio {event} timed out") from None
        if value.get("event") != event or value.get("id") != clip_id:
            raise RuntimeError(
                str(value.get("error", f"Unexpected Rust audio event: {value}"))
            )
        return value


@dataclass
class _ClipPlayback:
    worker: RustAudioWorker
    pcm: np.ndarray
    sample_rate: int
    channels: int
    latency: object
    cancelled: Any
    first_write: Any
    stop_requested: Event | None
    clip_id: int = field(init=False)
    started: float = field(default_factory=monotonic, init=False)
    finished: Event = field(default_factory=Event, init=False)
    stop_sent: Event = field(default_factory=Event, init=False)
    preload_ms: float = field(default=0.0, init=False)
    first_reported: bool = field(default=False, init=False)
    first_callback_received_ms: float | None = field(default=None, init=False)

    def run(self) -> tuple[bool, bool]:
        self.worker.next_id += 1
        self.clip_id = self.worker.next_id
        self.worker.active_id = self.clip_id
        watcher: Thread | None = None

        def interrupted() -> bool:
            return bool(
                (self.stop_requested is not None and self.stop_requested.is_set())
                or self.cancelled()
            )

        try:
            if not self.worker._arm_recording(interrupted):
                return False, False
            self._load()
            if (
                self.stop_requested is not None and self.stop_requested.is_set()
            ) or self.cancelled():
                self.worker.send({"op": "stop", "id": self.clip_id})
                self._validate_finished(
                    self.worker._expect("finished", self.clip_id, 3)
                )
                return False, False
            self.worker.send({"op": "start", "id": self.clip_id})
            watcher = Thread(
                target=self._watch_stop, name="vntts-rust-stop", daemon=True
            )
            watcher.start()
            return self._wait_for_completion()
        except BaseException:
            # Protocol/guard failures cannot leave previously admitted audio playing.
            self.worker.close()
            raise
        finally:
            self.finished.set()
            if watcher is not None:
                watcher.join(timeout=1)
            self.worker.active_id = None

    def _load(self) -> None:
        self.worker._send_bounded(
            {
                "op": "load",
                "id": self.clip_id,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "frames": len(self.pcm),
                "latency": self.latency,
            },
            self.pcm.tobytes(),
            self.stop_requested,
        )
        self.worker._expect("loaded", self.clip_id, 10)
        self.preload_ms = (monotonic() - self.started) * 1000

    def _watch_stop(self) -> None:
        while not self.finished.wait(0.01):
            if self.worker.device_changed.is_set() or (
                self.stop_requested is not None and self.stop_requested.is_set()
            ):
                self.stop_sent.set()
                try:
                    self.worker.send(
                        {"op": "stop", "id": self.clip_id}, only_active=self.clip_id
                    )
                except Exception:
                    self.worker.close()
                    return
                if not self.finished.wait(3):
                    with self.worker.state_lock:
                        if self.worker.active_id == self.clip_id:
                            self.worker.close()
                return

    def _wait_for_completion(self) -> tuple[bool, bool]:
        deadline = monotonic() + len(self.pcm) / self.sample_rate + 10
        stop_deadline: float | None = None
        while True:
            if self.stop_sent.is_set() and stop_deadline is None:
                stop_deadline = monotonic() + 3
            if monotonic() >= min(deadline, stop_deadline or deadline):
                raise RuntimeError("Rust audio completion timed out")
            if not self.stop_sent.is_set() and self.cancelled():
                self.stop_sent.set()
                self.worker.send({"op": "stop", "id": self.clip_id})
            try:
                event = self.worker.receive(0.02)
            except queue.Empty:
                continue
            result = self._handle_event(event)
            if result is not None:
                return result

    def _handle_event(self, event: dict[str, Any]) -> tuple[bool, bool] | None:
        if event.get("id") != self.clip_id:
            raise RuntimeError("Rust audio returned an event for another clip")
        kind = event["event"]
        if kind == "error":
            raise RuntimeError(str(event.get("error", "Rust audio failed")))
        if kind == "first_callback" and not self.first_reported:
            self._first_callback(event)
        elif kind == "finished":
            return self._finish(event)
        elif kind not in {"started", "paused", "resumed", "first_callback"}:
            raise RuntimeError(f"Unknown Rust audio event: {kind}")
        return None

    def _first_callback(self, event: dict[str, Any]) -> None:
        frames = event.get("frames_written")
        if type(frames) is not int or not 0 <= frames <= len(self.pcm):
            raise RuntimeError("Rust audio returned an invalid first buffer")
        if frames:
            self.first_reported = True
            self.first_callback_received_ms = (monotonic() - self.started) * 1000
            if self.first_write is not None:
                self.first_write()

    def _validate_finished(self, event: dict[str, Any]) -> None:
        if event.get("outcome") not in {"complete", "interrupted", "failed"}:
            raise RuntimeError("Rust audio returned an invalid outcome")
        for name in ("frames_played", "underflow_count", "callback_count"):
            count = event.get(name)
            if type(count) is not int or count < 0:
                raise RuntimeError(f"Rust audio returned invalid {name}")
        if (
            event.get("sample_rate") != self.sample_rate
            or event.get("channels") != self.channels
        ):
            raise RuntimeError("Rust audio returned a different output format")

    def _finish(self, event: dict[str, Any]) -> tuple[bool, bool]:
        self._validate_finished(event)
        details = {k: v for k, v in event.items() if k != "event"}
        details.update(
            reason="isolated-preloaded-pcm",
            preload_ms=round(self.preload_ms, 3),
            worker_startup_ms=round(self.worker.startup_ms, 3),
            worker_pid=self.worker.process.pid,
            worker_path=self.worker.executable_path,
            first_callback_received_ms=self.first_callback_received_ms,
        )
        record_audio_lifecycle("rust-pcm-summary", **details)
        if self.worker.device_changed.is_set():
            raise RuntimeError("System audio device changed during Rust playback")
        if event.get("outcome") == "failed":
            raise RuntimeError(str(event.get("error", "Rust playback failed")))
        count = event.get("frames_played")
        if type(count) is not int or not 0 <= count <= len(self.pcm):
            raise RuntimeError("Rust audio returned an invalid frame count")
        complete = event.get("outcome") == "complete"
        if complete and count != len(self.pcm):
            raise RuntimeError("Rust audio completed without playing the whole buffer")
        return complete and not self.stop_sent.is_set(), event["underflow_count"] > 0


def _validate_timing_diagnostics(event: dict[str, Any]) -> None:
    for clock in ("callback_timing", "dac_timing"):
        if clock not in event:
            continue  # Protocol 2 peers built before timing diagnostics remain supported.
        metric = event[clock]
        if not isinstance(metric, dict):
            raise RuntimeError(f"Rust audio returned invalid {clock}")
        for name in (
            "observation_count",
            "unavailable_count",
            "discontinuity_count",
            "max_offset_position_frames",
            "max_offset_output_frame",
        ):
            if type(metric.get(name)) is not int or metric[name] < 0:
                raise RuntimeError(f"Rust audio returned invalid {clock}.{name}")
        for name in ("max_gap_seconds", "max_offset_seconds"):
            value = metric.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or (name == "max_gap_seconds" and value < 0)
            ):
                raise RuntimeError(f"Rust audio returned invalid {clock}.{name}")
    for name in ("timing_output_frames", "timing_sample_rate"):
        if name in event:
            value = event[name]
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value < 0
                or (name == "timing_output_frames" and type(value) is not int)
            ):
                raise RuntimeError(f"Rust audio returned invalid {name}")


def active_audio_workers() -> tuple[dict[str, object], ...]:
    """Snapshot live audio owners before PCM, including idle owners, for process recording."""
    with _workers_lock:
        workers = tuple(_workers)
    result = []
    for worker in sorted(workers, key=lambda worker: worker.process.pid):
        with worker.state_lock:
            if (
                worker._audio_used
                and not worker.closed.is_set()
                and worker.process.poll() is None
            ):
                result.append(
                    {
                        "worker_pid": worker.process.pid,
                        "worker_path": worker.executable_path,
                        "worker_id": worker.diagnostic_worker_id,
                        "clip_id": worker._active_id,
                    }
                )
    return tuple(result)


def notify_audio_device_changed() -> None:
    with _workers_lock:
        workers = tuple(_workers)
    for worker in workers:
        worker.device_changed.set()
        worker.cancel_active()
