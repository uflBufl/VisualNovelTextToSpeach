"""Process lifetime fingerprints independent of wall-clock corrections."""

import sys

import psutil


def process_fingerprint(process: psutil.Process) -> dict[str, str]:
    created = process.create_time()
    if not process.is_running():
        raise psutil.NoSuchProcess(process.pid)
    if sys.platform == "win32":
        # Windows creation FILETIME is stable even when the fast identity query is denied.
        return {"scheme": "windows-create-time-v1", "token": float(created).hex()}
    # Process hashes use monotonic identity; public create_time may adjust with the clock.
    scheme = (
        f"psutil-process-hash-v1/{sys.platform}/{sys.implementation.name}/"
        f"{sys.version_info.major}.{sys.version_info.minor}/"
        f"{sys.hash_info.width}/{psutil.__version__}"
    )
    return {"scheme": scheme, "token": str(hash(process))}


def validate_process_fingerprint(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str) or not value[key]
        for key in ("scheme", "token")
    ):
        raise ValueError("Diagnostic process fingerprint is invalid")
    return {"scheme": value["scheme"], "token": value["token"]}
