import argparse
import hashlib
import os
import platform
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter, process_time
from typing import NotRequired, Protocol, TypeAlias, TypedDict, TypeGuard, overload

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.cli import cli_error, cli_messages
from vntts.services.tts_engine import TTSEngine
from vntts.settings import get_local_data_directory
from vntts.speech_backend import XTTSVoiceRouterBackend
from vntts.speech_worker import (
    create_chatterbox_worker_backend,
    create_moss_delay_worker_backend,
    create_moss_worker_backend,
    create_pocket_worker_backend,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisRequest,
    SynthesisResult,
)
from vntts.versioned_json import read_versioned_json
from vntts.voices import (
    CharacterVoiceRegistry,
    CharacterVoiceRouter,
    find_default_voice_manifest,
    is_narrator,
)

default_output = get_local_data_directory() / "benchmarks" / "tts"
default_text = "The tide is turning. We should return before the storm arrives."
TTS_BENCHMARK_CORPUS_VERSION = 1
TTS_BENCHMARK_CORPUS_SCHEMA = "vntts.tts-benchmark-corpus"
TTS_BENCHMARK_REPORT_SCHEMA = "vntts.tts-benchmark-report"
TTS_BENCHMARK_REPORT_VERSION = 1
PathInput: TypeAlias = str | os.PathLike[str]
Clock: TypeAlias = Callable[[], float]
BackendFactory: TypeAlias = Callable[[str, CharacterVoiceRegistry, PathInput], object]


class BenchmarkCorpusSample(TypedDict):
    id: str
    line_id: str
    character: str
    text: str
    text_sha256: str


class BenchmarkCorpus(TypedDict):
    name: str
    samples: list[BenchmarkCorpusSample]


class BenchmarkSampleInput(TypedDict):
    character: str
    text: str
    id: NotRequired[str]
    line_id: NotRequired[str]
    text_sha256: NotRequired[str]


class CacheStageReport(TypedDict):
    cache_source: str | None
    first_pcm_ms: float | None
    wall_ms: float | None
    underrun: None
    generation_limited: bool | None
    realtime_factor: NotRequired[float]


class BenchmarkSampleReport(TypedDict):
    id: str
    line_id: str
    character: str
    text: str
    text_sha256: str
    audio: str
    audio_sha256: str
    duration_seconds: float
    conditioning_ms: float
    first_audio_ms: float | None
    generation_wall_ms: float
    generation_cpu_ms: float
    realtime_factor: float
    cached_replay_ms: float
    fresh: CacheStageReport
    memory_cache: CacheStageReport
    persistent_cache: CacheStageReport
    dialogue_to_first_pcm_ms: float | None
    speaker_similarity_rating: None
    artifact_rating: None


class BenchmarkReport(TypedDict):
    schema: str
    schema_version: int
    version: int
    model_id: str
    seed: int | None
    backend: str
    platform: str
    python: str
    startup_wall_ms: float
    startup_cpu_ms: float
    peak_rss_mb: float | None
    corpus: str | None
    samples: list[BenchmarkSampleReport]


class _RenderableBackend(Protocol):
    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...


class BenchmarkBackend(_RenderableBackend, Protocol):
    registry: CharacterVoiceRegistry


class _ClearableCache(Protocol):
    def clear(self) -> None: ...


class _PersistentCacheBackend(_RenderableBackend, Protocol):
    persistent_audio_cache: object
    audio_cache: _ClearableCache


def _is_renderable_backend(value: object) -> TypeGuard[_RenderableBackend]:
    return callable(getattr(value, "render", None))


def _require_renderable_backend(value: object, name: str) -> _RenderableBackend:
    if not _is_renderable_backend(value):
        raise RuntimeError(f"Benchmark backend {name!r} has no typed renderer")
    return value


def _is_benchmark_backend(value: object) -> TypeGuard[BenchmarkBackend]:
    return isinstance(
        getattr(value, "registry", None), CharacterVoiceRegistry
    ) and _is_renderable_backend(value)


def _require_benchmark_backend(value: object, name: str) -> BenchmarkBackend:
    if not _is_benchmark_backend(value):
        raise RuntimeError(f"Benchmark backend {name!r} is missing its typed contract")
    return value


def _is_persistent_cache_backend(
    value: object,
) -> TypeGuard[_PersistentCacheBackend]:
    return hasattr(value, "persistent_audio_cache") and callable(
        getattr(getattr(value, "audio_cache", None), "clear", None)
    )


def _rss_mb() -> float | None:
    try:
        import resource
    except ImportError:  # pragma: no cover - unavailable on Windows
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        value *= 1024
    return value / (1024 * 1024)


def write_wav(path: PathInput, audio: object, sample_rate: int) -> Path:
    samples = np.asarray(audio, dtype=np.float32)
    if samples.ndim == 2 and samples.shape[1] in {1, 2}:
        samples = (
            samples[:, 0]
            if samples.shape[1] == 1
            else samples.mean(axis=1, dtype=np.float32)
        )
    if samples.ndim != 1:
        raise ValueError("Benchmark renderer PCM must be mono or frames-by-channel")
    return Path(write_pcm16_wav(path, samples, sample_rate))


@overload
def create_backend(
    name: str,
    registry: CharacterVoiceRegistry,
    cache_root: PathInput,
    *,
    model_name: object | None = None,
    model_revision: object | None = None,
    narrator_reference: PathInput | None = None,
    moss_streaming_first_chunk_frames: int | None = None,
    moss_streaming_interval: float | None = None,
    startup_cancellation: object | None = None,
    startup_progress: object | None = None,
    terms_accepted: bool = False,
    allow_gated_model_access: bool = False,
    require_cuda: bool = False,
    persistent_audio_cache_max_entries: int | None = None,
) -> BenchmarkBackend: ...


@overload
def create_backend(
    name: str,
    registry: CharacterVoiceRegistry,
    cache_root: PathInput,
    **options: object,
) -> BenchmarkBackend: ...


def create_backend(
    name: str,
    registry: CharacterVoiceRegistry,
    cache_root: PathInput,
    **options: object,
) -> BenchmarkBackend:
    allowed_options = {
        "model_name",
        "model_revision",
        "narrator_reference",
        "moss_streaming_first_chunk_frames",
        "moss_streaming_interval",
        "startup_cancellation",
        "startup_progress",
        "terms_accepted",
        "allow_gated_model_access",
        "require_cuda",
        "persistent_audio_cache_max_entries",
    }
    unexpected = sorted(set(options) - allowed_options)
    if unexpected:
        raise TypeError(f"Unexpected benchmark backend option: {unexpected[0]}")
    cache_root = Path(cache_root)
    model_name = options.get("model_name")
    model_revision = options.get("model_revision")
    narrator_reference = options.get("narrator_reference")
    moss_streaming_first_chunk_frames = options.get("moss_streaming_first_chunk_frames")
    moss_streaming_interval = options.get("moss_streaming_interval")
    startup_cancellation = options.get("startup_cancellation")
    startup_progress = options.get("startup_progress")
    persistent_audio_cache_max_entries = options.get(
        "persistent_audio_cache_max_entries"
    )
    common: dict[str, object] = {
        "persistent_audio_cache_directory": cache_root / "audio",
        **(
            {"persistent_audio_cache_max_entries": (persistent_audio_cache_max_entries)}
            if persistent_audio_cache_max_entries is not None
            else {}
        ),
        **(
            {"startup_cancellation": startup_cancellation}
            if startup_cancellation is not None
            else {}
        ),
        **(
            {"startup_progress": startup_progress}
            if startup_progress is not None
            else {}
        ),
        **(
            {"narrator_reference": narrator_reference}
            if narrator_reference is not None
            else {}
        ),
    }
    if name == "pocket-tts":
        return _require_benchmark_backend(
            create_pocket_worker_backend(
                registry,
                voice_state_cache_directory=cache_root / "voices",
                allow_gated_model_access=options.get("allow_gated_model_access", False),
                **common,
            ),
            name,
        )
    if name == "chatterbox-nano":
        return _require_benchmark_backend(
            create_chatterbox_worker_backend(
                registry,
                conditioning_cache_directory=cache_root / "conditionals",
                **common,
            ),
            name,
        )
    if name == "moss-tts":
        streaming_options = {
            key: value
            for key, value in {
                "streaming_first_chunk_frames": moss_streaming_first_chunk_frames,
                "streaming_interval": moss_streaming_interval,
            }.items()
            if value is not None
        }
        return _require_benchmark_backend(
            create_moss_worker_backend(
                registry,
                **({"model_name": str(model_name)} if model_name is not None else {}),
                **streaming_options,
                prompt_cache_directory=cache_root / "prompt-codes",
                **common,
            ),
            name,
        )
    if name == "moss-tts-delay":
        return _require_benchmark_backend(
            create_moss_delay_worker_backend(
                registry,
                **({"model_name": str(model_name)} if model_name is not None else {}),
                **(
                    {"model_revision": str(model_revision)}
                    if model_revision is not None
                    else {}
                ),
                generation_profile="expressive",
                require_cuda=options.get("require_cuda", False),
                **(
                    {"startup_cancellation": startup_cancellation}
                    if startup_cancellation is not None
                    else {}
                ),
                **(
                    {"narrator_reference": narrator_reference}
                    if narrator_reference is not None
                    else {}
                ),
            ),
            name,
        )
    if name == "coqui-xtts":
        if options.get("terms_accepted", False) is not True:
            raise ValueError(
                "XTTS v2 requires explicit acceptance of the Coqui Public Model "
                "License in the model-variant document"
            )
        os.environ["COQUI_TOS_AGREED"] = "1"
        engine = TTSEngine(
            model_name=str(
                model_name or "tts_models/multilingual/multi-dataset/xtts_v2"
            ),
            language="en",
            persisted_voice_cache=False,
        )
        voice_router_factory: Callable[..., object] = CharacterVoiceRouter
        xtts_backend_factory: Callable[..., BenchmarkBackend] = XTTSVoiceRouterBackend
        return xtts_backend_factory(
            voice_router_factory(engine, registry, force_reference_audio=True)
        )
    raise ValueError(f"Unsupported benchmark backend: {name}")


def load_tts_benchmark_corpus(path: PathInput) -> BenchmarkCorpus:
    document = read_versioned_json(
        Path(path),
        schema_version=TTS_BENCHMARK_CORPUS_VERSION,
        document_name="TTS benchmark corpus",
    )
    declared_schema = document.get("schema")
    if declared_schema not in {None, TTS_BENCHMARK_CORPUS_SCHEMA}:
        raise ValueError(
            f"Unsupported TTS benchmark corpus schema: {declared_schema!r}"
        )
    strict = declared_schema == TTS_BENCHMARK_CORPUS_SCHEMA
    raw_samples = document.get("samples", ())
    if not isinstance(raw_samples, list):
        raise ValueError("TTS benchmark corpus samples must be an array")
    samples: list[BenchmarkCorpusSample] = []
    seen_ids: set[str] = set()
    for index, sample in enumerate(raw_samples, start=1):
        if not isinstance(sample, dict):
            raise ValueError(f"TTS benchmark sample {index} must be an object")
        if not strict and any(key in sample for key in ("line_id", "text_sha256")):
            raise ValueError(
                "Strict benchmark identity fields require the "
                f"{TTS_BENCHMARK_CORPUS_SCHEMA!r} schema"
            )
        if strict:
            for field in ("id", "line_id", "character", "text"):
                value = sample.get(field)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"Strict TTS benchmark sample {index} {field} must be "
                        "non-empty text"
                    )
            character = str(sample["character"])
        else:
            character = str(sample.get("character") or "Narrator").strip() or "Narrator"
        raw_text = sample.get("text")
        text = (
            raw_text
            if strict and isinstance(raw_text, str)
            else " ".join(str(raw_text or "").split())
        )
        if not text:
            raise ValueError(f"TTS benchmark sample {index} has no text")
        sample_id = (
            str(sample["id"]) if strict else str(sample.get("id") or f"sample-{index}")
        )
        if sample_id in seen_ids:
            raise ValueError(f"Duplicate TTS benchmark sample ID: {sample_id!r}")
        seen_ids.add(sample_id)
        line_id = str(sample["line_id"]) if strict else sample_id
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if strict:
            if not sample.get("id") or not sample.get("line_id"):
                raise ValueError(
                    f"Strict TTS benchmark sample {index} requires id and line_id"
                )
            if sample.get("text_sha256") != text_digest:
                raise ValueError(
                    f"TTS benchmark sample {index} text_sha256 does not match exact text"
                )
        samples.append(
            {
                "id": sample_id,
                "line_id": line_id,
                "character": character,
                "text": text,
                "text_sha256": text_digest,
            }
        )
    if not samples:
        raise ValueError("TTS benchmark corpus has no samples")
    return {
        "name": str(document.get("name") or Path(path).stem),
        "samples": samples,
    }


def _safe_component(value: object, label: str) -> str:
    raw = str(value).strip()
    if not raw or raw in {".", ".."}:
        raise ValueError(f"{label} is not a safe output name: {value!r}")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")
    if not safe or safe in {".", ".."}:
        raise ValueError(f"{label} is not a safe output name: {value!r}")
    return safe


def _contained_child(root: PathInput, name: str, label: str) -> Path:
    root = Path(root).expanduser().resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError(f"{label} escapes benchmark output: {name!r}")
    return candidate


def _validate_render_result(
    result: SynthesisResult,
    request: SynthesisRequest,
    stage: str,
    *,
    expected_cache_source: str,
) -> SynthesisResult:
    if result.completion is not SynthesisCompletion.COMPLETE:
        raise RuntimeError(
            f"{stage} render did not complete: {result.completion.value}"
        )
    if not isinstance(result.sample_rate, int) or result.sample_rate <= 0:
        raise RuntimeError(f"{stage} render returned an invalid sample rate")
    if (
        result.diagnostics.seed != request.seed
        or result.diagnostics.generation_profile != request.generation_profile
    ):
        raise RuntimeError(f"{stage} render diagnostics do not match the request")
    if result.diagnostics.cache_source != expected_cache_source:
        raise RuntimeError(
            f"{stage} render used {result.diagnostics.cache_source!r}; "
            f"expected {expected_cache_source!r}"
        )
    return result


def benchmark_backend(
    backend_name: str,
    registry: CharacterVoiceRegistry,
    characters: Sequence[str],
    text: str,
    output_directory: PathInput,
    *,
    benchmark_samples: Sequence[BenchmarkCorpusSample | BenchmarkSampleInput]
    | None = None,
    corpus_name: str | None = None,
    model_id: object | None = None,
    seed: int | None = None,
    backend_factory: BackendFactory = create_backend,
    clock: Clock = perf_counter,
    cpu_clock: Clock = process_time,
) -> BenchmarkReport:
    """Finish every sample in staging before publishing this run's WAVs."""
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    created_backends: list[object] = []

    def tracked_backend_factory(
        name: str, registry: CharacterVoiceRegistry, cache: PathInput
    ) -> object:
        backend = backend_factory(name, registry, cache)
        created_backends.append(backend)
        return backend

    with TemporaryDirectory(
        prefix=".tts-benchmark-", dir=output_directory.parent
    ) as staging_directory:
        try:
            report = _benchmark_backend_staged(
                backend_name,
                registry,
                characters,
                text,
                staging_directory,
                benchmark_samples=benchmark_samples,
                corpus_name=corpus_name,
                model_id=model_id,
                seed=seed,
                backend_factory=tracked_backend_factory,
                clock=clock,
                cpu_clock=cpu_clock,
            )
        finally:
            stop_error = None
            for backend in reversed(created_backends):
                stop = getattr(backend, "stop", None)
                try:
                    if callable(stop):
                        stop()
                    shutdown = getattr(backend, "shutdown", None)
                    if callable(shutdown):
                        shutdown()
                except Exception as error:  # pragma: no cover - backend-specific
                    if stop_error is None:
                        stop_error = error
            if stop_error is not None and sys.exc_info()[0] is None:
                raise stop_error

        staging_root = Path(staging_directory).resolve()
        publications: list[tuple[BenchmarkSampleReport, Path, Path]] = []
        for sample in report["samples"]:
            staged = Path(sample["audio"]).resolve()
            if staged.parent != staging_root or not staged.is_file():
                raise RuntimeError("Benchmark staging produced an unsafe WAV path")
            destination = _contained_child(
                output_directory, staged.name, "Benchmark WAV"
            )
            if destination.exists():
                raise FileExistsError(
                    f"Benchmark WAV already exists; refusing to overwrite: {destination}"
                )
            publications.append((sample, staged, destination))

        output_directory.mkdir(parents=True, exist_ok=True)
        published = []
        try:
            for sample, staged, destination in publications:
                try:
                    os.link(staged, destination)
                except FileExistsError as error:
                    raise FileExistsError(
                        f"Benchmark WAV already exists; refusing to overwrite: {destination}"
                    ) from error
                published.append(destination)
                staged.unlink()
                sample["audio"] = str(destination)
        except Exception:
            for destination in published:
                destination.unlink(missing_ok=True)
            raise
    return report


def _benchmark_backend_staged(
    backend_name: str,
    registry: CharacterVoiceRegistry,
    characters: Sequence[str],
    text: str,
    output_directory: PathInput,
    *,
    benchmark_samples: Sequence[BenchmarkCorpusSample | BenchmarkSampleInput]
    | None = None,
    corpus_name: str | None = None,
    model_id: object | None = None,
    seed: int | None = None,
    backend_factory: BackendFactory = create_backend,
    clock: Clock = perf_counter,
    cpu_clock: Clock = process_time,
) -> BenchmarkReport:
    output_directory = Path(output_directory).expanduser().resolve()
    backend_component = _safe_component(backend_name, "Backend")
    work_items: tuple[BenchmarkCorpusSample | BenchmarkSampleInput, ...] = tuple(
        benchmark_samples
        or (
            {"id": character, "character": character, "text": text}
            for character in characters
        )
    )
    output_names: list[str] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(work_items, start=1):
        sample_id = str(item.get("id") or item.get("character") or f"sample-{index}")
        if sample_id in seen_ids:
            raise ValueError(f"Duplicate benchmark sample ID: {sample_id!r}")
        seen_ids.add(sample_id)
        character_component = _safe_component(item.get("character"), "Character")
        sample_component = _safe_component(sample_id, "Sample ID")
        output_names.append(
            f"{backend_component}-{character_component}-{sample_component}.wav"
        )
    if len(output_names) != len({name.casefold() for name in output_names}):
        raise ValueError("Benchmark samples collide as output WAV names")
    with TemporaryDirectory() as temporary_directory:
        wall_started = clock()
        cpu_started = cpu_clock()
        raw_backend = backend_factory(backend_name, registry, temporary_directory)
        backend = _require_renderable_backend(raw_backend, backend_name)
        generation_profile = getattr(backend, "generation_profile", "stable")
        if not isinstance(generation_profile, str):
            generation_profile = "stable"
        startup_wall_ms = (clock() - wall_started) * 1000
        startup_cpu_ms = (cpu_clock() - cpu_started) * 1000
        samples: list[BenchmarkSampleReport] = []
        for item, output_name in zip(work_items, output_names, strict=True):
            sample_id = str(item.get("id") or item["character"])
            character = item["character"]
            sample_text = item["text"]
            conditioning_started = clock()
            prime = getattr(backend, "prime", None)
            if callable(prime):
                prime(character)
            conditioning_ms = (clock() - conditioning_started) * 1000
            generation_started = clock()
            cpu_started = cpu_clock()
            fresh_request = SynthesisRequest(
                voice=character,
                text=sample_text,
                seed=seed,
                generation_profile=generation_profile,
                cache_policy=SynthesisCachePolicy.REFRESH,
            )
            rendered = _validate_render_result(
                backend.render(fresh_request).collect(),
                fresh_request,
                "Fresh",
                expected_cache_source="fresh-generation",
            )
            audio = rendered.pcm
            audio_sample_rate = rendered.sample_rate
            first_audio_ms = rendered.timing.first_chunk_ms
            fresh_cache_source = rendered.diagnostics.cache_source
            generation_wall_ms = (clock() - generation_started) * 1000
            generation_cpu_ms = (cpu_clock() - cpu_started) * 1000
            duration_seconds = len(audio) / audio_sample_rate
            fresh_underrun = None
            fresh_generation_limited = (
                rendered.completion is SynthesisCompletion.LIMITED
            )

            cached_started = clock()
            cache_request = replace(
                fresh_request, cache_policy=SynthesisCachePolicy.USE
            )
            memory_rendered = _validate_render_result(
                backend.render(cache_request).collect(),
                cache_request,
                "Memory-cache",
                expected_cache_source="memory-cache",
            )
            memory_cache_source = memory_rendered.diagnostics.cache_source
            cached_replay_ms = (clock() - cached_started) * 1000
            memory_first_audio_ms = memory_rendered.timing.first_chunk_ms
            memory_underrun = None
            memory_generation_limited = (
                memory_rendered.completion is SynthesisCompletion.LIMITED
            )

            persistent_replay_ms = None
            persistent_first_audio_ms = None
            persistent_cache_source = None
            persistent_underrun = None
            persistent_generation_limited = None
            if hasattr(backend, "persistent_audio_cache"):
                if not _is_persistent_cache_backend(backend):
                    raise RuntimeError("Persistent backend has no memory cache")
                backend.audio_cache.clear()
                persistent_started = clock()
                persistent_rendered = _validate_render_result(
                    backend.render(cache_request).collect(),
                    cache_request,
                    "Persistent-cache",
                    expected_cache_source="persistent-cache",
                )
                persistent_cache_source = persistent_rendered.diagnostics.cache_source
                persistent_replay_ms = (clock() - persistent_started) * 1000
                persistent_first_audio_ms = persistent_rendered.timing.first_chunk_ms
                persistent_underrun = None
                persistent_generation_limited = (
                    persistent_rendered.completion is SynthesisCompletion.LIMITED
                )

            expected_text_sha256 = hashlib.sha256(
                sample_text.encode("utf-8")
            ).hexdigest()
            declared_text_sha256 = item.get("text_sha256")
            if declared_text_sha256 not in {None, expected_text_sha256}:
                raise ValueError(
                    f"Benchmark sample {sample_id!r} text_sha256 does not match exact text"
                )
            audio_path = write_wav(
                _contained_child(output_directory, output_name, "Benchmark WAV"),
                audio,
                audio_sample_rate,
            )
            samples.append(
                {
                    "id": sample_id,
                    "line_id": str(item.get("line_id") or sample_id),
                    "character": character,
                    "text": sample_text,
                    "text_sha256": expected_text_sha256,
                    "audio": str(audio_path),
                    "audio_sha256": sha256_file(audio_path),
                    "duration_seconds": duration_seconds,
                    "conditioning_ms": conditioning_ms,
                    "first_audio_ms": first_audio_ms,
                    "generation_wall_ms": generation_wall_ms,
                    "generation_cpu_ms": generation_cpu_ms,
                    "realtime_factor": generation_wall_ms / (duration_seconds * 1000),
                    "cached_replay_ms": cached_replay_ms,
                    "fresh": {
                        "cache_source": fresh_cache_source,
                        "first_pcm_ms": first_audio_ms,
                        "wall_ms": generation_wall_ms,
                        "realtime_factor": generation_wall_ms
                        / (duration_seconds * 1000),
                        "underrun": fresh_underrun,
                        "generation_limited": fresh_generation_limited,
                    },
                    "memory_cache": {
                        "cache_source": memory_cache_source,
                        "first_pcm_ms": memory_first_audio_ms,
                        "wall_ms": cached_replay_ms,
                        "underrun": memory_underrun,
                        "generation_limited": memory_generation_limited,
                    },
                    "persistent_cache": {
                        "cache_source": persistent_cache_source,
                        "first_pcm_ms": persistent_first_audio_ms,
                        "wall_ms": persistent_replay_ms,
                        "underrun": persistent_underrun,
                        "generation_limited": persistent_generation_limited,
                    },
                    "dialogue_to_first_pcm_ms": (
                        conditioning_ms + first_audio_ms
                        if first_audio_ms is not None
                        else None
                    ),
                    "speaker_similarity_rating": None,
                    "artifact_rating": None,
                }
            )
    return {
        "schema": TTS_BENCHMARK_REPORT_SCHEMA,
        "schema_version": TTS_BENCHMARK_REPORT_VERSION,
        "version": 1,
        "model_id": str(model_id or backend_name),
        "seed": seed,
        "backend": backend_name,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "startup_wall_ms": startup_wall_ms,
        "startup_cpu_ms": startup_cpu_ms,
        "peak_rss_mb": _rss_mb(),
        "corpus": corpus_name,
        "samples": samples,
    }


def write_report(report: Mapping[str, object], output_directory: PathInput) -> Path:
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    backend_component = _safe_component(report["backend"], "Backend")
    path = _contained_child(
        output_directory, f"{backend_component}.json", "Benchmark report"
    )
    atomic_write_json(path, report)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark a live TTS backend")
    parser.add_argument(
        "--backend",
        required=True,
        choices=("pocket-tts", "chatterbox-nano", "moss-tts", "coqui-xtts"),
    )
    parser.add_argument("--character", action="append", dest="characters")
    parser.add_argument("--text", default=default_text)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--narrator-reference", type=Path)
    parser.add_argument(
        "--model",
        help="Local model path or backend model identifier (recommended offline)",
    )
    parser.add_argument(
        "--accept-xtts-terms",
        action="store_true",
        help="Accept the Coqui Public Model License required by XTTS v2",
    )
    parser.add_argument("--moss-first-chunk-frames", type=int)
    parser.add_argument("--moss-streaming-interval", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--output", type=Path, default=default_output)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    manifest = arguments.manifest or find_default_voice_manifest()
    narrator_reference = (
        arguments.narrator_reference.expanduser().resolve()
        if arguments.narrator_reference is not None
        else None
    )
    if narrator_reference is not None and not narrator_reference.is_file():
        cli_error(f"Narrator reference does not exist: {narrator_reference}")
        return 1
    if manifest is None and narrator_reference is None:
        cli_error("No complete voice manifest is available")
        return 1
    registry = (
        CharacterVoiceRegistry.from_file(manifest)
        if manifest is not None
        else CharacterVoiceRegistry()
    )
    corpus = (
        load_tts_benchmark_corpus(arguments.corpus)
        if arguments.corpus is not None
        else None
    )
    characters = (
        sorted({sample["character"] for sample in corpus["samples"]})
        if corpus is not None
        else arguments.characters or ["Kamuta", "Fatutu"]
    )
    missing = [
        character
        for character in characters
        if not is_narrator(character) and registry.resolve(character) is None
    ]
    if missing:
        cli_error(f"Voice is not available: {missing[0]}")
        return 1
    backend_factory: BackendFactory = create_backend
    if any(
        value is not None
        for value in (
            arguments.model,
            arguments.moss_first_chunk_frames,
            arguments.moss_streaming_interval,
            arguments.accept_xtts_terms or None,
            narrator_reference,
        )
    ):

        def configured_backend_factory(
            name: str, registry: CharacterVoiceRegistry, cache: PathInput
        ) -> object:
            return create_backend(
                name,
                registry,
                cache,
                model_name=arguments.model,
                moss_streaming_first_chunk_frames=(arguments.moss_first_chunk_frames),
                moss_streaming_interval=arguments.moss_streaming_interval,
                terms_accepted=arguments.accept_xtts_terms,
                **(
                    {"narrator_reference": narrator_reference}
                    if narrator_reference is not None
                    else {}
                ),
            )

        backend_factory = configured_backend_factory

    report = benchmark_backend(
        arguments.backend,
        registry,
        characters,
        arguments.text,
        arguments.output,
        benchmark_samples=corpus["samples"] if corpus is not None else None,
        corpus_name=corpus["name"] if corpus is not None else None,
        model_id=f"{arguments.backend}/{arguments.model or 'default'}",
        seed=arguments.seed,
        backend_factory=backend_factory,
    )
    report_path = write_report(report, arguments.output)
    cli_messages(
        (
            report_path,
            *(
                f"{sample['character']}: first audio {sample['first_audio_ms']:.0f} ms, "
                f"RTF {sample['realtime_factor']:.2f}, cache "
                f"{sample['cached_replay_ms']:.1f} ms"
                for sample in report["samples"]
            ),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
