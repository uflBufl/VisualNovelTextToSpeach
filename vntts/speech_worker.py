"""Isolated speech-model workers with typed framed PCM streaming."""

from __future__ import annotations

import importlib
import json
import os
import queue
import site
import struct
import subprocess
import sys
import threading
import uuid
from collections import deque
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from itertools import chain
from pathlib import Path
from time import monotonic

import numpy as np

from vntts.playback import PlaybackStatus, PreparedPlayback, outcome_for_prepared
from vntts.speech_backend import (
    ChatterboxNanoVoiceRouterBackend,
    MossTTSVoiceRouterBackend,
    PocketTTSVoiceRouterBackend,
    TTSConfigurationError,
    TTSSynthesisError,
    validate_speed,
    validate_volume,
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

_FRAME_LENGTH = struct.Struct(">I")
_BOOTSTRAP = (
    "import json,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "[sys.path.append(p) for p in json.loads(sys.argv.pop(1))];"
    "from vntts.speech_worker import worker_main;"
    "raise SystemExit(worker_main())"
)
_REQUIRED_MODULES = {
    "pocket-tts": ("numpy", "torch"),
    "chatterbox-nano": ("numpy", "torch", "transformers"),
    "moss-tts": ("numpy", "mlx.core"),
}
_BACKEND_CLASSES = {
    "pocket-tts": PocketTTSVoiceRouterBackend,
    "chatterbox-nano": ChatterboxNanoVoiceRouterBackend,
    "moss-tts": MossTTSVoiceRouterBackend,
}
_CAPABILITIES = {
    "pocket-tts": PocketTTSVoiceRouterBackend.capabilities,
    "chatterbox-nano": ChatterboxNanoVoiceRouterBackend.capabilities,
    "moss-tts": MossTTSVoiceRouterBackend.capabilities,
}


@dataclass(frozen=True)
class RemotePreparedSpeech:
    voice: str
    voice_key: str
    text: str
    generation_profile: str
    cache_policy: SynthesisCachePolicy


def _write_frame(stream, document, payload=b""):
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


def _read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream):
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
    payload_size = document.pop("payload_bytes", 0)
    if not isinstance(payload_size, int) or not 0 <= payload_size <= 512_000_000:
        raise ValueError("Speech worker emitted an invalid payload size")
    payload = _read_exact(stream, payload_size)
    if payload is None:
        raise EOFError("Speech worker frame payload was truncated")
    return document, payload


def _runtime_paths(backend, runtime_directory=None):
    if backend not in _BACKEND_CLASSES:
        raise TTSConfigurationError(f"Unsupported isolated backend: {backend!r}")
    configured = {
        "pocket-tts": "VNTTS_POCKET_TTS_RUNTIME",
        "chatterbox-nano": "VNTTS_CHATTERBOX_RUNTIME",
        "moss-tts": "VNTTS_MOSS_RUNTIME",
    }[backend]
    folder = {
        "pocket-tts": "pocket-tts",
        "chatterbox-nano": "chatterbox-nano",
        "moss-tts": "moss-tts",
    }[backend]
    root = (
        Path(
            runtime_directory
            or os.environ.get(configured, "")
            or Path(__file__).resolve().parents[1] / "backends" / folder / ".venv"
        )
        .expanduser()
        .resolve()
    )
    interpreter = root / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    if sys.platform == "win32":
        site_packages_candidates = (root / "Lib/site-packages",)
    else:
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
        raise TTSConfigurationError(
            f"{backend} isolated runtime is unavailable at {root}. Run `uv sync "
            f"--project backends/{folder}`, then restart the app."
        )
    return root, interpreter, site_packages.resolve()


def _support_paths():
    import vntts_artifacts

    values = [Path(vntts_artifacts.__file__).resolve().parents[1]]
    values.extend(Path(value).resolve() for value in site.getsitepackages())
    return tuple(dict.fromkeys(str(value) for value in values if value.is_dir()))


def _serialize_registry(registry):
    voices = []
    for voice in registry.unique_voices():
        voices.append(
            {
                "character": voice.character,
                "speaker": voice.speaker,
                "aliases": list(voice.aliases),
                "references": [str(value) for value in voice.references],
            }
        )
    assignments = {}
    for character, voice in registry.assignments.items():
        assignments[character] = (
            None
            if voice is None
            else {
                "character": voice.character,
                "speaker": voice.speaker,
                "aliases": list(voice.aliases),
                "references": [str(value) for value in voice.references],
            }
        )
    return {"voices": voices, "assignments": assignments}


def _voice_from_document(value):
    references = tuple(
        Path(item).expanduser().resolve() for item in value["references"]
    )
    return CharacterVoice(
        character=value["character"],
        speaker=value["speaker"],
        aliases=tuple(value.get("aliases", ())),
        references=references,
    )


def _registry_from_document(document):
    registry = CharacterVoiceRegistry(
        _voice_from_document(value) for value in document.get("voices", ())
    )
    registry.assignments = {
        character: None if value is None else _voice_from_document(value)
        for character, value in document.get("assignments", {}).items()
    }
    return registry


def _result_document(result):
    return {
        "sample_rate": result.sample_rate,
        "completion": result.completion.value,
        "limits": asdict(result.limits),
        "timing": asdict(result.timing),
        "diagnostics": asdict(result.diagnostics),
    }


def _result_from_document(document, chunks):
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


def _module_health(runtime_site, names):
    runtime_site = Path(runtime_site).resolve()
    modules = {}
    for name in names:
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
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


def worker_main(
    *,
    input_stream=None,
    output_stream=None,
    backend_classes=None,
    required_modules=None,
):
    protocol_out = output_stream or sys.stdout.buffer
    protocol_in = input_stream or sys.stdin.buffer
    backend_classes = backend_classes or _BACKEND_CLASSES
    required_modules = required_modules or _REQUIRED_MODULES
    try:
        initialized = _read_frame(protocol_in)
        if initialized is None:
            return 2
        document, _payload = initialized
        if document.get("type") != "initialize":
            raise TTSConfigurationError("Speech worker expected initialization")
        backend_name = document["backend"]
        runtime_site = Path(document["runtime_site"]).resolve()
        modules = _module_health(runtime_site, required_modules[backend_name])
        registry = _registry_from_document(document["registry"])
        options = dict(document.get("options", {}))
        for key, value in tuple(options.items()):
            if key.endswith("_directory") and value is not None:
                options[key] = Path(value).expanduser().resolve()
        with redirect_stdout(sys.stderr):
            backend = backend_classes[backend_name](registry, **options)
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
                backend.registry = _registry_from_document(command["registry"])
                narrator_reference = command.get("narrator_reference")
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
                        voice=command["voice"],
                        text=command["text"],
                        seed=command.get("seed"),
                        generation_profile=command["generation_profile"],
                        cache_policy=SynthesisCachePolicy(command["cache_policy"]),
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
                        primed = backend.prime(command["voice"])
                    _write_frame(
                        protocol_out,
                        {"type": "primed", "request_id": request_id, "value": primed},
                    )
                elif command_type == "set-live-mode":
                    value = backend.set_live_mode_active(command["active"])
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
        backend,
        registry,
        *,
        narrator_reference=None,
        volume=1.0,
        audio_output=None,
        clock=monotonic,
        runtime_directory=None,
        process_factory=subprocess.Popen,
        startup_timeout=1800.0,
        startup_cancellation=None,
        playback_latency=None,
        generation_profile=None,
        **worker_options,
    ):
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
        }[backend]
        self.capabilities = _CAPABILITIES[backend]
        self.generation_profile = generation_profile or (
            "stable" if backend == "moss-tts" else "default"
        )
        self.model_name = str(worker_options.get("model_name") or backend)
        self.audio_output = audio_output
        self.clock = clock
        self.process_factory = process_factory
        self.startup_timeout = float(startup_timeout)
        self.startup_cancellation = startup_cancellation
        self.playback_latency = playback_latency or (
            "high" if backend == "chatterbox-nano" else "low"
        )
        self.worker_options = worker_options
        self.runtime_root, self.interpreter, self.runtime_site = _runtime_paths(
            backend, runtime_directory
        )
        self.project_root = Path(__file__).resolve().parents[1]
        self.process = None
        self.health = None
        self._messages = queue.Queue()
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._playback_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._active_stream = None
        self._stderr = deque(maxlen=40)
        self.last_synthesis_ms = None
        self.last_first_audio_ms = None
        self.last_playback_ms = None
        self.last_playback_underrun = False
        self.last_generation_limited = False
        self.last_audio_source = None
        self._closed = False
        self.set_volume(volume)
        self.set_speed(1.0)
        self._start_worker()

    def _start_worker(self):
        if self._closed:
            raise TTSSynthesisError(f"{self.name} isolated worker is shut down")
        command = [
            str(self.interpreter),
            "-I",
            "-u",
            "-c",
            _BOOTSTRAP,
            str(self.project_root),
            json.dumps(_support_paths()),
        ]
        environment = dict(os.environ)
        environment["PYTHONNOUSERSITE"] = "1"
        process = self.process_factory(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.project_root),
            env=environment,
            bufsize=0,
        )
        self.process = process
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
        if Path(message["interpreter"]).resolve() != self.interpreter.resolve():
            self._terminate_process(process)
            raise TTSConfigurationError("Speech worker used an unexpected interpreter")
        if Path(message["prefix"]).resolve() != self.runtime_root:
            self._terminate_process(process)
            raise TTSConfigurationError("Speech worker used an unexpected environment")
        self.health = message
        self.sample_rate = int(message["sample_rate"])

    def _startup_cancelled(self):
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

    def _json_worker_options(self):
        values = {}
        for key, value in self.worker_options.items():
            values[key] = str(value) if isinstance(value, Path) else value
        if self.name == "moss-tts":
            values["generation_profile"] = self.generation_profile
        values["narrator_reference"] = (
            str(self.narrator_reference)
            if isinstance(self.narrator_reference, Path)
            else self.narrator_reference
        )
        return values

    def _read_messages(self, process):
        try:
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

    def _read_stderr(self, process):
        for line in iter(process.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def _send(self, process, document):
        if process is not self.process or process.poll() is not None:
            raise TTSSynthesisError(f"{self.name} isolated worker is not running")
        with self._send_lock:
            _write_frame(process.stdin, document)

    def _next_message(self, process, *, timeout=0.1):
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

    def _next_frame(self, process, *, timeout=0.1):
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

    def _ensure_worker(self):
        if self.process is None or self.process.poll() is not None:
            self._start_worker()
        return self.process

    def render(self, request):
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError("Isolated backend received an invalid request")
        return SynthesisChunkStream(self._render_chunks(request))

    def _render_chunks(self, request):
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
            chunks = []
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
                    pcm = (
                        np.frombuffer(payload, dtype=np.float32)
                        .copy()
                        .reshape(document["shape"])
                    )
                    chunks.append(pcm)
                    yield SynthesisChunk(
                        pcm=pcm,
                        sample_rate=document["sample_rate"],
                        index=document["index"],
                        elapsed_ms=document["elapsed_ms"],
                    )
                elif message_type == "result":
                    result = _result_from_document(document["result"], chunks)
                    self._apply_result_metrics(result)
                    return result
                elif message_type in {"error", "fatal", "reader-error", "eof"}:
                    raise TTSSynthesisError(
                        document.get("error")
                        or f"{self.name} isolated worker stopped during render"
                    )

    def _cancelled_result(self, request, chunks):
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

    def _apply_result_metrics(self, result):
        self.last_synthesis_ms = result.timing.first_chunk_ms
        self.last_audio_source = f"{self.name}:{result.diagnostics.cache_source}"
        self.last_generation_limited = result.completion is SynthesisCompletion.LIMITED

    def prepare_playback(self, character, text):
        payload = RemotePreparedSpeech(
            voice=character,
            voice_key=normalize_character_name(character) or "narrator",
            text=" ".join(str(text).split()),
            generation_profile=self.generation_profile,
            cache_policy=SynthesisCachePolicy.USE,
        )
        return PreparedPlayback(payload, None, None, None, f"live:{self.name}")

    def prepare(self, character, text):
        return self.prepare_playback(character, text).payload

    def synthesize(self, character, text):
        return (
            self.render(
                SynthesisRequest(
                    voice=character,
                    text=text,
                    generation_profile=self.generation_profile,
                )
            )
            .collect()
            .pcm.reshape(-1)
        )

    def speak(self, character, text, *, playback_guard=None):
        return self.play_prepared(
            self.prepare_playback(character, text), playback_guard=playback_guard
        ).successful

    def play_prepared(self, prepared, *, playback_guard=None):
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
            first_audio_ms = None
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
                        underflowed = bool(
                            underflowed or stream.write(self._prepare_audio(chunk.pcm))
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

    def prime(self, character):
        return self._request_value("prime", voice=character)

    def warm_up(self, *, progress=None, text="Voice ready."):
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

    def _request_value(self, command_type, **values):
        with self._request_lock:
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
            while True:
                document, _payload = self._next_frame(process, timeout=0.1)
                if document.get("request_id") != request_id:
                    continue
                if document.get("type") == "error":
                    raise TTSSynthesisError(document["error"])
                return document.get("value")

    def set_volume(self, volume):
        self.volume = validate_volume(volume)

    def set_speed(self, speed):
        self.speed = validate_speed(speed)

    def set_generation_profile(self, profile):
        profile = str(profile).strip().casefold()
        changed = profile != self.generation_profile
        self.generation_profile = profile
        return changed

    def set_live_mode_active(self, active):
        if not active and (self.process is None or self.process.poll() is not None):
            return False
        return bool(self._request_value("set-live-mode", active=bool(active)))

    def set_narrator_voice(self, voice, fallback=None):
        self.narrator_reference = (
            voice.references[0]
            if voice is not None and voice.references
            else voice.speaker
            if voice is not None and self.name == "pocket-tts"
            else fallback or "alba"
            if self.name == "pocket-tts"
            else fallback
        )

    def clear_runtime_cache(self):
        self._terminate_process(self.process)

    def stop(self):
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

    def shutdown(self):
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            self._send(process, {"type": "shutdown"})
            process.wait(timeout=2.0)
        except Exception:
            self._terminate_process(process)
        finally:
            if self.process is process:
                self.process = None

    def _terminate_process(self, process):
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

    def _resolve_audio_output(self):
        if self.audio_output is None:
            import sounddevice

            self.audio_output = sounddevice
        return self.audio_output

    def _prepare_audio(self, audio):
        prepared = np.asarray(audio, dtype=np.float32).copy()
        np.nan_to_num(prepared, copy=False)
        prepared *= self.volume
        np.clip(prepared, -0.95, 0.95, out=prepared)
        return prepared


def create_pocket_worker_backend(registry, **options):
    return IsolatedSpeechBackend("pocket-tts", registry, **options)


def create_chatterbox_worker_backend(registry, **options):
    return IsolatedSpeechBackend("chatterbox-nano", registry, **options)


def create_moss_worker_backend(registry, **options):
    return IsolatedSpeechBackend("moss-tts", registry, **options)


for _factory in (
    create_pocket_worker_backend,
    create_chatterbox_worker_backend,
    create_moss_worker_backend,
):
    _factory.supports_startup_cancellation = True
