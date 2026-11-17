"""Persistent fatal Python stacks, exported as locations rather than raw dumps."""

import faulthandler
import logging
import os
import re
import stat
from collections import deque
from pathlib import Path
from typing import TypedDict

from vntts.path_safety import open_regular_binary

_MAX_BYTES = 512 * 1024
_MAX_FAULTS = 4
_MAX_THREADS = 32
_MAX_FRAMES = 64
_descriptor: int | None = None
_path: Path | None = None
_recording_error: str | None = None
_FAULT = re.compile(
    r"^(?:Fatal Python error:|Windows fatal exception:)[^\r\n]{0,512}\r?$", re.M
)
_THREAD = re.compile(
    r"^(Current thread 0x[0-9a-f]+|Thread 0x[0-9a-f]+|Stack)"
    r" \(most recent call first\):[^\r\n]*\r?$",
    re.M | re.I,
)
_FRAME = re.compile(r'\s*File "([^\r\n]{1,500})", line ([0-9]{1,10}) in (.+)')
_FUNCTION = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]{0,159}|"
    r"<(?:module|lambda|listcomp|dictcomp|setcomp|genexpr)>)"
)


class _Frame(TypedDict):
    file: str
    function: str
    line: int


def configure_fatal_crash_log(path: Path) -> None:
    """Own one append-only descriptor until OS exit, including native teardown."""
    global _descriptor, _path, _recording_error
    if _descriptor is not None:
        return
    _path = path
    descriptor = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Fatal stack recording requires a regular file")
        faulthandler.enable(descriptor, all_threads=True, c_stack=False)
    except (OSError, RuntimeError) as error:
        if descriptor is not None:
            os.close(descriptor)
        _recording_error = type(error).__name__
        logging.getLogger(__name__).warning(
            "Fatal stack recording is unavailable (%s)", _recording_error
        )
        return
    _descriptor, _recording_error = descriptor, None
    # Closing or rotating this descriptor can redirect the handler to another file.


def _frame(line: str) -> _Frame | None:
    match = _FRAME.fullmatch(line)
    if match is None or not _FUNCTION.fullmatch(match[3]):
        return None
    line_number = int(match[2])
    if not 0 <= line_number < 2**31:
        return None
    source = re.search(
        r"(?:^|/)((?:vntts|scripts|tests)/(?:[A-Za-z_][A-Za-z0-9_]*/)*"
        r"[A-Za-z_][A-Za-z0-9_]*\.py)$",
        match[1].replace("\\", "/"),
    )
    return {
        "file": source[1] if source else "<external>",
        "function": match[3],
        "line": line_number,
    }


def _frames(text: str) -> tuple[list[_Frame], bool]:
    frames = []
    for line in text.splitlines():
        frame = _frame(line)
        if frame is not None:
            frames.append(frame)
            if len(frames) > _MAX_FRAMES:
                break
    return frames[:_MAX_FRAMES], len(frames) > _MAX_FRAMES


def _threads(text: str) -> tuple[list[dict[str, object]], bool]:
    headers = list(_THREAD.finditer(text))
    spans = [
        (header, headers[index + 1].start() if index + 1 < len(headers) else len(text))
        for index, header in enumerate(headers)
    ]
    spans.sort(key=lambda span: not span[0][1].lower().startswith(("current", "stack")))
    threads = []
    for header, end in spans[:_MAX_THREADS]:
        frames, truncated = _frames(text[header.end() : end])
        threads.append(
            {
                "current": header[1].lower().startswith(("current", "stack")),
                "frames": frames,
                "truncated": truncated,
            }
        )
    return threads, len(headers) > _MAX_THREADS


def _faults(text: str, *, input_truncated: bool) -> list[dict[str, object]]:
    headers = list(deque(_FAULT.finditer(text), maxlen=_MAX_FAULTS))
    faults = []
    if not headers and input_truncated:
        threads, _truncated = _threads(text)
        if threads:
            return [{"reason": "unknown", "threads": threads, "truncated": True}]
    reasons = {
        "segmentation fault",
        "aborted",
        "bus error",
        "illegal instruction",
        "floating-point exception",
        "access violation",
        "stack overflow",
    }
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        threads, truncated = _threads(text[header.end() : end])
        reason = header[0].partition(":")[2].strip().lower()
        faults.append(
            {
                "reason": reason if reason in reasons else "fatal-python-error",
                "threads": threads,
                "truncated": truncated,
            }
        )
    return faults


def fatal_crash_report() -> dict[str, object]:
    report: dict[str, object] = {
        "version": 1,
        "available": False,
        "recording_enabled": _descriptor is not None,
        "recording_error": _recording_error,
        "current_process_correlation_available": False,
        "native_memory_dump_included": False,
        "faults": [],
    }
    if _path is None:
        return report
    try:
        with open_regular_binary(_path) as source:
            size = source.seek(0, os.SEEK_END)
            source.seek(max(0, size - _MAX_BYTES))
            payload = source.read(_MAX_BYTES)
        if size > _MAX_BYTES:
            payload = payload.partition(b"\n")[2]
        faults = _faults(
            payload.decode("utf-8", errors="replace"), input_truncated=size > _MAX_BYTES
        )
        report.update(
            available=bool(faults), faults=faults, truncated=size > _MAX_BYTES
        )
    except FileNotFoundError:
        pass
    except OSError as error:
        report["read_error"] = type(error).__name__
    return report
