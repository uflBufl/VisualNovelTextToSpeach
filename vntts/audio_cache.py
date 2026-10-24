import json
import os
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from hashlib import blake2b, file_digest
from io import BytesIO
from math import prod
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from tokenize import TokenError
from typing import BinaryIO, Generic, TypeAlias, TypeVar

import numpy as np
import soundfile as sf
from numpy.lib import _format_impl as npy_format
from numpy.typing import NDArray

from vntts.audio_store import SharedAudioStore
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
    def __init__(
        self, directory: PathInput, *, sample_rate: int, max_entries: int = 256
    ) -> None:
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or sample_rate < 1
        ):
            raise ValueError("sample_rate must be a positive integer")
        self.store = SharedAudioStore(Path(directory))
        self.directory = self.store.directory
        self.sample_rate = sample_rate
        # Disk waveforms are durable; the old limit now only disables persistence.
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
        return self._get(key, migrate=True)

    def get_existing(self, key: object) -> AudioArray | None:
        """Read a verified saved WAV even when new persistence is disabled."""
        return self._get(key, migrate=False)

    def _get(self, key: object, *, migrate: bool) -> AudioArray | None:
        if not isinstance(key, str) or self._path_for_key(key) is None:
            return None
        try:
            payload = self.store.get_bytes(key)
            if payload is not None:
                with sf.SoundFile(BytesIO(payload)) as waveform:
                    if (
                        waveform.format != "WAV"
                        or waveform.subtype != "PCM_16"
                        or waveform.samplerate != self.sample_rate
                    ):
                        return None
                    audio = _prepared_audio(
                        waveform.read(dtype="float32", always_2d=True)
                    )
                if audio is None:
                    return None
                return audio[:, 0] if audio.shape[1] == 1 else audio
            return self._migrate_legacy(key) if migrate else None
        except OSError, ValueError, TypeError, sf.LibsndfileError:
            return None

    def put(self, key: object, audio: object) -> Path | None:
        if (
            self.max_entries == 0
            or not isinstance(key, str)
            or self._path_for_key(key) is None
        ):
            return None
        prepared = _prepared_audio(audio)
        if prepared is None:
            return None
        return self._put_prepared(key, prepared)

    def _put_prepared(
        self, key: str, prepared: AudioArray, *, preserve_existing: bool = False
    ) -> Path | None:
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                suffix=".wav", dir=self.directory, delete=False
            ) as output:
                temporary = Path(output.name)
            sf.write(
                temporary, prepared, self.sample_rate, format="WAV", subtype="PCM_16"
            )
            if preserve_existing and (existing := self.store.get(key)) is not None:
                with (
                    open_regular_binary(existing) as saved,
                    open_regular_binary(temporary) as candidate,
                ):
                    if (
                        file_digest(saved, "sha256").digest()
                        != file_digest(candidate, "sha256").digest()
                    ):
                        return None
                return existing
            return self.store.put(key, temporary)
        except OSError, ValueError, TypeError, sf.LibsndfileError:
            return None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _migrate_legacy(self, key: str) -> AudioArray | None:
        path = self._path_for_key(key)
        if path is None or path.is_symlink():
            return None
        try:
            with open_regular_binary(path) as source:
                original = os.fstat(source.fileno())
                if not self._same_legacy_file(
                    original, path.stat(follow_symlinks=False)
                ):
                    return None
                audio = _prepared_audio(_read_cached_array(source))
            if (
                audio is None
                or self._put_prepared(key, audio, preserve_existing=True) is None
            ):
                return None
            restored = self.get_existing(key)
            if restored is None:
                return None
            # Never remove a substituted legacy entry after publication.
            if self._same_legacy_file(original, path.stat(follow_symlinks=False)):
                path.unlink()
            return restored
        except OSError, ValueError, TypeError, EOFError, OverflowError, TokenError:
            return None

    @staticmethod
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

    def _path_for_key(self, key: object) -> Path | None:
        if (
            not isinstance(key, str)
            or not key
            or not all(character.isalnum() or character in "-_" for character in key)
        ):
            return None
        return self.directory / f"{key}.npy"
