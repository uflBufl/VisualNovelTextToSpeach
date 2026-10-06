"""Cancellation and captured commands shared by speech runtime preparation."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from time import monotonic
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

from vntts.cleanup import attempt_cleanup
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.subprocess_utils import terminate_process

if TYPE_CHECKING:
    from vntts.runtime_ownership import RuntimeUse

ProgressCallback: TypeAlias = Callable[[str], None]


@runtime_checkable
class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


Cancellation: TypeAlias = CancellationToken | Callable[[], bool] | None


def check_runtime_cancelled(cancellation: Cancellation) -> None:
    if cancellation is not None:
        cancelled = (
            cancellation.is_set()
            if isinstance(cancellation, CancellationToken)
            else cancellation()
        )
        if cancelled:
            raise TTSSynthesisError(
                "Speech runtime preparation cancelled. Retry when ready."
            )


def run_runtime_command(
    command: Sequence[str],
    *,
    cancellation: Cancellation,
    environment: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: float = 1800,
    runtime_use: RuntimeUse | None = None,
    include_stderr: bool = False,
) -> bytes:
    """Drain child output while keeping cancellation and shutdown bounded."""
    check_runtime_cancelled(cancellation)
    if runtime_use is not None:
        runtime_use.begin_launch()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if sys.platform == "win32"
                else 0
            ),
        )
    except Exception as launch_error:
        if runtime_use is not None:
            attempt_cleanup(
                lambda: runtime_use.launched(None),
                description="Speech runtime launch reset",
                primary_error=launch_error,
            )
        raise
    deadline = monotonic() + timeout
    try:
        if runtime_use is not None:
            runtime_use.launched(process)
        while True:
            check_runtime_cancelled(cancellation)
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
        check_runtime_cancelled(cancellation)
        return output + error if include_stderr else output
    finally:
        if process.poll() is None:
            terminate_process(process)
