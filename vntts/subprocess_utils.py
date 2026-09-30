"""Small shared helpers for captured subprocesses."""

import subprocess
from contextlib import suppress


def terminate_process(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], *, timeout: float = 5
) -> None:
    with suppress(OSError):
        process.terminate()
    try:
        process.communicate(timeout=timeout)
        return
    except subprocess.TimeoutExpired, OSError, ValueError:
        pass
    with suppress(OSError):
        process.kill()
    # Kill is the strongest local action; leave OS cleanup rather than hanging.
    with suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=timeout)


def last_output_line(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return next(
        (line.strip() for line in reversed(value.splitlines()) if line.strip()), None
    )


__all__ = ["last_output_line", "terminate_process"]
