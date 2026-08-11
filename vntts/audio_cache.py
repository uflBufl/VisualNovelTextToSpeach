import json
from hashlib import blake2b
from pathlib import Path
from uuid import uuid4

import numpy as np


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
            if audio.ndim != 1 or audio.size == 0 or not np.all(np.isfinite(audio)):
                return None
            path.touch()
            return audio
        except (OSError, ValueError, TypeError):
            return None

    def put(self, key, audio):
        if self.max_entries == 0:
            return None
        audio = np.atleast_1d(np.asarray(audio, dtype=np.float32).squeeze())
        if audio.ndim != 1 or audio.size == 0 or not np.all(np.isfinite(audio)):
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{key}.npy"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as destination:
                np.save(destination, audio, allow_pickle=False)
            temporary.replace(path)
            self._prune()
        except OSError:
            temporary.unlink(missing_ok=True)
            return None
        return path

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
