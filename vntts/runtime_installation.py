"""Provision only qualified source runtimes; portable builds use bundled ones."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from time import monotonic

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.runtime_paths import (
    RUNTIME_ENVIRONMENT_VARIABLES,
    get_bundle_root,
    managed_runtime_location,
    source_runtime_project,
)
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.subprocess_utils import terminate_process


def _check_cancelled(cancellation):
    if cancellation is not None:
        cancelled = (
            cancellation.is_set() if hasattr(cancellation, "is_set") else cancellation()
        )
        if cancelled:
            raise TTSSynthesisError(
                "Speech runtime preparation cancelled. Retry when ready."
            )


def runtime_installation_available(backend):
    """Never upgrade explicit/source environments or install experimental stacks."""
    variable = RUNTIME_ENVIRONMENT_VARIABLES.get(backend)
    if variable is None or os.environ.get(variable) or get_bundle_root() is not None:
        return False
    machine = platform.machine().casefold()
    pocket_host = (
        sys.platform == "darwin"
        and machine in {"arm64", "aarch64"}
        or sys.platform in {"win32", "linux"}
        and machine in {"amd64", "x86_64"}
    )
    qualified = (
        backend == "pocket-tts"
        and pocket_host
        or (
            backend == "moss-tts"
            and sys.platform == "darwin"
            and machine in {"arm64", "aarch64"}
        )
    )
    project = source_runtime_project(backend)
    return bool(
        qualified
        and project is not None
        and not (project / ".venv").exists()
        and shutil.which("uv")
    )


def _run(command, *, cancellation, environment=None, input_bytes=None, timeout=1800):
    """Drain child output while keeping cancellation and shutdown bounded."""
    _check_cancelled(cancellation)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        **(
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32"
            else {}
        ),
    )
    deadline = monotonic() + timeout
    try:
        while True:
            _check_cancelled(cancellation)
            if monotonic() >= deadline:
                raise TTSConfigurationError(
                    "Speech runtime preparation timed out. Retry when ready."
                )
            try:
                output, error = process.communicate(input_bytes, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                input_bytes = None
        if process.returncode:
            details = error.decode("utf-8", errors="replace")[-4000:].strip()
            raise TTSConfigurationError(
                f"Speech runtime preparation failed. Retry when ready. {details}"
            )
        _check_cancelled(cancellation)
        return output
    finally:
        if process.poll() is None:
            terminate_process(process)


def _nvidia_driver_status(cancellation):
    """Driver discovery is informational, never authority to install CUDA."""
    if sys.platform not in {"win32", "linux"}:
        return "not-applicable"
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return "unknown"
    try:
        output = _run(
            [executable, "--query-gpu=driver_version", "--format=csv,noheader"],
            cancellation=cancellation,
            timeout=5,
        )
    except OSError, TTSConfigurationError:
        return "unknown"
    return "detected" if output.strip() else "not-detected"


def ensure_speech_runtime(
    backend, *, runtime_directory=None, cancellation=None, progress=None
):
    """Return a usable runtime, installing only when no user runtime is present."""
    from vntts.speech_worker import probe_speech_runtime, resolve_speech_runtime_paths

    progress = progress or (lambda _message: None)
    _check_cancelled(cancellation)
    try:
        return resolve_speech_runtime_paths(backend, runtime_directory)
    except TTSConfigurationError:
        if runtime_directory or not runtime_installation_available(backend):
            raise
    location = managed_runtime_location(backend)
    project = source_runtime_project(backend)
    try:
        with exclusive_advisory_lock(location / "installation.lock"):
            # Another installer may have finished between resolution and the lock.
            try:
                return resolve_speech_runtime_paths(backend)
            except TTSConfigurationError:
                pass
            _check_cancelled(cancellation)
            if (location / "verified.json").exists():
                raise TTSConfigurationError(
                    "A previously verified speech runtime is damaged. "
                    "Refusing to modify an environment that another process may be using."
                )
            progress("Checking hardware before preparing the speech runtime...")
            hardware = {
                "platform": sys.platform,
                "machine": platform.machine(),
                "nvidia_driver": _nvidia_driver_status(cancellation),
            }
            device = "CPU" if backend == "pocket-tts" else "Apple Silicon"
            label = "Pocket TTS" if backend == "pocket-tts" else "MOSS-TTS"
            progress(
                f"Preparing {label} runtime for {device}. Downloading locked dependencies; this may take several minutes..."
                + (
                    " NVIDIA detected; Pocket TTS currently uses the CPU runtime."
                    if hardware["nvidia_driver"] == "detected"
                    else ""
                )
            )
            environment = dict(os.environ)
            environment.pop("VIRTUAL_ENV", None)
            environment["UV_PROJECT_ENVIRONMENT"] = str(location / "environment")
            _run(
                [
                    shutil.which("uv"),
                    "sync",
                    "--project",
                    str(project),
                    "--locked",
                    "--python",
                    "3.14",
                    "--no-dev",
                    "--no-install-project",
                ],
                cancellation=cancellation,
                environment=environment,
            )
            progress(f"Checking {label} dependencies in the isolated worker...")
            paths = resolve_speech_runtime_paths(backend, location / "environment")
            health = probe_speech_runtime(backend, paths, cancellation=cancellation)
            _check_cancelled(cancellation)
            if managed_runtime_location(backend) != location:
                raise TTSConfigurationError(
                    "Runtime recipe changed during installation. Retry to use the new version."
                )
            atomic_write_json(
                location / "verified.json",
                {
                    "schema": "vntts.speech-runtime-installation-v1",
                    "backend": backend,
                    "recipe": location.name,
                    "health": health,
                    "hardware": hardware,
                },
            )
            progress(f"{label} runtime dependencies are verified.")
            return paths
    except AdvisoryLockBusyError as error:
        raise TTSConfigurationError(
            "Another window or VNTTS process is preparing this speech runtime. "
            "Wait for it to finish, then retry."
        ) from error
