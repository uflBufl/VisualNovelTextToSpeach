"""Shared cache identity and settings for speech backends."""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from hashlib import blake2b
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeAlias

from vntts.cleanup import attempt_cleanup
from vntts.document_identity import file_sha256
from vntts.path_safety import contained_path
from vntts.runtime_paths import resolve_speech_runtime_root
from vntts.services.tts_engine import TTSConfigurationError
from vntts.services.tts_engine import validate_speed as validate_speed
from vntts.services.tts_engine import validate_volume as validate_volume

if TYPE_CHECKING:
    from vntts.runtime_ownership import RuntimeUse

PathInput: TypeAlias = str | Path
_managed_runtime_uses: dict[str, RuntimeUse] = {}


def shutdown_speech_backend(
    backend: object, *, primary_error: BaseException | None = None
) -> None:
    """Release a backend without replacing an earlier operation failure."""

    def release() -> None:
        shutdown = getattr(backend, "shutdown", None)
        if callable(shutdown):
            shutdown()
        else:
            stop = getattr(backend, "stop", None)
            if callable(stop):
                stop()

    attempt_cleanup(
        release,
        description="Speech backend shutdown",
        primary_error=primary_error,
    )


def activate_backend_runtime(
    runtime_directory: PathInput | None,
    *,
    environment_variable: str,
    backend_directory: str,
    missing_message: str,
) -> Path:
    """Expose one standalone backend environment to the current interpreter."""
    configured = runtime_directory or os.environ.get(environment_variable, "")
    runtime_directory, bundle_root = resolve_speech_runtime_root(
        backend_directory, configured
    )
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
    if bundle_root is not None:
        contained_path(
            bundle_root,
            site_packages,
            f"{backend_directory} bundled site-packages",
            error_type=TTSConfigurationError,
        )
    site_packages_text = str(site_packages)
    if bundle_root is None and site_packages_text not in _managed_runtime_uses:
        from vntts.runtime_ownership import claim_runtime

        use = claim_runtime(backend_directory, runtime_directory)
        if use is not None:
            # sys.path can load these modules lazily for the rest of this process.
            _managed_runtime_uses[site_packages_text] = use
    if site_packages_text not in sys.path:
        sys.path.insert(0, site_packages_text)
    return site_packages


@lru_cache(maxsize=1024)
def _file_content_identity(path: str, size: int, _modified_ns: int) -> str:
    return f"sha256:{file_sha256(path, error_type=OSError)}:{size}"


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
