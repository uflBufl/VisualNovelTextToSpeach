"""Leaf primitives for exclusive generation-output ownership."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.text_utils import slugify

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)

LEASE_SCHEMA = "vntts.authoring-generation-lease"
LEASE_VERSION = 1


class BulkGenerationError(RuntimeError):
    """A queue cannot be generated or resumed safely."""


def inspect_process_status(pid):
    """Return ``live``, ``dead`` or ``unknown`` without changing the process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if pid <= 0:
        return "unknown"
    if sys.platform == "win32":
        return _inspect_windows_process(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except (PermissionError, OSError):
        return "unknown"
    return "live"


def process_is_alive(pid):
    """Fail closed for ownership checks when liveness cannot be inspected."""
    return inspect_process_status(pid) != "dead"


def _inspect_windows_process(pid):
    # Windows implements os.kill(pid, 0) with TerminateProcess. A read-only
    # process handle is required for a genuinely non-destructive liveness probe.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return "dead"
        if error == error_access_denied:
            return "unknown"
        return "unknown"
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return "unknown"
        return "live" if exit_code.value == still_active else "dead"
    finally:
        kernel32.CloseHandle(handle)


def process_started_at(pid):
    """Return the operating-system process start identity when inspectable."""
    try:
        pid = int(pid)
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            check=True,
            capture_output=True,
            text=True,
        )
    except (TypeError, ValueError, OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def archive_interrupted_artifact(
    output_directory,
    source,
    *,
    expected_payload=None,
):
    """Move one abandoned output artifact into its contained recovery archive."""
    output_directory = Path(output_directory).resolve()
    source = Path(source)
    payload = source.read_bytes()
    if expected_payload is not None and payload != expected_payload:
        raise BulkGenerationError(
            f"Interrupted artifact changed before recovery: {source}"
        )
    archive_root = (output_directory / "interrupted").resolve()
    try:
        archive_root.relative_to(output_directory)
    except ValueError as error:
        raise BulkGenerationError(
            "Interrupted artifact directory must stay within generation output"
        ) from error
    archive_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()[:12]
    stem = slugify(source.name) or "artifact"
    candidate = archive_root / f"{stem}-{digest}{source.suffix}"
    suffix = 2
    while candidate.exists():
        if candidate.read_bytes() == payload:
            if expected_payload is not None and source.read_bytes() != expected_payload:
                raise BulkGenerationError(
                    f"Interrupted artifact changed before recovery: {source}"
                )
            source.unlink()
            return candidate
        candidate = archive_root / f"{stem}-{digest}-{suffix}{source.suffix}"
        suffix += 1
    if expected_payload is not None and source.read_bytes() != expected_payload:
        raise BulkGenerationError(
            f"Interrupted artifact changed before recovery: {source}"
        )
    os.replace(source, candidate)
    return candidate


class GenerationLease:
    """Exclusive, crash-recoverable ownership of one generated-audio directory."""

    def __init__(self, output_directory, queue_sha256, *, process_checker):
        self.output_directory = Path(output_directory)
        self.path = self.output_directory / ".generation-lease.json"
        self.queue_sha256 = queue_sha256
        self.process_checker = process_checker
        self.lease_id = secrets.token_hex(16)
        self.committed = False
        self.document = None

    def __enter__(self):
        try:
            with exclusive_advisory_lock(self.path.with_suffix(".guard")):
                if self.path.exists():
                    try:
                        lease_payload = self.path.read_bytes()
                        existing = json.loads(lease_payload.decode("utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise BulkGenerationError(
                            f"Unable to read generation lease {self.path}: {error}"
                        ) from error
                    if (
                        not isinstance(existing, dict)
                        or existing.get("schema") != LEASE_SCHEMA
                        or existing.get("schema_version") != LEASE_VERSION
                        or not isinstance(existing.get("queue_sha256"), str)
                    ):
                        raise BulkGenerationError(
                            f"Unrecognized generation lease blocks output: {self.path}"
                        )
                    same_host = existing.get("hostname") in {
                        None,
                        socket.gethostname(),
                    }
                    live = same_host and self.process_checker(existing.get("pid"))
                    recorded_start = existing.get("process_started_at")
                    if live and recorded_start is not None:
                        actual_start = process_started_at(existing.get("pid"))
                        if actual_start is not None:
                            live = recorded_start == actual_start
                    if not same_host or live:
                        raise BulkGenerationError(
                            "Another generation process is active with PID "
                            f"{existing.get('pid')}"
                        )
                    archive_interrupted_artifact(
                        self.output_directory,
                        self.path,
                        expected_payload=lease_payload,
                    )
                lease = {
                    "schema": LEASE_SCHEMA,
                    "schema_version": LEASE_VERSION,
                    "queue_sha256": self.queue_sha256,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "process_started_at": process_started_at(os.getpid()),
                    "lease_id": self.lease_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                self.document = lease
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                try:
                    descriptor = os.open(self.path, flags, 0o600)
                except FileExistsError as error:
                    raise BulkGenerationError(
                        "Another generation process acquired the output"
                    ) from error
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(lease, stream, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        except AdvisoryLockBusyError as error:
            raise BulkGenerationError(
                "Another generation process acquired the output"
            ) from error
        return self

    def __exit__(self, error_type, _error, _traceback):
        ownership_error = None
        try:
            with exclusive_advisory_lock(
                self.path.with_suffix(".guard"), blocking=True
            ):
                current = _load_json(self.path)
                if current == self.document:
                    self.path.unlink()
                else:
                    ownership_error = BulkGenerationError(
                        "Generation lease ownership changed during the run"
                    )
        except (BulkGenerationError, AdvisoryLockBusyError) as error:
            ownership_error = error
        if ownership_error is not None and error_type is None and not self.committed:
            raise BulkGenerationError(str(ownership_error)) from ownership_error

    def assert_owned(self):
        current = _load_json(self.path)
        if current != self.document:
            raise BulkGenerationError(
                "Generation lease ownership changed during the run"
            )

    def mark_committed(self):
        """Do not report cleanup ambiguity as failure after an external commit."""
        self.committed = True


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BulkGenerationError(
            f"Unable to read generation lease {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise BulkGenerationError("Generation lease must be a JSON object")
    return value


__all__ = [
    "LEASE_SCHEMA",
    "LEASE_VERSION",
    "BulkGenerationError",
    "GenerationLease",
    "archive_interrupted_artifact",
    "inspect_process_status",
    "process_is_alive",
    "process_started_at",
]
