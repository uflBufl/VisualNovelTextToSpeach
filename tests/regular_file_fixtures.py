"""Native regular-file acquisition and recovery controls shared by test cases."""

import builtins
import io
import os
import stat
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, TypedDict, TypeGuard
from unittest.mock import patch


class RegularFileGate(TypedDict):
    path: Path | None
    armed: bool


@contextmanager
def swap_at_acquisition(
    test_case: unittest.TestCase, gate: RegularFileGate
) -> Iterator[None]:
    native_open = os.open
    native_builtin_open, native_io_open = builtins.open, io.open
    descriptors: list[int] = []
    saved: bytes | None = None
    saved_mode: int | None = None

    def selected(path: object) -> TypeGuard[str | Path]:
        return (
            gate["armed"]
            and isinstance(path, (str, Path))
            and (Path(path) == gate["path"])
        )

    def guarded_open(
        owner: Callable[..., IO[bytes] | IO[str]],
        path: object,
        *args: object,
        **kwargs: object,
    ) -> IO[bytes] | IO[str]:
        if selected(path):
            test_case.assertIsNotNone(kwargs.get("opener"), "blocking source open")
        return owner(path, *args, **kwargs)

    def swap_and_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal saved, saved_mode
        if selected(path):
            test_case.assertTrue(flags & os.O_NONBLOCK)
            test_case.assertIsNone(saved, "source must stop after refused acquisition")
            # Save outside the open guard before swapping the admitted candidate.
            gate["armed"] = False
            saved_mode = stat.S_IMODE(Path(path).stat().st_mode)
            saved = Path(path).read_bytes()
            gate["armed"] = True
            Path(path).unlink()
            os.mkfifo(path)
        descriptor = native_open(path, flags, mode, dir_fd=dir_fd)
        if selected(path):
            descriptors.append(descriptor)
        return descriptor

    try:
        with (
            patch("vntts.path_safety.os.open", side_effect=swap_and_open),
            patch(
                "builtins.open",
                side_effect=lambda *a, **k: guarded_open(native_builtin_open, *a, **k),
            ),
            patch(
                "io.open",
                side_effect=lambda *a, **k: guarded_open(native_io_open, *a, **k),
            ),
        ):
            yield
        test_case.assertEqual(len(descriptors), 1)
        with test_case.assertRaises(OSError):
            os.fstat(descriptors[0])
    finally:
        gate["armed"] = False
        if saved is not None:
            target = gate["path"]
            assert target is not None and saved_mode is not None
            target.unlink()
            target.write_bytes(saved)
            target.chmod(saved_mode)
