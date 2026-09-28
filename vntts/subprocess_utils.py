"""Small shared helpers for captured subprocesses."""

import subprocess


def terminate_process(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], *, timeout: float = 5
) -> None:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.communicate(timeout=timeout)
        return
    except subprocess.TimeoutExpired, OSError, ValueError:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired, OSError:
        # ponytail: kill is the strongest local action; leave OS cleanup
        # rather than hanging shutdown forever.
        pass


def last_output_line(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return next(
        (line.strip() for line in reversed(value.splitlines()) if line.strip()), None
    )


__all__ = ["last_output_line", "terminate_process"]
