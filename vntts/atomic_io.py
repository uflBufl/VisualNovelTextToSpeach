"""Durable, collision-safe publication helpers for application-owned files."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def atomic_output_path(path):
    """Yield a sibling temporary path and publish it only after success."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.stem}.",
        suffix=f".tmp{destination.suffix}",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        yield temporary
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def atomic_output_group(*paths):
    """Stage new sibling files and publish all, removing partial publication."""
    destinations = tuple(Path(path) for path in paths)
    if not destinations or len(set(destinations)) != len(destinations):
        raise ValueError("atomic output destinations must be unique and non-empty")
    if any(destination.exists() for destination in destinations):
        raise FileExistsError("atomic output group only supports new destinations")
    temporaries = []
    try:
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.stem}.",
                suffix=f".tmp{destination.suffix}",
            )
            os.close(descriptor)
            temporaries.append(Path(temporary_name))
        yield tuple(temporaries)
        published = []
        try:
            for temporary, destination in zip(temporaries, destinations, strict=True):
                os.replace(temporary, destination)
                published.append(destination)
        except Exception:
            for destination in published:
                destination.unlink(missing_ok=True)
            raise
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)


def atomic_write_bytes(path, content):
    """Publish bytes without exposing a partial destination file."""
    with atomic_output_path(path) as temporary:
        with temporary.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    return Path(path)


def atomic_write_text(path, content, *, encoding="utf-8"):
    """Publish text without exposing a partial destination file."""
    return atomic_write_bytes(path, content.encode(encoding))


def atomic_write_json(path, value, *, ensure_ascii=False, indent=2, sort_keys=False):
    """Publish consistently formatted JSON with a trailing newline."""
    content = json.dumps(
        value, ensure_ascii=ensure_ascii, indent=indent, sort_keys=sort_keys
    )
    return atomic_write_text(path, content + "\n")
