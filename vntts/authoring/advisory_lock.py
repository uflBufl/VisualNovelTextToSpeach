"""Cross-platform advisory guards for authoring lease transitions."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path


class AdvisoryLockBusyError(RuntimeError):
    """Another process owns an authoring transition guard."""


@contextmanager
def exclusive_advisory_lock(path, *, blocking=False):
    """Hold one persistent file guard without deleting its shared inode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size < 1:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(descriptor, mode, 1)
            except OSError as error:
                raise AdvisoryLockBusyError(str(path)) from error
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(descriptor, flags)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise AdvisoryLockBusyError(str(path)) from error
                raise
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = ["AdvisoryLockBusyError", "exclusive_advisory_lock"]
