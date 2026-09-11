"""Keep one native OpenMOSS backend alive for the desktop application."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from vntts.application_directories import get_local_data_directory
from vntts.moss_cpp_backend import moss_cpp_requested
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.speech_worker import create_moss_worker_backend
from vntts.tts_benchmark import create_backend


class _LockedStream:
    def __init__(self, stream, lock):
        self._stream = stream
        self._lock = lock
        self._released = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._stream)
        except BaseException:
            self._release()
            raise

    @property
    def result(self):
        return self._stream.result

    def collect(self):
        for _chunk in self:
            pass
        return self.result

    def close(self):
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()
        finally:
            self._release()

    def _release(self):
        if not self._released:
            self._released = True
            self._lock.release()


class _MossBackendLease:
    def __init__(self, runtime, registry, options):
        self._runtime = runtime
        self._registry = registry
        self._options = options

    @property
    def registry(self):
        return self._registry

    @registry.setter
    def registry(self, value):
        self._registry = value

    def render(self, request):
        self._runtime._operation_lock.acquire()
        try:
            backend = self._runtime._configured_backend(self)
            return _LockedStream(backend.render(request), self._runtime._operation_lock)
        except BaseException:
            self._runtime._operation_lock.release()
            raise

    def shutdown(self):
        return None

    def stop(self):
        backend = self._runtime._backend
        return backend.stop() if backend is not None else False

    def __getattr__(self, name):
        backend = self._runtime._backend
        if backend is None:
            raise AttributeError(name)
        value = getattr(backend, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            with self._runtime._operation_lock:
                return getattr(self._runtime._configured_backend(self), name)(
                    *args, **kwargs
                )

        return call


class RetainedMossRuntime:
    """Own the native model until explicit unload or application shutdown."""

    supports_startup_cancellation = True
    supports_startup_progress = True

    def __init__(self, root=None, *, backend_factory=create_moss_worker_backend):
        self.root = (
            Path(root or get_local_data_directory() / "moss-runtime")
            .expanduser()
            .resolve()
        )
        self.backend_factory = backend_factory
        self._operation_lock = Lock()
        self._backend = None
        self._model_name = None

    @property
    def backend(self):
        return self._backend

    @property
    def loaded(self):
        return bool(
            self._backend is not None and getattr(self._backend, "runtime_status", None)
        )

    @property
    def runtime_status(self):
        return (
            getattr(self._backend, "runtime_status", None)
            if self._backend is not None
            else None
        )

    def backend_for(self, registry, **options):
        model_name = options.get("model_name")
        if not moss_cpp_requested(model_name):
            return self.backend_factory(registry, **options)
        with self._operation_lock:
            if self._backend is None or self._model_name != model_name:
                self._backend = shutdown_speech_backend(self._backend)
                self.root.mkdir(parents=True, exist_ok=True)
                retained_options = dict(options)
                retained_options["persistent_audio_cache_directory"] = (
                    self.root / "audio"
                )
                retained_options["prompt_cache_directory"] = self.root / "voices"
                retained_options.setdefault("persistent_audio_cache_max_entries", 512)
                self._backend = self.backend_factory(registry, **retained_options)
                self._model_name = model_name
            else:
                self._backend.registry = registry
                if "startup_cancellation" in options:
                    self._backend.startup_cancellation = options["startup_cancellation"]
                if "startup_progress" in options:
                    self._backend.startup_progress = options["startup_progress"]
                self._backend.load()
        return _MossBackendLease(self, registry, dict(options))

    def __call__(self, registry, **options):
        return self.backend_for(registry, **options)

    def benchmark_backend(self, name, registry, cache_root, **options):
        if name != "moss-tts" or not moss_cpp_requested(options.get("model_name")):
            return create_backend(name, registry, cache_root, **options)
        return self.backend_for(registry, **options)

    def _configured_backend(self, lease):
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

    def unload(self):
        backend = self._backend
        if backend is not None:
            backend.stop()
        with self._operation_lock:
            if self._backend is not None:
                self._backend.shutdown()

    def shutdown(self):
        backend = self._backend
        if backend is not None:
            backend.stop()
        with self._operation_lock:
            self._backend = shutdown_speech_backend(self._backend)
            self._model_name = None


__all__ = ["RetainedMossRuntime"]
