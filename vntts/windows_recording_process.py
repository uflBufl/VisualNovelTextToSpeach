"""Own Windows recorder helpers without attaching their recorded source processes."""

import ctypes
import subprocess
from typing import Any

from vntts.cleanup import attempt_cleanup


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_processes", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _Limits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class RecorderJob:
    def __init__(self, kernel32: Any = None) -> None:
        self._kernel = kernel32 or getattr(ctypes, "WinDLL")(
            "kernel32", use_last_error=True
        )
        for name, arguments, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
            (
                "SetInformationJobObject",
                [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32],
                ctypes.c_int32,
            ),
            (
                "AssignProcessToJobObject",
                [ctypes.c_void_p, ctypes.c_void_p],
                ctypes.c_int32,
            ),
            ("CloseHandle", [ctypes.c_void_p], ctypes.c_int32),
        ):
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = arguments, result
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("Cannot create recorder Job Object")
        limits = _Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = OSError("Cannot configure recorder Job Object")
            attempt_cleanup(
                self.close, description="Recorder Job Object", primary_error=error
            )
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        # Popen retains the exact process handle, avoiding a new lookup after PID reuse.
        handle = getattr(process, "_handle", None)
        if handle is None or not self._kernel.AssignProcessToJobObject(
            self._handle, int(handle)
        ):
            raise OSError("Cannot own recorder process in Job Object")

    def close(self) -> None:
        if self._handle:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                raise OSError("Cannot close recorder Job Object")


def request_stop(process: subprocess.Popen) -> None:
    stream = process.stdin
    if stream is None:
        return
    if process.poll() is None:
        try:
            stream.write(b"stop\n")
            stream.flush()
        except OSError, ValueError:
            pass
    try:
        stream.close()
    except OSError, ValueError:
        pass
