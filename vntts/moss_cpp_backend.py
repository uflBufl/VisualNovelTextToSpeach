"""Run the same MOSS Local v1.5 checkpoint in an owned openmoss C++ server."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import io
import json
import math
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import wave
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
from types import SimpleNamespace
from typing import BinaryIO, Protocol, TypeAlias, TypedDict, TypeGuard

import numpy as np
import soundfile as sf

from vntts.audio_output import AudioOutput
from vntts.native_resources import NativeResourceSampler, NativeResourceSnapshot
from vntts.playback import PreparedPlayback
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.speech_backend import (
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
    SpeechBackendCapabilities,
    moss_tts_generation_profiles,
)
from vntts.speech_backend_runtime import _source_identity
from vntts.support import native_speech_context, record_native_speech
from vntts.synthesis import (
    SynthesisChunk,
    SynthesisCompletion,
    SynthesisRequest,
    SynthesisResult,
)
from vntts.voices import CharacterVoiceRegistry

NATIVE_GENERATION_CONTRACT = "nonzero-seed-stable-1.7-v2"
_MANAGED_STARTUP_FAILURE_PREFIX = "VNTTS_STARTUP_FAILURE_JSON="
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002
_PROCESS_ASSIGN_JOB = 0x0101


class _CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


Cancellation: TypeAlias = Callable[[], bool] | _CancellationSignal
ProgressCallback: TypeAlias = Callable[[str], object]


if sys.platform == "win32":

    class _WindowsKillOnCloseJob:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            from ctypes import wintypes

            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("per_process_time", ctypes.c_longlong),
                    ("per_job_time", ctypes.c_longlong),
                    ("limit_flags", wintypes.DWORD),
                    ("minimum_working_set", ctypes.c_size_t),
                    ("maximum_working_set", ctypes.c_size_t),
                    ("active_process_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority_class", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("read_operations", ctypes.c_ulonglong),
                    ("write_operations", ctypes.c_ulonglong),
                    ("other_operations", ctypes.c_ulonglong),
                    ("read_bytes", ctypes.c_ulonglong),
                    ("write_bytes", ctypes.c_ulonglong),
                    ("other_bytes", ctypes.c_ulonglong),
                ]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("basic", BasicLimits),
                    ("io", IoCounters),
                    ("process_memory_limit", ctypes.c_size_t),
                    ("job_memory_limit", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t),
                    ("peak_job_memory", ctypes.c_size_t),
                ]

            class ThreadEntry(ctypes.Structure):
                _fields_ = [
                    ("size", wintypes.DWORD),
                    ("usage", wintypes.DWORD),
                    ("thread_id", wintypes.DWORD),
                    ("owner_process_id", wintypes.DWORD),
                    ("base_priority", wintypes.LONG),
                    ("priority_delta", wintypes.LONG),
                    ("flags", wintypes.DWORD),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CreateToolhelp32Snapshot.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Thread32First.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ThreadEntry),
            ]
            kernel32.Thread32First.restype = wintypes.BOOL
            kernel32.Thread32Next.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ThreadEntry),
            ]
            kernel32.Thread32Next.restype = wintypes.BOOL
            kernel32.OpenThread.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenThread.restype = wintypes.HANDLE
            kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
            kernel32.ResumeThread.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._kernel32 = kernel32
            self._handle = handle
            limits = ExtendedLimits()
            limits.basic.limit_flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            try:
                if not kernel32.SetInformationJobObject(
                    handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                process_handle = kernel32.OpenProcess(
                    _PROCESS_ASSIGN_JOB, False, process.pid
                )
                if not process_handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if not kernel32.AssignProcessToJobObject(handle, process_handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel32.CloseHandle(process_handle)
                snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
                if snapshot == wintypes.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    entry = ThreadEntry(size=ctypes.sizeof(ThreadEntry))
                    found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
                    while found and entry.owner_process_id != process.pid:
                        found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
                    if not found:
                        raise RuntimeError(
                            "Unable to find the suspended MOSS process thread"
                        )
                    thread = kernel32.OpenThread(
                        _THREAD_SUSPEND_RESUME, False, entry.thread_id
                    )
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        kernel32.CloseHandle(thread)
                finally:
                    kernel32.CloseHandle(snapshot)
            except BaseException:
                self.close()
                raise

        def close(self) -> None:
            if self._handle is not None:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None


else:

    class _WindowsKillOnCloseJob:
        def __init__(self, process: subprocess.Popen[bytes]) -> None:
            raise RuntimeError("Windows Job Objects require Windows")

        def close(self) -> None:
            pass


def _launch_owned_process(
    command: list[str],
    *,
    stdin: BinaryIO | int | None,
    stdout: BinaryIO | int | None,
    stderr: BinaryIO | int | None,
    cwd: str | Path | None,
    creationflags: int,
) -> tuple[subprocess.Popen[bytes], _WindowsKillOnCloseJob | None]:
    if sys.platform != "win32":
        return (
            subprocess.Popen(
                command,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                creationflags=creationflags,
            ),
            None,
        )
    process = subprocess.Popen(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        cwd=cwd,
        creationflags=creationflags | _CREATE_SUSPENDED,
    )
    try:
        return process, _WindowsKillOnCloseJob(process)
    except BaseException:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # ponytail: the process already received the strongest local signal.
            pass
        raise


class NativeControls(TypedDict):
    gpu_layers: int
    aux_cpu: int
    local_gpu: bool
    aux_cpu_threads: int | None


class MossCppBaseOptions(TypedDict):
    narrator_reference: str | Path | None
    language: object
    volume: int | float
    audio_output: AudioOutput | None
    clock: Callable[[], float]
    audio_cache_size: int
    playback_latency: object
    runtime_directory: str | Path | None
    prompt_cache_directory: str | Path | None
    persistent_audio_cache_directory: str | Path | None
    persistent_audio_cache_max_entries: int | None
    prompt_code_loader: Callable[[Path], object] | None
    prompt_code_saver: Callable[[object, Path], object] | None
    array_evaluator: Callable[[object], object] | None
    cached_stream_chunk_seconds: float
    streaming_first_chunk_frames: int
    streaming_interval: float
    generation_profile: object
    playback_consumer_join_timeout: float


NativeHeaders: TypeAlias = dict[str, str]
MossCppStartupOptions: TypeAlias = tuple[
    str | Path | None,
    Cancellation | None,
    ProgressCallback | None,
    float,
    float,
    bool,
]


def _cancelled(cancellation: Cancellation | None) -> bool:
    if cancellation is None:
        return False
    return bool(cancellation() if callable(cancellation) else cancellation.is_set())


def _path_option(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError("expected str or os.PathLike object")
    return Path(value)


def _reference_audio_path(value: object) -> Path:
    try:
        path = _path_option(value)
    except TypeError as error:
        raise TTSConfigurationError(
            "MOSS C++ reference audio path is invalid"
        ) from error
    if not path.is_file():
        raise TTSConfigurationError(f"MOSS C++ reference audio is missing: {path}")
    return path


def _option_path(value: object, name: str) -> str | Path | None:
    if value is None or isinstance(value, (str, Path)):
        return value
    raise TTSConfigurationError(f"MOSS C++ {name} must be text or a path")


def _option_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TTSConfigurationError(f"MOSS C++ {name} must be numeric")
    return float(value)


def _option_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TTSConfigurationError(f"MOSS C++ {name} must be an integer")
    return value


def _is_callback(value: object) -> TypeGuard[Callable[..., object]]:
    return callable(value)


def _is_cancellation(value: object) -> TypeGuard[Cancellation]:
    return callable(value) or callable(getattr(value, "is_set", None))


def _option_cancellation(value: object) -> Cancellation | None:
    if value is None:
        return None
    if not _is_cancellation(value):
        raise TTSConfigurationError(
            "MOSS C++ startup cancellation must be callable or Event-like"
        )
    return value


def _is_progress(value: object) -> TypeGuard[ProgressCallback]:
    return callable(value)


def _option_progress(value: object) -> ProgressCallback | None:
    if value is None:
        return None
    if not _is_progress(value):
        raise TTSConfigurationError("MOSS C++ startup progress must be callable")
    return value


def _startup_options(
    model_name: object,
    startup_cancellation: object,
    startup_progress: object,
    startup_timeout: object,
    request_timeout: object,
    allow_download: object,
) -> MossCppStartupOptions:
    checked_model_name = _option_path(model_name, "model_name")
    checked_cancellation = _option_cancellation(startup_cancellation)
    checked_progress = _option_progress(startup_progress)
    checked_startup_timeout = _option_number(startup_timeout, "startup_timeout")
    checked_request_timeout = _option_number(request_timeout, "request_timeout")
    if not isinstance(allow_download, bool):
        raise TTSConfigurationError("MOSS C++ allow_download must be boolean")
    return (
        checked_model_name,
        checked_cancellation,
        checked_progress,
        checked_startup_timeout,
        checked_request_timeout,
        allow_download,
    )


def _option_callback(value: object, name: str) -> Callable[..., object] | None:
    if value is None:
        return None
    if not _is_callback(value):
        raise TTSConfigurationError(f"MOSS C++ {name} must be callable")
    return value


def _is_audio_output(value: object) -> TypeGuard[AudioOutput]:
    return all(
        callable(getattr(value, name, None))
        for name in (
            "get_stream",
            "query_devices",
            "play",
            "wait",
            "stop",
            "OutputStream",
        )
    )


def _option_audio_output(value: object) -> AudioOutput | None:
    if value is None:
        return None
    if not _is_audio_output(value):
        raise TTSConfigurationError("MOSS C++ audio_output is invalid")
    return value


def _is_clock(value: object) -> TypeGuard[Callable[[], float]]:
    return callable(value)


def _required_option_integer(value: object, name: str) -> int:
    result = _option_integer(value, name)
    if result is None:
        raise TTSConfigurationError(f"MOSS C++ {name} must be an integer")
    return result


def _base_options(options: dict[str, object]) -> MossCppBaseOptions:
    if "model_factory" in options:
        raise TypeError("MOSS C++ owns its native model factory")
    clock = options.pop("clock", monotonic)
    if not _is_clock(clock):
        raise TTSConfigurationError("MOSS C++ clock must be callable")
    prompt_loader = _option_callback(
        options.pop("prompt_code_loader", None), "prompt_code_loader"
    )
    prompt_saver = _option_callback(
        options.pop("prompt_code_saver", None), "prompt_code_saver"
    )
    array_evaluator = _option_callback(
        options.pop("array_evaluator", None), "array_evaluator"
    )
    result: MossCppBaseOptions = {
        "narrator_reference": _option_path(
            options.pop("narrator_reference", None), "narrator_reference"
        ),
        "language": options.pop("language", "English"),
        "volume": _option_number(options.pop("volume", 1.0), "volume"),
        "audio_output": _option_audio_output(options.pop("audio_output", None)),
        "clock": clock,
        "audio_cache_size": _required_option_integer(
            options.pop("audio_cache_size", 32), "audio_cache_size"
        ),
        "playback_latency": options.pop("playback_latency", "low"),
        "runtime_directory": _option_path(
            options.pop("runtime_directory", None), "runtime_directory"
        ),
        "prompt_cache_directory": _option_path(
            options.pop("prompt_cache_directory", None), "prompt_cache_directory"
        ),
        "persistent_audio_cache_directory": _option_path(
            options.pop("persistent_audio_cache_directory", None),
            "persistent_audio_cache_directory",
        ),
        "persistent_audio_cache_max_entries": _option_integer(
            options.pop("persistent_audio_cache_max_entries", None),
            "persistent_audio_cache_max_entries",
        ),
        "prompt_code_loader": prompt_loader,
        "prompt_code_saver": prompt_saver,
        "array_evaluator": array_evaluator,
        "cached_stream_chunk_seconds": _option_number(
            options.pop("cached_stream_chunk_seconds", 0.2),
            "cached_stream_chunk_seconds",
        ),
        "streaming_first_chunk_frames": _required_option_integer(
            options.pop("streaming_first_chunk_frames", 4),
            "streaming_first_chunk_frames",
        ),
        "streaming_interval": _option_number(
            options.pop("streaming_interval", 0.25), "streaming_interval"
        ),
        "generation_profile": options.pop("generation_profile", "stable"),
        "playback_consumer_join_timeout": _option_number(
            options.pop("playback_consumer_join_timeout", 5.0),
            "playback_consumer_join_timeout",
        ),
    }
    if options:
        raise TypeError(f"Unexpected MOSS C++ option: {next(iter(options))}")
    return result


def _aux_cpu_workers(logical_count: object | None = None) -> int:
    """Keep the native auxiliary work small enough to leave the game a core."""
    try:
        raw_count = os.cpu_count() if logical_count is None else logical_count
        if not isinstance(raw_count, (str, bytes, bytearray, int, float)):
            raise TypeError
        count = int(raw_count)
    except TypeError, ValueError:
        count = 1
    if count < 4:
        return 1
    if count < 8:
        return 2
    if count < 16:
        return 4
    return 8


def _managed_runtime(model_name: object | None) -> bool:
    """Explicit server/model paths are advanced integrations, never guessed."""
    if os.environ.get("VNTTS_MOSS_QUALIFY_ADAPTIVE") == "1":
        return True
    return not (
        os.environ.get("VNTTS_MOSS_CPP_EXECUTABLE")
        or os.environ.get("VNTTS_MOSS_GGUF")
        or str(model_name or "").lower().endswith(".gguf")
    )


def _startup_failure_category(path: str | Path | None) -> str | None:
    """Read the native machine-readable startup category, never free-form logs."""
    if path is None:
        return None
    try:
        with open(path, "rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 64 * 1024))
            output = log.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(output.splitlines()):
        if not line.startswith(_MANAGED_STARTUP_FAILURE_PREFIX):
            continue
        try:
            category = json.loads(
                line.removeprefix(_MANAGED_STARTUP_FAILURE_PREFIX)
            ).get("category")
        except AttributeError, json.JSONDecodeError:
            return None
        return category if isinstance(category, str) else None
    return None


def _diagnostic_file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _normalize_reference_audio(
    path: str | Path | BinaryIO | SpooledTemporaryFile[bytes],
) -> tuple[bytes, float, int, int]:
    with sf.SoundFile(path) as reference:
        duration = round(reference.frames / reference.samplerate, 6)
        sample_rate = reference.samplerate
        channels = reference.channels
        if channels not in {1, 2} or not 8000 <= sample_rate <= 192000:
            raise TTSConfigurationError(
                "MOSS C++ reference must be mono/stereo audio at 8-192 kHz"
            )
        if reference.frames > sample_rate * 120:
            raise TTSConfigurationError(
                "MOSS C++ reference must be at most 120 seconds"
            )
        audio = reference.read(dtype="float32", always_2d=True)
        if audio.size == 0 or not np.isfinite(audio).all():
            raise TTSConfigurationError("MOSS C++ reference contains invalid audio")
        wav = io.BytesIO()
        sf.write(wav, audio, sample_rate, format="WAV", subtype="PCM_16")
    return wav.getvalue(), duration, sample_rate, channels


def _native_stage_timings(
    path: str | Path | None,
    offset: int,
    headers: Mapping[str, str],
    maximum_seconds: float,
) -> dict[str, str | float | None]:
    """Read only this request's bounded log tail; never export native log text."""
    output = ""
    complete_log = False
    if path is not None:
        try:
            with open(path, "rb") as log:
                log.seek(0, os.SEEK_END)
                complete_log = log.tell() - offset <= 64 * 1024
                log.seek(max(offset, log.tell() - 64 * 1024))
                output = log.read(64 * 1024).decode("utf-8", errors="replace")
        except OSError:
            pass

    def seconds(pattern: str | None = None, header: str | None = None) -> float | None:
        match = re.search(pattern, output) if pattern else None
        raw = headers.get(header) if header else None
        if raw is None and match:
            raw = match[1]
        if raw is None:
            return None
        try:
            value = float(raw)
        except TypeError, ValueError:
            return None
        return (
            round(value, 6)
            if math.isfinite(value) and 0 <= value <= maximum_seconds
            else None
        )

    reference_s = seconds(
        r"(?:voice '[^'\r\n]+' encoded: \d+ frames|encoded reference: \d+ frames \([^\r\n]*?\)) in ([\d.]+)s"
    )
    return {
        "native_error_hint": next(
            (
                hint
                for hint, pattern in (
                    ("allocation-error", r"out.of.memory|bad_alloc|failed to allocate"),
                    ("device-lost", r"VK_ERROR_DEVICE_LOST|ErrorDeviceLost"),
                    ("model-load-error", r"invalid GGUF|failed to load model"),
                )
                if re.search(pattern, output, re.IGNORECASE)
            ),
            None,
        ),
        "reference": (
            "encoded"
            if reference_s is not None
            else "cached-codes"
            if complete_log
            and any(
                marker in output for marker in ("(cached codes)", "(persistent codes)")
            )
            else "unavailable"
        ),
        "reference_encoding_s": reference_s,
        "prefill_s": seconds(r"prefill done in ([\d.]+)s"),
        "gen_s": seconds(
            r"generated \d+ steps in ([\d.]+)s", "x-moss-generate-seconds"
        ),
        "decode_s": seconds(
            r"codec decode produced [^\r\n]* in ([\d.]+)s", "x-moss-decode-seconds"
        ),
        # Optional fields from the opt-in timing build, not stock openmoss.
        "gen_backbone_s": seconds(header="x-moss-backbone-seconds"),
        "gen_frame_decoder_s": seconds(header="x-moss-frame-decoder-seconds"),
        "gen_input_embedding_s": seconds(header="x-moss-input-embedding-seconds"),
    }


def moss_cpp_requested(model_name: object | None = None) -> bool:
    return bool(
        sys.platform != "darwin"
        or platform.machine().casefold() != "arm64"
        or os.environ.get("VNTTS_MOSS_CPP_EXECUTABLE")
        or os.environ.get("VNTTS_MOSS_GGUF")
        or str(model_name or "").lower().endswith(".gguf")
    )


def moss_cpp_paths(model_name: str | Path | None = None) -> tuple[Path, Path, Path]:
    from vntts.moss_cpp_installation import configured_paths

    executable, model, sidecar = configured_paths(model_name)
    problems = []
    if not executable.is_file():
        problems.append(f"Native server executable is missing: {executable}")
    if not model.is_file():
        problems.append(f"Model GGUF is missing: {model}")
    if model.suffix.lower() != ".gguf":
        problems.append(f"Model must be a .gguf file, not a model directory: {model}")
    if not sidecar.is_file():
        problems.append(f"Audio sidecar is missing: {sidecar}")
    if problems:
        raise TTSConfigurationError(
            "MOSS C++/GGUF file check failed:\n"
            + "\n".join(problems)
            + "\nCheck the server path and existing Local v1.5 GGUF pair. "
            "For the native comparison, use --model PATH if weights are stored "
            "outside the configured location."
        )
    return executable.resolve(), model.resolve(), sidecar.resolve()


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if minimum <= value <= maximum:
            return value
    except ValueError:
        pass
    raise TTSConfigurationError(
        f"{name} must be an integer from {minimum} to {maximum}"
    )


class MossCppVoiceRouterBackend(MossTTSVoiceRouterBackend):
    # Qualified with the Windows native Centurion probe and listening approval.
    # Keep Delay defaults independent; its shared stable profile remains 0.8.
    _generation_profiles = {
        **moss_tts_generation_profiles,
        "stable": {
            **moss_tts_generation_profiles["stable"],
            "audio_temperature": 1.7,
        },
    }

    # ponytail: buffer each line; enable upstream streaming only when it reports
    # generation failures and completion reliably instead of swallowing errors.
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=False,
        concurrent_prepare_and_play=False,
        interrupt_on_dialog_replacement=True,
    )

    def materialize_prepared(
        self,
        prepared: PreparedPlayback,
        *,
        cancellation: Cancellation | None = None,
    ) -> PreparedPlayback:
        """Generate native PCM now so playback can overlap the next request."""
        if not isinstance(prepared, PreparedPlayback) or not isinstance(
            prepared.payload, MossTTSPreparedSpeech
        ):
            raise TTSConfigurationError("MOSS C++ received invalid prepared speech")
        payload = prepared.payload
        if payload.cached_audio is not None:
            return prepared
        if _cancelled(cancellation):
            return replace(prepared, generation_completed=False)
        if not self.playback_active:
            self.playback_stop.clear()
        result = self._render_prepared(
            payload,
            SynthesisRequest(
                voice=payload.voice_key,
                text=payload.text,
                seed=payload.seed,
                generation_profile=payload.generation_profile,
                cache_policy=payload.cache_policy,
            ),
        ).collect()
        if result.completion is not SynthesisCompletion.COMPLETE:
            return replace(prepared, generation_completed=False)
        return replace(
            prepared,
            payload=replace(
                payload,
                cached_audio=result.pcm,
                cache_source=result.diagnostics.cache_source,
            ),
            synthesis_ms=result.timing.first_chunk_ms,
            cache_source=result.diagnostics.cache_source,
            audio_source=f"moss-tts:{result.diagnostics.cache_source}",
        )

    def __init__(
        self,
        registry: CharacterVoiceRegistry,
        *,
        model_name: object = None,
        startup_cancellation: object = None,
        startup_progress: object = None,
        startup_timeout: object = 1800.0,
        request_timeout: object = 600.0,
        allow_download: object = False,
        **options: object,
    ) -> None:
        from vntts.moss_cpp_installation import ensure_moss_cpp

        (
            model_name,
            startup_cancellation,
            startup_progress,
            startup_timeout,
            request_timeout,
            allow_download,
        ) = _startup_options(
            model_name,
            startup_cancellation,
            startup_progress,
            startup_timeout,
            request_timeout,
            allow_download,
        )
        ensure_moss_cpp(
            model_name,
            cancellation=startup_cancellation,
            progress=startup_progress,
            allow_download=allow_download,
        )
        (
            base_options,
            (self.executable, self.gguf, self.sidecar),
            self._managed_runtime,
        ) = (
            _base_options(options),
            moss_cpp_paths(model_name),
            _managed_runtime(model_name),
        )
        self.gpu_layers = _integer_setting("VNTTS_MOSS_GPU_LAYERS", -1, -1, 1000)
        self.aux_cpu = _integer_setting("VNTTS_MOSS_AUX_CPU", 1, 0, 1)
        self.context_size = _integer_setting("VNTTS_MOSS_CONTEXT", 4096, 512, 131072)
        self.local_gpu = False
        self.aux_cpu_threads: int | None = None
        self._managed_local_gpu = False
        self._native_capabilities = None
        self._adaptive_managed_runtime = False
        self._fallback_category: str | None = None
        self._fallback_used = False
        self._effective_controls: NativeControls | None = None
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        if not all(
            math.isfinite(v) and v > 0
            for v in (
                self.startup_timeout,
                self.request_timeout,
            )
        ):
            raise TTSConfigurationError("MOSS C++ timeouts must be positive and finite")
        self.server_lock = Lock()
        self.server: subprocess.Popen[bytes] | None = None
        self.server_job: _WindowsKillOnCloseJob | None = None
        self.server_log: BinaryIO | None = None
        self.server_directory: TemporaryDirectory[str] | None = None
        self.port: int | None = None
        self.server_info: dict[str, object] | None = None
        self._diagnostic_salt = secrets.token_bytes(32)
        self._runtime_status: str | None = None
        self._registered_references: dict[str, tuple[str, float, int, int]] = {}
        prompt_cache = base_options["prompt_cache_directory"]
        self.native_voice_directory = (
            _path_option(prompt_cache).expanduser() / "openmoss"
            if prompt_cache is not None
            else None
        )
        self.startup_cancellation = startup_cancellation
        self.startup_progress = startup_progress or (lambda _message: None)
        self.startup_progress("Checking MOSS C++ model and audio codec...")
        # Bind generated audio to both weight files and the executable. GGUF and
        # MLX outputs must never share cache identity even with the same voice.
        self._source_identity = "openmoss-cpp:" + ":".join(
            _source_identity(p) for p in (self.executable, self.gguf, self.sidecar)
        )
        self._native_model_key = hashlib.sha256(
            self._source_identity.encode()
        ).hexdigest()[:24]
        try:
            from vntts.runtime_installation import _run

            help_output = _run(
                [str(self.executable), "--help"],
                cancellation=self._startup_cancelled,
                timeout=min(30, self.startup_timeout),
                include_stderr=True,
            )
            self.voice_registry_supported = bool(
                re.search(rb"(?:^|\s)--voice-dir(?:\s|$)", help_output)
            )
            self.voice_code_cache_supported = bool(
                re.search(rb"(?:^|\s)--voice-cache-key(?:\s|$)", help_output)
            )
            if self._managed_runtime:
                capabilities = self._managed_capabilities(_run, help_output)
                self._native_capabilities = capabilities
                self._adaptive_managed_runtime = capabilities is not None
                self._managed_local_gpu = bool(
                    capabilities
                    and capabilities.get("local_gpu") is True
                    and capabilities.get("vulkan_available") is True
                )
            self._start_server(self._startup_cancelled)
            super().__init__(
                registry,
                model_name=self._native_identity(),
                model_factory=lambda *_args, **_kwargs: SimpleNamespace(
                    sample_rate=48000
                ),
                **base_options,
            )
        except BaseException:
            self._stop_server()
            raise

    def _startup_cancelled(self) -> bool:
        value = self.startup_cancellation
        if value is None:
            return False
        if callable(value):
            return bool(value())
        return bool(value.is_set())

    def _managed_capabilities(
        self, run: Callable[..., bytes], help_output: bytes
    ) -> dict[str, object] | None:
        """Require the managed archive's small, versioned adaptation contract."""
        if not re.search(rb"(?:^|\s)--capabilities-json(?:\s|$)", help_output):
            return None  # Legacy custom v0.3.0 runtime: retain existing behavior.
        try:
            payload = json.loads(
                run(
                    [str(self.executable), "--capabilities-json"],
                    cancellation=self._startup_cancelled,
                    timeout=min(30, self.startup_timeout),
                )
            )
        except (TypeError, ValueError, TTSConfigurationError) as error:
            raise TTSConfigurationError(
                "Managed MOSS runtime does not provide valid capabilities JSON. "
                "Install the current managed runtime."
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "vntts.openmoss.capabilities"
            or payload.get("version") != 1
            or payload.get("vulkan_optional") is not True
        ):
            raise TTSConfigurationError(
                "Managed MOSS runtime capabilities do not support the required optional Vulkan path. "
                "Install the current managed runtime."
            )
        if (
            payload.get("aux_cpu_threads") is not True
            or payload.get("aux_cpu_threads_default") != 4
            or payload.get("aux_cpu_threads_min") != 1
            or payload.get("aux_cpu_threads_max") != 16
            or type(payload.get("local_gpu")) is not bool
            or type(payload.get("vulkan_available")) is not bool
        ):
            raise TTSConfigurationError(
                "Managed MOSS runtime cannot limit auxiliary CPU workers. "
                "Install the current managed runtime."
            )
        return payload

    def _controls_for_start(self) -> NativeControls:
        workers = _integer_setting(
            "VNTTS_MOSS_AUX_CPU_THREADS", _aux_cpu_workers(), 1, 16
        )
        if not self._adaptive_managed_runtime:
            return {
                "gpu_layers": self.gpu_layers,
                "aux_cpu": self.aux_cpu,
                "local_gpu": False,
                "aux_cpu_threads": None,
            }
        if self.gpu_layers == 0:
            return {
                "gpu_layers": 0,
                "aux_cpu": 1,
                "local_gpu": False,
                "aux_cpu_threads": workers,
            }
        if self._fallback_category == "local_gpu":
            return {
                "gpu_layers": -1,
                "aux_cpu": 1,
                "local_gpu": False,
                "aux_cpu_threads": workers,
            }
        if self._fallback_category in {"vulkan_allocation", "vulkan_device"}:
            return {
                "gpu_layers": 0,
                "aux_cpu": 1,
                "local_gpu": False,
                "aux_cpu_threads": workers,
            }
        return {
            "gpu_layers": -1,
            "aux_cpu": 1,
            "local_gpu": self._managed_local_gpu,
            "aux_cpu_threads": workers,
        }

    def _native_identity(self, controls: NativeControls | None = None) -> str:
        controls = controls or self._effective_controls or self._controls_for_start()
        return (
            self._source_identity
            + f":layers={controls['gpu_layers']}:aux_cpu={controls['aux_cpu']}"
            + f":local_gpu={int(controls['local_gpu'])}"
            + f":aux_cpu_threads={controls['aux_cpu_threads']}:ctx={self.context_size}"
            + f":{NATIVE_GENERATION_CONTRACT}"
        )

    def _set_effective_controls(self, controls: NativeControls) -> None:
        self._effective_controls = controls.copy()
        self.gpu_layers = controls["gpu_layers"]
        self.aux_cpu = controls["aux_cpu"]
        self.local_gpu = controls["local_gpu"]
        self.aux_cpu_threads = controls["aux_cpu_threads"]
        identity = self._native_identity(controls)
        self._native_model_key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        if hasattr(self, "persistent_cache_keys"):
            # A resumed owned server may observe a different CPU topology. Never
            # reuse generated WAVs across an effective native control change.
            self.model_name = identity
            self.persistent_cache_keys.model = identity
            self.audio_cache.clear()

    def _diagnostic_key(self, value: object) -> str:
        payload = json.dumps(value, sort_keys=True).encode("utf-8")
        return hashlib.sha256(self._diagnostic_salt + payload).hexdigest()[:24]

    @property
    def runtime_status(self) -> str | None:
        # The UI polls this property; never wait behind process shutdown.
        server, status = self.server, self._runtime_status
        if server is None or server.poll() is not None or self.server is not server:
            return None
        return status

    def load(self) -> str | None:
        """Ensure the owned native model is ready without synthesizing audio."""
        self.playback_stop.clear()
        self._start_server(self._startup_cancelled)
        return self.runtime_status

    def _confirmed_runtime_status(self) -> str:
        placement = (
            self.server_info.get("placement")
            if isinstance(self.server_info, dict)
            else None
        )
        if self._adaptive_managed_runtime and isinstance(placement, dict):
            backbone = placement.get("backbone")
            auxiliary = placement.get("auxiliary")
            local = placement.get("local")
            layers = placement.get("gpu_layers")
            workers = placement.get("aux_cpu_threads")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (backbone, auxiliary, local)
            ) or (
                not isinstance(layers, int)
                or isinstance(layers, bool)
                or layers < 0
                or not isinstance(workers, int)
                or isinstance(workers, bool)
                or workers < 1
            ):
                return "MOSS C++: managed placement unconfirmed"
            device = placement.get("device")
            fallback = (
                f"; fallback: {self._fallback_category.replace('_', ' ')}"
                if self._fallback_category
                else ""
            )
            device_label = (
                f" on {device.strip()}" if isinstance(device, str) and device else ""
            )
            cpu_warning = (
                "; MOSS may be slow on CPU; choose Pocket TTS in Voices for faster generation"
                if layers == 0
                else ""
            )
            return (
                f"MOSS C++: backbone {str(backbone).strip()}{device_label} "
                f"({layers} GPU layers); "
                f"audio frame model: {str(local).strip()}; audio model/codec: "
                f"{str(auxiliary).strip()}; auxiliary CPU workers: {workers}{fallback}"
                f"{cpu_warning}"
            )
        # Read through a separate handle: seeking the child's shared log handle
        # would move its write position and could overwrite earlier messages.
        with self.server_lock:
            if self.server_log is None:
                output = ""
            else:
                with open(self.server_log.name, "rb") as log:
                    log.seek(0, os.SEEK_END)
                    log.seek(max(0, log.tell() - 64 * 1024))
                    output = log.read(64 * 1024).decode("utf-8", errors="replace")
        offload = re.search(r"offloaded (\d+)/(\d+) layers to GPU", output)
        device = re.search(r"using device (\S+) \(([^\r\n]+?)\)", output)
        if device:
            gpu = f"{device[2]} ({device[1]})"
        else:
            device = re.search(r"pinning to GPU \d+ \(([^,\r\n]+),", output)
            gpu = device[1] if device else "device name unreported"
        if offload and 0 < int(offload[1]) <= int(offload[2]):
            placement = "GPU" if offload[1] == offload[2] else "GPU + CPU"
            backbone = f"{placement}: {gpu}, {offload[1]}/{offload[2]} GPU layers"
        elif self.gpu_layers == 0:
            backbone = "CPU (explicitly selected)"
        elif "no GPU device found; using CPU backend" in output:
            backbone = "CPU (no GPU detected)"
        elif offload and int(offload[1]) == 0:
            backbone = f"CPU (0/{offload[2]} GPU layers; GPU requested)"
        else:
            backbone = "device unconfirmed (GPU offload requested)"
        aux = re.search(r"Model::load: aux backend = ([^\r\n]+)", output)
        auxiliary = aux[1].strip() if aux else "device unconfirmed"
        local = re.search(r"Model::load: local decoder backend = ([^\r\n]+)", output)
        if local:
            return (
                f"MOSS C++: {backbone}; audio frame model: {local[1].strip()}; "
                f"input embeddings/codec: {auxiliary}"
            )
        return f"MOSS C++: {backbone}; audio model/codec: {auxiliary}"

    def _start_server(self, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise TTSSynthesisError("MOSS C++ startup cancelled")
        with self.server_lock:
            if self.server is not None and self.server.poll() is None:
                return
        # Hardware availability may change between owned-server lifetimes.
        # A fallback is one attempt, not a verdict cached by this backend.
        self._fallback_category = None
        self._fallback_used = False
        controls = self._controls_for_start()
        while True:
            self._set_effective_controls(controls)
            started, category, exit_code = self._start_server_once(cancelled, controls)
            if started:
                return
            record_native_speech(
                operation="server-failed",
                outcome="failed",
                model_key=self._native_model_key,
                exit_code=exit_code,
                fallback_reason=category,
                gpu_layers=self.gpu_layers,
                local_gpu=self.local_gpu,
                aux_cpu_threads=self.aux_cpu_threads,
                **_native_stage_timings(
                    self.server_log.name if self.server_log is not None else None,
                    0,
                    {},
                    self.startup_timeout,
                ),
            )
            fallback = self._startup_fallback(category)
            self._stop_server()
            if fallback is None:
                raise TTSConfigurationError(
                    "MOSS C++ server exited while loading. Check its DLLs and model "
                    "files; automatic hardware fallback was not applicable."
                )
            controls = fallback

    def _startup_fallback(self, category: str | None) -> NativeControls | None:
        if not self._adaptive_managed_runtime or self._fallback_used:
            return None
        if category == "local_gpu" and self.local_gpu:
            self._fallback_category = category
        elif category in {"vulkan_allocation", "vulkan_device"} and self.gpu_layers:
            self._fallback_category = category
        else:
            return None
        self._fallback_used = True
        self.startup_progress(
            "MOSS GPU startup failed; retrying once with a safer managed placement."
        )
        return self._controls_for_start()

    def _start_server_once(
        self, cancelled: Callable[[], bool], controls: NativeControls
    ) -> tuple[bool, str | None, int | None]:
        self._stop_server()
        self.server_directory = TemporaryDirectory(
            prefix="vntts-moss-", ignore_cleanup_errors=True
        )
        (Path(self.server_directory.name) / "voices").mkdir()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        command = [
            str(self.executable),
            "--model",
            str(self.gguf),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--no-webui",
            "--n-gpu-layers",
            str(controls["gpu_layers"]),
            "--n-ctx",
            str(self.context_size),
        ]
        if self.voice_registry_supported:
            voice_directory = self._voice_directory(self.server_directory.name)
            voice_directory.mkdir(parents=True, exist_ok=True)
            command.extend(["--voice-dir", str(voice_directory)])
            if self.voice_code_cache_supported:
                command.extend(["--voice-cache-key", self._native_model_key])
        if controls["aux_cpu"]:
            command.append("--aux-cpu")
        if controls["local_gpu"]:
            command.append("--local-gpu")
        if controls["aux_cpu_threads"] is not None:
            command.extend(["--aux-cpu-threads", str(controls["aux_cpu_threads"])])
        placement = (
            "CPU only for backbone"
            if controls["gpu_layers"] == 0
            else "automatic GPU offload"
            if controls["gpu_layers"] == -1
            else f"up to {controls['gpu_layers']} GPU layers"
        )
        self.startup_progress(
            f"Loading MOSS Local v1.5: {placement}; "
            f"audio model/codec {'on CPU' if controls['aux_cpu'] else 'on the selected device'}. "
            "Actual acceleration depends on available hardware and drivers."
        )
        with self.server_lock:
            self.server_log = open(
                Path(self.server_directory.name) / "server.log", "w+b"
            )
            started = monotonic()
            self.server, self.server_job = _launch_owned_process(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self.server_log,
                stderr=self.server_log,
                cwd=self.executable.parent,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deadline = monotonic() + self.startup_timeout
        pause = Event()
        while monotonic() < deadline:
            if cancelled():
                raise TTSSynthesisError("MOSS C++ startup cancelled")
            if self.server is None or self.server.poll() is not None:
                path = self.server_log.name if self.server_log is not None else None
                return (
                    False,
                    _startup_failure_category(path)
                    if self._adaptive_managed_runtime
                    else "server_exit_unclassified",
                    self.server.returncode if self.server is not None else None,
                )
            try:
                status, _headers, data = self._http("GET", "/info", timeout=0.5)
            except OSError, http.client.HTTPException:
                pause.wait(0.1)
                continue
            if status != 200:
                raise TTSConfigurationError("MOSS C++ server /info failed")
            info = json.loads(data)
            if not isinstance(info, dict) or any(
                (
                    info.get("architecture") != "moss_tts_local",
                    info.get("sampling_rate") != 48000,
                    info.get("n_channels") != 2,
                    info.get("n_vq") != 12,
                    info.get("codec_loaded") is not True,
                    self.voice_code_cache_supported
                    and info.get("persistent_voice_codes") is not True,
                )
            ):
                raise TTSConfigurationError(
                    "MOSS C++ requires Local v1.5 with its loaded 48 kHz stereo codec"
                )
            if self._adaptive_managed_runtime and not self._valid_managed_placement(
                info
            ):
                raise TTSConfigurationError(
                    "Managed MOSS runtime did not confirm placement and worker count. "
                    "Install the current managed runtime."
                )
            self.server_info = info
            self._runtime_status = self._confirmed_runtime_status()
            record_native_speech(
                operation="server-start",
                server_pid=self.server.pid,
                server_load_s=round(monotonic() - started, 3),
                compute=self._runtime_status,
                native_version=(
                    info["version"]
                    if isinstance(info.get("version"), str)
                    and re.fullmatch(r"[\w.+-]{1,64}", info["version"])
                    else None
                ),
                model_key=self._native_model_key,
                model_bytes=_diagnostic_file_size(self.gguf),
                codec_bytes=_diagnostic_file_size(self.sidecar),
                gpu_layers=info.get("placement", {}).get("gpu_layers", self.gpu_layers)
                if isinstance(info.get("placement"), dict)
                else self.gpu_layers,
                aux_cpu=self.aux_cpu,
                local_gpu=self.local_gpu,
                device=info.get("placement", {}).get("device")
                if isinstance(info.get("placement"), dict)
                else None,
                aux_cpu_threads=info.get("placement", {}).get(
                    "aux_cpu_threads", self.aux_cpu_threads
                )
                if isinstance(info.get("placement"), dict)
                else self.aux_cpu_threads,
                fallback_reason=self._fallback_category,
                capability_version=(self._native_capabilities or {}).get("version"),
                vulkan_available=(self._native_capabilities or {}).get(
                    "vulkan_available"
                ),
                context_size=self.context_size,
            )
            self.startup_progress(self._runtime_status)
            return True, None, None
        raise TTSConfigurationError("MOSS C++ model startup timed out")

    def _voice_directory(self, server_directory: str | Path) -> Path:
        if self.voice_code_cache_supported and self.native_voice_directory is not None:
            return self.native_voice_directory
        return Path(server_directory) / "voices"

    @staticmethod
    def _valid_managed_placement(info: Mapping[str, object]) -> bool:
        placement = info.get("placement")
        return (
            isinstance(placement, dict)
            and all(
                isinstance(placement.get(key), str) and placement[key].strip()
                for key in ("backbone", "local", "auxiliary")
            )
            and isinstance(placement.get("gpu_layers"), int)
            and not isinstance(placement.get("gpu_layers"), bool)
            and placement["gpu_layers"] >= 0
            and (
                isinstance(placement.get("device"), str)
                and 1 <= len(placement["device"]) <= 128
                and bool(placement["device"].strip())
                and placement["device"].isprintable()
                if placement["gpu_layers"] > 0
                else placement.get("device") is None or placement.get("device") == ""
            )
            and isinstance(placement.get("aux_cpu_threads"), int)
            and not isinstance(placement.get("aux_cpu_threads"), bool)
            and placement["aux_cpu_threads"] >= 1
        )

    def _http(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, NativeHeaders, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=timeout or self.request_timeout,
        )
        try:
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            # A line is capped at 20s; leave room for a malformed provider to be
            # rejected without reading an unbounded response into memory.
            data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise TTSSynthesisError("MOSS C++ response exceeds the audio limit")
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def _resolve_prompt_codes(self, character: str) -> tuple[str, str]:
        # Keep reference resolution/content checking in the existing voice route.
        voice_key, source = self._resolve_voice_source(character)
        return voice_key, str(source)

    def prime(self, character: str) -> bool:
        self._resolve_prompt_codes(character)
        return False

    def _render_chunks(
        self, prepared: MossTTSPreparedSpeech, request: SynthesisRequest
    ) -> Generator[SynthesisChunk, None, SynthesisResult]:
        if prepared.cached_audio is None:
            return (yield from super()._render_chunks(prepared, request))
        outcome = "failed"
        try:
            result = yield from super()._render_chunks(prepared, request)
            outcome = result.completion.value
            return result
        except GeneratorExit:
            outcome = "cancelled"
            raise
        finally:
            record_native_speech(
                operation="cached-wav",
                outcome=outcome,
                cache=prepared.cache_source,
                reference="not-used",
                gen_s=None,
                decode_s=None,
            )

    def _generate(
        self, prepared: MossTTSPreparedSpeech, request: SynthesisRequest
    ) -> Generator[SimpleNamespace, None, None]:
        def cancelled() -> bool:
            return bool(self.playback_stop.is_set()) or request.cancellation_requested()

        started = monotonic()
        path: str | None = None
        offset = 0
        headers: NativeHeaders = {}
        worker: Thread | None = None
        server_pid = None
        request_s = None
        reference_prepare_s = http_round_trip_s = response_pcm_decode_s = None
        audio_s = None
        request_key = reference_key = reference_mode = None
        actual_seed = frame_limit = frames = None
        resource_sampler = None
        reference_s = reference_sample_rate = reference_channels = None
        http_status = None
        stage, reason = "startup", None
        outcome = "failed"
        context = native_speech_context.get()
        context_attempt_id = (
            context.get("attempt_id") if isinstance(context, dict) else None
        )
        attempt_id = (
            context_attempt_id
            if isinstance(context_attempt_id, str) and context_attempt_id
            else secrets.token_hex(12)
        )
        try:
            if cancelled():
                return
            self._start_server(cancelled)
            with self.server_lock:
                if self.server is None or self.server_log is None:
                    raise TTSSynthesisError("MOSS C++ stopped before generation")
                server_pid = self.server.pid
                path = self.server_log.name
                server_info = self.server_info
                directory = self.server_directory
                if server_info is None or directory is None:
                    raise TTSSynthesisError("MOSS C++ stopped before generation")
                server_directory = Path(directory.name)
            try:
                resource_sampler = NativeResourceSampler(server_pid)
                resource_sampler.start()
            except Exception:
                resource_sampler = None
            offset = Path(path).stat().st_size
            stage = "reference"
            reference_started = monotonic()
            registered = (
                self.voice_registry_supported
                and server_info.get("voice_registry") is True
            )
            with ExitStack() as reference_stack:
                reference_identity = None
                prompt_audio_path = _reference_audio_path(prepared.prompt_audio_codes)
                reference: Path | BinaryIO | SpooledTemporaryFile[bytes] = (
                    prompt_audio_path
                )

                def registered_reference(
                    identity: str | None,
                ) -> tuple[str, float, int, int] | None:
                    if identity is None:
                        return None
                    cached = self._registered_references.get(identity)
                    if cached is None:
                        return None
                    return (
                        cached
                        if self._voice_directory(server_directory)
                        .joinpath(f"{cached[0]}.wav")
                        .is_file()
                        else None
                    )

                if registered:
                    digest = hashlib.sha256()
                    with prompt_audio_path.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk)
                    reference_identity = digest.hexdigest()
                cached_reference = registered_reference(reference_identity)
                if registered and cached_reference is None:
                    source = reference_stack.enter_context(prompt_audio_path.open("rb"))
                    # Keep a cache miss's identity and decoded audio on one
                    # immutable snapshot in case the source changes mid-read.
                    snapshot = reference_stack.enter_context(
                        SpooledTemporaryFile(max_size=8 * 1024 * 1024)
                    )
                    digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        snapshot.write(chunk)
                    snapshot.seek(0)
                    reference = snapshot
                    reference_identity = digest.hexdigest()
                    cached_reference = registered_reference(reference_identity)
                if cached_reference is None:
                    (
                        reference_wav,
                        reference_s,
                        reference_sample_rate,
                        reference_channels,
                    ) = _normalize_reference_audio(reference)
                    voice_id = hashlib.sha256(reference_wav).hexdigest()
                else:
                    (
                        voice_id,
                        reference_s,
                        reference_sample_rate,
                        reference_channels,
                    ) = cached_reference
                    reference_wav = None
            seed = prepared.seed
            if seed is not None and (type(seed) is not int or not 0 <= seed < 2**64):
                raise TTSConfigurationError(
                    "MOSS C++ seed must be an unsigned 64-bit integer"
                )
            frame_limit = math.ceil(prepared.max_audio_seconds * 12.5)
            # openmoss interprets zero as random; VNTTS zero means a fixed seed.
            actual_seed = (secrets.randbits(64) if seed is None else seed) or 1
            body = {
                "text": prepared.text,
                "language": self.language,
                "response_format": "wav",
                "stream": False,
                "max_new_tokens": frame_limit + 1,
                "sampling": {
                    **dict(prepared.generation_options),
                    "seed": actual_seed,
                    "max_audio_frames": frame_limit,
                },
            }
            reference_key = self._diagnostic_key(voice_id)
            request_key = self._diagnostic_key({**body, "reference": reference_key})
            if registered:
                # openmoss caches registered reference codes. Inline WAVs are
                # encoded again on every line, even when the voice is unchanged.
                # ponytail: immutable content-addressed voices have no eviction;
                # add a disk cap only if real libraries make this cache large.
                reference_mode = "registered"
                if reference_identity is None:
                    raise TTSSynthesisError(
                        "MOSS C++ reference identity is unavailable"
                    )
                reference_path = (
                    self._voice_directory(server_directory) / f"{voice_id}.wav"
                )
                if reference_wav is not None:
                    reference_path.with_suffix(".json").write_text(
                        "{}", encoding="utf-8"
                    )
                    reference_path.write_bytes(reference_wav)
                self._registered_references[reference_identity] = (
                    voice_id,
                    reference_s,
                    reference_sample_rate,
                    reference_channels,
                )
                body["voice"] = voice_id
            else:
                reference_mode = "inline"
                if reference_wav is None:
                    raise TTSSynthesisError("MOSS C++ reference WAV is unavailable")
                body["reference_wav_b64"] = base64.b64encode(reference_wav).decode(
                    "ascii"
                )
            reference_prepare_s = round(monotonic() - reference_started, 6)
            done = Event()
            result: list[tuple[int, NativeHeaders, bytes] | BaseException] = []

            def fetch() -> None:
                try:
                    result.append(self._http("POST", "/tts", body))
                except Exception as error:
                    result.append(error)
                finally:
                    done.set()

            worker = Thread(target=fetch, daemon=True)
            stage = "request"
            record_native_speech(
                operation="request-start",
                attempt_id=attempt_id,
                server_pid=server_pid,
                request_key=request_key,
                reference_key=reference_key,
                seed=actual_seed,
                frame_limit=frame_limit,
                model_key=self._native_model_key,
            )
            http_started = monotonic()
            worker.start()
            while not done.wait(0.1):
                if cancelled():
                    # ponytail: upstream has no cancellation endpoint; kill only
                    # our owned server and reload on the next uncached request.
                    return
            if cancelled():
                return
            if isinstance(result[0], BaseException):
                raise result[0]
            status, headers, data = result[0]
            http_round_trip_s = round(monotonic() - http_started, 6)
            http_status = status
            if status != 200:
                reason = "http-error"
                raise TTSSynthesisError(f"MOSS C++ generation failed (HTTP {status})")
            stage = "decode-response"
            response_started = monotonic()
            headers = {key.lower(): value for key, value in headers.items()}
            frames = int(headers.get("x-moss-audio-frames", "0"))
            if frames <= 0:
                reason = "missing-frame-count"
                raise TTSSynthesisError(
                    "MOSS C++ response is missing its audio-frame count"
                )
            with wave.open(io.BytesIO(data), "rb") as output:
                if (
                    output.getframerate() != 48000
                    or output.getnchannels() != 2
                    or output.getsampwidth() != 2
                    or output.getcomptype() != "NONE"
                ):
                    raise TTSSynthesisError(
                        "MOSS C++ returned an unexpected audio format"
                    )
                samples = output.getnframes()
                raw = output.readframes(samples)
                if len(raw) != samples * 4:
                    raise TTSSynthesisError("MOSS C++ returned a truncated WAV")
                pcm = (
                    np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float32)
                    / 32768
                )
            if not pcm.size or not np.isfinite(pcm).all():
                raise TTSSynthesisError("MOSS C++ returned empty or invalid audio")
            response_pcm_decode_s = round(monotonic() - response_started, 6)
            outcome = "limited" if frames >= frame_limit else "complete"
            reason = "frame-limit" if frames >= frame_limit else None
            stage = "provider-finished"
            audio_s = round(len(pcm) / self.sample_rate, 6)
            request_s = round(monotonic() - started, 3)
            yield SimpleNamespace(audio=pcm, generation_limited=frames >= frame_limit)
        except Exception:
            reason = reason or f"{stage}-failed"
            if cancelled():
                return
            raise
        finally:
            if cancelled():
                outcome = "cancelled"
            with self.server_lock:
                stages = _native_stage_timings(
                    path, offset, headers, self.request_timeout
                )
            resources: NativeResourceSnapshot | dict[str, str] = {
                "status": "not-started"
            }
            if resource_sampler is not None:
                try:
                    resources = resource_sampler.finish()
                except Exception:
                    resources = {"status": "probe-failed"}
            # Capture before teardown removes the owned temporary log: numeric
            # measurements and salted keys, never text/reference paths.
            record_native_speech(
                operation="fresh-generation",
                attempt_id=attempt_id,
                cache="fresh-generation",
                server_pid=server_pid,
                request_key=request_key,
                reference_key=reference_key,
                reference_mode=reference_mode,
                seed=actual_seed,
                frame_limit=frame_limit,
                audio_frames=frames,
                max_audio_s=prepared.max_audio_seconds,
                text_characters=len(prepared.text),
                text_words=len(prepared.text.split()),
                reference_s=reference_s,
                reference_sample_rate=reference_sample_rate,
                reference_channels=reference_channels,
                profile=prepared.generation_profile,
                sampling=dict(prepared.generation_options),
                model_key=self._native_model_key,
                resources=resources,
                stage=stage,
                reason=reason,
                http_status=http_status,
                outcome=outcome,
                audio_s=audio_s,
                reference_prepare_s=reference_prepare_s,
                http_round_trip_s=http_round_trip_s,
                response_pcm_decode_s=response_pcm_decode_s,
                request_s=(
                    round(monotonic() - started, 3) if request_s is None else request_s
                ),
                **stages,
            )
            if outcome in {"failed", "cancelled"}:
                self._stop_server()
                if worker is not None:
                    worker.join(timeout=2)

    def _stop_server(self) -> None:
        with self.server_lock:
            server, self.server = self.server, None
            job, self.server_job = self.server_job, None
            log, self.server_log = self.server_log, None
            directory, self.server_directory = self.server_directory, None
            self.server_info = None
            self._runtime_status = None
            self._registered_references.clear()
            try:
                if server is not None:
                    if server.poll() is None:
                        server.terminate()
                    try:
                        server.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        try:
                            server.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass  # Kill is final; do not turn shutdown into another hang.
            finally:
                if job is not None:
                    job.close()
                if log is not None:
                    log.close()
                if directory is not None:
                    # Windows scanners and log viewers may briefly retain the
                    # closed log. Temporary cleanup must not replace speech.
                    directory.cleanup()

    def shutdown(self) -> None:
        try:
            self.stop()
        finally:
            self._stop_server()
