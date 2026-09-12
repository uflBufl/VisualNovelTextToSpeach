"""Render a fixed, cacheless native MOSS pause comparison into a new directory."""

from __future__ import annotations

import argparse
import io
import os
import tempfile
import wave
import zipfile
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from uuid import uuid4

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import read_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts import support
from vntts.authoring.speech_quality import analyze_generated_speech_samples
from vntts.moss_cpp_backend import MossCppVoiceRouterBackend, moss_cpp_paths
from vntts.pregeneration_audition import _mono_pcm
from vntts.reference_quality import analyze_reference
from vntts.runtime_config import initialize_voice_registry
from vntts.settings import get_settings_path, load_app_settings
from vntts.speech_backend import (
    get_moss_tts_generation_profile,
    moss_tts_generation_profiles,
)
from vntts.support import collect_build_identity, native_speech_context
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
    moss_generation_limits,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry, find_voice_assignment

TEXTS = (
    ("short", "The storm has passed."),
    ("joined", "The storm has passed, we can continue our journey."),
    ("sentences", "The storm has passed. We can continue our journey."),
)
PROFILES = ("stable", "expressive")
# Freeze the diagnostic controls independently of production profile changes.
PROBE_SAMPLING = {
    profile: {**moss_tts_generation_profiles[profile], "audio_temperature": temperature}
    for profile, temperature in zip(PROFILES, (0.8, 1.7), strict=True)
}
ZIP_MAX_BYTES = 128 * 1024 * 1024


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--alternate-reference", type=Path)
    parser.add_argument("--timing-sequence", action="store_true")
    parser.add_argument(
        "--require-changing-voice",
        action="store_true",
        help="Use a second saved game voice when no alternate reference is supplied",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument(
        "--cancel-restart",
        action="store_true",
        help="Cancel one active request, then prove a fresh request restarts the server",
    )
    return parser


def _sha256(path):
    return sha256_file(path)


def _write_json(path, document):
    atomic_write_json(path, document, ensure_ascii=False, indent=2, sort_keys=True)


def _pcm16(samples):
    values = np.asarray(samples, dtype=np.float32)
    if not values.size or not np.isfinite(values).all():
        raise ValueError("MOSS probe received empty or non-finite PCM")
    return np.rint(np.clip(values, -1.0, 1.0) * 32767).astype(np.int16)


def _quality(samples, sample_rate):
    values = np.asarray(samples)
    quality, _spans = analyze_generated_speech_samples(
        values,
        sample_rate=sample_rate,
        duration_seconds=len(values) / sample_rate,
        analysis_version=2,
    )
    return asdict(quality)


def _raw_measurements(data):
    with wave.open(io.BytesIO(data), "rb") as source:
        channels, sample_rate, frames = (
            source.getnchannels(),
            source.getframerate(),
            source.getnframes(),
        )
        if (
            source.getsampwidth() != 2
            or channels != 2
            or sample_rate != 48000
            or source.getcomptype() != "NONE"
        ):
            raise ValueError("native response was not PCM16 stereo WAV")
        raw = source.readframes(frames)
        if len(raw) != frames * 4:
            raise ValueError("native response WAV was truncated")
        pcm16 = np.frombuffer(raw, dtype="<i2").reshape(-1, 2)
    pcm = pcm16.astype(np.float32) / 32768
    mono = _mono_pcm(pcm)
    return {
        "sample_rate": sample_rate,
        "wav_frames": frames,
        "seconds": round(frames / sample_rate, 6),
        "left": _quality(pcm16[:, 0], sample_rate),
        "right": _quality(pcm16[:, 1], sample_rate),
        "mono": _quality(_pcm16(mono), sample_rate),
    }


@contextmanager
def _configured_native_paths(executable, model, *, extra=None):
    changes = {
        "VNTTS_MOSS_CPP_EXECUTABLE": str(executable.expanduser().resolve())
        if executable is not None
        else None,
        "VNTTS_MOSS_GGUF": str(model.expanduser().resolve())
        if model is not None
        else None,
        **(extra or {}),
    }
    previous = {name: os.environ.get(name) for name in changes}
    try:
        for name, path in changes.items():
            if path is not None:
                os.environ[name] = str(path)
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _capture_native_response(backend):
    original = backend._http
    responses = []

    def wrapped(method, path, body=None, *, timeout=None):
        response = original(method, path, body, timeout=timeout)
        if path == "/tts" and method == "POST" and isinstance(response, tuple):
            if len(response) == 3 and isinstance(response[2], bytes):
                responses.append(response)
        return response

    backend._http = wrapped

    def restore():
        backend._http = original

    return responses, restore


def _attempt_name(index, profile, label):
    return f"attempt-{index:02d}-{profile}-{label}"


def _saved_narrator_reference(settings, registry_initializer):
    registry = registry_initializer(settings)
    assignment = find_voice_assignment(
        getattr(settings, "voice_assignments", {}), "Narrator"
    )
    voice = None
    if registry is not None:
        voice = (
            registry.resolve_source(assignment)
            if assignment
            else registry.resolve("Narrator")
        )
    references = () if voice is None else tuple(voice.references)
    if len(references) != 1 or not references[0].is_file():
        raise ValueError(
            "saved Narrator voice must resolve to exactly one existing reference; "
            "pass --reference PATH"
        )
    return references[0].resolve(), registry


def _qualification_alternate(options, registry, narrator_reference):
    explicit = getattr(options, "alternate_reference", None)
    if explicit is not None:
        reference = explicit.expanduser().resolve()
        voices = tuple(
            {id(voice): voice for voice in registry.voices.values()}.values()
        )
        voice = CharacterVoice("Qualification alternate", "alternate", reference)
        return voice.character, reference, CharacterVoiceRegistry((*voices, voice))
    if not getattr(options, "require_changing_voice", False):
        return None, None, registry
    voices = sorted(
        {id(voice): voice for voice in registry.voices.values()}.values(),
        key=lambda voice: voice.character.casefold(),
    )
    narrator_sha256 = _sha256(narrator_reference)
    for voice in voices:
        if voice.character.casefold() == "narrator" or not voice.references:
            continue
        reference = voice.references[0].expanduser().resolve()
        if (
            reference == narrator_reference
            or not reference.is_file()
            or _sha256(reference) == narrator_sha256
        ):
            continue
        try:
            preflight = analyze_reference(reference)
        except OSError, ValueError:
            continue
        if preflight["objective_preflight"] == "pass":
            return voice.character, reference, registry
    raise ValueError(
        "no second usable saved game voice was found; pass --alternate-reference PATH"
    )


def _write_archive(output, archive, *, recursive=False):
    paths = [
        path
        for path in (output.rglob("*") if recursive else output.iterdir())
        if path.is_file()
        and (
            path.name.endswith(".json")
            or path.name.endswith("-raw.wav")
            or path.name.endswith("-output-mono.wav")
        )
    ]
    if sum(path.stat().st_size for path in paths) > ZIP_MAX_BYTES:
        raise ValueError("probe artifacts exceed the 128 MiB archive limit")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{archive.name}-", dir=archive.parent
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
            for path in sorted(paths):
                bundle.write(path, path.relative_to(output).as_posix())
        Path(temporary).replace(archive)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _render_attempt(
    backend,
    output,
    index,
    profile,
    label,
    text,
    responses,
    *,
    sampling_profiles=PROBE_SAMPLING,
    voice="Narrator",
    phase="diagnostic-warm",
):
    name = _attempt_name(index, profile, label)
    expected_tokens, expected_seconds = moss_generation_limits(text)
    profile_name, sampling = get_moss_tts_generation_profile(
        profile, profiles=sampling_profiles
    )
    record = {
        "id": name,
        "seed": 1,
        "text": text,
        "voice": voice,
        "phase": phase,
        "profile": profile_name,
        "sampling": sampling,
        "production_limits": {
            "max_tokens": expected_tokens,
            "max_audio_seconds": expected_seconds,
        },
        "completion": None,
        "files": {},
    }
    before = len(responses)
    trace_id = uuid4().hex
    started = monotonic()
    try:
        with native_speech_context.set({"attempt_id": trace_id}):
            result = backend.render(
                SynthesisRequest(
                    voice,
                    text,
                    seed=1,
                    generation_profile=profile_name,
                    cache_policy=SynthesisCachePolicy.BYPASS,
                )
            ).collect()
        record["completion"] = result.completion.value
        record["elapsed_seconds"] = round(monotonic() - started, 3)
        record["result"] = {
            "cache_source": result.diagnostics.cache_source,
            "sample_rate": int(result.sample_rate),
            "pcm_frames": int(len(result.pcm)),
            "diagnostic_frames": int(result.diagnostics.sample_count),
            "timing_ms": round(float(result.timing.total_ms), 3),
            "limits": {
                "max_tokens": result.limits.max_tokens,
                "max_audio_seconds": result.limits.max_audio_seconds,
            },
        }
        output_validation_started = monotonic()
        mono = _mono_pcm(result.pcm)
        mono_path = output / f"{name}-output-mono.wav"
        write_pcm16_wav(mono_path, mono, result.sample_rate)
        written_mono, written_info = read_pcm16_mono_wav(mono_path)
        record["output_quality"] = _quality(
            np.asarray(written_mono, dtype=np.int16), written_info.sample_rate
        )
        record["output_wav_validation_s"] = round(
            monotonic() - output_validation_started, 6
        )
        record["files"].update(
            {
                "output_mono_wav": {
                    "path": mono_path.name,
                    "sha256": _sha256(mono_path),
                },
            }
        )
    except KeyboardInterrupt:
        record["completion"] = "cancelled"
        record["elapsed_seconds"] = round(monotonic() - started, 3)
        record["interrupted"] = True
    except Exception as error:
        record["completion"] = "failed"
        record["elapsed_seconds"] = round(monotonic() - started, 3)
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["native"] = next(
            (
                event["native"]
                for event in reversed(support.native_speech_log.report()["events"])
                if event.get("native", {}).get("attempt_id") == trace_id
                and event["native"].get("operation") == "fresh-generation"
            ),
            None,
        )
        for status, headers, data in responses[before:]:
            raw_validation_started = monotonic()
            raw_path = output / f"{name}-raw.wav"
            raw_path.write_bytes(data)
            record["raw_response"] = {
                "http_status": status,
                "headers": {
                    str(key).lower(): str(value) for key, value in headers.items()
                },
                "path": raw_path.name,
                "sha256": _sha256(raw_path),
                "bytes": len(data),
            }
            record["files"]["raw_wav"] = {
                "path": raw_path.name,
                "sha256": _sha256(raw_path),
            }
            try:
                record["raw_quality"] = _raw_measurements(data)
            except (EOFError, ValueError, wave.Error) as error:
                record["raw_quality_error"] = str(error)
            record["raw_wav_validation_s"] = round(
                record.get("raw_wav_validation_s", 0)
                + monotonic()
                - raw_validation_started,
                6,
            )
    return record


def _capture_owned_server(backend, servers):
    server = getattr(backend, "server", None)
    if server is not None and not any(server is known for known in servers):
        servers.append(server)


def _cancel_and_restart(
    backend,
    output,
    index,
    responses,
    *,
    timeout=15,
    cancellation_text="The qualification request must remain active until cancellation is observed.",
):
    cancellation = Event()
    attempt_id = uuid4().hex
    result = []

    def render():
        try:
            with native_speech_context.set({"attempt_id": attempt_id}):
                result.append(
                    backend.render(
                        SynthesisRequest(
                            "Narrator",
                            cancellation_text,
                            seed=1,
                            generation_profile="stable",
                            cache_policy=SynthesisCachePolicy.BYPASS,
                            cancellation=cancellation,
                        )
                    ).collect()
                )
        except BaseException as error:
            result.append(error)

    task = Thread(target=render, daemon=True)
    task.start()
    deadline = monotonic() + timeout
    old_server = None
    while task.is_alive() and monotonic() < deadline:
        started = any(
            event.get("native", {}).get("attempt_id") == attempt_id
            and event["native"].get("operation") == "request-start"
            for event in support.native_speech_log.report()["events"]
        )
        if started:
            with backend.server_lock:
                old_server = backend.server
            break
        cancellation.wait(0.05)
    if old_server is None:
        cancellation.set()
        task.join(timeout=2)
        raise RuntimeError("Cancellation probe did not observe an active request")

    # Let the HTTP worker enter native generation; the real candidate is much
    # slower than this bounded delay, while the fake server blocks explicitly.
    cancellation.wait(0.25)
    cancellation.set()
    task.join(timeout=timeout)
    if task.is_alive():
        backend.shutdown()
        raise RuntimeError("Cancelled MOSS request did not stop")
    if len(result) != 1 or isinstance(result[0], BaseException):
        error = result[0] if result else RuntimeError("missing cancellation result")
        raise RuntimeError(f"Cancelled MOSS request failed: {error}")
    if result[0].completion is not SynthesisCompletion.CANCELLED:
        raise RuntimeError(
            f"MOSS request completed before cancellation: {result[0].completion.value}"
        )
    if old_server.poll() is None:
        raise RuntimeError("Cancelled MOSS server is still running")

    restart = _render_attempt(
        backend,
        output,
        index,
        "stable",
        "restart",
        "The storm has passed.",
        responses,
    )
    if restart["completion"] != SynthesisCompletion.COMPLETE.value:
        raise RuntimeError("Fresh render after cancellation did not complete")
    with backend.server_lock:
        new_server = backend.server
    if new_server is None or new_server.pid == old_server.pid:
        raise RuntimeError("Fresh render did not start a new owned server")
    cancelled_event = next(
        event["native"]
        for event in reversed(support.native_speech_log.report()["events"])
        if event.get("native", {}).get("attempt_id") == attempt_id
        and event["native"].get("operation") == "fresh-generation"
    )
    return {
        "cancelled_completion": result[0].completion.value,
        "cancelled_outcome": cancelled_event.get("outcome"),
        "cancelled_server": {
            "pid": old_server.pid,
            "returncode": old_server.poll(),
            "confirmed_exited": old_server.poll() is not None,
        },
        "restart_server_pid": new_server.pid,
        "restart_attempt": restart,
    }


def run(
    options,
    *,
    backend_factory=MossCppVoiceRouterBackend,
    path_check=moss_cpp_paths,
    settings_loader=load_app_settings,
    registry_initializer=initialize_voice_registry,
    sampling_profiles=PROBE_SAMPLING,
):
    settings = (
        settings_loader()
        if options.reference is None or options.model is None
        else None
    )
    if options.reference is None:
        reference, registry = _saved_narrator_reference(settings, registry_initializer)
    else:
        reference, registry = (
            options.reference.expanduser().resolve(),
            CharacterVoiceRegistry(),
        )
    output = options.output.expanduser().resolve()
    archive = output.parent / f"{output.name}.zip"
    if not reference.is_file():
        raise ValueError(f"reference does not exist: {reference}")
    if output.exists():
        raise ValueError(f"output directory already exists: {output}")
    if archive.exists():
        raise ValueError(f"output archive already exists: {archive}")
    alternate_voice, alternate_reference, registry = _qualification_alternate(
        options, registry, reference
    )
    output.mkdir(parents=True)
    report = {
        "schema": "vntts.moss-native-probe",
        "schema_version": 1,
        "reference": reference.name,
        "reference_sha256": _sha256(reference),
        "contains_generated_voice_audio": True,
        "seed": 1,
        "matrix": {
            "texts": [dict(label=label, text=text) for label, text in TEXTS],
            "profiles": list(sampling_profiles),
        },
        "attempts": [],
    }
    if options.reference is None:
        report["saved_selection"] = {
            "settings_file": get_settings_path().name,
            "voice_manifest": Path(settings.voice_manifest).name
            if getattr(settings, "voice_manifest", None)
            else None,
            "narrator_assignment": find_voice_assignment(
                getattr(settings, "voice_assignments", {}), "Narrator"
            ),
        }
    backend = None
    prompt_cache = tempfile.TemporaryDirectory(
        prefix="vntts-moss-probe-voices-", ignore_cleanup_errors=True
    )
    owned_servers = []
    exit_code = 0
    try:
        print(
            f"Reference: {reference.name}\nSHA-256: {report['reference_sha256']}",
            flush=True,
        )
        preflight = {**analyze_reference(reference), "path": reference.name}
        report["reference_preflight"] = preflight
        if preflight["sha256"] != report["reference_sha256"]:
            raise ValueError("reference changed during preflight")
        if preflight["objective_preflight"] != "pass":
            raise ValueError(
                "Reference preflight failed "
                f"({preflight['duration_seconds']:.3f}s): "
                + ", ".join(preflight["rejection_reasons"])
                + ". Select a usable spoken reference or pass --reference PATH."
            )
        print(
            f"Reference preflight passed: {preflight['duration_seconds']:.3f}s",
            flush=True,
        )
        if alternate_reference is not None:
            alternate_preflight = {
                **analyze_reference(alternate_reference),
                "path": alternate_reference.name,
            }
            alternate_sha256 = _sha256(alternate_reference)
            if alternate_sha256 == report["reference_sha256"]:
                raise ValueError("alternate reference contains the same audio")
            if alternate_preflight["objective_preflight"] != "pass":
                raise ValueError(
                    "Alternate reference preflight failed: "
                    + ", ".join(alternate_preflight["rejection_reasons"])
                )
            report["alternate_reference"] = {
                "name": alternate_reference.name,
                "sha256": alternate_sha256,
                "preflight": alternate_preflight,
            }
        model_name = options.model if options.model is not None else settings.tts_model
        with _configured_native_paths(options.executable, options.model):
            executable, model, sidecar = path_check(model_name)
        # Constructor setup returns immediately only when both resolved assets are
        # explicit. This scope prevents the managed installer from downloading.
        with _configured_native_paths(executable, model):
            report["native_paths"] = {
                "executable": executable.name,
                "model": model.name,
                "sidecar": sidecar.name,
            }
            startup_started = monotonic()
            backend = backend_factory(
                registry,
                model_name=model,
                narrator_reference=reference,
                startup_timeout=180,
                request_timeout=120,
                startup_progress=lambda message: print(message, flush=True),
                audio_cache_size=0,
                persistent_audio_cache_directory=output / ".cache-disabled",
                persistent_audio_cache_max_entries=0,
                prompt_cache_directory=Path(prompt_cache.name),
            )
            _capture_owned_server(backend, owned_servers)
            report["startup_seconds"] = round(monotonic() - startup_started, 3)
            report["runtime"] = getattr(backend, "server_info", None)
            report["compute"] = getattr(backend, "runtime_status", None)
            backend._generation_profiles = sampling_profiles
            responses, restore = _capture_native_response(backend)
            report["http_capture_method"] = "_http"
            try:
                sequence = [
                    (
                        profile,
                        label,
                        text,
                        "Narrator",
                        "process-cold" if not index else "diagnostic-warm",
                    )
                    for index, (profile, (label, text)) in enumerate(
                        (profile, item)
                        for profile in sampling_profiles
                        for item in TEXTS
                    )
                ]
                if getattr(options, "timing_sequence", False):
                    sequence.insert(
                        1,
                        (
                            "stable",
                            "same-voice-warm",
                            TEXTS[0][1],
                            "Narrator",
                            "same-voice-warm",
                        ),
                    )
                if alternate_reference is not None:
                    sequence.extend(
                        (
                            (
                                "stable",
                                phase,
                                TEXTS[0][1],
                                alternate_voice,
                                phase,
                            )
                            for phase in ("changed-voice-cold", "changed-voice-warm")
                        )
                    )
                report["expected_attempt_count"] = len(sequence) + int(
                    getattr(options, "cancel_restart", False)
                )
                for index, (profile, label, text, voice, phase) in enumerate(
                    sequence,
                    1,
                ):
                    print(
                        f"[{index}/{len(sequence)}] {profile} {label}",
                        flush=True,
                    )
                    try:
                        attempt = _render_attempt(
                            backend,
                            output,
                            index,
                            profile,
                            label,
                            text,
                            responses,
                            sampling_profiles=sampling_profiles,
                            voice=voice,
                            phase=phase,
                        )
                        report["attempts"].append(attempt)
                        _write_json(output / f"{attempt['id']}.json", attempt)
                        _write_json(output / "report.json", report)
                        if attempt["completion"] == "failed":
                            exit_code = 1
                            break
                        if attempt.get("interrupted"):
                            report["interrupted"] = True
                            exit_code = 130
                            break
                    finally:
                        _capture_owned_server(backend, owned_servers)
                if getattr(options, "cancel_restart", False) and exit_code == 0:
                    print("[cancel/restart] active request", flush=True)
                    receipt = _cancel_and_restart(
                        backend, output, len(report["attempts"]) + 1, responses
                    )
                    report["cancel_restart"] = {
                        key: value
                        for key, value in receipt.items()
                        if key != "restart_attempt"
                    }
                    report["attempts"].append(receipt["restart_attempt"])
                    attempt = receipt["restart_attempt"]
                    _write_json(output / f"{attempt['id']}.json", attempt)
                    _write_json(output / "report.json", report)
                    _capture_owned_server(backend, owned_servers)
            finally:
                restore()
    except KeyboardInterrupt:
        report["interrupted"] = True
        exit_code = 130
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], flush=True)
        exit_code = 1
    finally:
        if backend is not None:
            _capture_owned_server(backend, owned_servers)
            try:
                backend.shutdown()
            except (Exception, KeyboardInterrupt) as error:
                report["shutdown_error"] = f"{type(error).__name__}: {error}"
                exit_code = (
                    130
                    if isinstance(error, KeyboardInterrupt) or exit_code == 130
                    else 1
                )
        prompt_cache.cleanup()
        # Keep each observed Popen, not a PID lookup: shutdown clears backend.server
        # and the OS may reuse its PID. Unknown is not proof of clean shutdown.
        receipts = []
        for server in owned_servers:
            try:
                returncode = server.poll()
                receipts.append(
                    {
                        "pid": server.pid,
                        "returncode": returncode,
                        "confirmed_exited": returncode is not None,
                    }
                )
            except Exception as error:
                receipts.append(
                    {
                        "confirmed_exited": None,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
        confirmed = [receipt["confirmed_exited"] for receipt in receipts]
        report["server_shutdown"] = {
            "servers": receipts,
            "confirmed_exited": (
                False
                if False in confirmed
                else None
                if None in confirmed or not confirmed
                else True
            ),
        }
        if False in confirmed:
            report["shutdown_error"] = "Owned native server is still running"
            exit_code = 130 if exit_code == 130 else 1
        elif None in confirmed:
            report["shutdown_error"] = "Owned native server shutdown status is unknown"
            exit_code = 130 if exit_code == 130 else 1
        report["all_requests_complete"] = (
            len(report["attempts"]) == report.get("expected_attempt_count")
            and bool(report["attempts"])
            and all(
                attempt.get("completion") == SynthesisCompletion.COMPLETE.value
                and attempt.get("result", {}).get("cache_source") == "fresh-generation"
                and attempt.get("raw_response", {}).get("http_status") == 200
                and "raw_quality_error" not in attempt
                for attempt in report["attempts"]
            )
        )
        report["exit_code"] = exit_code
        report["archive"] = archive.name
        _write_json(output / "report.json", report)
        _write_json(output / "native-speech.json", support.native_speech_log.report())
        _write_json(output / "build.json", collect_build_identity())
        _write_archive(output, archive)
    return exit_code


def main(argv=None):
    options = _parser().parse_args(argv)
    try:
        exit_code = run(options)
    except ValueError as error:
        _parser().error(str(error))
    output = options.output.expanduser().resolve()
    archive = output.parent / f"{output.name}.zip"
    print(f"archive: {archive} (includes locally stored generated audio)", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
