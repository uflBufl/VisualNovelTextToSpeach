"""Run the same MOSS Local v1.5 checkpoint in an owned openmoss C++ server."""

from __future__ import annotations

import base64
import http.client
import io
import json
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import wave
from tempfile import TemporaryFile
from threading import Event, Lock, Thread
from time import monotonic
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.speech_backend import (
    MossTTSVoiceRouterBackend,
    SpeechBackendCapabilities,
)
from vntts.speech_backend_runtime import _source_identity


def moss_cpp_requested(model_name=None):
    return bool(
        sys.platform != "darwin"
        or platform.machine().casefold() != "arm64"
        or os.environ.get("VNTTS_MOSS_CPP_EXECUTABLE")
        or os.environ.get("VNTTS_MOSS_GGUF")
        or str(model_name or "").lower().endswith(".gguf")
    )


def moss_cpp_paths(model_name=None):
    from vntts.moss_cpp_installation import configured_paths

    executable, model, sidecar = configured_paths(model_name)
    if (
        not executable.is_file()
        or not model.is_file()
        or model.suffix.lower() != ".gguf"
    ):
        raise TTSConfigurationError(
            "MOSS Local on Windows/Linux or Intel Mac uses C++/GGUF, not the "
            "Apple Silicon MLX Python runtime. Set VNTTS_MOSS_CPP_EXECUTABLE to moss-tts-server "
            "and VNTTS_MOSS_GGUF (or the Model setting) pointing to the Local v1.5 "
            "GGUF. See scripts/run-moss-windows.ps1."
        )
    if not sidecar.is_file():
        raise TTSConfigurationError(f"MOSS C++ audio sidecar is missing: {sidecar}")
    return executable.resolve(), model.resolve(), sidecar.resolve()


def _integer_setting(name, default, minimum, maximum):
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
    # ponytail: buffer each line; enable upstream streaming only when it reports
    # generation failures and completion reliably instead of swallowing errors.
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=False,
        concurrent_prepare_and_play=False,
        interrupt_on_dialog_replacement=True,
    )

    def __init__(
        self,
        registry,
        *,
        model_name=None,
        startup_cancellation=None,
        startup_progress=None,
        startup_timeout=1800.0,
        request_timeout=600.0,
        **options,
    ):
        from vntts.moss_cpp_installation import ensure_moss_cpp

        ensure_moss_cpp(
            model_name, cancellation=startup_cancellation, progress=startup_progress
        )
        self.executable, self.gguf, self.sidecar = moss_cpp_paths(model_name)
        self.gpu_layers = _integer_setting("VNTTS_MOSS_GPU_LAYERS", -1, -1, 1000)
        self.aux_cpu = _integer_setting("VNTTS_MOSS_AUX_CPU", 1, 0, 1)
        self.context_size = _integer_setting("VNTTS_MOSS_CONTEXT", 4096, 512, 131072)
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
        self.server = None
        self.server_log = None
        self.port = None
        self.server_info = None
        self.startup_cancellation = startup_cancellation
        self.startup_progress = startup_progress or (lambda _message: None)
        self.startup_progress("Checking MOSS C++ model and audio codec...")
        # Bind generated audio to both weight files and the executable. GGUF and
        # MLX outputs must never share cache identity even with the same voice.
        identity = (
            "openmoss-cpp:"
            + ":".join(
                _source_identity(p) for p in (self.executable, self.gguf, self.sidecar)
            )
            + f":layers={self.gpu_layers}:aux_cpu={self.aux_cpu}:ctx={self.context_size}"
        )
        try:
            self._start_server(self._startup_cancelled)
            super().__init__(
                registry,
                model_name=identity,
                model_factory=lambda *_args, **_kwargs: SimpleNamespace(
                    sample_rate=48000
                ),
                **options,
            )
        except BaseException:
            self._stop_server()
            raise

    def _startup_cancelled(self):
        value = self.startup_cancellation
        return bool(
            value.is_set()
            if hasattr(value, "is_set")
            else value()
            if callable(value)
            else False
        )

    def _start_server(self, cancelled):
        if cancelled():
            raise TTSSynthesisError("MOSS C++ startup cancelled")
        with self.server_lock:
            if self.server is not None and self.server.poll() is None:
                return
        self._stop_server()
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
            str(self.gpu_layers),
            "--n-ctx",
            str(self.context_size),
        ]
        if self.aux_cpu:
            command.append("--aux-cpu")
        placement = (
            "CPU only"
            if self.gpu_layers == 0
            else "automatic GPU offload"
            if self.gpu_layers == -1
            else f"up to {self.gpu_layers} GPU layers"
        )
        self.startup_progress(
            f"Loading MOSS Local v1.5: {placement}; "
            f"audio codec {'on CPU' if self.aux_cpu else 'on the selected device'}. "
            "Actual acceleration depends on available hardware and drivers."
        )
        with self.server_lock:
            self.server_log = TemporaryFile(mode="w+b")
            self.server = subprocess.Popen(
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
                raise TTSConfigurationError(
                    "MOSS C++ server exited while loading. Check its DLLs and model "
                    "files; reduce VNTTS_MOSS_GPU_LAYERS if GPU memory is exhausted."
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
                )
            ):
                raise TTSConfigurationError(
                    "MOSS C++ requires Local v1.5 with its loaded 48 kHz stereo codec"
                )
            self.server_info = info
            return
        raise TTSConfigurationError("MOSS C++ model startup timed out")

    def _http(self, method, path, body=None, *, timeout=None):
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

    def _resolve_prompt_codes(self, character):
        # Keep reference resolution/content checking in the existing voice route.
        voice_key, source = self._resolve_voice_source(character)
        return voice_key, str(source)

    def prime(self, character):
        self._resolve_prompt_codes(character)
        return False

    def _generate(self, prepared, request):
        def cancelled():
            return self.playback_stop.is_set() or request.cancellation_requested()

        if cancelled():
            return
        try:
            self._start_server(cancelled)
            with sf.SoundFile(prepared.prompt_audio_codes) as reference:
                if (
                    reference.channels not in {1, 2}
                    or not 8000 <= reference.samplerate <= 192000
                ):
                    raise TTSConfigurationError(
                        "MOSS C++ reference must be mono/stereo audio at 8-192 kHz"
                    )
                if reference.frames > reference.samplerate * 120:
                    raise TTSConfigurationError(
                        "MOSS C++ reference must be at most 120 seconds"
                    )
                audio = reference.read(dtype="float32", always_2d=True)
                if audio.size == 0 or not np.isfinite(audio).all():
                    raise TTSConfigurationError(
                        "MOSS C++ reference contains invalid audio"
                    )
                wav = io.BytesIO()
                sf.write(
                    wav, audio, reference.samplerate, format="WAV", subtype="PCM_16"
                )
            seed = prepared.seed
            if seed is not None and (type(seed) is not int or not 0 <= seed < 2**64):
                raise TTSConfigurationError(
                    "MOSS C++ seed must be an unsigned 64-bit integer"
                )
            frame_limit = math.ceil(prepared.max_audio_seconds * 12.5)
            body = {
                "text": prepared.text,
                "language": self.language,
                "reference_wav_b64": base64.b64encode(wav.getvalue()).decode("ascii"),
                "response_format": "wav",
                "stream": False,
                "max_new_tokens": frame_limit + 1,
                "sampling": {
                    **dict(prepared.generation_options),
                    "seed": secrets.randbits(64) if seed is None else seed,
                    "max_audio_frames": frame_limit,
                },
            }
            done = Event()
            result = []

            def fetch():
                try:
                    result.append(self._http("POST", "/tts", body))
                except Exception as error:
                    result.append(error)
                finally:
                    done.set()

            worker = Thread(target=fetch, daemon=True)
            worker.start()
            while not done.wait(0.1):
                if cancelled():
                    # ponytail: upstream has no cancellation endpoint; kill only
                    # our owned server and reload on the next uncached request.
                    self._stop_server()
                    worker.join(timeout=2)
                    return
            if cancelled():
                return
            if isinstance(result[0], Exception):
                raise result[0]
            status, headers, data = result[0]
            if status != 200:
                raise TTSSynthesisError(f"MOSS C++ generation failed (HTTP {status})")
            headers = {key.lower(): value for key, value in headers.items()}
            frames = int(headers.get("x-moss-audio-frames", "0"))
            if frames <= 0:
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
            yield SimpleNamespace(audio=pcm, generation_limited=frames >= frame_limit)
        except Exception:
            self._stop_server()
            if cancelled():
                return
            raise

    def _stop_server(self):
        with self.server_lock:
            server, self.server = self.server, None
            log, self.server_log = self.server_log, None
            try:
                if server is not None:
                    if server.poll() is None:
                        server.terminate()
                    try:
                        server.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=2)
            finally:
                if log is not None:
                    log.close()

    def shutdown(self):
        self.stop()
        self._stop_server()
