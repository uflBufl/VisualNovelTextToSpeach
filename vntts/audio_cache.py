import json
import os
from collections import OrderedDict
from hashlib import blake2b
from pathlib import Path
from time import time_ns
from typing import Generic, TypeAlias, TypeVar

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.atomic_io import atomic_output_path

PathInput: TypeAlias = str | os.PathLike[str]
AudioArray: TypeAlias = NDArray[np.float32]
CacheKey = TypeVar("CacheKey")
CacheValue = TypeVar("CacheValue")


def _prepared_audio(value: object) -> AudioArray | None:
    audio: AudioArray = np.atleast_1d(np.asarray(value, dtype=np.float32).squeeze())
    if audio.ndim not in {1, 2} or audio.size == 0 or not np.all(np.isfinite(audio)):
        return None
    return audio


class BoundedCache(Generic[CacheKey, CacheValue]):
    """Small least-recently-used cache shared by speech backends."""

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max(0, int(max_entries))
        self._values: OrderedDict[CacheKey, CacheValue] = OrderedDict()

    def get(self, key: CacheKey) -> CacheValue | None:
        value = self._values.pop(key, None)
        if value is not None:
            self._values[key] = value
        return value

    def put(self, key: CacheKey, value: CacheValue) -> None:
        if self.max_entries == 0:
            return
        self._values.pop(key, None)
        self._values[key] = value
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)

    def clear(self) -> None:
        self._values.clear()


class PersistentAudioCache:
    def __init__(self, directory: PathInput, *, max_entries: int = 256) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.max_entries = max(0, int(max_entries))

    def key(
        self,
        *,
        backend: str,
        model: str,
        voice: str,
        text: str,
        settings: dict[str, object],
    ) -> str:
        document = {
            "version": 1,
            "backend": str(backend),
            "model": str(model),
            "voice": str(voice),
            "text": " ".join((text or "").split()),
            "settings": settings,
        }
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return blake2b(encoded, digest_size=24).hexdigest()

    def get(self, key: object) -> AudioArray | None:
        if self.max_entries == 0:
            return None
        path = self._path_for_key(key)
        if path is None or path.is_symlink():
            return None
        try:
            with path.open("rb") as source:
                loaded = np.load(source, allow_pickle=False)
            audio = _prepared_audio(loaded)
            if audio is None:
                return None
            self._touch_newest(path)
            return audio
        except OSError, ValueError, TypeError, EOFError:
            return None

    def put(self, key: object, audio: object) -> Path | None:
        if self.max_entries == 0:
            return None
        prepared = _prepared_audio(audio)
        if prepared is None:
            return None
        path = self._path_for_key(key)
        if path is None:
            return None
        try:
            with atomic_output_path(path) as temporary:
                with temporary.open("wb") as destination:
                    np.save(destination, prepared, allow_pickle=False)
            self._touch_newest(path)
            self._prune()
        except OSError:
            return None
        return path

    def _path_for_key(self, key: object) -> Path | None:
        if (
            not isinstance(key, str)
            or not key
            or not all(character.isalnum() or character in "-_" for character in key)
        ):
            return None
        return self.directory / f"{key}.npy"

    def _touch_newest(self, path: Path) -> None:
        newest = max(
            (
                candidate.stat().st_mtime_ns
                for candidate in self.directory.glob("*.npy")
            ),
            default=0,
        )
        timestamp = max(time_ns(), newest + 1_000_000)
        try:
            os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
        except NotImplementedError:
            # Windows cannot update this timestamp without following symlinks.
            # Keep the cache entry and use its creation time for pruning.
            pass

    def _prune(self) -> None:
        files = sorted(
            self.directory.glob("*.npy"),
            key=_cache_entry_recency,
            reverse=True,
        )
        for path in files[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass


def _cache_entry_recency(path: Path) -> tuple[int, int, str]:
    entry = path.stat(follow_symlinks=False)
    return entry.st_mtime_ns, entry.st_ctime_ns, path.name
