"""Conservative cleanup for extractor-owned voice-candidate directories."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Lock, RLock

from vntts.authoring.advisory_lock import AdvisoryLockBusyError, exclusive_advisory_lock

_JOB_ID = re.compile(r"[0-9a-f]{24}")
_PACK_ID = re.compile(r"pack-[0-9a-f]{24}")
_MAX_JOBS = 512
_MAX_PACKS_PER_JOB = 64
_MAX_REFERENCE_DOCUMENT_BYTES = 4 * 1024 * 1024
_MAX_CANDIDATE_TREE_ENTRIES = 4 * 1024
_MAX_DELETIONS = 8
_ACTIVE_CLAIM = re.compile(r"\.active-[0-9]+\.guard")
_GC_GUARD = ".voice-candidate-gc.guard"

# Kept module-visible so tests can release process-lifetime claims before a
# temporary directory is removed.
_candidate_claims = ExitStack()
_claimed_paths: set[Path] = set()
_claims_lock = RLock()
_root_guard_lock = Lock()


class _VoiceCandidateCacheGuard:
    def __init__(self, root: Path) -> None:
        self.root = root

    def claim(self, manifest_path: str | Path) -> None:
        _claim_candidate(self.root, manifest_path)


@contextmanager
def voice_candidate_cache_guard(
    candidate_root: str | Path, *, blocking: bool = True
) -> Iterator[_VoiceCandidateCacheGuard]:
    """Serialize candidate extraction/claiming with cache collection."""
    root = Path(candidate_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if _unsafe(root):
        raise ValueError("Voice candidate root must not be a symlink")
    root = root.resolve(strict=True)
    with _thread_root_lock(root, blocking=blocking):
        with exclusive_advisory_lock(root / _GC_GUARD, blocking=blocking):
            yield _VoiceCandidateCacheGuard(root)


def claim_voice_candidate_cache(
    candidate_root: str | Path, manifest_path: str | Path
) -> None:
    """Keep an extracted candidate cache alive until this process exits."""
    with voice_candidate_cache_guard(candidate_root) as guard:
        guard.claim(manifest_path)


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
        with voice_candidate_cache_guard(root, blocking=False) as guard:
            root = guard.root
            candidates = _candidate_directories(root)
            if not candidates:
                return ()
            if not all(_safe_candidate_tree(directory) for directory in candidates):
                return ()
            referenced = _candidate_references(root, jobs, candidates, protected_paths)
            if referenced is None:
                return ()

            removed: list[Path] = []
            for directory in candidates:
                if directory.name in referenced or len(removed) == _MAX_DELETIONS:
                    continue
                if not _remove_candidate(directory):
                    return tuple(removed)
                removed.append(directory)
            return tuple(removed)
    except AdvisoryLockBusyError, OSError, ValueError:
        return ()


def _candidate_references(
    root: Path,
    jobs: Path,
    candidates: tuple[Path, ...],
    protected_paths: Iterable[str | Path],
) -> set[str] | None:
    referenced = _protected_candidates(root, protected_paths)
    if referenced is None:
        return None
    active = _active_candidate_claims(candidates)
    if active is None:
        return None
    references = _references_in_jobs(root, jobs)
    if references is None:
        return None
    return referenced | active | references


def _remove_candidate(directory: Path) -> bool:
    signature = _signature(directory)
    if signature is None or _signature(directory) != signature:
        return False
    try:
        shutil.rmtree(directory)
    except OSError:
        return False
    return True


def _claim_candidate(root: Path, manifest_path: str | Path) -> None:
    manifest = Path(manifest_path).expanduser()
    if _unsafe(manifest) or _unsafe(manifest.parent):
        raise ValueError("Voice candidate manifest must not be a symlink")
    manifest = manifest.resolve(strict=True)
    try:
        relative = manifest.relative_to(root)
    except ValueError as error:
        raise ValueError(
            "Voice candidate manifest is outside its cache root"
        ) from error
    if (
        len(relative.parts) != 2
        or relative.name != "manifest.json"
        or _unsafe(manifest)
        or _unsafe(manifest.parent)
        or not manifest.is_file()
    ):
        raise ValueError("Voice candidate manifest has an unexpected layout")
    claim = manifest.parent / f".active-{os.getpid()}.guard"
    if _unsafe(claim):
        raise ValueError("Voice candidate claim must not be a symlink")
    with _claims_lock:
        if claim in _claimed_paths:
            return
        _candidate_claims.enter_context(exclusive_advisory_lock(claim, blocking=True))
        _claimed_paths.add(claim)
        _candidate_claims.callback(_claimed_paths.discard, claim)


def _active_candidate_claims(candidates: Iterable[Path]) -> set[str] | None:
    active: set[str] = set()
    for directory in candidates:
        try:
            claims = tuple(
                path
                for path in directory.iterdir()
                if _ACTIVE_CLAIM.fullmatch(path.name)
            )
        except OSError:
            return None
        for claim in claims:
            if _unsafe(claim) or not claim.is_file():
                return None
            try:
                with exclusive_advisory_lock(claim, blocking=False):
                    pass
            except AdvisoryLockBusyError:
                active.add(directory.name)
            except OSError:
                return None
    return active


@contextmanager
def _thread_root_lock(root: Path, *, blocking: bool) -> Iterator[None]:
    if not _root_guard_lock.acquire(blocking=blocking):
        raise AdvisoryLockBusyError(str(root))
    try:
        yield
    finally:
        _root_guard_lock.release()


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
        path = Path(value).expanduser()
        for base in (root, None):
            candidate = _candidate_for_path(root, path, base=base)
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
        found = _references_in_job(root, directory)
        if found is None:
            return None
        references.update(found)
    return references


def _references_in_job(root: Path, directory: Path) -> set[str] | None:
    references: set[str] = set()
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


atexit.register(_candidate_claims.close)


__all__ = [
    "claim_voice_candidate_cache",
    "prune_obsolete_voice_candidate_caches",
    "voice_candidate_cache_guard",
]
