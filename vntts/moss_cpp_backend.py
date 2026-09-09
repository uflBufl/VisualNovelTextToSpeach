"""Run the same MOSS Local v1.5 checkpoint in an owned openmoss C++ server."""

from __future__ import annotations

import base64
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
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from vntts.native_resources import NativeResourceSampler
from vntts.services.tts_engine import TTSConfigurationError, TTSSynthesisError
from vntts.speech_backend import (
    MossTTSVoiceRouterBackend,
    SpeechBackendCapabilities,
    moss_tts_generation_profiles,
)
from vntts.speech_backend_runtime import _source_identity
from vntts.support import native_speech_context, record_native_speech

NATIVE_GENERATION_CONTRACT = "nonzero-seed-stable-1.7-v2"


def _diagnostic_file_size(path):
    try:
        return path.stat().st_size
    except OSError:
        return None


def _native_stage_timings(path, offset, headers, maximum_seconds):
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

    def seconds(pattern=None, header=None):
        match = re.search(pattern, output) if pattern else None
        raw = headers.get(header) if header else None
        if raw is None and match:
            raw = match[1]
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
            if complete_log and "(cached codes)" in output
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
        self.server_directory = None
        self.port = None
        self.server_info = None
        self._diagnostic_salt = secrets.token_bytes(32)
        self._runtime_status = None
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
            + f":{NATIVE_GENERATION_CONTRACT}"
        )
        self._native_model_key = hashlib.sha256(identity.encode()).hexdigest()[:24]
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

    def _diagnostic_key(self, value):
        payload = json.dumps(value, sort_keys=True).encode("utf-8")
        return hashlib.sha256(self._diagnostic_salt + payload).hexdigest()[:24]

    @property
    def runtime_status(self):
        # The UI polls this property; never wait behind process shutdown.
        server, status = self.server, self._runtime_status
        if server is None or server.poll() is not None or self.server is not server:
            return None
        return status

    def _confirmed_runtime_status(self):
        # Read through a separate handle: seeking the child's shared log handle
        # would move its write position and could overwrite earlier messages.
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
        return f"MOSS C++: {backbone}; audio model/codec: {auxiliary}"

    def _start_server(self, cancelled):
        if cancelled():
            raise TTSSynthesisError("MOSS C++ startup cancelled")
        with self.server_lock:
            if self.server is not None and self.server.poll() is None:
                return
        self._stop_server()
        self.server_directory = TemporaryDirectory(prefix="vntts-moss-")
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
            str(self.gpu_layers),
            "--n-ctx",
            str(self.context_size),
        ]
        if self.voice_registry_supported:
            command.extend(
                ["--voice-dir", str(Path(self.server_directory.name) / "voices")]
            )
        if self.aux_cpu:
            command.append("--aux-cpu")
        placement = (
            "CPU only for backbone"
            if self.gpu_layers == 0
            else "automatic GPU offload"
            if self.gpu_layers == -1
            else f"up to {self.gpu_layers} GPU layers"
        )
        self.startup_progress(
            f"Loading MOSS Local v1.5: {placement}; "
            f"audio model/codec {'on CPU' if self.aux_cpu else 'on the selected device'}. "
            "Actual acceleration depends on available hardware and drivers."
        )
        with self.server_lock:
            self.server_log = open(
                Path(self.server_directory.name) / "server.log", "w+b"
            )
            started = monotonic()
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
                record_native_speech(
                    operation="server-failed",
                    outcome="failed",
                    model_key=self._native_model_key,
                    exit_code=self.server.returncode
                    if self.server is not None
                    else None,
                    **_native_stage_timings(
                        self.server_log.name if self.server_log is not None else None,
                        0,
                        {},
                        self.startup_timeout,
                    ),
                )
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
                gpu_layers=self.gpu_layers,
                aux_cpu=self.aux_cpu,
                context_size=self.context_size,
            )
            self.startup_progress(self._runtime_status)
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

    def _render_chunks(self, prepared, request):
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

    def _generate(self, prepared, request):
        def cancelled():
            return self.playback_stop.is_set() or request.cancellation_requested()

        started = monotonic()
        path, offset, headers, worker = None, 0, {}, None
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
        attempt_id = (native_speech_context.get() or {}).get(
            "attempt_id"
        ) or secrets.token_hex(12)
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
                server_directory = Path(self.server_directory.name)
            try:
                resource_sampler = NativeResourceSampler(server_pid)
                resource_sampler.start()
            except Exception:
                resource_sampler = None
            offset = Path(path).stat().st_size
            stage = "reference"
            reference_started = monotonic()
            with sf.SoundFile(prepared.prompt_audio_codes) as reference:
                reference_s = round(reference.frames / reference.samplerate, 6)
                reference_sample_rate = reference.samplerate
                reference_channels = reference.channels
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
            voice_id = hashlib.sha256(wav.getvalue()).hexdigest()
            reference_key = self._diagnostic_key(voice_id)
            request_key = self._diagnostic_key({**body, "reference": reference_key})
            if (
                self.voice_registry_supported
                and server_info.get("voice_registry") is True
            ):
                # openmoss caches registered reference codes. Inline WAVs are
                # encoded again on every line, even when the voice is unchanged.
                # ponytail: codes/WAVs live until server shutdown; add eviction
                # only if long-running sessions with many unique voices need it.
                reference_mode = "registered"
                reference_path = server_directory / "voices" / f"{voice_id}.wav"
                if not reference_path.is_file():
                    reference_path.with_suffix(".json").write_text(
                        "{}", encoding="utf-8"
                    )
                    reference_path.write_bytes(wav.getvalue())
                body["voice"] = voice_id
            else:
                reference_mode = "inline"
                body["reference_wav_b64"] = base64.b64encode(wav.getvalue()).decode(
                    "ascii"
                )
            reference_prepare_s = round(monotonic() - reference_started, 6)
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
            if isinstance(result[0], Exception):
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
            stages = _native_stage_timings(path, offset, headers, self.request_timeout)
            resources = {"status": "not-started"}
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

    def _stop_server(self):
        with self.server_lock:
            server, self.server = self.server, None
            log, self.server_log = self.server_log, None
            directory, self.server_directory = self.server_directory, None
            self.server_info = None
            self._runtime_status = None
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
                if directory is not None:
                    # Retry only the Windows sharing violation; do not hide
                    # other cleanup failures.
                    for attempt in range(3):
                        try:
                            directory.cleanup()
                            break
                        except PermissionError as error:
                            if getattr(error, "winerror", None) != 32 or attempt == 2:
                                raise
                            sleep(0.05 * (attempt + 1))

    def shutdown(self):
        self.stop()
        self._stop_server()
