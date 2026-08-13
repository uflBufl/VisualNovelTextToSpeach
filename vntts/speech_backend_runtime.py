"""Shared cache identity, bounded memory, and settings for speech backends."""

from __future__ import annotations

import re
from collections import OrderedDict
from hashlib import blake2b
from pathlib import Path

from vntts.services.tts_engine import TTSConfigurationError


class BoundedCache:
    """Small least-recently-used cache with a deliberately minimal interface."""

    def __init__(self, max_entries):
        self.max_entries = max(0, int(max_entries))
        self._values = OrderedDict()

    def get(self, key):
        value = self._values.pop(key, None)
        if value is not None:
            self._values[key] = value
        return value

    def put(self, key, value):
        if self.max_entries == 0:
            return
        self._values.pop(key, None)
        self._values[key] = value
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)


def validate_volume(volume):
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise TTSConfigurationError("Volume must be a number from 0 to 1")
    if not 0 <= volume <= 1:
        raise TTSConfigurationError("Volume must be between 0 and 1")
    return float(volume)


def validate_speed(speed):
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise TTSConfigurationError("Speech speed must be a number")
    if not 0.5 <= speed <= 1.5:
        raise TTSConfigurationError("Speech speed must be between 0.5 and 1.5")
    return float(speed)


def _source_identity(source):
    source_path = Path(str(source)).expanduser()
    try:
        stat = source_path.stat()
        return f"{source_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(source)


def voice_source_identity(voice_key, source):
    return f"{voice_key}:{_source_identity(source)}"


class SpeechCacheKeyFactory:
    """Own persistent generated-audio identities for one loaded backend model."""

    def __init__(self, cache, *, backend, model, sample_rate):
        self.cache = cache
        self.backend = backend
        self.model = f"{model.__class__.__module__}.{model.__class__.__qualname__}"
        self.sample_rate = sample_rate

    def key(self, *, voice_key, source, text, speed):
        return self.cache.key(
            backend=self.backend,
            model=self.model,
            voice=voice_source_identity(voice_key, source),
            text=text,
            settings={"sample_rate": self.sample_rate, "speed": speed},
        )


def voice_artifact_cache_path(
    directory,
    *,
    voice_key,
    source,
    model_identity,
    suffix,
):
    """Return a stable cache path for model state derived from one voice source."""
    digest = blake2b(
        f"{model_identity}:{_source_identity(source)}".encode(),
        digest_size=12,
    ).hexdigest()
    safe_key = re.sub(r"[^a-z0-9]+", "-", str(voice_key).casefold()).strip("-")
    return Path(directory) / f"{safe_key or 'voice'}-{digest}{suffix}"
