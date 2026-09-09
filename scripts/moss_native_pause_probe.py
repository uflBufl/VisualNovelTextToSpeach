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
from time import monotonic

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import read_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.speech_quality import analyze_generated_speech_samples
from vntts.moss_cpp_backend import MossCppVoiceRouterBackend, moss_cpp_paths
from vntts.pregeneration_audition import _mono_pcm
from vntts.reference_quality import analyze_reference
from vntts.runtime_config import initialize_voice_registry
from vntts.settings import load_app_settings
from vntts.speech_backend import (
    get_moss_tts_generation_profile,
    moss_tts_generation_profiles,
)
from vntts.support import collect_build_identity, native_speech_log
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisRequest,
    moss_generation_limits,
)
from vntts.voices import CharacterVoiceRegistry

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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--executable", type=Path)
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
def _configured_native_paths(executable, model):
    changes = {
        "VNTTS_MOSS_CPP_EXECUTABLE": executable,
        "VNTTS_MOSS_GGUF": model,
    }
    previous = {name: os.environ.get(name) for name in changes}
    try:
        for name, path in changes.items():
            if path is not None:
                os.environ[name] = str(path.expanduser().resolve())
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
    voice = registry.resolve("Narrator") if registry is not None else None
    references = () if voice is None else tuple(voice.references)
    if len(references) != 1 or not references[0].is_file():
        raise ValueError(
            "saved Narrator voice must resolve to exactly one existing reference; "
            "pass --reference PATH"
        )
    return references[0].resolve(), registry


def _write_archive(output, archive):
    paths = [
        path
        for path in output.iterdir()
        if path.name == "report.json"
        or path.name.endswith(".json")
        or path.name.endswith("-raw.wav")
        or path.name.endswith("-output-mono.wav")
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
                bundle.write(path, path.name)
        Path(temporary).replace(archive)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _render_attempt(backend, output, index, profile, label, text, responses):
    name = _attempt_name(index, profile, label)
    expected_tokens, expected_seconds = moss_generation_limits(text)
    profile_name, sampling = get_moss_tts_generation_profile(
        profile, profiles=PROBE_SAMPLING
    )
    record = {
        "id": name,
        "seed": 1,
        "text": text,
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
    started = monotonic()
    try:
        result = backend.render(
            SynthesisRequest(
                "Narrator",
                text,
                seed=1,
                generation_profile=profile_name,
                cache_policy=SynthesisCachePolicy.BYPASS,
            )
        ).collect()
        record["completion"] = result.completion.value
        record["elapsed_seconds"] = round(monotonic() - started, 3)
        record["result"] = {
            "sample_rate": int(result.sample_rate),
            "pcm_frames": int(len(result.pcm)),
            "diagnostic_frames": int(result.diagnostics.sample_count),
            "timing_ms": round(float(result.timing.total_ms), 3),
            "limits": {
                "max_tokens": result.limits.max_tokens,
                "max_audio_seconds": result.limits.max_audio_seconds,
            },
        }
        mono = _mono_pcm(result.pcm)
        mono_path = output / f"{name}-output-mono.wav"
        write_pcm16_wav(mono_path, mono, result.sample_rate)
        written_mono, written_info = read_pcm16_mono_wav(mono_path)
        record["output_quality"] = _quality(
            np.asarray(written_mono, dtype=np.int16), written_info.sample_rate
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
        for status, headers, data in responses[before:]:
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
    return record


def run(
    options,
    *,
    backend_factory=MossCppVoiceRouterBackend,
    path_check=moss_cpp_paths,
    settings_loader=load_app_settings,
    registry_initializer=initialize_voice_registry,
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
    output.mkdir(parents=True)
    report = {
        "reference": str(reference),
        "reference_sha256": _sha256(reference),
        "seed": 1,
        "matrix": {
            "texts": [dict(label=label, text=text) for label, text in TEXTS],
            "profiles": list(PROFILES),
        },
        "attempts": [],
    }
    backend = None
    exit_code = 0
    try:
        preflight = analyze_reference(reference)
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
        model_name = options.model if options.model is not None else settings.tts_model
        with _configured_native_paths(options.executable, options.model):
            executable, model, sidecar = path_check(model_name)
        # Constructor setup returns immediately only when both resolved assets are
        # explicit. This scope prevents the managed installer from downloading.
        with _configured_native_paths(executable, model):
            report["native_paths"] = {
                "executable": str(executable),
                "model": str(model),
                "sidecar": str(sidecar),
            }
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
            )
            backend._generation_profiles = PROBE_SAMPLING
            responses, restore = _capture_native_response(backend)
            report["http_capture_method"] = "_http"
            try:
                for index, (profile, (label, text)) in enumerate(
                    ((profile, item) for profile in PROFILES for item in TEXTS), 1
                ):
                    print(f"[{index}/6] {profile} {label}", flush=True)
                    attempt = _render_attempt(
                        backend, output, index, profile, label, text, responses
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
            backend.shutdown()
        report["archive"] = archive.name
        _write_json(output / "report.json", report)
        _write_json(output / "native-speech.json", native_speech_log.report())
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
