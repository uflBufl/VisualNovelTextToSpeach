import json
import os
from collections import OrderedDict
from contextlib import suppress
from hashlib import blake2b
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Generic, TypeAlias, TypeVar

import numpy as np
import soundfile as sf
from numpy.typing import NDArray

from vntts.audio_store import AudioCatalogError, SharedAudioStore

PathInput: TypeAlias = str | os.PathLike[str]
AudioArray: TypeAlias = NDArray[np.float32]
CacheKey = TypeVar("CacheKey")
CacheValue = TypeVar("CacheValue")


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


def _valid_cache_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(character.isalnum() or character in "-_" for character in value)
    )


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
        return self._get(key)

    def get_existing(self, key: object) -> AudioArray | None:
        """Read a verified saved WAV even when new persistence is disabled."""
        return self._get(key)

    def _get(self, key: object) -> AudioArray | None:
        if not isinstance(key, str) or not _valid_cache_key(key):
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
                        raise AudioCatalogError(
                            "Saved WAV format/sample rate differs from its synthesis request"
                        )
                    audio = _prepared_audio(
                        waveform.read(dtype="float32", always_2d=True)
                    )
                if audio is None:
                    raise AudioCatalogError("Saved WAV contains invalid samples")
                return audio[:, 0] if audio.shape[1] == 1 else audio
            return None
        except AudioCatalogError:
            raise
        except (OSError, ValueError, TypeError, sf.LibsndfileError) as error:
            raise AudioCatalogError(
                f"Saved audio could not be decoded: {error}"
            ) from error

    def put(self, key: object, audio: object) -> Path | None:
        if (
            self.max_entries == 0
            or not isinstance(key, str)
            or not _valid_cache_key(key)
        ):
            return None
        prepared = _prepared_audio(audio)
        if prepared is None:
            return None
        return self._put_prepared(key, prepared)

    def _put_prepared(self, key: str, prepared: AudioArray) -> Path | None:
        temporary = None
        try:
            self.store.initialize()
            with NamedTemporaryFile(
                suffix=".wav", dir=self.directory, delete=False
            ) as output:
                temporary = Path(output.name)
            sf.write(
                temporary, prepared, self.sample_rate, format="WAV", subtype="PCM_16"
            )
            return self.store.put(key, temporary)
        except AudioCatalogError:
            raise
        except OSError, ValueError, TypeError, sf.LibsndfileError:
            return None
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
