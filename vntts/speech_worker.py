"""Isolated speech-model workers with typed framed PCM streaming."""

from __future__ import annotations

import importlib
import json
import os
import platform
import queue
import struct
import subprocess
import sys
import threading
import uuid
from collections import deque
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import redirect_stdout
from io import BytesIO
from itertools import chain
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeGuard

import numpy as np
from numpy.typing import NDArray

from vntts.audio_output import AudioOutput, StreamingAudioStream, resolve_audio_output
from vntts.moss_delay_backend import MossTTSDelayVoiceRouterBackend
from vntts.playback import (
    PlaybackOutcome,
    PlaybackStatus,
    PreparedPlayback,
    outcome_for_prepared,
)
from vntts.runtime_paths import (
    RUNTIME_ENVIRONMENT_VARIABLES,
    default_source_speech_runtime,
    find_bundled_speech_runtime,
    get_bundle_root,
)
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    TTSConfigurationError,
    TTSSynthesisError,
    validate_speed,
    validate_volume,
)
from vntts.speech_worker_messages import (
    FrameDocument,
    RegistryDocument,
    RemotePreparedSpeech,
    SynthesisDiagnosticsDocument,
    SynthesisLimitsDocument,
    SynthesisResultDocument,
    SynthesisTimingDocument,
    VoiceDocument,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    normalize_character_name,
)

if TYPE_CHECKING:
    from vntts.runtime_ownership import RuntimeUse

_FRAME_LENGTH = struct.Struct(">I")
_BOOTSTRAP = (
    "import sys;"
    "source=sys.argv.pop(1);"
    "source and sys.path.insert(0,source);"
    "from vntts.speech_worker import worker_main;"
    "raise SystemExit(worker_main())"
)
_COMMON_REQUIRED_MODULES = (
    "numpy",
    "scipy",
    "platformdirs",
    "vntts_artifacts",
    "durable_file",
)
_REQUIRED_MODULES = {
    "pocket-tts": _COMMON_REQUIRED_MODULES + ("pocket_tts", "torch", "safetensors"),
    "chatterbox-nano": _COMMON_REQUIRED_MODULES
    + (
        "chatterbox",
        "torch",
        "torchaudio",
        "transformers",
        "tokenizers",
        "safetensors",
    ),
    "moss-tts": _COMMON_REQUIRED_MODULES
    + (
        "mlx.core",
        "mlx_audio",
        "transformers",
        "tokenizers",
        "safetensors",
    ),
    "moss-tts-delay": _COMMON_REQUIRED_MODULES
    + ("torch", "transformers", "tokenizers", "safetensors"),
}
_BACKEND_CLASSES = {
    "pocket-tts": PocketTTSVoiceRouterBackend,
    "chatterbox-nano": ChatterboxNanoVoiceRouterBackend,
    "moss-tts": MossTTSVoiceRouterBackend,
    "moss-tts-delay": MossTTSDelayVoiceRouterBackend,
}
_CAPABILITIES = {
    "pocket-tts": PocketTTSVoiceRouterBackend.capabilities,
    "chatterbox-nano": ChatterboxNanoVoiceRouterBackend.capabilities,
    "moss-tts": MossTTSVoiceRouterBackend.capabilities,
    "moss-tts-delay": MossTTSDelayVoiceRouterBackend.capabilities,
}

WorkerFrame: TypeAlias = tuple[FrameDocument, bytes]


class _CancellationToken(Protocol):
    def is_set(self) -> bool: ...


Cancellation: TypeAlias = Callable[[], bool] | _CancellationToken | None
WorkerOptions: TypeAlias = dict[str, object]
RuntimePaths: TypeAlias = tuple[Path, Path, Path]
ProcessFactory: TypeAlias = Callable[..., object]
PlaybackGuard: TypeAlias = Callable[[], bool] | None
WarmupProgress: TypeAlias = Callable[[int, int, str], None]
StartupProgress: TypeAlias = Callable[[str], None] | None


class _ReadableBinaryStream(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def readline(self, size: int = -1) -> bytes: ...


class _WritableBinaryStream(Protocol):
    def write(self, data: bytes) -> object: ...

    def flush(self) -> object: ...


class WorkerProcess(Protocol):
    stdin: _WritableBinaryStream | None
    stdout: _ReadableBinaryStream | None
    stderr: _ReadableBinaryStream | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


WorkerMessage: TypeAlias = tuple[WorkerProcess, FrameDocument, bytes]


class _RetainedBackend(Protocol):
    process: WorkerProcess | None
    registry: CharacterVoiceRegistry
    narrator_reference: str | Path | None
    startup_cancellation: Cancellation
    startup_progress: StartupProgress

    def shutdown(self) -> None: ...

    def set_volume(self, volume: object) -> None: ...


class _AudioStream(Protocol):
    def __enter__(self) -> "_AudioStream": ...

    def __exit__(
        self, exception_type: object, exception: object, traceback: object
    ) -> bool | None: ...

    def write(self, audio: NDArray[np.float32]) -> object: ...

    def abort(self) -> object: ...


class _AudioOutput(Protocol):
    def OutputStream(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        latency: str,
    ) -> _AudioStream: ...


class _WorkerBackend(Protocol):
    registry: CharacterVoiceRegistry
    narrator_reference: str | Path | None
    sample_rate: int

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...

    def prime(self, voice: str) -> bool: ...

    def set_live_mode_active(self, active: bool) -> bool: ...


WorkerBackendFactory: TypeAlias = Callable[..., object]


def _is_document(value: object) -> TypeGuard[FrameDocument]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _is_text(value: object) -> TypeGuard[str]:
    return isinstance(value, str)


def _required_document_value(document: Mapping[str, object], field: str) -> object:
    return document[field]


def _required_text(document: Mapping[str, object], field: str) -> str:
    value = _required_document_value(document, field)
    if not isinstance(value, str):
        raise TTSConfigurationError(f"Speech worker {field} must be text")
    return value


def _required_integer(document: Mapping[str, object], field: str) -> int:
    value = _required_document_value(document, field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TTSConfigurationError(f"Speech worker {field} must be an integer")
    return value


def _optional_seed(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_cancellation(value: object) -> TypeGuard[Cancellation]:
    return value is None or callable(value) or callable(getattr(value, "is_set", None))


def _is_startup_progress(value: object) -> TypeGuard[StartupProgress]:
    return value is None or callable(value)


def _required_float(document: Mapping[str, object], field: str) -> float:
    value = _required_document_value(document, field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TTSConfigurationError(f"Speech worker {field} must be a number")
    return float(value)


def _document_path(document: Mapping[str, object], field: str) -> Path:
    value = document.get(field, "")
    if not isinstance(value, (str, Path)):
        raise TypeError("expected str, bytes or os.PathLike object")
    return Path(value)


def _chunk_shape(document: Mapping[str, object]) -> tuple[int, ...]:
    value = _required_document_value(document, "shape")
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise TTSConfigurationError("Speech worker chunk shape is malformed")
    return tuple(value)


def _document_value(value: object, label: str) -> FrameDocument:
    if not _is_document(value):
        raise TTSConfigurationError(f"Speech worker {label} is malformed")
    return value


def _voice_document(value: object) -> VoiceDocument:
    document = _document_value(value, "voice registry")
    aliases = document.get("aliases", ())
    references = document.get("references", ())
    reference_root = document.get("reference_root")
    if (
        not isinstance(aliases, list)
        or not all(isinstance(item, str) for item in aliases)
        or not isinstance(references, list)
        or not all(isinstance(item, str) for item in references)
        or reference_root is not None
        and not isinstance(reference_root, str)
    ):
        raise TTSConfigurationError("Speech worker voice registry is malformed")
    return {
        "character": _required_text(document, "character"),
        "speaker": _required_text(document, "speaker"),
        "aliases": aliases,
        "references": references,
        "reference_root": reference_root,
    }


def _registry_document(value: object) -> RegistryDocument:
    document = _document_value(value, "voice registry")
    voices = document.get("voices", ())
    assignments = document.get("assignments", {})
    if not isinstance(voices, list) or not _is_document(assignments):
        raise TTSConfigurationError("Speech worker voice registry is malformed")
    return {
        "voices": [_voice_document(item) for item in voices],
        "assignments": {
            character: None if item is None else _voice_document(item)
            for character, item in assignments.items()
        },
    }


def _worker_options(value: object) -> WorkerOptions:
    if value is None:
        return {}
    document = _document_value(value, "options")
    return dict(document)


def _result_document_value(value: object) -> SynthesisResultDocument:
    document = _document_value(value, "render result")
    limits = _document_value(document.get("limits"), "render result limits")
    timing = _document_value(document.get("timing"), "render result timing")
    diagnostics = _document_value(
        document.get("diagnostics"), "render result diagnostics"
    )
    max_tokens = limits.get("max_tokens")
    max_audio_seconds = limits.get("max_audio_seconds")
    first_chunk_ms = timing.get("first_chunk_ms")
    seed = diagnostics.get("seed")
    if (
        not isinstance(document.get("sample_rate"), int)
        or not isinstance(document.get("completion"), str)
        or max_tokens is not None
        and not isinstance(max_tokens, int)
        or max_audio_seconds is not None
        and not isinstance(max_audio_seconds, float)
        or first_chunk_ms is not None
        and not isinstance(first_chunk_ms, float)
        or not isinstance(timing.get("total_ms"), float)
        or not isinstance(diagnostics.get("backend"), str)
        or not isinstance(diagnostics.get("cache_source"), str)
        or not isinstance(diagnostics.get("generation_profile"), str)
        or seed is not None
        and not isinstance(seed, int)
        or not isinstance(diagnostics.get("chunk_count"), int)
        or not isinstance(diagnostics.get("sample_count"), int)
    ):
        raise TTSConfigurationError("Speech worker render result is malformed")
    sample_rate = _required_integer(document, "sample_rate")
    completion = _required_text(document, "completion")
    total_ms = _required_float(timing, "total_ms")
    backend = _required_text(diagnostics, "backend")
    cache_source = _required_text(diagnostics, "cache_source")
    generation_profile = _required_text(diagnostics, "generation_profile")
    chunk_count = _required_integer(diagnostics, "chunk_count")
    sample_count = _required_integer(diagnostics, "sample_count")
    return {
        "sample_rate": sample_rate,
        "completion": completion,
        "limits": {
            "max_tokens": max_tokens,
            "max_audio_seconds": max_audio_seconds,
        },
        "timing": {"first_chunk_ms": first_chunk_ms, "total_ms": total_ms},
        "diagnostics": {
            "backend": backend,
            "cache_source": cache_source,
            "generation_profile": generation_profile,
            "seed": seed,
            "chunk_count": chunk_count,
            "sample_count": sample_count,
        },
    }


def _backend_factory(value: object) -> WorkerBackendFactory:
    if not callable(value):
        raise TTSConfigurationError("Speech worker backend factory is malformed")
    return value


def _is_worker_backend(value: object) -> TypeGuard[_WorkerBackend]:
    return (
        hasattr(value, "registry")
        and hasattr(value, "narrator_reference")
        and isinstance(getattr(value, "sample_rate", None), int)
        and callable(getattr(value, "render", None))
        and callable(getattr(value, "prime", None))
        and callable(getattr(value, "set_live_mode_active", None))
    )


def _is_worker_process(value: object) -> TypeGuard[WorkerProcess]:
    return (
        hasattr(value, "stdin")
        and hasattr(value, "stdout")
        and hasattr(value, "stderr")
        and callable(getattr(value, "poll", None))
        and callable(getattr(value, "wait", None))
        and callable(getattr(value, "terminate", None))
        and callable(getattr(value, "kill", None))
    )


def _write_frame(
    stream: _WritableBinaryStream,
    document: Mapping[str, object],
    payload: bytes = b"",
) -> None:
    header = json.dumps(
        {**document, "payload_bytes": len(payload)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    stream.write(_FRAME_LENGTH.pack(len(header)))
    stream.write(header)
    if payload:
        stream.write(payload)
    stream.flush()


def _read_exact(stream: _ReadableBinaryStream, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream: _ReadableBinaryStream) -> WorkerFrame | None:
    prefix = _read_exact(stream, _FRAME_LENGTH.size)
    if prefix is None:
        return None
    header_size = _FRAME_LENGTH.unpack(prefix)[0]
    if not 0 < header_size <= 1_000_000:
        raise ValueError("Speech worker emitted an invalid frame header")
    header_bytes = _read_exact(stream, header_size)
    if header_bytes is None:
        raise EOFError("Speech worker frame header was truncated")
    document = json.loads(header_bytes.decode("utf-8"))
    if not _is_document(document):
        raise ValueError("Speech worker emitted an invalid frame header")
    payload_size = document.pop("payload_bytes", 0)
    if not isinstance(payload_size, int) or not 0 <= payload_size <= 512_000_000:
        raise ValueError("Speech worker emitted an invalid payload size")
    payload = _read_exact(stream, payload_size)
    if payload is None:
        raise EOFError("Speech worker frame payload was truncated")
    return document, payload


def _runtime_paths(
    backend: str, runtime_directory: str | Path | None = None
) -> RuntimePaths:
    if backend not in _BACKEND_CLASSES:
        raise TTSConfigurationError(f"Unsupported isolated backend: {backend!r}")
    configured = RUNTIME_ENVIRONMENT_VARIABLES[backend]
    folder = backend
    configured_root = runtime_directory or os.environ.get(configured, "")
    bundle_root = get_bundle_root() if not configured_root else None
    bundled_root = find_bundled_speech_runtime(backend) if not configured_root else None
    root_value: str | Path
    if configured_root:
        root_value = configured_root
    elif bundled_root is not None:
        root_value = bundled_root
    else:
        root_value = (
            bundle_root / "speech-runtimes" / folder
            if bundle_root is not None
            else default_source_speech_runtime(backend)
        )
    root = Path(root_value).expanduser().resolve()
    if sys.platform == "win32":
        interpreter = next(
            (
                candidate
                for candidate in (root / "python.exe", root / "Scripts/python.exe")
                if candidate.is_file()
            ),
            root / "python.exe",
        )
        site_packages_candidates = (root / "Lib/site-packages",)
    else:
        interpreter = root / "bin/python"
        site_packages_candidates = tuple(
            sorted((root / "lib").glob("python*/site-packages"))
        )
    site_packages = next(
        (candidate for candidate in site_packages_candidates if candidate.is_dir()),
        None,
    )
    if (
        not interpreter.is_file()
        or site_packages is None
        or sum(candidate.is_dir() for candidate in site_packages_candidates) != 1
    ):
        if bundle_root is not None:
            remediation = "Reinstall the application from a complete release package."
        else:
            remediation = (
                f"Run `uv sync --project backends/{folder}`, then restart the app."
            )
        raise TTSConfigurationError(
            f"{backend} isolated runtime is unavailable at {root}. {remediation}"
        )
    return root, interpreter, site_packages.resolve()


def resolve_speech_runtime_paths(
    backend: str, runtime_directory: str | Path | None = None
) -> RuntimePaths:
    """Resolve one isolated backend runtime without starting its worker."""
    return _runtime_paths(backend, runtime_directory)


def probe_speech_runtime(
    backend: str,
    paths: RuntimePaths,
    *,
    cancellation: Cancellation = None,
    runtime_use: RuntimeUse | None = None,
) -> FrameDocument:
    """Use the real worker import/provenance gate without loading model weights."""
    from vntts.runtime_installation import _run
    from vntts.runtime_ownership import claim_runtime

    root, interpreter, site = paths
    request = BytesIO()
    _write_frame(
        request,
        {
            "type": "runtime_probe",
            "backend": backend,
            "runtime_site": str(site),
        },
    )
    use: RuntimeUse | None = runtime_use or claim_runtime(backend, root)
    try:
        output = _run(
            [
                str(interpreter),
                "-I",
                "-B",
                "-u",
                "-c",
                _BOOTSTRAP,
                "" if get_bundle_root() else str(Path(__file__).resolve().parents[1]),
            ],
            cancellation=cancellation,
            input_bytes=request.getvalue(),
            timeout=120,
            runtime_use=use,
        )
    finally:
        if runtime_use is None and use is not None:
            use.close()
    frame = _read_frame(BytesIO(output))
    health: FrameDocument = frame[0] if frame else {}
    if (
        health.get("type") != "runtime_health"
        or health.get("backend") != backend
        or _document_path(health, "interpreter").resolve() != interpreter.resolve()
        or _document_path(health, "prefix").resolve() != root.resolve()
        or _document_path(health, "runtime_site").resolve() != site.resolve()
    ):
        raise TTSConfigurationError(
            "Speech runtime verification failed; installation was not accepted."
        )
    return health


def _serialize_registry(registry: CharacterVoiceRegistry) -> RegistryDocument:
    voices: list[VoiceDocument] = []
    for voice in registry.unique_voices():
        voices.append(
            {
                "character": voice.character,
                "speaker": voice.speaker,
                "aliases": list(voice.aliases),
                "references": [str(value) for value in voice.references],
                "reference_root": (
                    None if voice.reference_root is None else str(voice.reference_root)
                ),
            }
        )
    assignments: dict[str, VoiceDocument | None] = {}
    for character, assignment_voice in registry.assignments.items():
        assignments[character] = (
            None
            if assignment_voice is None
            else {
                "character": assignment_voice.character,
                "speaker": assignment_voice.speaker,
                "aliases": list(assignment_voice.aliases),
                "references": [str(value) for value in assignment_voice.references],
                "reference_root": (
                    None
                    if assignment_voice.reference_root is None
                    else str(assignment_voice.reference_root)
                ),
            }
        )
    return {"voices": voices, "assignments": assignments}


def _voice_from_document(value: VoiceDocument) -> CharacterVoice:
    references = tuple(
        Path(item).expanduser().resolve() for item in value["references"]
    )
    return CharacterVoice(
        character=value["character"],
        speaker=value["speaker"],
        aliases=tuple(value.get("aliases", ())),
        references=references,
        reference_root=(
            None
            if value.get("reference_root") is None
            else Path(str(value["reference_root"])).expanduser().resolve()
        ),
    )


def _registry_from_document(document: RegistryDocument) -> CharacterVoiceRegistry:
    registry = CharacterVoiceRegistry(
        [_voice_from_document(value) for value in document.get("voices", ())]
    )
    registry.assignments = {
        character: None if value is None else _voice_from_document(value)
        for character, value in document.get("assignments", {}).items()
    }
    return registry


def _result_document(result: SynthesisResult) -> SynthesisResultDocument:
    return {
        "sample_rate": result.sample_rate,
        "completion": result.completion.value,
        "limits": SynthesisLimitsDocument(
            max_tokens=result.limits.max_tokens,
            max_audio_seconds=result.limits.max_audio_seconds,
        ),
        "timing": SynthesisTimingDocument(
            first_chunk_ms=result.timing.first_chunk_ms,
            total_ms=result.timing.total_ms,
        ),
        "diagnostics": SynthesisDiagnosticsDocument(
            backend=result.diagnostics.backend,
            cache_source=result.diagnostics.cache_source,
            generation_profile=result.diagnostics.generation_profile,
            seed=result.diagnostics.seed,
            chunk_count=result.diagnostics.chunk_count,
            sample_count=result.diagnostics.sample_count,
        ),
    }


def _result_from_document(
    document: SynthesisResultDocument, chunks: Sequence[NDArray[np.float32]]
) -> SynthesisResult:
    pcm = (
        np.concatenate(chunks, axis=0) if chunks else np.empty((0, 1), dtype=np.float32)
    )
    return SynthesisResult(
        pcm=pcm,
        sample_rate=document["sample_rate"],
        completion=SynthesisCompletion(document["completion"]),
        limits=SynthesisLimits(**document["limits"]),
        timing=SynthesisTiming(**document["timing"]),
        diagnostics=SynthesisDiagnostics(**document["diagnostics"]),
    )


def _module_health(
    runtime_site: str | Path, names: Sequence[str]
) -> dict[str, dict[str, str]]:
    runtime_site = Path(runtime_site).resolve()
    modules = {}
    for name in names:
        module = importlib.import_module(name)
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise TTSConfigurationError(
                f"Isolated runtime loaded {name} without an import origin"
            )
        origin = Path(module_file).resolve()
        try:
            origin.relative_to(runtime_site)
        except ValueError as error:
            raise TTSConfigurationError(
                f"Isolated runtime loaded {name} from outside its environment: {origin}"
            ) from error
        modules[name] = {
            "origin": str(origin),
            "version": str(getattr(module, "__version__", "unknown")),
        }
    return modules


def _backend_runtime_metadata(backend: object) -> FrameDocument:
    device = str(getattr(backend, "device", "unknown"))
    metadata: FrameDocument = {
        "device": device,
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    torch_module = getattr(backend, "torch", None)
    if device != "cuda" or torch_module is None:
        return metadata
    accelerator: FrameDocument = {
        "runtime": str(getattr(getattr(torch_module, "version", None), "cuda", None)),
    }
    try:
        index = int(torch_module.cuda.current_device())
        properties = torch_module.cuda.get_device_properties(index)
        accelerator.update(
            {
                "device_index": index,
                "name": str(properties.name),
                "total_memory_bytes": int(properties.total_memory),
                "capability": list(torch_module.cuda.get_device_capability(index)),
            }
        )
    except AttributeError, RuntimeError, TypeError, ValueError:
        accelerator["inspection"] = "unavailable"
    metadata["accelerator"] = accelerator
    return metadata


def worker_main(
    *,
    input_stream: _ReadableBinaryStream | None = None,
    output_stream: _WritableBinaryStream | None = None,
    backend_classes: Mapping[str, object] | None = None,
    required_modules: Mapping[str, Sequence[str]] | None = None,
) -> int:
    protocol_out = output_stream or sys.stdout.buffer
    protocol_in = input_stream or sys.stdin.buffer
    backend_classes = backend_classes or _BACKEND_CLASSES
    required_modules = required_modules or _REQUIRED_MODULES
    try:
        initialized = _read_frame(protocol_in)
        if initialized is None:
            return 2
        document, _payload = initialized
        if document.get("type") not in {"initialize", "runtime_probe"}:
            raise TTSConfigurationError("Speech worker expected initialization")
        backend_name = _required_text(document, "backend")
        runtime_site = Path(_required_text(document, "runtime_site")).resolve()
        with redirect_stdout(sys.stderr):
            modules = _module_health(runtime_site, required_modules[backend_name])
        if document["type"] == "runtime_probe":
            _write_frame(
                protocol_out,
                {
                    "type": "runtime_health",
                    "backend": backend_name,
                    "interpreter": str(Path(sys.executable).resolve()),
                    "prefix": str(Path(sys.prefix).resolve()),
                    "runtime_site": str(runtime_site),
                    "modules": modules,
                },
            )
            return 0
        registry = _registry_from_document(_registry_document(document["registry"]))
        options = _worker_options(document.get("options"))
        for key, value in tuple(options.items()):
            if key.endswith("_directory") and value is not None:
                if not isinstance(value, (str, Path)):
                    raise TTSConfigurationError(
                        f"Speech worker option {key} must be a path"
                    )
                options[key] = Path(value).expanduser().resolve()
        with redirect_stdout(sys.stderr):
            candidate = _backend_factory(backend_classes[backend_name])(
                registry, **options
            )
        if not _is_worker_backend(candidate):
            raise TTSConfigurationError("Speech worker backend is malformed")
        backend = candidate
        _write_frame(
            protocol_out,
            {
                "type": "health",
                "backend": backend_name,
                "interpreter": str(Path(sys.executable).resolve()),
                "prefix": str(Path(sys.prefix).resolve()),
                "runtime_site": str(runtime_site),
                "sample_rate": backend.sample_rate,
                "modules": modules,
                **_backend_runtime_metadata(backend),
            },
        )
        while True:
            incoming = _read_frame(protocol_in)
            if incoming is None:
                return 0
            command, _payload = incoming
            command_type = command.get("type")
            if command_type == "shutdown":
                return 0
            request_id = command.get("request_id")
            try:
                backend.registry = _registry_from_document(
                    _registry_document(command["registry"])
                )
                narrator_reference = command.get("narrator_reference")
                if narrator_reference is not None and not isinstance(
                    narrator_reference, (str, Path)
                ):
                    raise TTSConfigurationError(
                        "Speech worker narrator_reference must be text or a path"
                    )
                if narrator_reference != backend.narrator_reference:
                    backend.narrator_reference = narrator_reference
                    for cache_name in (
                        "voice_states",
                        "prompt_audio_codes",
                        "conditionals",
                    ):
                        cache = getattr(backend, cache_name, None)
                        if isinstance(cache, dict):
                            cache.pop("narrator", None)
                if command_type == "render":
                    request = SynthesisRequest(
                        voice=_required_text(command, "voice"),
                        text=_required_text(command, "text"),
                        seed=_optional_seed(command.get("seed")),
                        generation_profile=_required_text(
                            command, "generation_profile"
                        ),
                        cache_policy=SynthesisCachePolicy(
                            _required_text(command, "cache_policy")
                        ),
                    )
                    with redirect_stdout(sys.stderr):
                        rendered = backend.render(request)
                        for chunk in rendered:
                            pcm = np.asarray(chunk.pcm, dtype=np.float32, order="C")
                            _write_frame(
                                protocol_out,
                                {
                                    "type": "chunk",
                                    "request_id": request_id,
                                    "sample_rate": chunk.sample_rate,
                                    "index": chunk.index,
                                    "elapsed_ms": chunk.elapsed_ms,
                                    "shape": list(pcm.shape),
                                },
                                pcm.tobytes(order="C"),
                            )
                    _write_frame(
                        protocol_out,
                        {
                            "type": "result",
                            "request_id": request_id,
                            "result": _result_document(rendered.result),
                        },
                    )
                elif command_type == "prime":
                    with redirect_stdout(sys.stderr):
                        primed = backend.prime(_required_text(command, "voice"))
                    _write_frame(
                        protocol_out,
                        {"type": "primed", "request_id": request_id, "value": primed},
                    )
                elif command_type == "set-live-mode":
                    active = command["active"]
                    if not isinstance(active, bool):
                        raise TTSConfigurationError(
                            "Speech worker active must be boolean"
                        )
                    value = backend.set_live_mode_active(active)
                    _write_frame(
                        protocol_out,
                        {"type": "live-mode", "request_id": request_id, "value": value},
                    )
                else:
                    raise TTSConfigurationError(
                        f"Unsupported speech worker command: {command_type!r}"
                    )
            except Exception as error:
                _write_frame(
                    protocol_out,
                    {
                        "type": "error",
                        "request_id": request_id,
                        "error": str(error),
                        "error_type": type(error).__name__,
                    },
                )
    except Exception as error:
        print(f"Speech worker failed: {error}", file=sys.stderr)
        try:
            _write_frame(
                protocol_out,
                {
                    "type": "fatal",
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
            )
        except Exception:
            pass
        return 1


class IsolatedSpeechBackend:
    """Parent-side audio owner for a model loaded in its locked interpreter."""

    def __init__(
        self,
        backend: str,
        registry: CharacterVoiceRegistry,
        *,
        narrator_reference: str | Path | None = None,
        volume: object = 1.0,
        audio_output: AudioOutput | None = None,
        clock: Callable[[], float] = monotonic,
        runtime_directory: str | Path | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        startup_timeout: float = 1800.0,
        request_timeout: float = 120.0,
        startup_cancellation: Cancellation = None,
        startup_progress: StartupProgress = None,
        playback_latency: str | None = None,
        generation_profile: str | None = None,
        allow_gated_model_access: bool = False,
        **worker_options: object,
    ) -> None:
        self.name = backend
        self.registry = registry
        self.narrator_reference = (
            narrator_reference or "alba"
            if backend == "pocket-tts"
            else narrator_reference
        )
        self.narrator_speaker = {
            "pocket-tts": "Pocket TTS default",
            "chatterbox-nano": "Chatterbox default",
            "moss-tts": "MOSS reference voice",
            "moss-tts-delay": "MOSS Delay reference voice",
        }[backend]
        self.capabilities = _CAPABILITIES[backend]
        self.generation_profile = generation_profile or (
            "expressive"
            if backend == "moss-tts-delay"
            else "stable"
            if backend == "moss-tts"
            else "default"
        )
        self.model_name = str(worker_options.get("model_name") or backend)
        self.audio_output: AudioOutput | None = audio_output
        self.clock = clock
        self.process_factory = process_factory
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        if self.request_timeout <= 0:
            raise TTSConfigurationError(
                "Speech worker request timeout must be positive"
            )
        self.startup_cancellation = startup_cancellation
        self.startup_progress = startup_progress
        self.playback_latency = playback_latency or (
            "high" if backend == "chatterbox-nano" else "low"
        )
        self.allow_gated_model_access = bool(allow_gated_model_access)
        self.worker_options: WorkerOptions = worker_options
        from vntts.runtime_installation import ensure_speech_runtime

        self.runtime_root, self.interpreter, self.runtime_site = ensure_speech_runtime(
            backend,
            runtime_directory=runtime_directory,
            cancellation=startup_cancellation,
            progress=startup_progress,
        )
        self.project_root = Path(__file__).resolve().parents[1]
        self.bundle_root = get_bundle_root()
        self.worker_source_root = None if self.bundle_root else self.project_root
        self.worker_working_directory = self.bundle_root or self.project_root
        self.process: WorkerProcess | None = None
        self.health: FrameDocument | None = None
        self._messages: queue.Queue[WorkerMessage] = queue.Queue()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._playback_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._active_stream: StreamingAudioStream | None = None
        self._stderr: deque[str] = deque(maxlen=40)
        self.last_synthesis_ms: float | None = None
        self.last_first_audio_ms: float | None = None
        self.last_playback_ms: float | None = None
        self.last_playback_underrun = False
        self.last_generation_limited = False
        self.last_audio_source: str | None = None
        self._closed = False
        self._runtime_use: RuntimeUse | None = None
        self.set_volume(volume)
        self.set_speed(1.0)
        self._start_worker()

    def _start_worker(self) -> None:
        from vntts.runtime_ownership import claim_runtime

        if self._runtime_use is None:
            self._runtime_use = claim_runtime(self.name, self.runtime_root)
        try:
            self._launch_worker()
        except BaseException:
            try:
                self._terminate_process(self.process)
            finally:
                if self._runtime_use is not None:
                    self._runtime_use.close()
                    self._runtime_use = None
            raise

    def _launch_worker(self) -> None:
        if self._closed:
            raise TTSSynthesisError(f"{self.name} isolated worker is shut down")
        self.health = None
        command = [
            str(self.interpreter),
            "-I",
            "-B",
            "-u",
            "-c",
            _BOOTSTRAP,
            "" if self.worker_source_root is None else str(self.worker_source_root),
        ]
        environment = dict(os.environ)
        environment["PYTHONNOUSERSITE"] = "1"
        if self.name == "pocket-tts" and self.bundle_root is not None:
            for name in (
                "HF_HUB_CACHE",
                "HUGGINGFACE_HUB_CACHE",
                "TRANSFORMERS_CACHE",
            ):
                environment.pop(name, None)
            if not self.allow_gated_model_access:
                environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
                environment.pop("HF_TOKEN", None)
                environment.pop("HUGGING_FACE_HUB_TOKEN", None)
        if self._runtime_use is not None:
            self._runtime_use.begin_launch()
        try:
            candidate = self.process_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.worker_working_directory),
                env=environment,
                bufsize=0,
            )
        except Exception:
            if self._runtime_use is not None:
                self._runtime_use.launched(None)
            raise
        if not _is_worker_process(candidate):
            raise TTSConfigurationError("Speech worker process factory is malformed")
        process = candidate
        self.process = process
        if self._runtime_use is not None:
            self._runtime_use.launched(process)
        threading.Thread(
            target=self._read_messages,
            args=(process,),
            name=f"vntts-{self.name}-worker-reader",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name=f"vntts-{self.name}-worker-stderr",
            daemon=True,
        ).start()
        try:
            self._send(
                process,
                {
                    "type": "initialize",
                    "backend": self.name,
                    "runtime_site": str(self.runtime_site),
                    "registry": _serialize_registry(self.registry),
                    "options": self._json_worker_options(),
                },
            )
            deadline = monotonic() + self.startup_timeout
            while True:
                if self._startup_cancelled():
                    self._terminate_process(process)
                    raise TTSSynthesisError(
                        f"{self.name} isolated worker startup was cancelled"
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    details = "\n".join(self._stderr)
                    raise TTSConfigurationError(
                        f"{self.name} isolated worker health check timed out after "
                        f"{self.startup_timeout:g} seconds"
                        + (f": {details}" if details else "")
                    )
                try:
                    message = self._next_message(process, timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                break
        except Exception:
            self._terminate_process(process)
            raise
        if message.get("type") != "health":
            self._terminate_process(process)
            details = "\n".join(self._stderr)
            reason = message.get("error") or details or str(message)
            raise TTSConfigurationError(
                f"{self.name} isolated worker failed health check: {reason}"
            )
        if (
            Path(_required_text(message, "interpreter")).resolve()
            != self.interpreter.resolve()
        ):
            self._terminate_process(process)
            raise TTSConfigurationError("Speech worker used an unexpected interpreter")
        if Path(_required_text(message, "prefix")).resolve() != self.runtime_root:
            self._terminate_process(process)
            raise TTSConfigurationError("Speech worker used an unexpected environment")
        self.health = message
        self.sample_rate = _required_integer(message, "sample_rate")

    def _startup_cancelled(self) -> bool:
        cancellation = self.startup_cancellation
        if cancellation is None:
            return False
        is_set = getattr(cancellation, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        if callable(cancellation):
            return bool(cancellation())
        raise TTSConfigurationError(
            "Speech worker startup cancellation must be callable or Event-like"
        )

    def _json_worker_options(self) -> WorkerOptions:
        values: WorkerOptions = {}
        for key, value in self.worker_options.items():
            values[key] = str(value) if isinstance(value, Path) else value
        if self.name in {"moss-tts", "moss-tts-delay"}:
            values["generation_profile"] = self.generation_profile
        if self.name != "moss-tts-delay":
            values["runtime_directory"] = str(self.runtime_root)
        values["narrator_reference"] = (
            str(self.narrator_reference)
            if isinstance(self.narrator_reference, Path)
            else self.narrator_reference
        )
        return values

    def _read_messages(self, process: WorkerProcess) -> None:
        try:
            if process.stdout is None:
                raise TTSSynthesisError("Speech worker stdout is unavailable")
            while True:
                frame = _read_frame(process.stdout)
                if frame is None:
                    break
                document, payload = frame
                self._messages.put((process, document, payload))
        except Exception as error:
            self._messages.put(
                (process, {"type": "reader-error", "error": str(error)}, b"")
            )
        finally:
            self._messages.put((process, {"type": "eof"}, b""))

    def _read_stderr(self, process: WorkerProcess) -> None:
        if process.stderr is None:
            return
        for line in iter(process.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _send(self, process: WorkerProcess, document: Mapping[str, object]) -> None:
        if process is not self.process or process.poll() is not None:
            raise TTSSynthesisError(f"{self.name} isolated worker is not running")
        if process.stdin is None:
            raise TTSSynthesisError(f"{self.name} isolated worker stdin is unavailable")
        with self._send_lock:
            _write_frame(process.stdin, document)

    def _next_message(
        self, process: WorkerProcess, *, timeout: float = 0.1
    ) -> FrameDocument:
        while True:
            try:
                owner, document, _payload = self._messages.get(timeout=timeout)
            except queue.Empty as error:
                if process.poll() is not None:
                    details = "\n".join(self._stderr)
                    raise TTSSynthesisError(
                        f"{self.name} isolated worker exited unexpectedly"
                        + (f": {details}" if details else "")
                    ) from error
                raise
            if owner is process:
                return document

    def _next_frame(
        self, process: WorkerProcess, *, timeout: float = 0.1
    ) -> WorkerFrame:
        while True:
            try:
                owner, document, payload = self._messages.get(timeout=timeout)
            except queue.Empty as error:
                if process.poll() is not None:
                    details = "\n".join(self._stderr)
                    raise TTSSynthesisError(
                        f"{self.name} isolated worker exited unexpectedly"
                        + (f": {details}" if details else "")
                    ) from error
                raise
            if owner is process:
                return document, payload

    def _ensure_worker(self) -> WorkerProcess:
        if self.process is None or self.process.poll() is not None:
            self._start_worker()
        if self.process is None:
            raise TTSSynthesisError(f"{self.name} isolated worker is not running")
        return self.process

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream:
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError("Isolated backend received an invalid request")
        return SynthesisChunkStream(self._render_chunks(request))

    def _render_chunks(
        self, request: SynthesisRequest
    ) -> Generator[SynthesisChunk, None, SynthesisResult]:
        with self._request_lock:
            process = self._ensure_worker()
            request_id = uuid.uuid4().hex
            self._send(
                process,
                {
                    "type": "render",
                    "request_id": request_id,
                    "voice": request.voice,
                    "text": request.text,
                    "seed": request.seed,
                    "generation_profile": request.generation_profile,
                    "cache_policy": SynthesisCachePolicy(request.cache_policy).value,
                    "registry": _serialize_registry(self.registry),
                    "narrator_reference": (
                        str(self.narrator_reference)
                        if isinstance(self.narrator_reference, Path)
                        else self.narrator_reference
                    ),
                },
            )
            chunks: list[NDArray[np.float32]] = []
            while True:
                if self._stop_requested.is_set() or request.cancellation_requested():
                    self._terminate_process(process)
                    return self._cancelled_result(request, chunks)
                try:
                    document, payload = self._next_frame(process, timeout=0.05)
                except queue.Empty:
                    continue
                if document.get("request_id") not in {None, request_id}:
                    continue
                message_type = document.get("type")
                if message_type == "chunk":
                    pcm: NDArray[np.float32] = (
                        np.frombuffer(payload, dtype=np.float32)
                        .copy()
                        .reshape(_chunk_shape(document))
                    )
                    chunks.append(pcm)
                    yield SynthesisChunk(
                        pcm=pcm,
                        sample_rate=_required_integer(document, "sample_rate"),
                        index=_required_integer(document, "index"),
                        elapsed_ms=_required_float(document, "elapsed_ms"),
                    )
                elif message_type == "result":
                    result = _result_from_document(
                        _result_document_value(document["result"]), chunks
                    )
                    self._apply_result_metrics(result)
                    return result
                elif message_type in {"error", "fatal", "reader-error", "eof"}:
                    raise TTSSynthesisError(
                        document.get("error")
                        or f"{self.name} isolated worker stopped during render"
                    )

    def _cancelled_result(
        self, request: SynthesisRequest, chunks: Sequence[NDArray[np.float32]]
    ) -> SynthesisResult:
        pcm = (
            np.concatenate(chunks, axis=0)
            if chunks
            else np.empty((0, 1), dtype=np.float32)
        )
        return SynthesisResult(
            pcm=pcm,
            sample_rate=getattr(self, "sample_rate", 0),
            completion=SynthesisCompletion.CANCELLED,
            limits=SynthesisLimits(None, None),
            timing=SynthesisTiming(None, 0.0),
            diagnostics=SynthesisDiagnostics(
                self.name,
                "cancelled",
                request.generation_profile,
                request.seed,
                len(chunks),
                len(pcm),
            ),
        )

    def _apply_result_metrics(self, result: SynthesisResult) -> None:
        self.last_synthesis_ms = result.timing.first_chunk_ms
        self.last_audio_source = f"{self.name}:{result.diagnostics.cache_source}"
        self.last_generation_limited = result.completion is SynthesisCompletion.LIMITED

    def prepare_playback(self, character: str, text: str) -> PreparedPlayback:
        payload = RemotePreparedSpeech(
            voice=character,
            voice_key=normalize_character_name(character) or "narrator",
            text=" ".join(str(text).split()),
            generation_profile=self.generation_profile,
            cache_policy=SynthesisCachePolicy.USE,
        )
        return PreparedPlayback(payload, None, None, None, f"live:{self.name}")

    def prepare(self, character: str, text: str) -> RemotePreparedSpeech:
        payload = self.prepare_playback(character, text).payload
        if not isinstance(payload, RemotePreparedSpeech):
            raise TTSConfigurationError("Isolated backend produced invalid playback")
        return payload

    def synthesize(self, character: str, text: str) -> NDArray[np.float32]:
        return np.asarray(self.render(SynthesisRequest(character, text)).collect().pcm)

    def speak(
        self, character: str, text: str, *, playback_guard: PlaybackGuard = None
    ) -> bool:
        return bool(
            self.play_prepared(
                self.prepare_playback(character, text), playback_guard=playback_guard
            ).successful
        )

    def play_prepared(
        self, prepared: object, *, playback_guard: PlaybackGuard = None
    ) -> PlaybackOutcome:
        if not isinstance(prepared, PreparedPlayback) or not isinstance(
            prepared.payload, RemotePreparedSpeech
        ):
            raise TTSConfigurationError("Isolated backend received invalid playback")
        with self._playback_lock:
            if playback_guard is not None and not playback_guard():
                return outcome_for_prepared(prepared, PlaybackStatus.INTERRUPTED, None)
            self._stop_requested.clear()
            started = self.clock()
            rendered = self.render(
                SynthesisRequest(
                    voice=prepared.payload.voice,
                    text=prepared.payload.text,
                    generation_profile=prepared.payload.generation_profile,
                    cancellation=lambda: (
                        self._stop_requested.is_set()
                        or (playback_guard is not None and not playback_guard())
                    ),
                    cache_policy=prepared.payload.cache_policy,
                )
            )
            underflowed = False
            first_audio_ms: float | None = None
            interrupted = False
            try:
                first = next(rendered)
            except StopIteration:
                result = rendered.result
                return outcome_for_prepared(
                    prepared,
                    PlaybackStatus.INTERRUPTED,
                    (self.clock() - started) * 1000,
                    generation_limited=result.completion is SynthesisCompletion.LIMITED,
                )
            try:
                audio_output = self._resolve_audio_output()
                with audio_output.OutputStream(
                    samplerate=first.sample_rate,
                    channels=first.pcm.shape[1],
                    dtype="float32",
                    latency=self.playback_latency,
                ) as stream:
                    self._active_stream = stream
                    for chunk in chain((first,), rendered):
                        if self._stop_requested.is_set() or (
                            playback_guard is not None and not playback_guard()
                        ):
                            interrupted = True
                            self._terminate_process(self.process)
                            break
                        if first_audio_ms is None:
                            first_audio_ms = (self.clock() - started) * 1000
                        underflowed = (
                            bool(stream.write(self._prepare_audio(chunk.pcm)))
                            or underflowed
                        )
                if interrupted:
                    return outcome_for_prepared(
                        prepared,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                        underflowed=underflowed,
                        first_audio_ms=first_audio_ms,
                    )
                result = rendered.result
                completed = result.completion is not SynthesisCompletion.CANCELLED
                resolved = PreparedPlayback(
                    prepared.payload,
                    result.timing.first_chunk_ms,
                    first_audio_ms,
                    result.diagnostics.cache_source,
                    f"{self.name}:{result.diagnostics.cache_source}",
                )
                self.last_first_audio_ms = first_audio_ms
                self.last_playback_ms = (self.clock() - started) * 1000
                self.last_playback_underrun = underflowed
                return outcome_for_prepared(
                    resolved,
                    PlaybackStatus.COMPLETED
                    if completed
                    else PlaybackStatus.INTERRUPTED,
                    self.last_playback_ms,
                    underflowed=underflowed,
                    generation_limited=result.completion is SynthesisCompletion.LIMITED,
                    first_audio_ms=first_audio_ms,
                )
            except Exception as error:
                return outcome_for_prepared(
                    prepared,
                    (
                        PlaybackStatus.INTERRUPTED
                        if self._stop_requested.is_set()
                        else PlaybackStatus.FAILED
                    ),
                    (self.clock() - started) * 1000,
                    underflowed=underflowed,
                    error=None if self._stop_requested.is_set() else str(error),
                    error_type=None if self._stop_requested.is_set() else type(error),
                )
            finally:
                rendered.close()
                self._active_stream = None

    def prime(self, character: str) -> bool:
        return bool(self._request_value("prime", voice=character))

    def warm_up(
        self, *, progress: WarmupProgress | None = None, text: str = "Voice ready."
    ) -> int:
        del text
        progress = progress or (lambda _current, _total, _character: None)
        voices = sorted(
            self.registry.unique_voices(), key=lambda value: value.character.casefold()
        )
        characters = ["Narrator", *(value.character for value in voices)]
        for current, character in enumerate(characters, start=1):
            progress(current, len(characters), character)
            self.prime(character)
        return len(characters)

    def _request_value(self, command_type: str, **values: object) -> object:
        with self._request_lock:
            self._stop_requested.clear()
            process = self._ensure_worker()
            request_id = uuid.uuid4().hex
            self._send(
                process,
                {
                    "type": command_type,
                    "request_id": request_id,
                    "registry": _serialize_registry(self.registry),
                    "narrator_reference": (
                        str(self.narrator_reference)
                        if isinstance(self.narrator_reference, Path)
                        else self.narrator_reference
                    ),
                    **values,
                },
            )
            deadline = monotonic() + self.request_timeout
            while True:
                if self._closed or self._stop_requested.is_set():
                    self._terminate_process(process)
                    raise TTSSynthesisError(
                        f"{self.name} isolated worker request was cancelled"
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    raise TTSSynthesisError(
                        f"{self.name} isolated worker did not answer {command_type!r} "
                        f"within {self.request_timeout:g} seconds"
                    )
                try:
                    document, _payload = self._next_frame(
                        process,
                        timeout=min(0.1, remaining),
                    )
                except queue.Empty:
                    continue
                if document.get("request_id") != request_id:
                    continue
                if document.get("type") == "error":
                    raise TTSSynthesisError(_required_text(document, "error"))
                return document.get("value")

    def set_volume(self, volume: object) -> None:
        self.volume = validate_volume(volume)

    def set_speed(self, speed: object) -> None:
        self.speed = validate_speed(speed)

    def set_generation_profile(self, profile: object) -> bool:
        profile = (
            str(profile).strip().casefold()
            if self.name in {"moss-tts", "moss-tts-delay"}
            else "default"
        )
        changed = profile != self.generation_profile
        self.generation_profile = profile
        return changed

    def set_live_mode_active(self, active: bool) -> bool:
        if not active and (self.process is None or self.process.poll() is not None):
            return False
        return bool(self._request_value("set-live-mode", active=bool(active)))

    def set_narrator_voice(
        self, voice: CharacterVoice | None, fallback: str | Path | None = None
    ) -> None:
        self.narrator_reference = (
            voice.references[0]
            if voice is not None and voice.references
            else voice.speaker
            if voice is not None and self.name == "pocket-tts"
            else fallback or "alba"
            if self.name == "pocket-tts"
            else fallback
        )

    def clear_runtime_cache(self) -> None:
        self._terminate_process(self.process)

    def stop(self) -> bool:
        was_active = self._active_stream is not None or self._request_lock.locked()
        self._stop_requested.set()
        stream = self._active_stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        if self._request_lock.locked():
            self._terminate_process(self.process)
        return was_active

    def shutdown(self) -> None:
        self._closed = True
        self._stop_requested.set()
        process = self.process
        if process is None:
            if self._runtime_use is not None:
                self._runtime_use.close()
                self._runtime_use = None
            return
        try:
            self._send(process, {"type": "shutdown"})
            process.wait(timeout=2.0)
        except Exception:
            self._terminate_process(process)
        finally:
            if self.process is process:
                self.process = None
            if self._runtime_use is not None:
                self._runtime_use.close()
                self._runtime_use = None

    def _terminate_process(self, process: WorkerProcess | None) -> None:
        if process is None:
            return
        if self.process is process:
            self.process = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)

    def _resolve_audio_output(self) -> AudioOutput:
        audio_output = resolve_audio_output(self.audio_output)
        self.audio_output = audio_output
        return audio_output

    def _prepare_audio(self, audio: object) -> NDArray[np.float32]:
        prepared = np.asarray(audio, dtype=np.float32).copy()
        np.nan_to_num(prepared, copy=False)
        prepared *= self.volume
        np.clip(prepared, -0.95, 0.95, out=prepared)
        return prepared


class _RetainedWorkerLease:
    def __init__(self, backend: _RetainedBackend) -> None:
        self._backend = backend

    def shutdown(self) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._backend, name)


_isolated_backend_constructor: Callable[..., IsolatedSpeechBackend] = (
    IsolatedSpeechBackend
)


def _retained_isolated_factory(
    backend: str,
) -> Callable[..., _RetainedBackend]:
    def create(registry: CharacterVoiceRegistry, **options: object) -> _RetainedBackend:
        return _isolated_backend_constructor(backend, registry, **options)

    return create


class RetainedWorkerRuntime:
    """Keep one unchanged isolated worker alive across controller restarts."""

    supports_startup_cancellation = True
    supports_startup_progress = True

    def __init__(
        self,
        backend: str,
        *,
        backend_factory: Callable[..., _RetainedBackend] | None = None,
    ) -> None:
        self.backend = backend
        self.backend_factory: Callable[..., _RetainedBackend] = (
            backend_factory or _retained_isolated_factory(backend)
        )
        self._lock = threading.Lock()
        self._instance: _RetainedBackend | None = None
        self._identity: str | None = None

    def __call__(
        self, registry: CharacterVoiceRegistry, **options: object
    ) -> _RetainedWorkerLease:
        identity = self._configuration_identity(registry, options)
        with self._lock:
            instance = self._instance
            process = None if instance is None else instance.process
            dead = instance is not None and (
                process is None or process.poll() is not None
            )
            if instance is not None and (identity != self._identity or dead):
                instance.shutdown()
                instance = None
            if instance is None:
                instance = self.backend_factory(registry, **options)
                self._instance = instance
                self._identity = identity
            else:
                instance.registry = registry
                narrator_reference = options.get(
                    "narrator_reference", instance.narrator_reference
                )
                if narrator_reference is not None and not isinstance(
                    narrator_reference, (str, Path)
                ):
                    raise TTSConfigurationError(
                        "Speech worker narrator_reference must be text or a path"
                    )
                instance.narrator_reference = narrator_reference
                startup_cancellation = options.get("startup_cancellation")
                if not _is_cancellation(startup_cancellation):
                    raise TTSConfigurationError(
                        "Speech worker startup cancellation must be callable or Event-like"
                    )
                instance.startup_cancellation = startup_cancellation
                startup_progress = options.get("startup_progress")
                if not _is_startup_progress(startup_progress):
                    raise TTSConfigurationError(
                        "Speech worker startup progress is invalid"
                    )
                instance.startup_progress = startup_progress
                if "volume" in options:
                    instance.set_volume(options["volume"])
        return _RetainedWorkerLease(instance)

    def shutdown(self) -> None:
        with self._lock:
            instance, self._instance = self._instance, None
            self._identity = None
        if instance is not None:
            instance.shutdown()

    @staticmethod
    def _configuration_identity(
        registry: CharacterVoiceRegistry, options: Mapping[str, object]
    ) -> str:
        stable_options = {
            key: _worker_option_identity(value)
            for key, value in options.items()
            if key not in {"startup_cancellation", "startup_progress", "volume"}
        }
        return json.dumps(
            [_serialize_registry(registry), stable_options],
            sort_keys=True,
            separators=(",", ":"),
        )


def _worker_option_identity(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    return {"object_id": id(value)}


def create_pocket_worker_backend(
    registry: CharacterVoiceRegistry, **options: object
) -> IsolatedSpeechBackend:
    return _isolated_backend_constructor("pocket-tts", registry, **options)


def create_chatterbox_worker_backend(
    registry: CharacterVoiceRegistry, **options: object
) -> IsolatedSpeechBackend:
    return _isolated_backend_constructor("chatterbox-nano", registry, **options)


def create_moss_worker_backend(
    registry: CharacterVoiceRegistry, **options: object
) -> object:
    from vntts.moss_cpp_backend import MossCppVoiceRouterBackend, moss_cpp_requested

    if moss_cpp_requested(options.get("model_name")):
        options.pop("allow_gated_model_access", None)
        return MossCppVoiceRouterBackend(registry, **options)
    return _isolated_backend_constructor("moss-tts", registry, **options)


def create_moss_delay_worker_backend(
    registry: CharacterVoiceRegistry, **options: object
) -> IsolatedSpeechBackend:
    return _isolated_backend_constructor("moss-tts-delay", registry, **options)


for _factory in (
    create_pocket_worker_backend,
    create_chatterbox_worker_backend,
    create_moss_worker_backend,
    create_moss_delay_worker_backend,
):
    setattr(_factory, "supports_startup_cancellation", True)
    setattr(_factory, "supports_startup_progress", True)
