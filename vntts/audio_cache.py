import json
import os
from collections import OrderedDict
from collections.abc import Callable
from hashlib import blake2b
from math import prod
from pathlib import Path
from stat import S_ISREG
from threading import RLock
from time import time_ns
from tokenize import TokenError
from typing import BinaryIO, Generic, TypeAlias, TypeVar

import numpy as np
from numpy.lib import _format_impl as npy_format
from numpy.typing import NDArray
from vntts_artifacts.atomic_io import atomic_output_path

from vntts.path_safety import open_regular_binary

PathInput: TypeAlias = str | os.PathLike[str]
AudioArray: TypeAlias = NDArray[np.float32]
CacheKey = TypeVar("CacheKey")
CacheValue = TypeVar("CacheValue")

# shortcut: NumPy lacks a public NPY3 header reader; replace when one is available.
_read_npy_header: Callable[
    [BinaryIO, tuple[int, int]],
    tuple[tuple[int, ...], bool, np.dtype[np.generic]],
] = getattr(npy_format, "_read_array_header")


def _prepared_audio(value: object) -> AudioArray | None:
    try:
        audio: AudioArray = np.asarray(value, dtype=np.float32)
    except TypeError, ValueError, OverflowError:
        return None
    if audio.ndim not in {1, 2}:
        audio = np.atleast_1d(audio.squeeze())
    if audio.ndim not in {1, 2} or audio.size == 0 or not np.all(np.isfinite(audio)):
        return None
    return audio


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


class BoundedCache(Generic[CacheKey, CacheValue]):
    """Small least-recently-used cache shared by speech backends."""

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max(0, int(max_entries))
        self._values: OrderedDict[CacheKey, CacheValue] = OrderedDict()
        self._lock = RLock()

    def get(self, key: CacheKey) -> CacheValue | None:
        with self._lock:
            try:
                self._values.move_to_end(key)
            except KeyError:
                return None
            return self._values[key]

    def put(self, key: CacheKey, value: CacheValue) -> None:
        with self._lock:
            if self.max_entries == 0:
                return
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
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
            with open_regular_binary(path) as source:
                loaded = _read_cached_array(source)
            audio = _prepared_audio(loaded)
            if audio is None:
                return None
            self._touch_newest(path)
            return audio
        except OSError, ValueError, TypeError, EOFError, OverflowError, TokenError:
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
            with (
                atomic_output_path(path) as temporary,
                temporary.open("wb") as destination,
            ):
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
        newest = max((recency[0] for _, recency in self._regular_files()), default=0)
        timestamp = max(time_ns(), newest + 1_000_000)
        try:
            os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)
        except OSError, NotImplementedError:
            # Recency metadata must not turn a valid cache entry into a miss.
            # Pruning can fall back to its existing timestamps.
            pass

    def _prune(self) -> None:
        files = sorted(self._regular_files(), key=lambda item: item[1], reverse=True)
        for path, _ in files[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass

    def _regular_files(self) -> list[tuple[Path, tuple[int, int, str]]]:
        files: list[tuple[Path, tuple[int, int, str]]] = []
        for path in self.directory.glob("*.npy"):
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if S_ISREG(metadata.st_mode):
                files.append(
                    (path, (metadata.st_mtime_ns, metadata.st_ctime_ns, path.name))
                )
        return files
