import json
import os
from hashlib import blake2b
from pathlib import Path
from time import time_ns

import numpy as np
from vntts_artifacts.atomic_io import atomic_output_path


class PersistentAudioCache:
    def __init__(self, directory, *, max_entries=256):
        self.directory = Path(directory).expanduser().resolve()
        self.max_entries = max(0, int(max_entries))

    def key(self, *, backend, model, voice, text, settings):
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

    def get(self, key):
        if self.max_entries == 0:
            return None
        path = self.directory / f"{key}.npy"
        try:
            with path.open("rb") as source:
                audio = np.load(source, allow_pickle=False)
            audio = np.atleast_1d(np.asarray(audio, dtype=np.float32).squeeze())
            if (
                audio.ndim not in {1, 2}
                or audio.size == 0
                or not np.all(np.isfinite(audio))
            ):
                return None
            self._touch_newest(path)
            return audio
        except OSError, ValueError, TypeError:
            return None

    def put(self, key, audio):
        if self.max_entries == 0:
            return None
        audio = np.atleast_1d(np.asarray(audio, dtype=np.float32).squeeze())
        if (
            audio.ndim not in {1, 2}
            or audio.size == 0
            or not np.all(np.isfinite(audio))
        ):
            return None
        path = self.directory / f"{key}.npy"
        try:
            with atomic_output_path(path) as temporary:
                with temporary.open("wb") as destination:
                    np.save(destination, audio, allow_pickle=False)
            self._touch_newest(path)
            self._prune()
        except OSError:
            return None
        return path

    def _touch_newest(self, path):
        newest = max(
            (
                candidate.stat().st_mtime_ns
                for candidate in self.directory.glob("*.npy")
            ),
            default=0,
        )
        timestamp = max(time_ns(), newest + 1_000_000)
        os.utime(path, ns=(timestamp, timestamp))

    def _prune(self):
        files = sorted(
            self.directory.glob("*.npy"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in files[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass
