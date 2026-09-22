import json
import os
from hashlib import blake2b
from pathlib import Path
from time import time_ns
from typing import TypeAlias

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.atomic_io import atomic_output_path

PathInput: TypeAlias = str | os.PathLike[str]
AudioArray: TypeAlias = NDArray[np.float32]


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
            audio: AudioArray = np.atleast_1d(
                np.asarray(loaded, dtype=np.float32).squeeze()
            )
            if (
                audio.ndim not in {1, 2}
                or audio.size == 0
                or not np.all(np.isfinite(audio))
            ):
                return None
            self._touch_newest(path)
            return audio
        except OSError, ValueError, TypeError, EOFError:
            return None

    def put(self, key: object, audio: object) -> Path | None:
        if self.max_entries == 0:
            return None
        prepared: AudioArray = np.atleast_1d(
            np.asarray(audio, dtype=np.float32).squeeze()
        )
        if (
            prepared.ndim not in {1, 2}
            or prepared.size == 0
            or not np.all(np.isfinite(prepared))
        ):
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
        os.utime(path, ns=(timestamp, timestamp), follow_symlinks=False)

    def _prune(self) -> None:
        files = sorted(
            self.directory.glob("*.npy"),
            key=lambda path: path.stat(follow_symlinks=False).st_mtime_ns,
            reverse=True,
        )
        for path in files[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass
