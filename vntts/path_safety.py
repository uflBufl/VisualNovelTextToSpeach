"""Shared filesystem containment checks."""

from pathlib import Path, PurePath, PurePosixPath


def safe_relative_path(value, label, *, error_type=ValueError):
    """Validate one canonical POSIX-relative path without touching the filesystem."""
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise error_type(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise error_type(f"{label} must stay inside its workspace")
    return Path(*pure.parts)


def contained_path(root, relative, label, *, error_type=ValueError):
    """Resolve a relative path and require it to stay inside its canonical root."""
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise error_type(f"{label} leaves its owning directory") from error
    return path


def contained_regular_file(root, relative, label, *, error_type=ValueError):
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


__all__ = ["contained_path", "contained_regular_file", "safe_relative_path"]
