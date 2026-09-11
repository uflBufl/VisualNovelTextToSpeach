"""Shared cache identity, bounded memory, and settings for speech backends."""

from __future__ import annotations

import os
import re
import sys
from collections import OrderedDict
from functools import lru_cache
from hashlib import blake2b
from pathlib import Path
from typing import Generic, Protocol, TypeAlias, TypeVar

from vntts_artifacts.file_integrity import sha256_file

from vntts.runtime_paths import (
    default_source_speech_runtime,
    find_bundled_speech_runtime,
    get_bundle_root,
)
from vntts.services.tts_engine import TTSConfigurationError

PathInput: TypeAlias = str | Path
CacheKey = TypeVar("CacheKey")
CacheValue = TypeVar("CacheValue")

_managed_runtime_uses: dict[str, object] = {}


class BoundedCache(Generic[CacheKey, CacheValue]):
    """Small least-recently-used cache with a deliberately minimal interface."""

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


def shutdown_speech_backend(backend: object) -> None:
    """Shut down a backend if present and return the cleared slot value."""
    shutdown = getattr(backend, "shutdown", None)
    if callable(shutdown):
        shutdown()
    else:
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
    return None


def activate_backend_runtime(
    runtime_directory: PathInput | None,
    *,
    environment_variable: str,
    backend_directory: str,
    missing_message: str,
) -> Path:
    """Expose one standalone backend environment to the current interpreter."""
    configured = runtime_directory or os.environ.get(environment_variable, "")
    bundle_root = get_bundle_root() if not configured else None
    bundled = (
        find_bundled_speech_runtime(backend_directory, bundle_root)
        if bundle_root is not None
        else None
    )
    source_runtime = (
        None
        if configured or bundled
        else default_source_speech_runtime(backend_directory)
        if bundle_root is None
        else bundle_root / "speech-runtimes" / backend_directory
    )
    selected_runtime = configured or bundled or source_runtime
    if selected_runtime is None:
        raise TTSConfigurationError(missing_message)
    runtime_directory = Path(selected_runtime).expanduser().resolve()
    if sys.platform == "win32":
        site_packages = runtime_directory / "Lib" / "site-packages"
    else:
        site_packages = (
            runtime_directory
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    if not site_packages.is_dir():
        if bundle_root is not None:
            raise TTSConfigurationError(
                f"{backend_directory} runtime is missing from the application "
                "package. Reinstall the application from a complete release package."
            )
        raise TTSConfigurationError(missing_message)
    site_packages_text = str(site_packages)
    if site_packages_text not in _managed_runtime_uses:
        from vntts.runtime_ownership import claim_runtime

        use = claim_runtime(backend_directory, runtime_directory)
        if use is not None:
            # sys.path can load these modules lazily for the rest of this process.
            _managed_runtime_uses[site_packages_text] = use
    if site_packages_text not in sys.path:
        sys.path.insert(0, site_packages_text)
    return site_packages


def validate_volume(volume: int | float) -> float:
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise TTSConfigurationError("Volume must be a number from 0 to 1")
    if not 0 <= volume <= 1:
        raise TTSConfigurationError("Volume must be between 0 and 1")
    return float(volume)


def validate_speed(speed: int | float) -> float:
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise TTSConfigurationError("Speech speed must be a number")
    if not 0.5 <= speed <= 1.5:
        raise TTSConfigurationError("Speech speed must be between 0.5 and 1.5")
    return float(speed)


@lru_cache(maxsize=1024)
def _file_content_identity(path: str, size: int, _modified_ns: int) -> str:
    return f"sha256:{sha256_file(path)}:{size}"


def _source_identity(source: object) -> str:
    source_path = Path(str(source)).expanduser()
    try:
        if source_path.is_file():
            resolved = source_path.resolve()
            stat = resolved.stat()
            return _file_content_identity(
                str(resolved),
                stat.st_size,
                stat.st_mtime_ns,
            )
        stat = source_path.stat()
        return f"{source_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return str(source)


def voice_source_identity(voice_key: str, source: object) -> str:
    return f"{voice_key}:{_source_identity(source)}"


class CacheKeyBuilder(Protocol):
    def key(
        self,
        *,
        backend: str,
        model: str,
        voice: str,
        text: str,
        settings: dict[str, object],
    ) -> str: ...


class SpeechCacheKeyFactory:
    """Own persistent generated-audio identities for one loaded backend model."""

    def __init__(
        self,
        cache: CacheKeyBuilder,
        *,
        backend: str,
        model: object,
        sample_rate: int,
        model_identity: str | None = None,
    ) -> None:
        self.cache = cache
        self.backend = backend
        self.model = model_identity or (
            f"{model.__class__.__module__}.{model.__class__.__qualname__}"
        )
        self.sample_rate = sample_rate

    def key(
        self,
        *,
        voice_key: str,
        source: object,
        text: str,
        speed: float,
        **settings: object,
    ) -> str:
        return self.cache.key(
            backend=self.backend,
            model=self.model,
            voice=voice_source_identity(voice_key, source),
            text=text,
            settings={
                "sample_rate": self.sample_rate,
                "speed": speed,
                **settings,
            },
        )


def voice_artifact_cache_path(
    directory: PathInput,
    *,
    voice_key: str,
    source: object,
    model_identity: str,
    suffix: str,
) -> Path:
    """Return a stable cache path for model state derived from one voice source."""
    digest = blake2b(
        f"{model_identity}:{_source_identity(source)}".encode(),
        digest_size=12,
    ).hexdigest()
    safe_key = re.sub(r"[^a-z0-9]+", "-", str(voice_key).casefold()).strip("-")
    return Path(directory) / f"{safe_key or 'voice'}-{digest}{suffix}"
