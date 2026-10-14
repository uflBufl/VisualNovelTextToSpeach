"""Shared filesystem containment and regular-file admission."""

import os
import shutil
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from io import BufferedReader, TextIOWrapper, text_encoding
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath


def open_regular_candidate(path: str | os.PathLike[str], flags: int) -> int:
    # Reject FIFOs with fstat without waiting for a writer, even after a path swap.
    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0))


@contextmanager
def open_regular_binary(path: str | os.PathLike[str]) -> Iterator[BufferedReader]:
    """Own a binary stream only after its opened descriptor is a regular file."""
    with open(path, "rb", opener=open_regular_candidate) as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        yield source


def read_regular_bytes(path: str | os.PathLike[str]) -> bytes:
    """Capture a regular file once, preserving raw filesystem errors."""
    with open_regular_binary(path) as source:
        return source.read()


@contextmanager
def open_regular_text(
    path: str | os.PathLike[str], encoding: str | None = None
) -> Iterator[TextIOWrapper]:
    """Own strict decoded text with the platform's universal-newline semantics."""
    encoding = text_encoding(encoding)
    with (
        open_regular_binary(path) as source,
        TextIOWrapper(source, encoding=encoding) as text,
    ):
        yield text


def read_regular_text(path: str | os.PathLike[str], encoding: str | None = None) -> str:
    """Read complete admitted text, preserving filesystem and decoding errors."""
    with open_regular_text(path, encoding) as source:
        return source.read()


def copy_regular_file(
    source: str | Path,
    destination: str | Path,
    *,
    preserve_metadata: bool = False,
) -> None:
    """Copy an admitted regular source into a caller-owned destination."""
    source, destination = Path(source), Path(destination)
    if destination.exists() and source.samefile(destination):
        raise shutil.SameFileError(f"{source!r} and {destination!r} are the same file")
    with (
        open_regular_binary(source) as original,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(original, output)
    if preserve_metadata:
        shutil.copystat(source, destination)


def ensure_separate_output(
    output: str | Path,
    inputs: Iterable[str | Path],
    *,
    error_type: type[Exception] = ValueError,
) -> None:
    """Refuse a report destination that aliases an existing input file."""
    output = Path(output)
    if output.exists() and any(
        Path(source).exists() and output.samefile(source) for source in inputs
    ):
        raise error_type(f"Output must differ from input files: {output}")


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


def contained_no_replace_destination(
    root: str | Path,
    relative: str | PurePath,
    label: str,
    *,
    error_type: type[Exception] = ValueError,
) -> Path:
    """Admit an immutable destination without following its final symlink."""
    if isinstance(relative, PurePath):
        relative = relative.as_posix()
    relative = safe_relative_path(relative, label, error_type=error_type)
    root = Path(root).resolve()
    path = no_replace_destination(root / relative)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise error_type(f"{label} leaves its owning directory") from error
    if path.is_symlink():
        raise error_type(f"{label} must not be a symbolic link")
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
    "contained_no_replace_destination",
    "contained_path",
    "contained_regular_file",
    "copy_regular_file",
    "ensure_separate_output",
    "no_replace_destination",
    "open_regular_candidate",
    "open_regular_binary",
    "open_regular_text",
    "read_regular_bytes",
    "read_regular_text",
    "safe_relative_path",
]
