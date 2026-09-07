"""Provision only qualified source runtimes; portable builds use bundled ones."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from time import monotonic
from uuid import uuid4

from vntts_artifacts.atomic_io import atomic_write_json

from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.runtime_ownership import (
    OWNER_SCHEMA,
    RuntimeUse,
    cleanup_managed_runtimes,
    owned_generation,
    remove_inactive_generation,
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


def _run(
    command,
    *,
    cancellation,
    environment=None,
    input_bytes=None,
    timeout=1800,
    runtime_use=None,
    include_stderr=False,
):
    """Drain child output while keeping cancellation and shutdown bounded."""
    _check_cancelled(cancellation)
    if runtime_use is not None:
        runtime_use.begin_launch()
    try:
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
    except Exception:
        if runtime_use is not None:
            runtime_use.launched(None)
        raise
    deadline = monotonic() + timeout
    try:
        if runtime_use is not None:
            runtime_use.launched(process)
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
        return output + error if include_stderr else output
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
    try:
        paths = _ensure_speech_runtime(
            backend,
            runtime_directory=runtime_directory,
            cancellation=cancellation,
            progress=progress,
        )
    except AdvisoryLockBusyError as error:
        raise TTSConfigurationError(str(error)) from error
    try:
        cleanup_managed_runtimes(backend, paths[0], progress=progress)
    except (OSError, AdvisoryLockBusyError) as error:
        if progress is not None:
            progress(
                f"Speech runtime is ready; unused-copy cleanup was deferred: {error}"
            )
    return paths


def _ensure_speech_runtime(
    backend, *, runtime_directory=None, cancellation=None, progress=None
):
    """Return a usable runtime, installing only when no user runtime is present."""
    from vntts.speech_worker import probe_speech_runtime, resolve_speech_runtime_paths

    progress = progress or (lambda _message: None)
    _check_cancelled(cancellation)
    try:
        paths = resolve_speech_runtime_paths(backend, runtime_directory)
        if owned_generation(backend, paths[0]) is not None:
            progress(f"Checking installed {backend} runtime dependencies...")
            probe_speech_runtime(backend, paths, cancellation=cancellation)
        return paths
    except TTSConfigurationError, OSError, EOFError:
        if runtime_directory or not runtime_installation_available(backend):
            raise
    location = managed_runtime_location(backend)
    project = source_runtime_project(backend)
    try:
        with exclusive_advisory_lock(location / "installation.lock"):
            _check_cancelled(cancellation)
            progress("Checking hardware before preparing the speech runtime...")
            hardware = {
                "platform": sys.platform,
                "machine": platform.machine(),
                "nvidia_driver": _nvidia_driver_status(cancellation),
            }
            device = "CPU" if backend == "pocket-tts" else "Apple Silicon"
            label = "Pocket TTS" if backend == "pocket-tts" else "MOSS-TTS"
            action = (
                "Repairing" if (location / "verified.json").exists() else "Preparing"
            )
            progress(
                f"{action} {label} runtime for {device} in a separate copy. Downloading locked dependencies; this may take several minutes..."
                + (
                    " NVIDIA detected; Pocket TTS currently uses the CPU runtime."
                    if hardware["nvidia_driver"] == "detected"
                    else ""
                )
            )
            environment = dict(os.environ)
            environment.pop("VIRTUAL_ENV", None)
            generation = location / "generations" / uuid4().hex
            generation.mkdir(parents=True)
            atomic_write_json(
                generation / "owner.json",
                {
                    "schema": OWNER_SCHEMA,
                    "backend": backend,
                    "recipe": location.name,
                    "generation": generation.name,
                },
            )
            use = RuntimeUse(generation)
            published = False
            environment["UV_PROJECT_ENVIRONMENT"] = str(generation / "environment")
            try:
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
                    runtime_use=use,
                )
                progress(f"Checking {label} dependencies in the isolated worker...")
                paths = resolve_speech_runtime_paths(
                    backend, generation / "environment"
                )
                health = probe_speech_runtime(
                    backend, paths, cancellation=cancellation, runtime_use=use
                )
                _check_cancelled(cancellation)
                if managed_runtime_location(backend) != location:
                    raise TTSConfigurationError(
                        "Runtime recipe changed during installation. Retry to use the new version."
                    )
                atomic_write_json(
                    location / "verified.json",
                    {
                        "schema": "vntts.speech-runtime-installation-v2",
                        "backend": backend,
                        "recipe": location.name,
                        "generation": generation.name,
                        "health": health,
                        "hardware": hardware,
                    },
                )
                published = True
            finally:
                use.close()
                if not published:
                    try:
                        remove_inactive_generation(backend, generation)
                    except OSError as error:
                        progress(f"Incomplete runtime cleanup was deferred: {error}")
            progress(f"{label} runtime dependencies are verified.")
            return paths
    except AdvisoryLockBusyError as error:
        raise TTSConfigurationError(
            "Another window or VNTTS process is preparing this speech runtime. "
            "Wait for it to finish, then retry."
        ) from error
