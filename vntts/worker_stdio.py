"""Reconnect inherited worker pipes when Windows starts Python without a console."""

import ctypes
import os
import sys
from ctypes import wintypes

from vntts.cleanup import attempt_cleanup


def restore_worker_stdio() -> None:
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return

    import _winapi
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = (wintypes.DWORD,)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetFileType.argtypes = (wintypes.HANDLE,)
    kernel32.GetFileType.restype = wintypes.DWORD

    for name, identifier in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        handle = kernel32.GetStdHandle(identifier)
        if (
            handle in (None, ctypes.c_void_p(-1).value)
            or kernel32.GetFileType(handle) != 3
        ):
            continue
        # GetStdHandle borrows a process handle; the Python stream owns only a duplicate.
        process = _winapi.GetCurrentProcess()
        duplicate = _winapi.DuplicateHandle(
            process, handle, process, 0, False, _winapi.DUPLICATE_SAME_ACCESS
        )
        try:
            descriptor = msvcrt.open_osfhandle(
                duplicate, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT
            )
        except BaseException as error:
            attempt_cleanup(
                lambda: _winapi.CloseHandle(duplicate),
                description="Worker pipe handle cleanup",
                primary_error=error,
            )
            raise
        try:
            stream = os.fdopen(descriptor, "w", buffering=1, errors="backslashreplace")
        except BaseException as error:
            attempt_cleanup(
                lambda: os.close(descriptor),
                description="Worker pipe descriptor cleanup",
                primary_error=error,
            )
            raise
        setattr(sys, name, stream)
