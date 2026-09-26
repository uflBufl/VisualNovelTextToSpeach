"""Conservative cleanup for extractor-owned voice-candidate directories."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable, Iterator
from pathlib import Path

_JOB_ID = re.compile(r"[0-9a-f]{24}")
_PACK_ID = re.compile(r"pack-[0-9a-f]{24}")
_MAX_JOBS = 512
_MAX_PACKS_PER_JOB = 64
_MAX_REFERENCE_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_CANDIDATE_TREE_ENTRIES = 4 * 1024
_MAX_CANDIDATES = 64
_MAX_DELETIONS = 8


def prune_obsolete_voice_candidate_caches(
    candidate_root: str | Path,
    job_root: str | Path,
    *,
    protected_paths: Iterable[str | Path] = (),
) -> tuple[Path, ...]:
    """Delete at most eight unreferenced extractor cache directories.

    A read error, malformed reference, symlink, or unexpected cache layout makes
    this a no-op.  ``protected_paths`` covers manifests still held in memory.
    """
    root = Path(candidate_root).expanduser()
    jobs = Path(job_root).expanduser()
    if _unsafe(root) or not root.is_dir() or _unsafe(jobs):
        return ()
    try:
        root = root.resolve(strict=True)
        candidates = _candidate_directories(root)
        if candidates is None:
            return ()
        if len(candidates) > _MAX_CANDIDATES or not all(
            _safe_candidate_tree(directory) for directory in candidates
        ):
            return ()
        referenced = _protected_candidates(root, protected_paths)
        if referenced is None:
            return ()
        references = _references_in_jobs(root, jobs)
        if references is None:
            return ()
        referenced.update(references)
    except OSError, ValueError:
        return ()

    removed: list[Path] = []
    for directory in candidates:
        if directory.name in referenced or len(removed) == _MAX_DELETIONS:
            continue
        signature = _signature(directory)
        if signature is None:
            return tuple(removed)
        if _signature(directory) != signature:
            return tuple(removed)
        try:
            shutil.rmtree(directory)
        except OSError:
            return tuple(removed)
        removed.append(directory)
    return tuple(removed)


def _candidate_directories(root: Path) -> tuple[Path, ...] | None:
    candidates: list[Path] = []
    for path in root.iterdir():
        if _unsafe(path):
            return None
        # Narrator preview caches use a different identity and may be open in
        # another voice picker; only the story-candidate cache is reclaimable.
        if not path.is_dir() or not _JOB_ID.fullmatch(path.name):
            continue
        manifest = path / "manifest.json"
        if _unsafe(manifest) or not manifest.is_file():
            return None
        candidates.append(path)
    return tuple(
        sorted(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    )


def _protected_candidates(root: Path, paths: Iterable[str | Path]) -> set[str] | None:
    protected: set[str] = set()
    for value in paths:
        candidate = _candidate_for_path(root, Path(value).expanduser())
        if candidate is not None:
            protected.add(candidate)
    return protected


def _references_in_jobs(root: Path, jobs: Path) -> set[str] | None:
    if not jobs.exists():
        return set()
    if not jobs.is_dir():
        return None
    references: set[str] = set()
    directories = tuple(jobs.iterdir())
    if len(directories) > _MAX_JOBS:
        return None
    for directory in directories:
        if _unsafe(directory):
            return None
        if not directory.is_dir() or not _JOB_ID.fullmatch(directory.name):
            continue
        for path in (directory / "voice-plan.json", *_pack_manifests(directory)):
            if path is None:
                return None
            if not path.exists():
                continue
            found = _references_in_document(root, path)
            if found is None:
                return None
            references.update(found)
    return references


def _pack_manifests(job_directory: Path) -> Iterator[Path | None]:
    packs = job_directory / "game-packs"
    if not packs.exists():
        return
    if _unsafe(packs) or not packs.is_dir():
        yield None
        return
    directories = tuple(packs.iterdir())
    if len(directories) > _MAX_PACKS_PER_JOB:
        yield None
        return
    for directory in directories:
        if (
            _unsafe(directory)
            or not directory.is_dir()
            or not _PACK_ID.fullmatch(directory.name)
        ):
            yield None
            return
        yield directory / "game-pack.json"


def _references_in_document(root: Path, path: Path) -> set[str] | None:
    if _unsafe(path) or not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_REFERENCE_DOCUMENT_BYTES:
            return None
        payload = path.read_text(encoding="utf-8")
        document = (
            [json.loads(line) for line in payload.splitlines() if line.strip()]
            if path.suffix == ".jsonl"
            else json.loads(payload)
        )
    except OSError, UnicodeError, json.JSONDecodeError:
        return None
    references: set[str] = set()
    for value in _strings(document):
        candidate = _candidate_for_path(
            root, Path(value).expanduser(), base=path.parent
        )
        if candidate is not None:
            references.add(candidate)
    return references


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _candidate_for_path(
    root: Path, path: Path, *, base: Path | None = None
) -> str | None:
    if not path.is_absolute() and base is not None:
        path = base / path
    try:
        relative = path.resolve(strict=False).relative_to(root)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def _safe_candidate_tree(directory: Path) -> bool:
    entries = 0
    for base, directories, files in os.walk(directory, followlinks=False):
        for name in (*directories, *files):
            entries += 1
            if entries > _MAX_CANDIDATE_TREE_ENTRIES or _unsafe(Path(base) / name):
                return False
    return True


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_dev, status.st_ino, status.st_mtime_ns


def _unsafe(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


__all__ = ["prune_obsolete_voice_candidate_caches"]
