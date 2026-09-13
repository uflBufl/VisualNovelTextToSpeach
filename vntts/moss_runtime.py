"""Keep one native OpenMOSS backend alive for the desktop application."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Lock
from typing import Protocol, TypeAlias, TypeGuard

from vntts.application_directories import get_local_data_directory
from vntts.moss_cpp_backend import moss_cpp_requested
from vntts.playback import PreparedPlayback
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.speech_worker import create_moss_worker_backend
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisRequest,
    SynthesisResult,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import CharacterVoiceRegistry

BackendOptions: TypeAlias = dict[str, object]
PlaybackGuard: TypeAlias = Callable[[], bool] | None


class _MossBackend(Protocol):
    registry: CharacterVoiceRegistry
    narrator_reference: object
    startup_cancellation: object
    startup_progress: object
    runtime_status: str | None

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...

    def stop(self) -> bool: ...

    def shutdown(self) -> None: ...

    def load(self) -> str | None: ...

    def set_volume(self, volume: object) -> object: ...

    def set_generation_profile(self, generation_profile: object) -> object: ...

    def play_prepared(
        self, prepared: PreparedPlayback, *, playback_guard: PlaybackGuard = None
    ) -> object: ...


def _is_moss_backend(value: object) -> TypeGuard[_MossBackend]:
    return hasattr(value, "runtime_status") and all(
        callable(getattr(value, name, None))
        for name in (
            "render",
            "stop",
            "shutdown",
            "load",
            "set_volume",
            "set_generation_profile",
            "play_prepared",
        )
    )


class _LockedStream:
    def __init__(self, stream: SynthesisChunkStream, lock: Lock) -> None:
        self._stream = stream
        self._lock = lock
        self._released = False

    def __iter__(self) -> Iterator[SynthesisChunk]:
        return self

    def __next__(self) -> SynthesisChunk:
        try:
            return next(self._stream)
        except BaseException:
            self._release()
            raise

    @property
    def result(self) -> SynthesisResult:
        return self._stream.result

    def collect(self) -> SynthesisResult:
        for _chunk in self:
            pass
        return self.result

    def close(self) -> None:
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()
        finally:
            self._release()

    def _release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class _MossBackendLease:
    def __init__(
        self,
        runtime: RetainedMossRuntime,
        registry: CharacterVoiceRegistry,
        options: BackendOptions,
    ) -> None:
        self._runtime = runtime
        self._registry = registry
        self._options = options

    @property
    def registry(self) -> CharacterVoiceRegistry:
        return self._registry

    @registry.setter
    def registry(self, value: CharacterVoiceRegistry) -> None:
        self._registry = value

    def render(self, request: SynthesisRequest) -> _LockedStream:
        self._runtime._operation_lock.acquire()
        try:
            backend = self._runtime._configured_backend(self)
            return _LockedStream(backend.render(request), self._runtime._operation_lock)
        except BaseException:
            self._runtime._operation_lock.release()
            raise

    def shutdown(self) -> None:
        return None

    def stop(self) -> bool:
        backend = self._runtime._backend
        return backend.stop() if backend is not None else False

    def play_prepared(
        self, prepared: PreparedPlayback, *, playback_guard: PlaybackGuard = None
    ) -> object:
        if (
            getattr(getattr(prepared, "payload", None), "cached_audio", None)
            is not None
        ):
            backend = self._runtime._backend
            if backend is None:
                raise RuntimeError("OpenMOSS runtime is unloaded")
            return backend.play_prepared(prepared, playback_guard=playback_guard)
        with self._runtime._operation_lock:
            backend = self._runtime._configured_backend(self)
            return backend.play_prepared(prepared, playback_guard=playback_guard)

    def __getattr__(self, name: str) -> object:
        backend = self._runtime._backend
        if backend is None:
            raise AttributeError(name)
        value = getattr(backend, name)
        if not callable(value):
            return value

        def call(*args: object, **kwargs: object) -> object:
            with self._runtime._operation_lock:
                return getattr(self._runtime._configured_backend(self), name)(
                    *args, **kwargs
                )

        return call


class RetainedMossRuntime:
    """Own the native model until explicit unload or application shutdown."""

    supports_startup_cancellation = True
    supports_startup_progress = True

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        backend_factory: Callable[..., object] = create_moss_worker_backend,
    ) -> None:
        self.root = (
            Path(root or get_local_data_directory() / "moss-runtime")
            .expanduser()
            .resolve()
        )
        self.backend_factory = backend_factory
        self._operation_lock = Lock()
        self._backend: _MossBackend | None = None
        self._model_name: object | None = None

    def _create_backend(
        self, registry: CharacterVoiceRegistry, **options: object
    ) -> _MossBackend:
        backend = self.backend_factory(registry, **options)
        if not _is_moss_backend(backend):
            raise TypeError("OpenMOSS backend factory returned an invalid backend")
        return backend

    @property
    def backend(self) -> _MossBackend | None:
        return self._backend

    @property
    def loaded(self) -> bool:
        return bool(
            self._backend is not None and getattr(self._backend, "runtime_status", None)
        )

    @property
    def runtime_status(self) -> str | None:
        return (
            getattr(self._backend, "runtime_status", None)
            if self._backend is not None
            else None
        )

    def backend_for(
        self, registry: CharacterVoiceRegistry, **options: object
    ) -> _MossBackend | _MossBackendLease:
        model_name = options.get("model_name")
        if not moss_cpp_requested(model_name):
            return self._create_backend(registry, **options)
        with self._operation_lock:
            if self._backend is None or self._model_name != model_name:
                shutdown_speech_backend(self._backend)
                self._backend = None
                self.root.mkdir(parents=True, exist_ok=True)
                retained_options = dict(options)
                retained_options["persistent_audio_cache_directory"] = (
                    self.root / "audio"
                )
                retained_options["prompt_cache_directory"] = self.root / "voices"
                retained_options.setdefault("persistent_audio_cache_max_entries", 512)
                retained_options.setdefault("allow_download", True)
                self._backend = self._create_backend(registry, **retained_options)
                self._model_name = model_name
            else:
                self._backend.registry = registry
                if "startup_cancellation" in options:
                    self._backend.startup_cancellation = options["startup_cancellation"]
                if "startup_progress" in options:
                    self._backend.startup_progress = options["startup_progress"]
                self._backend.load()
        return _MossBackendLease(self, registry, dict(options))

    def __call__(
        self, registry: CharacterVoiceRegistry, **options: object
    ) -> _MossBackend | _MossBackendLease:
        return self.backend_for(registry, **options)

    def benchmark_backend(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        cache_root: str | Path,
        **options: object,
    ) -> object:
        if name != "moss-tts" or not moss_cpp_requested(options.get("model_name")):
            return create_backend(name, registry, cache_root, **options)
        return self.backend_for(registry, **options)

    def _configured_backend(self, lease: _MossBackendLease) -> _MossBackend:
        backend = self._backend
        if backend is None:
            raise RuntimeError("OpenMOSS runtime is unloaded")
        backend.registry = lease.registry
        options = lease._options
        if "narrator_reference" in options:
            backend.narrator_reference = options["narrator_reference"]
        if "startup_cancellation" in options:
            backend.startup_cancellation = options["startup_cancellation"]
        if "startup_progress" in options:
            backend.startup_progress = options["startup_progress"]
        if "volume" in options:
            backend.set_volume(options["volume"])
        if "generation_profile" in options:
            backend.set_generation_profile(options["generation_profile"])
        return backend

    def unload(self) -> None:
        backend = self._backend
        if backend is not None:
            backend.stop()
        with self._operation_lock:
            if self._backend is not None:
                self._backend.shutdown()

    def shutdown(self) -> None:
        backend = self._backend
        if backend is not None:
            backend.stop()
        with self._operation_lock:
            shutdown_speech_backend(self._backend)
            self._backend = None
            self._model_name = None


__all__ = ["RetainedMossRuntime"]
