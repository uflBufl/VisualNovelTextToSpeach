"""Shared atomic publication primitives for authoritative authoring outputs."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

from vntts.authoring.generation_lease import GenerationLease


class AtomicPublicationError(RuntimeError):
    """Raised when the platform cannot provide no-replace publication."""


@contextmanager
def generation_publication_leases(sources, *, process_checker):
    """Hold generation leases for a stable, deadlock-free source set."""
    normalized = sorted(
        {(Path(output).resolve(), queue_sha256) for output, queue_sha256 in sources},
        key=lambda item: str(item[0]),
    )
    with ExitStack() as stack:
        leases = tuple(
            stack.enter_context(
                GenerationLease(
                    output,
                    queue_sha256,
                    process_checker=process_checker,
                )
            )
            for output, queue_sha256 in normalized
        )
        yield leases


def rename_directory_no_replace(source, destination):
    """Atomically rename a staged directory without replacing a destination."""
    source = Path(source)
    destination = Path(destination)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise AtomicPublicationError(
                "Atomic no-replace directory publication is unavailable on this Linux runtime"
            )
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, source_bytes, -100, destination_bytes, 1)
    elif os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError as error:
            raise AtomicPublicationError(
                f"Publication destination already exists: {destination}"
            ) from error
        return
    else:
        raise AtomicPublicationError(
            f"Atomic no-replace directory publication is unsupported on {sys.platform}"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise AtomicPublicationError(
            f"Publication destination already exists: {destination}"
        )
    if error_number in {errno.ENOSYS, errno.ENOTSUP}:
        raise AtomicPublicationError(
            "Atomic no-replace directory publication is unavailable on this filesystem"
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


__all__ = [
    "AtomicPublicationError",
    "generation_publication_leases",
    "rename_directory_no_replace",
]
