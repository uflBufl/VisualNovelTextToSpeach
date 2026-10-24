"""Explicit, recoverable migration of identified synthesis caches.

Run ``python -m vntts.audio_cache_migration --manifest roots.json`` to inspect.
The manifest is a JSON list of objects with directory, backend, model,
sample_rate and sample_rate_evidence. Add ``--apply`` only after checking it.
NPY files contain no sample rate: evidence must identify the original model's
rate, not a provider default or a rate inferred from the waveform.
An individual key's optional completed_wav record (path, sha256) reuses a
checksum-verified final recording after an exact artifact PCM comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from hashlib import file_digest, sha256
from io import BytesIO
from math import prod
from pathlib import Path
from tempfile import NamedTemporaryFile
from tokenize import TokenError
from typing import BinaryIO, TypedDict

import numpy as np
import soundfile as sf
from numpy.lib import _format_impl as npy_format
from numpy.typing import NDArray

from vntts.audio_cache import (
    AudioArray,
    PersistentAudioCache,
    _prepared_audio,
)
from vntts.audio_store import (
    _CATALOG_SCHEMA,
    AudioCatalogError,
    AudioStorageUpgradeRequired,
    SharedAudioStore,
    _sync_directory,
    _sync_regular_file,
)
from vntts.path_safety import open_regular_binary, read_regular_text

# NumPy has no public NPY3 header reader; isolated to the one-time converter.
_read_npy_header = getattr(npy_format, "_read_array_header")


def _read_cached_array(source: BinaryIO) -> NDArray[np.generic]:
    version = np.lib.format.read_magic(source)
    shape, fortran_order, dtype = _read_npy_header(source, version)
    count = prod(shape)
    remaining = os.fstat(source.fileno()).st_size - source.tell()
    if (
        dtype.hasobject
        or any(dimension < 0 for dimension in shape)
        or count * dtype.itemsize > remaining
    ):
        raise ValueError("Invalid cached array body")
    loaded = np.fromfile(source, dtype=dtype, count=count)
    if loaded.size != count:
        raise ValueError("Incomplete cached array body")
    return loaded.reshape(shape, order="F" if fortran_order else "C")


def _same_legacy_file(original: os.stat_result, current: os.stat_result) -> bool:
    return all(
        getattr(original, field) == getattr(current, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _saved_wav(store: SharedAudioStore, key: str) -> Path | None:
    if not store.catalog_path.exists():
        if any((store.directory / name).exists() for name in ("objects", "bindings")):
            raise AudioStorageUpgradeRequired(
                "Existing WAV storage requires the explicit storage upgrade"
            )
        return None
    return store.get(key) if key in dict(store.iter_bindings()) else None


def _prepare_conversion_catalog(store: SharedAudioStore) -> None:
    if store.catalog_path.exists():
        list(store.iter_bindings())
        return
    _saved_wav(store, "catalog-preflight")
    keys = sorted(
        path.stem
        for path in store.directory.iterdir()
        if path.suffix == ".npy" and _KEY.fullmatch(path.stem) is not None
    )
    with NamedTemporaryFile(
        dir=store.directory,
        prefix=".audio-catalog.conversion-",
        suffix=".sqlite3",
        delete=False,
    ) as output:
        temporary = Path(output.name)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            for statement in _CATALOG_SCHEMA.values():
                connection.execute(statement)
            connection.execute("PRAGMA user_version=2")
            connection.executemany(
                "INSERT INTO unavailable_keys VALUES (?, ?)",
                (
                    (
                        key,
                        "Preserved NPY samples require model/sample-rate evidence before reuse",
                    )
                    for key in keys
                ),
            )
            connection.commit()
            store._validate_catalog(connection)
        _sync_regular_file(temporary)
        try:
            os.link(temporary, store.catalog_path)
        except FileExistsError:
            list(store.iter_bindings())
        _sync_directory(store.directory)
    finally:
        temporary.unlink(missing_ok=True)


def _put_migrated(
    cache: PersistentAudioCache, key: str, audio: AudioArray
) -> Path | None:
    existing = _saved_wav(cache.store, key)
    if existing is not None:
        with BytesIO() as encoded:
            sf.write(encoded, audio, cache.sample_rate, format="WAV", subtype="PCM_16")
            with open_regular_binary(existing) as saved:
                if (
                    file_digest(saved, "sha256").digest()
                    != sha256(encoded.getvalue()).digest()
                ):
                    return None
        return existing
    _prepare_conversion_catalog(cache.store)
    return cache.put(key, audio)


def _migrate_legacy(cache: PersistentAudioCache, key: str) -> AudioArray | None:
    path = cache.directory / f"{key}.npy"
    if path.is_symlink():
        return None
    try:
        with open_regular_binary(path) as source:
            original = os.fstat(source.fileno())
            if not _same_legacy_file(original, path.stat(follow_symlinks=False)):
                return None
            audio = _prepared_audio(_read_cached_array(source))
            if not all(
                _same_legacy_file(original, info)
                for info in (
                    os.fstat(source.fileno()),
                    path.stat(follow_symlinks=False),
                )
            ):
                return None
        if audio is None or _put_migrated(cache, key, audio) is None:
            return None
        restored = cache.get_existing(key)
        if restored is None:
            return None
        # Never remove a substituted legacy entry after publication.
        if _same_legacy_file(original, path.stat(follow_symlinks=False)):
            path.unlink()
        return restored
    except AudioCatalogError:
        raise
    except OSError, ValueError, TypeError, EOFError, OverflowError, TokenError:
        return None


_BACKENDS = {"chatterbox-nano", "pocket-tts", "moss-tts", "qwen-tts"}
_KEY = re.compile(r"[a-f0-9]{48}")


class MigrationReport(TypedDict):
    directory: str
    backend: str
    model: str
    sample_rate: int | None
    sample_rate_evidence: str
    apply: bool
    counts: dict[str, int]
    entries: list[dict[str, object]]


def _reuse_completed_wav(
    cache: PersistentAudioCache,
    key: str,
    audio: AudioArray,
    record: object,
    *,
    apply: bool,
) -> Path:
    if (
        not isinstance(record, dict)
        or set(record) != {"path", "sha256"}
        or not isinstance(record["path"], str)
        or not isinstance(record["sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", record["sha256"]) is None
    ):
        raise ValueError("completed_wav requires an absolute path and SHA256")
    candidate = Path(record["path"]).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ValueError("Completed WAV must be an absolute regular file")
    # Final generation averages channels before clipping/quantization.
    mono = audio if audio.ndim == 1 else audio.mean(axis=1, dtype=np.float32)
    with open_regular_binary(candidate) as source:
        original = os.fstat(source.fileno())
        if file_digest(source, "sha256").hexdigest() != record["sha256"]:
            raise ValueError("Completed WAV checksum does not match its evidence")
        source.seek(0)
        with sf.SoundFile(source) as waveform:
            if (
                waveform.format != "WAV"
                or waveform.subtype != "PCM_16"
                or waveform.channels != 1
                or waveform.samplerate != cache.sample_rate
            ):
                raise ValueError(
                    "Completed WAV format/rate does not match its evidence"
                )
            expected = np.round(np.clip(mono, -1, 1) * 32767).astype(np.int16)
            if not np.array_equal(waveform.read(dtype="int16"), expected):
                raise ValueError(
                    "Completed WAV PCM differs from legacy artifact quantization"
                )
        if not _same_legacy_file(original, os.fstat(source.fileno())):
            raise ValueError("Completed WAV changed during verification")
    if not _same_legacy_file(original, candidate.stat(follow_symlinks=False)):
        raise ValueError("Completed WAV changed during verification")
    existing = _saved_wav(cache.store, key)
    if existing is not None:
        with open_regular_binary(existing) as source:
            if file_digest(source, "sha256").hexdigest() != record["sha256"]:
                raise ValueError("Existing WAV conflicts with the completed recording")
    if not apply:
        return candidate
    _prepare_conversion_catalog(cache.store)
    saved = existing or cache.store.put(key, candidate)
    with open_regular_binary(saved) as source:
        if file_digest(source, "sha256").hexdigest() != record["sha256"]:
            raise ValueError(
                "Published WAV checksum differs from the completed recording"
            )
    if cache.get_existing(key) is None:
        raise ValueError("Completed WAV readback failed")
    return saved


def _cache_with_provenance(
    root: Path, proof: dict[str, object]
) -> PersistentAudioCache | None:
    rate = proof.get("sample_rate")
    if not (
        proof.get("backend") in _BACKENDS
        and type(rate) is int
        and rate > 0
        and isinstance(model := proof.get("model"), str)
        and bool(model.strip())
        and isinstance(evidence := proof.get("sample_rate_evidence"), str)
        and bool(evidence.strip())
    ):
        return None
    return PersistentAudioCache(root, sample_rate=rate)


def _record_unavailable_arrays(store: SharedAudioStore, keys: set[str]) -> None:
    with store._catalog() as connection:
        assert connection is not None
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany(
            "INSERT INTO unavailable_keys SELECT ?, ? WHERE NOT EXISTS "
            "(SELECT 1 FROM bindings WHERE synthesis_key=?) "
            "ON CONFLICT(synthesis_key) DO NOTHING",
            (
                (
                    key,
                    "Preserved NPY samples require model/sample-rate evidence before reuse",
                    key,
                )
                for key in sorted(keys)
            ),
        )
        connection.commit()


def _saved_catalog_inventory(
    store: SharedAudioStore, legacy_keys: set[str], *, apply: bool
) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
    catalog_entries: list[dict[str, object]] = []
    try:
        saved_bindings = (
            list(store.iter_bindings()) if store.catalog_path.exists() else []
        )
        issues = []
        if store.catalog_path.exists():
            if apply:
                _record_unavailable_arrays(store, legacy_keys)
            with store._catalog() as connection:
                assert connection is not None
                issues = connection.execute(
                    "SELECT synthesis_key, reason FROM unavailable_keys ORDER BY synthesis_key"
                ).fetchall()
    except (OSError, ValueError) as error:
        catalog_entries.append(
            dict(
                path=str(store.catalog_path),
                status="preserved_error",
                reason=str(error),
            )
        )
        saved_bindings, issues = [], []
    for key, reason in issues:
        if key not in legacy_keys:
            catalog_entries.append(
                dict(
                    path=str(store.catalog_path),
                    key=key,
                    status="preserved_error",
                    reason=reason,
                )
            )
    return saved_bindings, catalog_entries


def _saved_binding_entry(
    store: SharedAudioStore, key: str, provenance: dict[str, object]
) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": str(store.catalog_path),
        "key": key,
        "provenance": provenance,
    }
    cache = _cache_with_provenance(store.directory, provenance)
    if _KEY.fullmatch(key) is None:
        return {**entry, "status": "preserved_unknown"}
    if cache is None:
        return {
            **entry,
            "status": "preserved_unproven",
            "reason": "Missing model/rate evidence",
        }
    try:
        if cache.get_existing(key) is None:
            raise ValueError("Saved WAV verification failed")
        saved = store.get(key)
        if saved is None:
            raise ValueError("Saved WAV binding disappeared")
        completed = provenance.get("completed_wav")
        if completed is not None:
            with open_regular_binary(saved) as source:
                actual = file_digest(source, "sha256").hexdigest()
            if not isinstance(completed, dict) or completed.get("sha256") != actual:
                raise ValueError(
                    "Saved WAV conflicts with completed recording evidence"
                )
        entry.update(status="already_wav", wav=str(saved))
    except (OSError, ValueError, TypeError, sf.LibsndfileError) as error:
        entry.update(status="preserved_error", reason=str(error))
    return entry


def migrate_legacy_cache(
    directory: str | Path,
    *,
    backend: str,
    model: str,
    sample_rate: int | None = None,
    sample_rate_evidence: str = "",
    key_provenance: dict[str, dict[str, object]] | None = None,
    apply: bool = False,
) -> MigrationReport:
    """Inventory one synthesis root; preserve anything not safely migratable.

    Only direct, canonical synthesis keys are eligible. Prompt/voice/reference
    trees are never traversed. The caller supplies independently checked rate
    provenance; an absent rate/evidence leaves every original untouched. If
    key_provenance is supplied, only its individually proven keys are eligible;
    root-level evidence is never inherited by unlisted keys in a mixed root.
    """
    root = Path(directory).expanduser()
    if backend not in _BACKENDS or not (
        root.name == "audio"
        or (root.parent.name == "audio-cache" and root.name == backend)
    ):
        raise ValueError(f"Not a recognized synthesis cache root: {root}")
    if root.is_symlink() or not root.is_dir():
        raise ValueError(
            f"Cache root must be an existing, non-symlink directory: {root}"
        )
    root = root.parent.resolve() / root.name
    proof: dict[str, object] = dict(
        backend=backend,
        model=model,
        sample_rate=sample_rate,
        sample_rate_evidence=sample_rate_evidence,
    )
    if key_provenance is not None and (
        not isinstance(key_provenance, dict)
        or any(
            not isinstance(value, dict)
            or not set(value) <= set(proof) | {"completed_wav"}
            for value in key_provenance.values()
        )
    ):
        raise ValueError(
            "key_provenance must map synthesis keys to model/rate evidence records"
        )
    entries: list[dict[str, object]] = []
    store = SharedAudioStore(root)
    legacy_keys: set[str] = set()
    for path in sorted(root.iterdir()):
        if path.name in {
            store.catalog_path.name + suffix
            for suffix in ("", "-journal", "-wal", "-shm")
        }:
            continue
        if (
            path.name in {"objects", "bindings"}
            and path.is_dir()
            and not path.is_symlink()
        ):
            continue
        entry: dict[str, object] = {"path": str(path)}
        entries.append(entry)
        if path.suffix != ".npy" or _KEY.fullmatch(path.stem) is None:
            entry.update(status="preserved_unknown", reason="Not a synthesis NPY key")
            continue
        key = path.stem
        legacy_keys.add(key)
        entry["key"] = key
        provenance = proof if key_provenance is None else key_provenance.get(key, {})
        cache = _cache_with_provenance(root, provenance)
        entry["provenance"] = provenance
        if cache is None:
            entry.update(
                status="preserved_unproven", reason="Missing model/rate evidence"
            )
            continue
        try:
            if path.is_symlink():
                raise ValueError("Legacy entry is a symbolic link")
            with open_regular_binary(path) as source:
                original = os.fstat(source.fileno())
                if not _same_legacy_file(original, path.stat(follow_symlinks=False)):
                    raise ValueError("Legacy entry changed during inspection")
                audio = _prepared_audio(_read_cached_array(source))
                if not _same_legacy_file(
                    original, os.fstat(source.fileno())
                ) or not _same_legacy_file(original, path.stat(follow_symlinks=False)):
                    raise ValueError("Legacy entry changed while reading")
            if audio is None or (audio.ndim == 2 and audio.shape[1] not in (1, 2)):
                raise ValueError("Legacy array is not finite mono/stereo audio")
            entry.update(
                source_bytes=original.st_size,
                frames=len(audio),
                channels=1 if audio.ndim == 1 else audio.shape[1],
            )
            if "completed_wav" in provenance:
                completed_saved = _reuse_completed_wav(
                    cache, key, audio, provenance["completed_wav"], apply=apply
                )
                if not apply:
                    entry.update(status="ready", wav=str(completed_saved))
                    continue
                if _same_legacy_file(original, path.stat(follow_symlinks=False)):
                    path.unlink()
            elif not apply and (saved := _saved_wav(cache.store, key)) is not None:
                with BytesIO() as encoded:
                    sf.write(
                        encoded,
                        audio,
                        cache.sample_rate,
                        format="WAV",
                        subtype="PCM_16",
                    )
                    with open_regular_binary(saved) as source:
                        if (
                            file_digest(source, "sha256").digest()
                            != sha256(encoded.getvalue()).digest()
                        ):
                            raise ValueError(
                                "Existing WAV conflicts with the legacy audio"
                            )
            if not apply:
                entry["status"] = "ready"
                continue
            if (
                "completed_wav" not in provenance
                and _migrate_legacy(cache, key) is None
            ):
                raise ValueError(
                    "WAV publication/readback failed or existing WAV conflicts"
                )
            entry["wav"] = str(cache.store.get(key))
            entry["status"] = (
                "preserved_changed"
                if path.exists() or path.is_symlink()
                else "migrated"
            )
        except (
            OSError,
            ValueError,
            TypeError,
            EOFError,
            OverflowError,
            TokenError,
            sf.LibsndfileError,
        ) as error:
            entry.update(status="preserved_error", reason=str(error))
    saved_bindings, catalog_entries = _saved_catalog_inventory(
        store, legacy_keys, apply=apply
    )
    entries.extend(catalog_entries)
    for key, _digest in saved_bindings:
        if key not in legacy_keys:
            provenance = (
                proof if key_provenance is None else key_provenance.get(key, {})
            )
            entries.append(_saved_binding_entry(store, key, provenance))
    return MigrationReport(
        directory=str(root),
        backend=backend,
        model=model,
        sample_rate=sample_rate,
        sample_rate_evidence=sample_rate_evidence,
        apply=apply,
        counts=dict(Counter(str(entry["status"]) for entry in entries)),
        entries=entries,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        roots = json.loads(read_regular_text(arguments.manifest, encoding="utf-8"))
        allowed = {
            "directory",
            "backend",
            "model",
            "sample_rate",
            "sample_rate_evidence",
            "key_provenance",
        }
        if not isinstance(roots, list) or any(
            not isinstance(root, dict) or not set(root) <= allowed for root in roots
        ):
            raise ValueError("Manifest must contain a list of synthesis root records")
        reports = [migrate_legacy_cache(**root) for root in roots]
        if arguments.apply:
            reports = [migrate_legacy_cache(**root, apply=True) for root in roots]
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return int(
        any(
            any(status.startswith("preserved") for status in r["counts"])
            for r in reports
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
