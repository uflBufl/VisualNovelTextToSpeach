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
from collections import Counter
from hashlib import file_digest, sha256
from io import BytesIO
from pathlib import Path
from tokenize import TokenError
from typing import TypedDict

import numpy as np
import soundfile as sf

from vntts.audio_cache import (
    AudioArray,
    PersistentAudioCache,
    _prepared_audio,
    _read_cached_array,
)
from vntts.path_safety import open_regular_binary, read_regular_text

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
        if not cache._same_legacy_file(original, os.fstat(source.fileno())):
            raise ValueError("Completed WAV changed during verification")
    if not cache._same_legacy_file(original, candidate.stat(follow_symlinks=False)):
        raise ValueError("Completed WAV changed during verification")
    existing = cache.store.get(key)
    if existing is not None:
        with open_regular_binary(existing) as source:
            if file_digest(source, "sha256").hexdigest() != record["sha256"]:
                raise ValueError("Existing WAV conflicts with the completed recording")
    if not apply:
        return candidate
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
    legacy_keys: set[str] = set()
    for path in sorted(root.iterdir()):
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
                if not cache._same_legacy_file(
                    original, path.stat(follow_symlinks=False)
                ):
                    raise ValueError("Legacy entry changed during inspection")
                audio = _prepared_audio(_read_cached_array(source))
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
                if cache._same_legacy_file(original, path.stat(follow_symlinks=False)):
                    path.unlink()
            elif not apply and (saved := cache.store.get(key)) is not None:
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
            if "completed_wav" not in provenance and cache._migrate_legacy(key) is None:
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
    bindings = root / "bindings"
    if bindings.is_symlink():
        entries.append(
            dict(path=str(bindings), status="preserved_error", reason="Symlink")
        )
    elif bindings.is_dir():
        for path in sorted(bindings.iterdir()):
            if path.stem in legacy_keys:
                continue
            entry = {"path": str(path), "key": path.stem}
            entries.append(entry)
            provenance = (
                proof if key_provenance is None else key_provenance.get(path.stem, {})
            )
            cache = _cache_with_provenance(root, provenance)
            entry["provenance"] = provenance
            if path.suffix != ".json" or _KEY.fullmatch(path.stem) is None:
                entry["status"] = "preserved_unknown"
            elif cache is None:
                entry.update(
                    status="preserved_unproven", reason="Missing model/rate evidence"
                )
            elif cache.get_existing(path.stem) is None:
                entry.update(
                    status="preserved_error", reason="Saved WAV verification failed"
                )
            else:
                saved = cache.store.get(path.stem)
                if saved is None:
                    entry.update(
                        status="preserved_error", reason="Saved WAV binding disappeared"
                    )
                    continue
                completed = provenance.get("completed_wav")
                if completed is not None:
                    with open_regular_binary(saved) as source:
                        actual = file_digest(source, "sha256").hexdigest()
                    if (
                        not isinstance(completed, dict)
                        or completed.get("sha256") != actual
                    ):
                        entry.update(
                            status="preserved_error",
                            reason="Saved WAV conflicts with completed recording evidence",
                        )
                        continue
                entry.update(status="already_wav", wav=str(saved))
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
