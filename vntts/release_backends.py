"""Speech backend availability for source and frozen application builds."""

from vntts.runtime_paths import find_bundled_speech_runtime, get_bundle_root

SPEECH_BACKEND_LABELS = {
    "pocket-tts": "Pocket TTS (recommended)",
    "coqui-xtts": "XTTS",
    "chatterbox-nano": "Chatterbox Nano",
    "moss-tts": "MOSS-TTS Local v1.5",
}
_SOURCE_BACKENDS = tuple(SPEECH_BACKEND_LABELS)


def packaged_speech_backend_available(backend, bundle_root=None):
    bundle_root = get_bundle_root() if bundle_root is None else bundle_root
    if bundle_root is None:
        return backend in _SOURCE_BACKENDS
    if backend == "coqui-xtts":
        return True
    if backend == "moss-tts":
        from vntts.moss_cpp_backend import moss_cpp_requested

        return moss_cpp_requested()
    if backend == "pocket-tts":
        return find_bundled_speech_runtime(backend, bundle_root) is not None
    return False


def speech_backend_options(current_backend, bundle_root=None):
    bundle_root = get_bundle_root() if bundle_root is None else bundle_root
    if bundle_root is None:
        return tuple(
            (label, backend, True) for backend, label in SPEECH_BACKEND_LABELS.items()
        )
    options = [
        (label, backend, True)
        for backend, label in SPEECH_BACKEND_LABELS.items()
        if packaged_speech_backend_available(backend, bundle_root)
    ]
    if current_backend and not any(
        backend == current_backend for _label, backend, _available in options
    ):
        label = SPEECH_BACKEND_LABELS.get(current_backend, current_backend)
        options.append(
            (f"{label} (not included in this package)", current_backend, False)
        )
    return tuple(options)
