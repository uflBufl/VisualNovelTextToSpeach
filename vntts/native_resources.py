"""Best-effort resource samples for one native synthesis process.

This module is deliberately diagnostic-only: every probe failure becomes an
unavailable value instead of an error for the synthesis path.
"""

import math
import os
import re
import subprocess
import sys
from tempfile import TemporaryFile
from threading import Event, Lock, Thread, current_thread
from time import monotonic

try:
    import psutil
except ImportError:  # pragma: no cover - dependency is declared by the app.
    psutil = None


class NativeResourceSampler:
    """Collect bounded, best-effort resource data for ``pid`` off the UI thread."""

    SAMPLE_INTERVAL_SECONDS = 0.25
    GPU_INTERVAL_SECONDS = 2.0
    GPU_TIMEOUT_SECONDS = 1.0
    FINISH_TIMEOUT_SECONDS = 0.02
    MAX_GPU_BOARDS = 8
    MAX_GPU_OUTPUT_BYTES = 8192
    MAX_VRAM_BYTES = 1 << 40

    def __init__(self, pid):
        self.pid = int(pid)
        self._stop = Event()
        self._start_lock = Lock()
        self._worker = None
        self._started_at = None
        self._first_sample_at = None
        self._last_sample_at = None
        self._next_gpu_at = None
        self._process = None
        self._host_process = None
        self._process_status = "unavailable"
        self._host_status = "unavailable"
        self._system_status = "unavailable"
        self._sample_count = 0
        self._cpu_start = None
        self._cpu_end = None
        self._native_rss_peak = None
        self._host_rss_peak = None
        self._ram_total = None
        self._ram_available_min = None
        self._swap_used = None
        self._cpu_logical_count = None
        self._thread_count_peak = None
        self._gpu_status = "unavailable"
        self._gpu_disabled = False
        self._gpu_sample_count = 0
        self._gpu_boards = {}

    def start(self):
        """Start owned daemon sampling and return immediately."""
        with self._start_lock:
            if self._worker is not None:
                return self
            self._started_at = monotonic()
            self._next_gpu_at = self._started_at
            self._worker = Thread(
                target=self._run, name="native-resource-sampler", daemon=True
            )
            self._worker.start()
        return self

    def finish(self):
        """Stop sampling and return a JSON-safe summary without raising."""
        self._stop.set()
        worker = self._worker
        if worker is not None and worker is not current_thread():
            worker.join(self.FINISH_TIMEOUT_SECONDS)
        return self._summary(complete=worker is None or not worker.is_alive())

    def _run(self):
        try:
            while not self._stop.is_set():
                self._sample(monotonic())
                self._stop.wait(self.SAMPLE_INTERVAL_SECONDS)
        except Exception:  # Diagnostics must never affect generation.
            return

    def _sample(self, now):
        if self._stop.is_set():
            return
        self._sample_process(now)
        if self._stop.is_set():
            return
        self._sample_system()
        if self._stop.is_set():
            return
        self._next_gpu_at = now if self._next_gpu_at is None else self._next_gpu_at
        if not self._gpu_disabled and now >= self._next_gpu_at:
            self._next_gpu_at = now + self.GPU_INTERVAL_SECONDS
            self._sample_gpu()

    def _sample_process(self, now):
        if psutil is None:
            return
        try:
            if self._process is None:
                self._process = psutil.Process(self.pid)
            memory = self._process.memory_info()
            cpu = self._process.cpu_times()
            threads = self._process.num_threads()
        except Exception as error:
            self._process_status = _process_status(error)
        else:
            self._process_status = "available"
            self._sample_count += 1
            self._first_sample_at = self._first_sample_at or now
            self._last_sample_at = now
            cpu_seconds = _finite_number(getattr(cpu, "user", None))
            system_seconds = _finite_number(getattr(cpu, "system", None))
            if cpu_seconds is not None and system_seconds is not None:
                total = cpu_seconds + system_seconds
                self._cpu_start = total if self._cpu_start is None else self._cpu_start
                self._cpu_end = total
            self._native_rss_peak = _peak(
                self._native_rss_peak, _integer(getattr(memory, "rss", None))
            )
            self._thread_count_peak = _peak(self._thread_count_peak, _integer(threads))
        try:
            if self._host_process is None:
                self._host_process = psutil.Process(os.getpid())
            self._host_rss_peak = _peak(
                self._host_rss_peak,
                _integer(getattr(self._host_process.memory_info(), "rss", None)),
            )
            self._host_status = "available"
        except Exception as error:
            self._host_status = _process_status(error)

    def _sample_system(self):
        if psutil is None:
            return
        available = False
        try:
            memory = psutil.virtual_memory()
        except Exception as error:
            self._system_status = _process_status(error)
        else:
            available = True
            self._ram_total = _integer(getattr(memory, "total", None))
            self._ram_available_min = _minimum(
                self._ram_available_min, _integer(getattr(memory, "available", None))
            )
        try:
            swap = psutil.swap_memory()
        except Exception:
            pass
        else:
            available = True
            self._swap_used = _integer(getattr(swap, "used", None))
        try:
            logical_count = psutil.cpu_count(logical=True)
        except Exception:
            pass
        else:
            available = True
            self._cpu_logical_count = _integer(logical_count)
        if available:
            self._system_status = "available"

    def _sample_gpu(self):
        try:
            with TemporaryFile(mode="w+b") as output:
                result = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=index,driver_version,utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits",
                    ],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=self.GPU_TIMEOUT_SECONDS,
                    check=False,
                    **_subprocess_options(),
                )
                output.seek(0)
                raw_output = output.read(self.MAX_GPU_OUTPUT_BYTES + 1)
        except FileNotFoundError:
            self._disable_gpu("missing")
            return
        except subprocess.TimeoutExpired:
            self._disable_gpu("timeout")
            return
        except OSError:
            self._disable_gpu("unavailable")
            return
        if result.returncode:
            self._disable_gpu("unsupported")
            return
        if len(raw_output) > self.MAX_GPU_OUTPUT_BYTES:
            self._disable_gpu("output-limit")
            return
        boards = _gpu_boards(raw_output.decode("utf-8", errors="replace"))
        if not boards:
            self._disable_gpu("unavailable")
            return
        partial = False
        self._gpu_sample_count += 1
        for board in boards:
            index = board["logical_index"]
            current = self._gpu_boards.get(index)
            if current is None:
                if len(self._gpu_boards) >= self.MAX_GPU_BOARDS:
                    continue
                current = {
                    "logical_index": index,
                    "driver_version": board["driver_version"],
                    "utilization_percent_peak": None,
                    "utilization_percent_mean": None,
                    "utilization_sample_count": 0,
                    "vram_used_bytes_peak": None,
                    "vram_total_bytes": None,
                }
                self._gpu_boards[index] = current
            elif board["driver_version"] is not None:
                current["driver_version"] = board["driver_version"]
            current["utilization_percent_peak"] = _peak(
                current["utilization_percent_peak"], board["utilization_percent"]
            )
            if board["utilization_percent"] is not None:
                count = current["utilization_sample_count"]
                current["utilization_percent_mean"] = (
                    (current["utilization_percent_mean"] or 0) * count
                    + board["utilization_percent"]
                ) / (count + 1)
                current["utilization_sample_count"] = count + 1
            current["vram_used_bytes_peak"] = _peak(
                current["vram_used_bytes_peak"], board["vram_used_bytes"]
            )
            current["vram_total_bytes"] = _peak(
                current["vram_total_bytes"], board["vram_total_bytes"]
            )
            partial = partial or any(
                value is None
                for value in (
                    board["utilization_percent"],
                    board["vram_used_bytes"],
                    board["vram_total_bytes"],
                    board["driver_version"],
                )
            )
        self._gpu_status = "partial" if partial else "available"

    def _disable_gpu(self, status):
        self._gpu_status = status
        self._gpu_disabled = True

    def _summary(self, *, complete=True):
        coverage = (
            self._last_sample_at - self._first_sample_at
            if self._first_sample_at is not None and self._last_sample_at is not None
            else None
        )
        raw_cpu_delta = (
            self._cpu_end - self._cpu_start
            if self._cpu_start is not None and self._cpu_end is not None
            else None
        )
        cpu_delta = raw_cpu_delta if coverage is not None and coverage > 0 else None
        return {
            "complete": complete,
            "sample_count": self._sample_count,
            "interval_seconds": self.SAMPLE_INTERVAL_SECONDS,
            "coverage_seconds": _finite_number(coverage),
            "native_process": {
                "status": self._process_status,
                "cpu_seconds_delta": _finite_number(cpu_delta),
                "avg_cores_used": (
                    _finite_number(cpu_delta / coverage)
                    if coverage and cpu_delta is not None
                    else None
                ),
                "rss_bytes_peak": self._native_rss_peak,
                "thread_count_peak": self._thread_count_peak,
            },
            "host_app": {
                "status": self._host_status,
                "rss_bytes_peak": self._host_rss_peak,
            },
            "system": {
                "status": self._system_status,
                "ram_total_bytes": self._ram_total,
                "ram_available_bytes_min": self._ram_available_min,
                "swap_used_bytes": self._swap_used,
                "cpu_logical_count": self._cpu_logical_count,
            },
            "gpu": {
                "status": self._gpu_status,
                "sample_count": self._gpu_sample_count,
                "boards": [
                    {key: value for key, value in board.items()}
                    for _, board in sorted(tuple(self._gpu_boards.items()))
                ],
            },
        }


def _gpu_boards(output):
    boards = []
    seen_indexes = set()
    for line in output.splitlines()[: NativeResourceSampler.MAX_GPU_BOARDS]:
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 5:
            continue
        index = _nonnegative_integer(fields[0])
        if index is None or index in seen_indexes:
            continue
        seen_indexes.add(index)
        utilization = _percentage(fields[2])
        used_mib = _nonnegative(fields[3])
        total_mib = _nonnegative(fields[4])
        boards.append(
            {
                "logical_index": index,
                "driver_version": _driver_version(fields[1]),
                "utilization_percent": utilization,
                "vram_used_bytes": _mib_to_bytes(used_mib),
                "vram_total_bytes": _mib_to_bytes(total_mib),
            }
        )
    return boards


def _subprocess_options():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _finite_number(value):
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _integer(value):
    number = _finite_number(value)
    return int(number) if number is not None else None


def _nonnegative_integer(value):
    number = _finite_number(value)
    return (
        int(number)
        if number is not None and number >= 0 and number.is_integer()
        else None
    )


def _percentage(value):
    number = _finite_number(value)
    return number if number is not None and 0 <= number <= 100 else None


def _nonnegative(value):
    number = _finite_number(value)
    return number if number is not None and number >= 0 else None


def _mib_to_bytes(value):
    if value is None:
        return None
    bytes_value = _finite_number(value * 1024 * 1024)
    return (
        int(bytes_value)
        if bytes_value is not None
        and bytes_value <= NativeResourceSampler.MAX_VRAM_BYTES
        else None
    )


def _driver_version(value):
    return value if len(value) <= 32 and re.fullmatch(r"\d+(?:\.\d+)*", value) else None


def _peak(previous, current):
    return (
        current
        if previous is None
        else previous
        if current is None
        else max(previous, current)
    )


def _minimum(previous, current):
    return (
        current
        if previous is None
        else previous
        if current is None
        else min(previous, current)
    )


def _process_status(error):
    if psutil is not None and isinstance(error, getattr(psutil, "NoSuchProcess", ())):
        return "exited"
    if psutil is not None and isinstance(error, getattr(psutil, "AccessDenied", ())):
        return "access-denied"
    return "unsupported"
