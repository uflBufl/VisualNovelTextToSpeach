"""Shared filesystem containment and regular-file admission."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import BinaryIO


def open_regular_candidate(path: str | os.PathLike[str], flags: int) -> int:
    # Reject FIFOs with fstat without waiting for a writer, even after a path swap.
    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0))


@contextmanager
def open_regular_binary(path: str | os.PathLike[str]) -> Iterator[BinaryIO]:
    """Own a binary stream only after its opened descriptor is a regular file."""
    with open(path, "rb", opener=open_regular_candidate) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        yield source


def no_replace_destination(value: str | Path) -> Path:
    """Canonicalize the parent while preserving the leaf for atomic no-replace."""
    requested = Path(value).expanduser()
    return requested.parent.resolve() / requested.name


def safe_relative_path(
    value: object,
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> Path:
    """Validate one canonical POSIX-relative path without touching the filesystem."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\\" in value
        or "\x00" in value
    ):
        raise error_type(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or PureWindowsPath(value).drive
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise error_type(f"{label} must stay inside its workspace")
    return Path(*pure.parts)


def contained_path(
    root: str | Path,
    relative: str | PurePath,
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> Path:
    """Resolve a relative path and require it to stay inside its canonical root."""
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise error_type(f"{label} leaves its owning directory") from error
    return path


def contained_regular_file(
    root: str | Path,
    relative: object,
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> Path:
    """Resolve one canonical relative path without accepting symlink components."""
    root = Path(root).resolve()
    if isinstance(relative, PurePath):
        relative = relative.as_posix()
    try:
        relative = safe_relative_path(relative, label, error_type=error_type)
    except error_type as error:
        raise error_type(f"{label.capitalize()} leaves its root") from error
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise error_type(
                f"{label.capitalize()} is unsafe: symlinks are not allowed"
            )
    path = contained_path(root, relative, label, error_type=error_type)
    if not path.is_file():
        raise error_type(f"{label.capitalize()} is missing or unsafe")
    return path


__all__ = [
    "contained_path",
    "contained_regular_file",
    "no_replace_destination",
    "open_regular_candidate",
    "open_regular_binary",
    "safe_relative_path",
]
