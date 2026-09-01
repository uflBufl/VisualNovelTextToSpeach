"""Small shared helpers for captured subprocesses."""

import subprocess


def terminate_process(process, *, timeout=5):
    process.terminate()
    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def last_output_line(value):
    if not isinstance(value, str):
        return None
    return next(
        (line.strip() for line in reversed(value.splitlines()) if line.strip()), None
    )


__all__ = ["last_output_line", "terminate_process"]
