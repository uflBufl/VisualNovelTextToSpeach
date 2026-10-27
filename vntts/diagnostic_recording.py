"""Explicit native recording for a bounded Reading diagnostic session."""

import ctypes
import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from itertools import islice
from pathlib import Path
from threading import Condition, Event, Lock
from time import monotonic
from uuid import uuid4

import psutil

from vntts.path_safety import open_regular_binary

MAX_DIAGNOSTIC_SECONDS = 3600
MAX_DIAGNOSTIC_MEDIA_BYTES = 9 * 1024**3


def process_executable(pid: int) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Process recording currently requires macOS")
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
        raise RuntimeError("Selected recording process is unavailable")
    return buffer.value.decode()


def _stop_process(process: subprocess.Popen, timeout: float) -> None:
    # The owned group can retain compiler children after its parent exits.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=max(0.01, timeout))
    except subprocess.TimeoutExpired:
        pass
    finally:
        # A compiler child can survive its parent's exit; the owned session must not.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def _run_checked(
    command: list[str],
    cancelled: Event,
    timeout: float,
    *,
    deadline: float | None = None,
) -> None:
    if cancelled.is_set():
        raise InterruptedError("Recorder preparation cancelled")
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("Diagnostic session deadline reached during preparation")
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        deadline = (
            min(monotonic() + timeout, deadline)
            if deadline is not None
            else monotonic() + timeout
        )
        try:
            while process.poll() is None:
                if cancelled.wait(0.05):
                    raise InterruptedError("Recorder preparation cancelled")
                if monotonic() >= deadline:
                    raise TimeoutError("Recorder preparation timed out")
                if os.fstat(log.fileno()).st_size > 2 * 1024**2:
                    raise RuntimeError("Recorder preparation log exceeded its limit")
            if process.returncode:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 4096))
                detail = log.read(4096).decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    "Native recorder preparation failed: "
                    + (detail or "no diagnostic output")
                )
        finally:
            _stop_process(process, 2)


def prepare_recorders(
    directory: Path, cancelled: Event | None = None, *, deadline: float | None = None
) -> tuple[Path, Path]:
    """Build before measurement; Stop also owns any compiler/preflight children."""
    cancelled = cancelled if cancelled is not None else Event()
    if sys.platform != "darwin":
        raise RuntimeError("Window/process recording currently requires macOS")
    compiler = shutil.which("xcrun")
    source = Path(__file__).resolve().parents[1] / "scripts" / "mac_live_recording"
    files = [
        source / name
        for name in (
            "recorder.swift",
            "capture.swift",
            "selfcheck.swift",
            "process_tap.m",
            "process_tap_info.plist",
        )
    ]
    if compiler is None or not all(path.is_file() for path in files):
        raise RuntimeError(
            "Native recording requires Xcode Command Line Tools and recorder sources"
        )
    digest = hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest()[
        :20
    ]
    build = directory / digest
    build.mkdir(parents=True, exist_ok=True, mode=0o700)
    video, audio = build / "window-recorder", build / "process-recorder"
    commands = [
        [
            compiler,
            "swiftc",
            "-parse-as-library",
            "-module-cache-path",
            str(build / "swift-cache"),
            *map(str, files[:3]),
            "-o",
            str(video),
        ],
        [
            compiler,
            "clang",
            "-std=c11",
            "-fobjc-arc",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-mmacosx-version-min=14.2",
            "-framework",
            "Foundation",
            "-framework",
            "CoreAudio",
            "-framework",
            "AudioToolbox",
            "-Wl,-sectcreate,__TEXT,__info_plist," + str(files[4]),
            str(files[3]),
            "-o",
            str(audio),
        ],
    ]
    for output, command in zip((video, audio), commands, strict=True):
        if output.is_symlink():
            raise ValueError("Recorder cache contains a symlink")
        if not output.is_file() or not os.access(output, os.X_OK):
            staging = output.with_name(output.name + "." + uuid4().hex + ".building")
            command[-1] = str(staging)
            try:
                _run_checked(command, cancelled, 90, deadline=deadline)
                staging.replace(output)
            finally:
                staging.unlink(missing_ok=True)
    _run_checked([str(video), "--preflight"], cancelled, 10, deadline=deadline)
    _run_checked([str(audio), "--selfcheck"], cancelled, 10, deadline=deadline)
    return video, audio


def _session(output: Path) -> dict:
    with open_regular_binary(output / "session.json") as source:
        payload = source.read(65537)
    if len(payload) > 65536:
        raise ValueError("Recorder metadata exceeds its limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Invalid recorder metadata")
    return value


def _has_sample(output: Path, video: bool) -> bool:
    try:
        with open_regular_binary(
            output / ("game.jsonl" if video else "audio.jsonl")
        ) as source:
            # Startup only needs one complete native sample; never read the growing journal whole.
            for line in source.read(65536).splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("kind") == ("video" if video else "audio") and (
                    event.get("written") if video else event.get("frame_count", 0) > 0
                ):
                    return True
    except OSError:
        pass
    return False


class _RecordingStop(Event):
    def __init__(self) -> None:
        super().__init__()
        self.requested_monotonic: float | None = None
        self._request_lock = Lock()

    def set(self) -> None:
        with self._request_lock:
            if self.requested_monotonic is None:
                self.requested_monotonic = monotonic()
            super().set()


class NativeDiagnosticRecording:
    """One selected window and explicit game/Rust processes; never a desktop tap."""

    def __init__(
        self,
        directory: Path,
        target: dict[str, object],
        seconds: int,
        workers: Callable[[], Iterable[dict[str, object]]],
        tools_directory: Path,
        *,
        deadline: float | None = None,
    ) -> None:
        if type(seconds) is not int or not 1 <= seconds <= MAX_DIAGNOSTIC_SECONDS:
            raise ValueError("Recording duration must be between 1 and 3600 seconds")
        self.directory, self.target, self.seconds = (
            directory.resolve(),
            dict(target),
            seconds,
        )
        self.workers, self.tools_directory = workers, tools_directory
        self.deadline = deadline
        self.cancelled = _RecordingStop()
        self.processes: list[tuple[subprocess.Popen[bytes], Path]] = []
        self.status = "preparing"
        self.error_type: str | None = None
        self.error_message: str | None = None
        self.stop_reason: str | None = None
        self.started_monotonic: float | None = None
        self.ready_monotonic: float | None = None
        self._launched_at: dict[str, float] = {}
        self._helper_created: dict[str, float] = {}
        self._sample_observed_at: dict[str, float] = {}
        self._failed_stop_started: dict[str, float] = {}
        self._identities: dict[str, dict[str, object]] = {}
        self._failures: dict[str, dict[str, object]] = {}
        self._worker_lifetimes: list[dict[str, object]] = []
        self._arming = Condition()
        self._worker_arms: dict[tuple[object, ...], str] = {}
        self._arm_gaps: dict[tuple[object, ...], dict[str, object]] = {}
        self._finished = Event()

    def wait_for_worker(
        self, identity: dict[str, object], interrupted: Callable[[], bool]
    ) -> tuple[bool, str]:
        """Wait off the GUI thread for an explicit tap, without requiring source PCM."""
        key = tuple(
            identity[name] for name in ("worker_pid", "worker_path", "worker_id")
        )
        deadline = min(monotonic() + 10, self.deadline or float("inf"))
        with self._arming:
            while True:
                if interrupted() or self.cancelled.is_set():
                    return False, "cancelled"
                gap = self._arm_gaps.get(key)
                if gap is not None:
                    return False, str(gap["error_message"])
                if self._finished.is_set():
                    reason = "recorder-stopped"
                    break
                state = self._worker_arms.get(key)
                if state == "armed":
                    return True, "armed"
                if state == "failed":
                    reason = "capture-failed"
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                self._arming.wait(min(0.02, remaining))
            if self.error_type is None:
                self.error_type, self.error_message = "RustRecordingArmError", reason
            self.status = "failed" if self._finished.is_set() else "degraded"
            self._arm_gaps.setdefault(
                key,
                {
                    "capture": "rust-arming",
                    **identity,
                    "detected_monotonic": monotonic(),
                    "status": "failed-or-partial",
                    "error_type": "RustRecordingArmError",
                    "error_message": reason,
                    "scope": "Worker output started without a verified process tap",
                },
            )
        return False, reason

    def _launch(self, command: list[str], output: Path) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Recording cancelled")
        with (self.directory / (output.name + "-launcher.log")).open("xb") as log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        self.processes.append((process, output))
        self._launched_at[output.name] = monotonic()
        try:
            self._helper_created[output.name] = psutil.Process(
                process.pid
            ).create_time()
        except psutil.Error:
            pass

    def run(self) -> None:
        try:
            deadline = self.deadline
            video, audio = prepare_recorders(
                self.tools_directory, self.cancelled, deadline=deadline
            )
            if self.cancelled.is_set():
                return
            if shutil.disk_usage(self.directory).free < 2 * 1024**3:
                raise RuntimeError(
                    "At least 2 GiB free space is required for native recording"
                )
            pid, window = (
                int(str(self.target["pid"])),
                int(str(self.target["window_id"])),
            )
            if not 0 < pid <= 2**31 - 1 or not 0 < window <= 2**32 - 1:
                raise ValueError("Explicit positive process and window IDs required")
            executable = process_executable(pid)
            if executable != self.target["executable"]:
                raise RuntimeError("Selected game process changed before recording")
            self.started_monotonic = monotonic()
            if deadline is None:
                deadline = self.started_monotonic + self.seconds
            self.deadline = deadline
            remaining_seconds = min(self.seconds, deadline - self.started_monotonic)
            if remaining_seconds < 1:
                raise TimeoutError(
                    "Diagnostic session deadline leaves no capture window"
                )
            for name, command in (
                (
                    "window",
                    [
                        str(video),
                        "--video-only",
                        str(pid),
                        str(window),
                        str(remaining_seconds),
                    ],
                ),
                (
                    "game-audio",
                    [
                        str(audio),
                        "--capture",
                        str(pid),
                        executable,
                        str(remaining_seconds),
                    ],
                ),
            ):
                output = self.directory / name
                try:
                    self._launch(
                        command
                        + [str(output)]
                        + ([] if name == "window" else ["--allow-audio-capture"]),
                        output,
                    )
                except Exception as error:
                    self._fail_capture(name, error)
            seen: dict[tuple[int, str, str], str] = {}
            previous: set[tuple[int, str, str]] = set()
            self.status = "degraded" if self.error_type else "starting"
            last_budget_check = 0.0
            while not self.cancelled.is_set() and monotonic() < deadline:
                current: set[tuple[int, str, str]] = set()
                try:
                    workers = tuple(islice(self.workers(), 9))
                except Exception as error:
                    self._fail_capture("worker-discovery", error)
                    workers = ()
                    current.update(previous)
                for worker in workers:
                    try:
                        worker_pid = int(str(worker["worker_pid"]))
                        worker_path = str(worker["worker_path"])
                        worker_id = str(worker.get("worker_id", worker_pid))
                        if (
                            not 0 < worker_pid <= 2**31 - 1
                            or not Path(worker_path).is_absolute()
                            or not 1 <= len(worker_id) <= 160
                        ):
                            raise ValueError("Invalid explicit audio worker identity")
                    except (KeyError, TypeError, ValueError) as error:
                        self._fail_capture("worker-discovery", error)
                        continue
                    identity = (worker_pid, worker_path, worker_id)
                    current.add(identity)
                    if identity in seen:
                        continue
                    if len(seen) >= 8:
                        self._fail_capture(
                            "worker-limit",
                            RuntimeError(
                                "Audio worker recording limit reached; session is incomplete"
                            ),
                        )
                        continue
                    ordinal = 1 + sum(key[0] == worker_pid for key in seen)
                    name = f"rust-{worker_pid}" + (f"-{ordinal}" if ordinal > 1 else "")
                    seen[identity] = name
                    self._identities[name] = {
                        "worker_pid": worker_pid,
                        "worker_path": worker_path,
                        "worker_id": worker_id,
                    }
                    lifetime = {
                        **self._identities[name],
                        "capture": name,
                        "observed_monotonic": monotonic(),
                    }
                    self._worker_lifetimes.append(lifetime)
                    remaining = max(
                        1,
                        min(MAX_DIAGNOSTIC_SECONDS, math.ceil(deadline - monotonic())),
                    )
                    output = self.directory / name
                    try:
                        self._launch(
                            [
                                str(audio),
                                "--capture",
                                str(worker_pid),
                                worker_path,
                                str(remaining),
                                str(output),
                                "--allow-audio-capture",
                            ],
                            output,
                        )
                    except Exception as error:
                        self._fail_capture(name, error)
                for identity in previous - current:
                    retired_name = seen.get(identity)
                    if retired_name is None:
                        continue
                    for lifetime in self._worker_lifetimes:
                        if lifetime["capture"] == retired_name:
                            lifetime["retired_observed_monotonic"] = monotonic()
                    self._fail_capture(
                        retired_name,
                        RuntimeError(
                            "Audio worker retired; process capture is partial"
                        ),
                    )
                previous = current
                ready = not self.error_type
                for process, output in self.processes:
                    if output.name in self._failures:
                        self._stop_failed_capture(process, output)
                        continue
                    try:
                        if process.poll() not in (None, 0):
                            raise RuntimeError(
                                f"Recorder failed for {output.name}; inspect its session.json"
                            )
                        if (
                            self.directory / (output.name + "-launcher.log")
                        ).stat().st_size > 2 * 1024**2:
                            raise RuntimeError("Recorder log exceeded its limit")
                        try:
                            metadata = _session(output)
                        except FileNotFoundError:
                            metadata = {}
                        expected = (
                            "succeeded"
                            if output.name == "window"
                            else "captured_unqualified"
                        )
                        if (
                            metadata.get("status") in {"failed", "incomplete"}
                            or metadata.get("errors")
                            or (
                                process.poll() is not None
                                and metadata.get("status") != expected
                            )
                        ):
                            raise RuntimeError(
                                f"Recorder failed for {output.name}; inspect its session.json"
                            )
                        identity = self._identities.get(output.name)
                        if identity is not None:
                            worker_key = (
                                identity["worker_pid"],
                                identity["worker_path"],
                                identity["worker_id"],
                            )
                            target = metadata.get("target")
                            started = metadata.get("capture_started_monotonic_s")
                            if (
                                process.poll() is None
                                and metadata.get("status") == "in_progress"
                                and isinstance(target, dict)
                                and target.get("pid") == identity["worker_pid"]
                                and target.get("executable") == identity["worker_path"]
                                and type(started) in (int, float)
                                and math.isfinite(started)
                                and started > 0
                            ):
                                with self._arming:
                                    self._worker_arms[worker_key] = "armed"
                                    self._arming.notify_all()
                            elif process.poll() is not None:
                                with self._arming:
                                    self._worker_arms[worker_key] = "failed"
                                    self._arming.notify_all()
                        observed = _has_sample(output, output.name == "window")
                        ready &= observed
                        if observed:
                            self._sample_observed_at.setdefault(
                                output.name, monotonic()
                            )
                        if (
                            not observed
                            and monotonic() - self._launched_at[output.name] > 10
                        ):
                            raise RuntimeError(
                                f"Recorder readiness unverified for {output.name}"
                            )
                    except Exception as error:
                        ready = False
                        self._fail_capture(output.name, error)
                        self._stop_failed_capture(process, output)
                if ready and self.ready_monotonic is None:
                    self.ready_monotonic = monotonic()
                    self.status = "recording"
                if monotonic() - last_budget_check >= 1:
                    self._check_media_budget()
                    last_budget_check = monotonic()
                if shutil.disk_usage(self.directory).free < 512 * 1024**2:
                    raise RuntimeError("Recording stopped because free space is low")
                self.cancelled.wait(0.1)
            self.status = "degraded" if self.error_type else "stopping"
            requested = self.cancelled.requested_monotonic
            self.stop_reason = (
                "requested_stop"
                if requested is not None and requested < deadline
                else "deadline"
            )
        except InterruptedError as error:
            if not self.cancelled.is_set():
                self.error_type, self.error_message = type(error).__name__, str(error)
        except Exception as error:
            self.error_type, self.error_message = type(error).__name__, str(error)
        finally:
            try:
                # The shared deadline owns acquisition; helpers drain/finalize on TERM.
                self._stop_processes()
                if self.stop_reason is not None:
                    assert deadline is not None
                    for process, output in self.processes:
                        if output.name in self._failures:
                            continue
                        try:
                            metadata = _session(output)
                            expected = (
                                "succeeded"
                                if output.name == "window"
                                else "captured_unqualified"
                            )
                            if (
                                process.returncode != 0
                                or metadata.get("status") != expected
                                or metadata.get("errors")
                            ):
                                raise RuntimeError(
                                    f"Recorder evidence incomplete for {output.name}"
                                )
                            if output.name != "window" and metadata.get(
                                "stop_requested"
                            ):
                                stopped = metadata.get("capture_stopped_monotonic_s")
                                cutoff = min(
                                    deadline,
                                    self.cancelled.requested_monotonic
                                    if self.cancelled.requested_monotonic is not None
                                    else deadline,
                                )
                                if (
                                    not isinstance(stopped, (int, float))
                                    or isinstance(stopped, bool)
                                    or not math.isfinite(stopped)
                                    or stopped < cutoff
                                ):
                                    raise RuntimeError(
                                        f"Recorder stopped before owned cutoff for {output.name}"
                                    )
                            if not _has_sample(output, output.name == "window"):
                                raise RuntimeError(
                                    f"Recorder has no samples for {output.name}"
                                )
                        except Exception as error:
                            self._fail_capture(output.name, error)
            except Exception as error:
                self.error_type, self.error_message = type(error).__name__, str(error)
            self.status = "failed" if self.error_type or self._arm_gaps else "stopped"
            self._finished.set()
            with self._arming:
                self._arming.notify_all()

    def _fail_capture(self, name: str, error: Exception) -> None:
        identity = self._identities.get(name)
        if identity is not None:
            with self._arming:
                self._worker_arms[
                    tuple(
                        identity[name]
                        for name in ("worker_pid", "worker_path", "worker_id")
                    )
                ] = "failed"
                self._arming.notify_all()
        if name not in self._failures:
            self._failures[name] = {
                "capture": name,
                **self._identities.get(name, {}),
                "detected_monotonic": monotonic(),
                "status": "failed-or-partial",
                "error_type": type(error).__name__,
                "error_message": str(error),
                "scope": "Acquisition loss; uncovered interval is unknown until the next verified capture",
            }
        if self.error_type is None:
            self.error_type, self.error_message = type(error).__name__, str(error)
        self.status = "degraded"

    def _stop_failed_capture(self, process: subprocess.Popen, output: Path) -> None:
        if process.poll() is not None:
            return
        started = self._failed_stop_started.get(output.name)
        if started is None:
            self._failed_stop_started[output.name] = monotonic()
            action = signal.SIGTERM
        elif monotonic() - started >= 2:
            action = signal.SIGKILL
        else:
            return
        try:
            os.killpg(process.pid, action)
        except ProcessLookupError:
            pass

    def _stop_processes(self) -> None:
        # Signal every recorder first so their orderly finalization runs concurrently.
        running = [process for process, _ in self.processes if process.poll() is None]
        for process in running:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = monotonic() + 10
        for process in running:
            try:
                process.wait(timeout=max(0.01, deadline - monotonic()))
            except subprocess.TimeoutExpired:
                pass
        for process in running:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = monotonic() + 2
        for process in running:
            process.wait(timeout=max(0.01, deadline - monotonic()))

    def _check_media_budget(self) -> None:
        limit = min(
            MAX_DIAGNOSTIC_MEDIA_BYTES, 1536 * 1024**2 * math.ceil(self.seconds / 600)
        )
        total = 0
        for index, path in enumerate(self.directory.rglob("*")):
            if index >= 512:
                raise RuntimeError("Recording evidence exceeded its file limit")
            try:
                metadata = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                # Session metadata is replaced atomically while recording.
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("Recording evidence exceeded its file limit")
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
            if total > limit:
                raise RuntimeError(
                    f"Recording stopped at its {limit / 1024**3:g} GiB evidence limit"
                )

    def report(self) -> dict[str, object]:
        with self._arming:
            arm_gaps = list(self._arm_gaps.values())
        captures: list[dict[str, object]] = []
        for process, output in self.processes:
            record: dict[str, object] = {
                "directory": output.name,
                "helper_pid": process.pid if type(process.pid) is int else None,
                "helper_created": self._helper_created.get(output.name),
                "returncode": process.poll(),
                "launched_monotonic": self._launched_at.get(output.name),
                "first_sample_observed_monotonic": self._sample_observed_at.get(
                    output.name
                ),
                "worker_identity": self._identities.get(output.name),
                "acquisition_failure": self._failures.get(output.name),
            }
            try:
                record["session"] = _session(output)
            except OSError, ValueError:
                record["session"] = {"status": "unavailable"}
            captures.append(record)
        return {
            "status": self.status,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "target": self.target,
            "started_monotonic": self.started_monotonic,
            "ready_monotonic": self.ready_monotonic,
            "deadline_monotonic": self.deadline,
            "stop_reason": self.stop_reason,
            "stop_requested_monotonic": self.cancelled.requested_monotonic,
            "qualification": "failed"
            if self.error_type or arm_gaps
            else "partial"
            if self.cancelled.requested_monotonic is not None
            and (
                self.deadline is None
                or self.cancelled.requested_monotonic < self.deadline
            )
            else "pending"
            if not captures or self.status in {"preparing", "starting"}
            else "recording-unqualified"
            if self.status in {"recording", "stopping"}
            else "captured-unqualified",
            "captures": captures,
            "acquisition_gaps": [*self._failures.values(), *arm_gaps],
            "worker_lifetimes": list(self._worker_lifetimes),
            "limitation": "Digital process output, not physical speakers; source correlation and video verification required",
        }
