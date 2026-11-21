"""Explicit native recording for a bounded Reading diagnostic session."""

import ctypes
import hashlib
import json
import math
import ntpath
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from itertools import islice
from pathlib import Path
from subprocess import Popen as launch_process
from threading import Condition, Event, Lock
from time import monotonic
from uuid import uuid4

import psutil

from vntts.path_safety import open_regular_binary
from vntts.process_identity import process_fingerprint
from vntts.windows_recording_process import RecorderJob, request_stop

MAX_DIAGNOSTIC_SECONDS = 3600
MAX_DIAGNOSTIC_MEDIA_BYTES = 9 * 1024**3
WORKER_RETIREMENT_TIMEOUT = 3.0


def process_executable(pid: int) -> str:
    if sys.platform == "win32":
        try:
            executable = psutil.Process(pid).exe()
        except psutil.Error as error:
            raise RuntimeError("Selected recording process is unavailable") from error
        if not executable:
            raise RuntimeError("Selected recording process has no executable")
        return executable
    if sys.platform != "darwin":
        raise RuntimeError("Process recording requires macOS or Windows")
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
        raise RuntimeError("Selected recording process is unavailable")
    return buffer.value.decode()


def _same_executable(actual: object, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    if sys.platform == "win32":
        return ntpath.normcase(ntpath.normpath(actual)) == ntpath.normcase(
            ntpath.normpath(expected)
        )
    return actual == expected


def _launch_options() -> dict:
    if sys.platform == "win32":
        return {"stdin": subprocess.PIPE, "creationflags": 0x08000000}
    return {"start_new_session": True}


def _stop_process(process: subprocess.Popen, timeout: float) -> None:
    if sys.platform == "win32":
        request_stop(process)
        try:
            process.wait(timeout=max(0.01, timeout))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        return
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
    with tempfile.TemporaryFile() as log, ExitStack() as ownership:
        job = RecorderJob() if sys.platform == "win32" else None
        if job is not None:
            ownership.callback(job.close)
        process = launch_process(
            command, stdout=log, stderr=subprocess.STDOUT, **_launch_options()
        )
        ownership.callback(_stop_process, process, 2)
        if job is not None:
            job.assign(process)
        deadline = (
            min(monotonic() + timeout, deadline)
            if deadline is not None
            else monotonic() + timeout
        )
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


def prepare_recorders(
    directory: Path, cancelled: Event | None = None, *, deadline: float | None = None
) -> tuple[Path, Path]:
    """Build before measurement; Stop also owns any compiler/preflight children."""
    cancelled = cancelled if cancelled is not None else Event()
    if sys.platform == "win32":
        from vntts.native_capture import worker_executable

        worker = worker_executable()
        _run_checked([str(worker), "--preflight"], cancelled, 10, deadline=deadline)
        _run_checked([str(worker), "--selfcheck"], cancelled, 10, deadline=deadline)
        return worker, worker
    if sys.platform != "darwin":
        raise RuntimeError("Window/process recording requires macOS or Windows")
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
        self._jobs: dict[int, RecorderJob] = {}
        self.status = "preparing"
        self.error_type: str | None = None
        self.error_message: str | None = None
        self.stop_reason: str | None = None
        self.started_monotonic: float | None = None
        self.ready_monotonic: float | None = None
        self._launched_at: dict[str, float] = {}
        self._helper_created: dict[str, float] = {}
        self._helper_fingerprints: dict[str, dict[str, str]] = {}
        self._sample_observed_at: dict[str, float] = {}
        self._capture_stop_started: dict[str, float] = {}
        self._identities: dict[str, dict[str, object]] = {}
        self._failures: dict[str, dict[str, object]] = {}
        self._native_gaps: dict[str, list[dict[str, object]]] = {}
        self._worker_lifetimes: list[dict[str, object]] = []
        self._arming = Condition()
        self._worker_arms: dict[tuple[object, ...], str] = {}
        self._arm_gaps: dict[tuple[object, ...], dict[str, object]] = {}
        self._retirement_requests: dict[tuple[object, ...], float] = {}
        self._retirement_gaps: dict[tuple[object, ...], dict[str, object]] = {}
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

    def finish_worker(self, identity: dict[str, object]) -> tuple[bool, str]:
        """Keep the source alive until its recorder finalizes, with bounded waiting."""
        key = tuple(
            identity[name] for name in ("worker_pid", "worker_path", "worker_id")
        )
        deadline = monotonic() + WORKER_RETIREMENT_TIMEOUT
        with self._arming:
            if self._finished.is_set():
                return True, "recorder-stopped"
            self._retirement_requests.setdefault(key, monotonic())
            while True:
                state = self._worker_arms.get(key)
                if state == "retired":
                    return True, "finalized"
                if state == "failed":
                    return False, "capture-failed"
                if self._finished.is_set():
                    return True, "recorder-stopped"
                remaining = deadline - monotonic()
                if remaining <= 0:
                    reason = "Audio worker recording finalization timed out"
                    self._retirement_gaps.setdefault(
                        key,
                        {
                            "capture": "rust-retirement",
                            **identity,
                            "detected_monotonic": monotonic(),
                            "status": "failed-or-partial",
                            "error_type": "RustRecordingRetirementError",
                            "error_message": reason,
                            "scope": "Worker exited without acknowledged segment finalization",
                        },
                    )
                    if self.error_type is None:
                        self.error_type = "RustRecordingRetirementError"
                        self.error_message = reason
                    self.status = "degraded"
                    return False, "timeout"
                self._arming.wait(min(0.02, remaining))

    def _finish_worker_captures(self, *, request_stop: bool = True) -> None:
        with self._arming:
            requests = tuple(self._retirement_requests.items())
        for identity, cutoff in requests:
            with self._arming:
                if self._worker_arms.get(identity) in {"retired", "failed"}:
                    continue
            name = next(
                (
                    name
                    for name, owner in self._identities.items()
                    if tuple(
                        owner[key] for key in ("worker_pid", "worker_path", "worker_id")
                    )
                    == identity
                ),
                None,
            )
            capture = next(
                (
                    (process, output)
                    for process, output in self.processes
                    if output.name == name
                ),
                None,
            )
            if capture is None:
                continue
            process, output = capture
            if process.poll() is None:
                if request_stop:
                    self._request_capture_stop(process, output)
                continue
            try:
                self._verify_final_capture(process, output, self.deadline or cutoff)
            except Exception as error:
                self._fail_capture(output.name, error)
            else:
                for lifetime in self._worker_lifetimes:
                    if lifetime["capture"] == name:
                        lifetime["retirement_requested_monotonic"] = cutoff
                        lifetime["capture_finalized_monotonic"] = monotonic()
                with self._arming:
                    self._worker_arms[identity] = "retired"
                    self._arming.notify_all()

    def _launch(self, command: list[str], output: Path) -> None:
        if self.cancelled.is_set():
            raise InterruptedError("Recording cancelled")
        with (
            (self.directory / (output.name + "-launcher.log")).open("xb") as log,
            ExitStack() as ownership,
        ):
            job = RecorderJob() if sys.platform == "win32" else None
            if job is not None:
                ownership.callback(job.close)
            process = launch_process(
                command, stdout=log, stderr=subprocess.STDOUT, **_launch_options()
            )
            ownership.callback(_stop_process, process, 2)
            if job is not None:
                job.assign(process)
                self._jobs[process.pid] = job
            self.processes.append((process, output))
            ownership.pop_all()
        self._launched_at[output.name] = monotonic()
        try:
            identity = psutil.Process(process.pid)
            self._helper_created[output.name] = identity.create_time()
            self._helper_fingerprints[output.name] = process_fingerprint(identity)
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
            deadline = self._start_target_captures(video, audio, deadline)
            self._record_workers(audio, deadline)
            self.status = (
                "degraded" if self.error_type or self._native_gaps else "stopping"
            )
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
                    self._verify_final_captures(deadline)
            except Exception as error:
                self.error_type, self.error_message = type(error).__name__, str(error)
            self.status = (
                "failed"
                if self.error_type or self._arm_gaps
                else "degraded"
                if self._native_gaps
                else "stopped"
            )
            self._finished.set()
            with self._arming:
                self._arming.notify_all()

    def _start_target_captures(
        self, video: Path, audio: Path, deadline: float | None
    ) -> float:
        if shutil.disk_usage(self.directory).free < 2 * 1024**3:
            raise RuntimeError(
                "At least 2 GiB free space is required for native recording"
            )
        pid, window = (
            int(str(self.target["pid"])),
            int(str(self.target["window_id"])),
        )
        max_window = 2**63 - 1 if sys.platform == "win32" else 2**32 - 1
        max_pid = 2**32 - 1 if sys.platform == "win32" else 2**31 - 1
        if not 0 < pid <= max_pid or not 0 < window <= max_window:
            raise ValueError("Explicit positive process and window IDs required")
        executable = process_executable(pid)
        if not _same_executable(executable, str(self.target["executable"])):
            raise RuntimeError("Selected game process changed before recording")
        self.started_monotonic = monotonic()
        if deadline is None:
            deadline = self.started_monotonic + self.seconds
        self.deadline = deadline
        remaining_seconds = min(self.seconds, deadline - self.started_monotonic)
        if remaining_seconds < 1:
            raise TimeoutError("Diagnostic session deadline leaves no capture window")
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
        return deadline

    def _record_workers(self, audio: Path, deadline: float) -> None:
        seen: dict[tuple[int, str, str], str] = {}
        previous: set[tuple[int, str, str]] = set()
        self.status = "degraded" if self.error_type else "starting"
        last_budget_check = 0.0
        while not self.cancelled.is_set() and monotonic() < deadline:
            current = self._discover_worker_captures(audio, deadline, seen, previous)
            self._finish_worker_captures()
            self._retire_worker_captures(seen, previous - current)
            previous = current
            ready = self._check_capture_readiness()
            if ready and self.ready_monotonic is None:
                self.ready_monotonic = monotonic()
                self.status = "degraded" if self._native_gaps else "recording"
            if monotonic() - last_budget_check >= 1:
                self._check_media_budget()
                last_budget_check = monotonic()
            if shutil.disk_usage(self.directory).free < 512 * 1024**2:
                raise RuntimeError("Recording stopped because free space is low")
            self.cancelled.wait(0.1)

    def _discover_worker_captures(
        self,
        audio: Path,
        deadline: float,
        seen: dict[tuple[int, str, str], str],
        previous: set[tuple[int, str, str]],
    ) -> set[tuple[int, str, str]]:
        current: set[tuple[int, str, str]] = set()
        try:
            workers = tuple(islice(self.workers(), 9))
        except Exception as error:
            self._fail_capture("worker-discovery", error)
            workers = ()
            current.update(previous)
        for worker in workers:
            self._start_worker_capture(worker, audio, deadline, seen, current)
        return current

    def _start_worker_capture(
        self,
        worker: dict[str, object],
        audio: Path,
        deadline: float,
        seen: dict[tuple[int, str, str], str],
        current: set[tuple[int, str, str]],
    ) -> None:
        try:
            worker_pid = int(str(worker["worker_pid"]))
            worker_path = str(worker["worker_path"])
            worker_id = str(worker.get("worker_id", worker_pid))
            if (
                not 0
                < worker_pid
                <= (2**32 - 1 if sys.platform == "win32" else 2**31 - 1)
                or not Path(worker_path).is_absolute()
                or not 1 <= len(worker_id) <= 160
            ):
                raise ValueError("Invalid explicit audio worker identity")
        except (KeyError, TypeError, ValueError) as error:
            self._fail_capture("worker-discovery", error)
            return
        identity = (worker_pid, worker_path, worker_id)
        current.add(identity)
        if identity in seen:
            return
        if len(seen) >= 8:
            self._fail_capture(
                "worker-limit",
                RuntimeError(
                    "Audio worker recording limit reached; session is incomplete"
                ),
            )
            return
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

    def _retire_worker_captures(
        self,
        seen: dict[tuple[int, str, str], str],
        retired: set[tuple[int, str, str]],
    ) -> None:
        for identity in retired:
            retired_name = seen.get(identity)
            if retired_name is None:
                continue
            for lifetime in self._worker_lifetimes:
                if lifetime["capture"] == retired_name:
                    lifetime["retired_observed_monotonic"] = monotonic()
            with self._arming:
                finalized = self._worker_arms.get(identity) == "retired"
            if not finalized:
                self._fail_capture(
                    retired_name,
                    RuntimeError("Audio worker retired; process capture is partial"),
                )

    def _check_capture_readiness(self) -> bool:
        ready = not self.error_type
        for process, output in self.processes:
            if output.name in self._failures:
                self._request_capture_stop(process, output)
                continue
            try:
                ready &= self._check_capture(process, output)
            except Exception as error:
                ready = False
                self._fail_capture(output.name, error)
                self._request_capture_stop(process, output)
        return ready

    def _check_capture(self, process: subprocess.Popen, output: Path) -> bool:
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
        self._observe_capture_gaps(output, metadata)
        expected = (
            {"succeeded", "partial"}
            if output.name == "window"
            else {"captured_unqualified"}
        )
        if (
            metadata.get("status") in {"failed", "incomplete"}
            or metadata.get("errors")
            or (process.poll() is not None and metadata.get("status") not in expected)
        ):
            raise RuntimeError(
                f"Recorder failed for {output.name}; inspect its session.json"
            )
        self._update_capture_arming(process, output, metadata)
        observed = _has_sample(output, output.name == "window")
        if observed:
            self._sample_observed_at.setdefault(output.name, monotonic())
        if not observed and monotonic() - self._launched_at[output.name] > 10:
            raise RuntimeError(f"Recorder readiness unverified for {output.name}")
        return observed

    def _observe_capture_gaps(self, output: Path, metadata: dict) -> None:
        gaps = metadata.get("acquisition_gaps", [])
        if not isinstance(gaps, list) or len(gaps) > 16:
            raise ValueError("Invalid native acquisition gaps")
        for gap in gaps:
            if output.name == "window":
                if (
                    not isinstance(gap, dict)
                    or gap.get("kind") != "gap"
                    or gap.get("reason")
                    not in {"video_source_suspended", "video_source_blank"}
                    or gap.get("end_reason")
                    not in {"frame_resumed", "recording_stopped"}
                    or any(
                        type(gap.get(name)) not in (int, float)
                        or not math.isfinite(gap[name])
                        for name in ("started_monotonic_s", "ended_monotonic_s")
                    )
                    or gap["started_monotonic_s"] < 0
                    or gap["ended_monotonic_s"] < gap["started_monotonic_s"]
                ):
                    raise ValueError("Invalid native video acquisition gap")
                continue
            if (
                not isinstance(gap, dict)
                or gap.get("kind") != "gap"
                or gap.get("reason") != "source_sample_gap"
                or type(gap.get("file_frame_start")) is not int
                or gap["file_frame_start"] < 0
                or any(
                    type(gap.get(name)) not in (int, float)
                    or not math.isfinite(gap[name])
                    for name in (
                        "sample_delta_frames",
                        "started_monotonic_s",
                        "ended_monotonic_s",
                    )
                )
                or gap["sample_delta_frames"] <= 0
                or gap["started_monotonic_s"] < 0
                or gap["ended_monotonic_s"] <= gap["started_monotonic_s"]
            ):
                raise ValueError("Invalid native acquisition gap")
        if output.name == "window" and (
            (
                metadata.get("status") == "partial"
                and (
                    metadata.get("native_status") != "succeeded"
                    or metadata.get("video_change_coverage") != "source_interrupted"
                    or not gaps
                )
            )
            or (
                metadata.get("status") == "succeeded"
                and (
                    gaps
                    or metadata.get("video_change_coverage") == "source_interrupted"
                )
            )
        ):
            raise ValueError("Invalid native video interruption evidence")
        if gaps:
            self._native_gaps[output.name] = gaps
            self.status = "degraded"

    def _update_capture_arming(
        self,
        process: subprocess.Popen,
        output: Path,
        metadata: dict,
    ) -> None:
        worker_identity = self._identities.get(output.name)
        if worker_identity is not None:
            worker_key = (
                worker_identity["worker_pid"],
                worker_identity["worker_path"],
                worker_identity["worker_id"],
            )
            with self._arming:
                if worker_key in self._retirement_requests:
                    return
            target = metadata.get("target")
            started = metadata.get("capture_started_monotonic_s")
            if (
                process.poll() is None
                and metadata.get("status") == "in_progress"
                and isinstance(target, dict)
                and target.get("pid") == worker_identity["worker_pid"]
                and _same_executable(
                    target.get("executable"), str(worker_identity["worker_path"])
                )
                and (type(started) is int or type(started) is float)
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

    def _verify_final_captures(self, deadline: float) -> None:
        for process, output in self.processes:
            if output.name in self._failures:
                continue
            try:
                self._verify_final_capture(process, output, deadline)
            except Exception as error:
                self._fail_capture(output.name, error)

    def _verify_final_capture(
        self, process: subprocess.Popen, output: Path, deadline: float
    ) -> None:
        metadata = _session(output)
        self._observe_capture_gaps(output, metadata)
        expected = (
            {"succeeded", "partial"}
            if output.name == "window"
            else {"captured_unqualified"}
        )
        if (
            process.returncode != 0
            or metadata.get("status") not in expected
            or metadata.get("errors")
        ):
            raise RuntimeError(f"Recorder evidence incomplete for {output.name}")
        if output.name != "window" and metadata.get("stop_requested"):
            stopped = metadata.get("capture_stopped_monotonic_s")
            cutoff = min(
                deadline,
                self.cancelled.requested_monotonic
                if self.cancelled.requested_monotonic is not None
                else deadline,
            )
            identity = self._identities.get(output.name)
            if identity is not None:
                key = tuple(
                    identity[name]
                    for name in ("worker_pid", "worker_path", "worker_id")
                )
                with self._arming:
                    cutoff = min(cutoff, self._retirement_requests.get(key, cutoff))
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
            raise RuntimeError(f"Recorder has no samples for {output.name}")

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

    def _request_capture_stop(self, process: subprocess.Popen, output: Path) -> None:
        if process.poll() is not None:
            return
        started = self._capture_stop_started.get(output.name)
        if sys.platform == "win32":
            if started is None:
                self._capture_stop_started[output.name] = monotonic()
                request_stop(process)
            elif monotonic() - started >= 2:
                try:
                    job = self._jobs.get(process.pid)
                    if job is not None:
                        job.close()
                finally:
                    if process.poll() is None:
                        process.kill()
            return
        if started is None:
            self._capture_stop_started[output.name] = monotonic()
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
        if sys.platform == "win32":
            try:
                for process, _ in self.processes:
                    request_stop(process)
                deadline = monotonic() + 10
                self._wait_capture_stops(running, deadline)
            finally:
                try:
                    with ExitStack() as ownership:
                        for job in self._jobs.values():
                            ownership.callback(job.close)
                        self._jobs.clear()
                finally:
                    for process in running:
                        if process.poll() is None:
                            process.kill()
                    deadline = monotonic() + 2
                    for process in running:
                        process.wait(timeout=max(0.01, deadline - monotonic()))
            self._finish_worker_captures(request_stop=False)
            return
        for process in running:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = monotonic() + 10
        self._wait_capture_stops(running, deadline)
        for process in running:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = monotonic() + 2
        for process in running:
            process.wait(timeout=max(0.01, deadline - monotonic()))
        self._finish_worker_captures(request_stop=False)

    def _wait_capture_stops(
        self, running: list[subprocess.Popen], deadline: float
    ) -> None:
        while True:
            self._finish_worker_captures(request_stop=False)
            pending = next(
                (process for process in running if process.poll() is None), None
            )
            remaining = deadline - monotonic()
            if pending is None or remaining <= 0:
                return
            try:
                pending.wait(timeout=min(0.02, remaining))
            except subprocess.TimeoutExpired:
                pass

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
            retirement_gaps = list(self._retirement_gaps.values())
        captures: list[dict[str, object]] = []
        for process, output in self.processes:
            record: dict[str, object] = {
                "directory": output.name,
                "helper_pid": process.pid if type(process.pid) is int else None,
                "helper_created": self._helper_created.get(output.name),
                "helper_fingerprint": self._helper_fingerprints.get(output.name),
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
            if self._native_gaps
            or (
                self.cancelled.requested_monotonic is not None
                and (
                    self.deadline is None
                    or self.cancelled.requested_monotonic < self.deadline
                )
            )
            else "pending"
            if not captures or self.status in {"preparing", "starting"}
            else "recording-unqualified"
            if self.status in {"recording", "stopping"}
            else "captured-unqualified",
            "captures": captures,
            "acquisition_gaps": [
                *self._failures.values(),
                *arm_gaps,
                *retirement_gaps,
                *(
                    {**gap, "capture": name, **self._identities.get(name, {})}
                    for name, gaps in list(self._native_gaps.items())
                    for gap in gaps
                ),
            ],
            "worker_lifetimes": list(self._worker_lifetimes),
            "limitation": "Digital process output, not physical speakers; source correlation and video verification required",
        }
