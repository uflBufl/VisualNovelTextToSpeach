"""Generic, device-independent model comparison for authoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_bytes, atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import VoiceManifestError

from vntts.authoring.bulk_generation import (
    load_generation_state,
)
from vntts.authoring.speech_quality import measure_generated_speech_bytes
from vntts.cli import cli_error, cli_messages
from vntts.document_identity import canonical_document_sha256
from vntts.settings import get_local_data_directory
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    find_default_voice_manifest,
    is_narrator,
    read_voice_reference_bytes,
)

CORPUS_SCHEMA = "vntts.tts-benchmark-corpus"
MODEL_REPORT_SCHEMA = "vntts.voice-model-report"
BENCHMARK_SCHEMA = "vntts.voice-model-benchmark"
SCHEMA_VERSION = 1
default_output = get_local_data_directory() / "authoring" / "model-benchmark"
UNSEEDED_BACKENDS = frozenset({"coqui-xtts", "pocket-tts"})


class ModelBenchmarkError(RuntimeError):
    """A generic model benchmark cannot be completed safely."""


@dataclass(frozen=True)
class ModelVariant:
    model_id: str
    backend: str
    model: str | None = None
    model_revision: str | None = None
    generation_profile: str = "stable"
    voice: str | None = None
    terms_accepted: bool = False
    require_cuda: bool = False


def select_representative_items(items, sample_size=24):
    """Round-robin generation-ready records by delivery/emotion label."""
    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size < 1
    ):
        raise ModelBenchmarkError("Benchmark sample size must be positive")
    buckets = defaultdict(list)
    for item in items:
        document = item.document if hasattr(item, "document") else item
        if document.get("action") != "generate":
            continue
        emotion = document.get("emotion")
        if isinstance(emotion, dict):
            label = str(emotion.get("primary") or "neutral")
        else:
            label = str(emotion or "neutral")
        buckets[label].append(document)
    selected = []
    while len(selected) < sample_size and buckets:
        for label in sorted(tuple(buckets)):
            if len(selected) >= sample_size:
                break
            selected.append(buckets[label].pop(0))
            if not buckets[label]:
                del buckets[label]
    return selected


def build_benchmark_corpus(queue_path, output_path, *, sample_size=24, name=None):
    """Create a generic corpus from a shared generation queue."""
    queue = VoiceGenerationQueue.load(queue_path)
    selected = select_representative_items(queue.items, sample_size)
    if not selected:
        raise ModelBenchmarkError("Generation queue has no generation-ready samples")
    samples = []
    for item in selected:
        character = str(
            item.get("voice_character") or item.get("speaker") or "Narrator"
        )
        samples.append(
            {
                "id": str(item["queue_id"]),
                "character": character,
                "text": str(item["text"]),
                "line_id": str(item["line_id"]),
                "text_sha256": str(item["text_sha256"]),
            }
        )
    document = {
        "schema": CORPUS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "name": name or Path(queue_path).stem,
        "source_queue": str(Path(queue_path).expanduser().resolve()),
        "samples": samples,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, document, sort_keys=True)
    return document


def build_failure_comparison_corpus(
    queue_path,
    state_path,
    output_path,
    *,
    pocket_sample_size=12,
    control_sample_size=12,
    name=None,
    manifest_path=None,
    narrator_character=None,
    state_loader=load_generation_state,
):
    """Bind failures, Pocket recoveries and MOSS controls into one exact corpus."""
    queue_path = Path(queue_path).expanduser().resolve()
    state_path = Path(state_path).expanduser().resolve()
    try:
        queue_payload = queue_path.read_bytes()
        state_payload = state_path.read_bytes()
        state_snapshot = json.loads(state_payload)
    except (OSError, json.JSONDecodeError) as error:
        raise ModelBenchmarkError(
            f"Unable to capture comparison inputs: {error}"
        ) from error
    queue_sha256 = hashlib.sha256(queue_payload).hexdigest()
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    queue = VoiceGenerationQueue.load(queue_path)
    state = state_loader(state_path, queue_path)
    if state != state_snapshot:
        raise ModelBenchmarkError(
            "Validated generation state does not match its captured bytes"
        )
    queue_by_id = {item.queue_id: item.document for item in queue.items}
    voice_context = (
        _comparison_voice_context(manifest_path, narrator_character)
        if manifest_path is not None
        else None
    )
    failed = []
    recovered = []
    controls = []
    for queue_id, result in state["items"].items():
        if queue_id not in queue_by_id or not isinstance(result, dict):
            continue
        attempts = result.get("attempts_by_provider")
        moss_attempted = isinstance(attempts, dict) and attempts.get("moss-tts", 0) > 0
        status = result.get("status")
        provider = result.get("provider")
        if status == "failed" and (provider == "moss-tts" or moss_attempted):
            failed.append((queue_id, result))
        elif (
            provider == "pocket-tts"
            and moss_attempted
            and status in {"generated", "approved"}
        ):
            recovered.append((queue_id, result))
        elif provider == "moss-tts" and status in {"generated", "approved"}:
            controls.append((queue_id, result))
    if not failed:
        raise ModelBenchmarkError(
            "Generation state has no unresolved MOSS failures for comparison"
        )
    selected = [
        *(item + ("unresolved_moss_failure",) for item in sorted(failed)),
        *(
            item + ("moss_to_pocket_recovery",)
            for item in _round_robin_state_items(
                recovered, queue_by_id, pocket_sample_size
            )
        ),
        *(
            item + ("moss_control",)
            for item in _round_robin_state_items(
                controls, queue_by_id, control_sample_size
            )
        ),
    ]
    samples = []
    for queue_id, result, group in selected:
        item = queue_by_id[queue_id]
        binding = result.get("source_reference_binding")
        synthesis_voice = (
            binding.get("synthesis_voice_character")
            if isinstance(binding, dict)
            else None
        )
        character = str(
            synthesis_voice
            or result.get("voice_character")
            or result.get("requested_voice_character")
            or item.get("voice_character")
            or item.get("speaker")
            or "Narrator"
        )
        prior_synthesis_voice = character
        if voice_context is not None:
            character = _resolve_comparison_voice(
                queue_id,
                item,
                result,
                voice_context,
            )
        failure = result.get("failure")
        samples.append(
            {
                "id": queue_id,
                "character": character,
                "text": str(item["text"]),
                "line_id": str(item["line_id"]),
                "text_sha256": str(item["text_sha256"]),
                "comparison_group": group,
                "prior_status": str(result.get("status") or "unknown"),
                "prior_provider": str(result.get("provider") or "unknown"),
                "prior_synthesis_voice": prior_synthesis_voice,
                "prior_failure_kind": (
                    str(failure.get("kind"))
                    if isinstance(failure, dict) and failure.get("kind")
                    else None
                ),
                "source_state_item_sha256": hashlib.sha256(
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    document = {
        "schema": CORPUS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "name": name or f"{queue_path.stem}-failure-comparison",
        "source_queue": str(queue_path),
        "source_queue_sha256": queue_sha256,
        "source_state": str(state_path),
        "source_state_sha256": state_sha256,
        **(
            {
                "source_voice_manifest": str(voice_context["path"]),
                "source_voice_manifest_sha256": voice_context["sha256"],
                "narrator_character": voice_context["narrator_character"],
            }
            if voice_context is not None
            else {}
        ),
        "selection": {
            "unresolved_moss_failures": len(failed),
            "moss_to_pocket_recoveries": sum(
                sample["comparison_group"] == "moss_to_pocket_recovery"
                for sample in samples
            ),
            "moss_controls": sum(
                sample["comparison_group"] == "moss_control" for sample in samples
            ),
        },
        "samples": samples,
    }
    if (
        _sha256_file(queue_path) != queue_sha256
        or _sha256_file(state_path) != state_sha256
        or (
            voice_context is not None
            and _sha256_file(voice_context["path"]) != voice_context["sha256"]
        )
    ):
        raise ModelBenchmarkError(
            "Queue, generation state or voice manifest changed while the corpus "
            "was built"
        )
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_path, document, sort_keys=True)
    return document


def _comparison_voice_context(manifest_path, narrator_character):
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        payload = manifest_path.read_bytes()
        document = json.loads(payload)
        registry = CharacterVoiceRegistry.from_file(manifest_path)
    except (OSError, json.JSONDecodeError, VoiceManifestError) as error:
        raise ModelBenchmarkError(
            f"Unable to capture comparison voice manifest: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ModelBenchmarkError("Comparison voice manifest must be an object")
    narrator_character = (
        str(narrator_character).strip() if narrator_character is not None else None
    )
    if narrator_character and registry.resolve(narrator_character) is None:
        raise ModelBenchmarkError(
            f"Comparison narrator voice is not in the manifest: {narrator_character!r}"
        )
    queue_overrides = {}
    bindings = document.get("vntts.authoring.source_reference_bindings")
    selected_variants = (
        bindings.get("selected_variants") if isinstance(bindings, dict) else ()
    )
    if selected_variants is None:
        selected_variants = ()
    if not isinstance(selected_variants, list):
        raise ModelBenchmarkError(
            "Comparison voice manifest source reference bindings are invalid"
        )
    for variant in selected_variants:
        if not isinstance(variant, dict):
            raise ModelBenchmarkError(
                "Comparison voice manifest selected variant is invalid"
            )
        voice_character = variant.get("voice_character")
        queue_ids = variant.get("queue_ids")
        if (
            not isinstance(voice_character, str)
            or not voice_character.strip()
            or not isinstance(queue_ids, list)
            or registry.resolve(voice_character) is None
        ):
            raise ModelBenchmarkError(
                "Comparison voice manifest selected variant cannot be resolved"
            )
        for queue_id in queue_ids:
            if not isinstance(queue_id, str) or not queue_id.strip():
                raise ModelBenchmarkError(
                    "Comparison voice manifest selected queue ID is invalid"
                )
            existing = queue_overrides.get(queue_id)
            if existing is not None and existing != voice_character:
                raise ModelBenchmarkError(
                    f"Comparison queue ID has conflicting voice bindings: {queue_id}"
                )
            queue_overrides[queue_id] = voice_character
    return {
        "path": manifest_path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "registry": registry,
        "queue_overrides": queue_overrides,
        "narrator_character": narrator_character,
    }


def _resolve_comparison_voice(queue_id, item, result, context):
    binding = result.get("source_reference_binding")
    source_voice = (
        binding.get("source_voice_character") if isinstance(binding, dict) else None
    )
    source_voice = str(
        source_voice
        or result.get("requested_voice_character")
        or item.get("voice_character")
        or item.get("speaker")
        or "Narrator"
    )
    if is_narrator(source_voice):
        candidate = context["narrator_character"] or source_voice
    else:
        candidate = context["queue_overrides"].get(queue_id, source_voice)
    voice = context["registry"].resolve(candidate)
    if voice is None:
        raise ModelBenchmarkError(
            f"Comparison sample {queue_id!r} has no exact manifest voice for "
            f"{source_voice!r}"
        )
    return voice.character


def _round_robin_state_items(values, queue_by_id, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ModelBenchmarkError("Comparison sample limits must be non-negative")
    buckets = defaultdict(list)
    for queue_id, result in sorted(values):
        item = queue_by_id[queue_id]
        key = str(item.get("voice_character") or item.get("speaker") or "Narrator")
        buckets[key].append((queue_id, result))
    selected = []
    while len(selected) < limit and buckets:
        for key in sorted(tuple(buckets), key=str.casefold):
            if len(selected) >= limit:
                break
            selected.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return selected


def load_benchmark_corpus(path):
    """Load the authoring corpus without normalizing exact identity or text."""
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelBenchmarkError(
            f"Unable to read benchmark corpus {path}: {error}"
        ) from error
    if not isinstance(document, dict) or (
        document.get("schema") != CORPUS_SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        raise ModelBenchmarkError("Unsupported authoring benchmark corpus schema")
    raw_samples = document.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ModelBenchmarkError("Authoring benchmark corpus has no samples")
    samples = []
    seen_ids = set()
    for index, sample in enumerate(raw_samples, start=1):
        if not isinstance(sample, dict):
            raise ModelBenchmarkError(
                f"Benchmark corpus sample {index} must be an object"
            )
        sample_id = _required_text(sample.get("id"), f"sample {index} id")
        line_id = _required_text(sample.get("line_id"), f"sample {index} line_id")
        character = _required_text(sample.get("character"), f"sample {index} character")
        text = sample.get("text")
        if not isinstance(text, str) or not text:
            raise ModelBenchmarkError(
                f"Benchmark corpus sample {index} text is invalid"
            )
        text_hash = _required_sha256(
            sample.get("text_sha256"), f"sample {index} text_sha256"
        )
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_hash:
            raise ModelBenchmarkError(
                f"Benchmark corpus sample {index} text_sha256 does not match exact text"
            )
        if sample_id in seen_ids:
            raise ModelBenchmarkError(
                f"Duplicate benchmark corpus sample ID: {sample_id!r}"
            )
        seen_ids.add(sample_id)
        samples.append(
            {
                **sample,
                "id": sample_id,
                "line_id": line_id,
                "character": character,
                "text": text,
                "text_sha256": text_hash,
            }
        )
    return {**document, "samples": samples}


def benchmark_renderer(
    variant,
    backend,
    samples,
    output_directory,
    *,
    seed=0,
    reported_output_directory=None,
    voice_controls_sha256=None,
    voice_controls_content_sha256=None,
):
    """Render a common corpus through one typed backend without playback."""
    output_directory = Path(output_directory).expanduser().resolve()
    reported_output_directory = (
        Path(reported_output_directory or output_directory).expanduser().resolve()
    )
    if output_directory.exists() and (
        not output_directory.is_dir() or any(output_directory.iterdir())
    ):
        raise ModelBenchmarkError(
            f"Model output already exists; refusing to overwrite: {output_directory}"
        )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=f".{output_directory.name}-", dir=output_directory.parent
    ) as temporary_directory:
        staging = Path(temporary_directory) / output_directory.name
        report = _benchmark_renderer_staged(
            variant,
            backend,
            samples,
            staging,
            reported_output_directory=reported_output_directory,
            seed=seed,
            voice_controls_sha256=voice_controls_sha256,
            voice_controls_content_sha256=voice_controls_content_sha256,
        )
        try:
            if output_directory.exists():
                output_directory.rmdir()
            os.rename(staging, output_directory)
        except OSError as error:
            raise ModelBenchmarkError(
                f"Unable to publish model output {output_directory}: {error}"
            ) from error
    return report


def _benchmark_renderer_staged(
    variant,
    backend,
    samples,
    output_directory,
    *,
    reported_output_directory,
    seed,
    voice_controls_sha256,
    voice_controls_content_sha256,
):
    render = getattr(backend, "render", None)
    if not callable(render):
        raise ModelBenchmarkError(
            f"Model {variant.model_id!r} does not implement the typed render API"
        )
    output_directory = Path(output_directory).resolve()
    reported_output_directory = Path(reported_output_directory).resolve()
    audio_root = output_directory / "audio"
    audio_root.mkdir(parents=True, exist_ok=True)
    rendered_samples = []
    for index, sample in enumerate(samples, start=1):
        synthesis_voice = variant.voice or sample["character"]
        request_seed = None if variant.backend in UNSEEDED_BACKENDS else seed
        request = SynthesisRequest(
            voice=synthesis_voice,
            text=sample["text"],
            seed=request_seed,
            generation_profile=variant.generation_profile,
            cache_policy=SynthesisCachePolicy.BYPASS,
        )
        base_record = {
            **sample,
            "synthesis_voice": synthesis_voice,
            "requested_shared_seed": seed,
            "seed_policy": (
                "unsupported" if variant.backend in UNSEEDED_BACKENDS else "shared"
            ),
        }
        try:
            result = render(request).collect()
        except Exception as error:
            rendered_samples.append(
                {
                    **base_record,
                    "outcome": "error",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
            )
            continue
        if (
            result.diagnostics.seed != request.seed
            or result.diagnostics.generation_profile != request.generation_profile
            or result.diagnostics.backend != variant.backend
        ):
            raise ModelBenchmarkError(
                f"Model {variant.model_id!r} returned diagnostics for a different request"
            )
        if result.completion is not SynthesisCompletion.COMPLETE:
            rendered_samples.append(
                {
                    **base_record,
                    "outcome": result.completion.value,
                    "seed": result.diagnostics.seed,
                    "generation_profile": result.diagnostics.generation_profile,
                    "sample_rate": result.sample_rate,
                    "sample_count": result.diagnostics.sample_count,
                    "first_pcm_ms": result.timing.first_chunk_ms,
                    "wall_ms": result.timing.total_ms,
                    "limits": asdict(result.limits),
                }
            )
            continue
        safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(sample["id"])).strip("-")
        staged_audio_path = write_pcm16_wav(
            audio_root / f"{index:04d}-{safe_id or 'sample'}.wav",
            _mono_pcm(result.pcm),
            result.sample_rate,
        )
        info = probe_pcm16_mono_wav(staged_audio_path)
        speech_quality = measure_generated_speech_bytes(staged_audio_path.read_bytes())
        reported_audio_path = (
            reported_output_directory / "audio" / staged_audio_path.name
        )
        rendered_samples.append(
            {
                **base_record,
                "outcome": "complete",
                "audio": str(reported_audio_path),
                "audio_sha256": _sha256_file(staged_audio_path),
                "sample_rate": info.sample_rate,
                "sample_count": info.sample_count,
                "duration_seconds": round(info.duration_seconds, 6),
                "peak": round(info.peak, 6),
                "speech_quality": asdict(speech_quality),
                "first_pcm_ms": result.timing.first_chunk_ms,
                "wall_ms": result.timing.total_ms,
                "real_time_factor": round(
                    result.timing.total_ms / (info.duration_seconds * 1000), 6
                ),
                "seed": result.diagnostics.seed,
                "generation_profile": result.diagnostics.generation_profile,
            }
        )
    outcomes = {
        value: sum(sample["outcome"] == value for sample in rendered_samples)
        for value in ("complete", "limited", "cancelled", "error")
    }
    group_summary = {}
    for sample in rendered_samples:
        group = str(sample.get("comparison_group") or "all")
        summary = group_summary.setdefault(
            group,
            {
                "total": 0,
                "complete": 0,
                "limited": 0,
                "cancelled": 0,
                "error": 0,
            },
        )
        summary["total"] += 1
        summary[sample["outcome"]] += 1
    report = {
        "schema": MODEL_REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_id": variant.model_id,
        "provider": variant.backend,
        "backend": variant.backend,
        "model": variant.model or variant.backend,
        "model_revision": variant.model_revision,
        "generation_profile": variant.generation_profile,
        "voice_override": variant.voice,
        "terms_accepted": variant.terms_accepted,
        "require_cuda": variant.require_cuda,
        "voice_controls_sha256": voice_controls_sha256,
        "voice_controls_content_sha256": voice_controls_content_sha256,
        "seed_policy": (
            "unsupported" if variant.backend in UNSEEDED_BACKENDS else "shared"
        ),
        "runtime": _runtime_identity(backend),
        "summary": {"total": len(rendered_samples), **outcomes},
        "group_summary": group_summary,
        "samples": rendered_samples,
    }
    atomic_write_json(output_directory / "report.json", report, sort_keys=True)
    return report


def benchmark_model_variants(
    corpus_path,
    variants,
    registry,
    output_directory,
    *,
    seed=0,
    backend_factory=create_backend,
):
    """Benchmark multiple model variants over one exact corpus."""
    corpus = load_benchmark_corpus(corpus_path)
    variants = tuple(variants)
    if not variants:
        raise ModelBenchmarkError("At least one model variant is required")
    model_ids = [variant.model_id for variant in variants]
    if len(model_ids) != len(set(model_ids)):
        raise ModelBenchmarkError("Model variant IDs must be unique")
    safe_model_ids = [_safe_name(model_id) for model_id in model_ids]
    if len(safe_model_ids) != len({value.casefold() for value in safe_model_ids}):
        raise ModelBenchmarkError("Model variant IDs collide as output directory names")
    for variant in variants:
        if variant.backend == "coqui-xtts" and not variant.terms_accepted:
            raise ModelBenchmarkError(
                "XTTS v2 requires explicit CPML acceptance in the "
                "model-variant document"
            )
        if variant.backend in {"moss-tts", "moss-tts-delay", "coqui-xtts"}:
            unresolved = sorted(
                {
                    variant.voice or sample["character"]
                    for sample in corpus["samples"]
                    if registry.resolve(variant.voice or sample["character"]) is None
                },
                key=str.casefold,
            )
            if unresolved:
                raise ModelBenchmarkError(
                    "Model comparison has unresolved manifest voices: "
                    + ", ".join(unresolved)
                )
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.exists() and (
        not output_directory.is_dir() or any(output_directory.iterdir())
    ):
        raise ModelBenchmarkError(
            f"Benchmark output already exists; refusing to overwrite: {output_directory}"
        )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    with (
        TemporaryDirectory() as cache,
        TemporaryDirectory(
            prefix=f".{output_directory.name}-", dir=output_directory.parent
        ) as temporary_output,
    ):
        cache_root = Path(cache).resolve()
        staging_root = Path(temporary_output).resolve() / output_directory.name
        staging_root.mkdir(parents=True)
        published_corpus = staging_root / "benchmark-corpus.json"
        atomic_write_json(published_corpus, corpus, sort_keys=True)
        requires_voice_controls = any(
            variant.backend in {"moss-tts", "moss-tts-delay", "coqui-xtts"}
            for variant in variants
        )
        if requires_voice_controls:
            used_characters = {
                variant.voice or sample["character"]
                for variant in variants
                if variant.backend in {"moss-tts", "moss-tts-delay", "coqui-xtts"}
                for sample in corpus["samples"]
            }
            snapshot_registry, voice_controls = _snapshot_voice_registry(
                registry,
                used_characters,
                staging_root / "voice-controls",
                output_directory / "voice-controls",
            )
        else:
            snapshot_registry = registry
            voice_controls = []
        voice_controls_sha256 = canonical_document_sha256(voice_controls)
        voice_controls_content_sha256 = canonical_document_sha256(
            [
                {
                    key: control[key]
                    for key in (
                        "character",
                        "speaker",
                        "reference_index",
                        "sha256",
                        "size",
                    )
                }
                for control in voice_controls
            ]
        )
        reports = []
        model_summaries = []
        for variant, safe_model_id in zip(variants, safe_model_ids, strict=True):
            cache_directory = _contained_child(cache_root, safe_model_id, "model cache")
            model_output = _contained_child(staging_root, safe_model_id, "model output")
            reported_model_output = _contained_child(
                output_directory, safe_model_id, "reported model output"
            )
            try:
                backend = backend_factory(
                    variant.backend,
                    snapshot_registry,
                    cache_directory,
                    model_name=variant.model,
                    **(
                        {"model_revision": variant.model_revision}
                        if variant.model_revision is not None
                        else {}
                    ),
                    **(
                        {"terms_accepted": True}
                        if variant.backend == "coqui-xtts"
                        else {}
                    ),
                    **(
                        {"require_cuda": True}
                        if variant.backend == "moss-tts-delay" and variant.require_cuda
                        else {}
                    ),
                )
            except (TypeError, ValueError) as error:
                raise ModelBenchmarkError(str(error)) from error
            try:
                report = benchmark_renderer(
                    variant,
                    backend,
                    corpus["samples"],
                    model_output,
                    seed=seed,
                    reported_output_directory=reported_model_output,
                    voice_controls_sha256=voice_controls_sha256,
                    voice_controls_content_sha256=voice_controls_content_sha256,
                )
                reports.append(str(reported_model_output / "report.json"))
                model_summaries.append(
                    {
                        "model_id": variant.model_id,
                        "backend": variant.backend,
                        "model": variant.model or variant.backend,
                        "summary": report["summary"],
                    }
                )
            finally:
                stop = getattr(backend, "stop", None)
                if callable(stop):
                    stop()
        aggregate = {
            "schema": BENCHMARK_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "corpus": str(output_directory / "benchmark-corpus.json"),
            "corpus_sha256": _sha256_file(published_corpus),
            "voice_controls": voice_controls,
            "voice_controls_sha256": voice_controls_sha256,
            "voice_controls_content_sha256": voice_controls_content_sha256,
            "sample_count": len(corpus["samples"]),
            "manual_review_required": True,
            "comparison_ready": len(variants) >= 2,
            "models": model_summaries,
            "reports": reports,
        }
        atomic_write_json(staging_root / "benchmark.json", aggregate, sort_keys=True)
        try:
            if output_directory.exists():
                output_directory.rmdir()
            os.rename(staging_root, output_directory)
        except OSError as error:
            raise ModelBenchmarkError(
                f"Unable to publish benchmark output {output_directory}: {error}"
            ) from error
    return aggregate


def _snapshot_voice_registry(
    registry,
    used_characters,
    staging_root,
    reported_root,
):
    staging_root = Path(staging_root).resolve()
    reported_root = Path(reported_root).resolve()
    voices = {}
    for character in sorted(used_characters, key=str.casefold):
        voice = registry.resolve(character)
        if voice is None:
            raise ModelBenchmarkError(
                f"Model comparison has unresolved manifest voice: {character}"
            )
        if not voice.references:
            raise ModelBenchmarkError(
                f"Model comparison voice has no reference audio: {character}"
            )
        voices[id(voice)] = voice
    snapshot_voices = []
    inventory = []
    for voice_index, voice in enumerate(
        sorted(voices.values(), key=lambda value: value.character.casefold()),
        start=1,
    ):
        references = []
        for reference_index, source in enumerate(voice.references, start=1):
            try:
                payload = read_voice_reference_bytes(voice, source)
            except (OSError, VoiceManifestError) as error:
                raise ModelBenchmarkError(
                    f"Unable to capture comparison voice reference {source}: {error}"
                ) from error
            source = Path(source).expanduser().resolve()
            suffix = source.suffix.lower() or ".audio"
            relative = Path(f"voice-{voice_index:03d}") / (
                f"reference-{reference_index:03d}{suffix}"
            )
            destination = _contained_child(
                staging_root,
                relative,
                "voice control",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(destination, payload)
            digest = hashlib.sha256(payload).hexdigest()
            if _sha256_file(destination) != digest:
                raise ModelBenchmarkError(
                    f"Captured comparison voice reference changed: {source}"
                )
            references.append(destination)
            inventory.append(
                {
                    "character": voice.character,
                    "speaker": voice.speaker,
                    "reference_index": reference_index,
                    "source": str(source),
                    "audio": str(reported_root / relative),
                    "sha256": digest,
                    "size": len(payload),
                }
            )
        snapshot_voices.append(
            CharacterVoice(
                character=voice.character,
                speaker=voice.speaker,
                aliases=voice.aliases,
                references=tuple(references),
            )
        )
    return CharacterVoiceRegistry(snapshot_voices), inventory


def load_model_variants(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelBenchmarkError(
            f"Unable to read model variants {path}: {error}"
        ) from error
    if not isinstance(document, list) or not document:
        raise ModelBenchmarkError("Model variants must be a non-empty list")
    variants = []
    for index, item in enumerate(document):
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("model_id", "backend")
        ):
            raise ModelBenchmarkError(f"Model variant {index} is invalid")
        voice = item.get("voice")
        if "voice" in item and (not isinstance(voice, str) or not voice.strip()):
            raise ModelBenchmarkError(
                f"Model variant {index} voice must be non-empty text"
            )
        model = item.get("model")
        if "model" in item and (not isinstance(model, str) or not model.strip()):
            raise ModelBenchmarkError(
                f"Model variant {index} model must be non-empty text"
            )
        model_revision = item.get("model_revision")
        if "model_revision" in item and (
            not isinstance(model_revision, str)
            or re.fullmatch(r"[0-9a-f]{40,64}", model_revision) is None
        ):
            raise ModelBenchmarkError(
                f"Model variant {index} model_revision must be an exact commit"
            )
        if model_revision is not None and item["backend"] != "moss-tts-delay":
            raise ModelBenchmarkError(
                f"Model variant {index} model_revision applies only to moss-tts-delay"
            )
        terms_accepted = item.get("terms_accepted", False)
        if not isinstance(terms_accepted, bool):
            raise ModelBenchmarkError(
                f"Model variant {index} terms_accepted must be true or false"
            )
        if terms_accepted and item["backend"] != "coqui-xtts":
            raise ModelBenchmarkError(
                f"Model variant {index} terms_accepted applies only to coqui-xtts"
            )
        require_cuda = item.get("require_cuda", False)
        if not isinstance(require_cuda, bool):
            raise ModelBenchmarkError(
                f"Model variant {index} require_cuda must be true or false"
            )
        if require_cuda and item["backend"] != "moss-tts-delay":
            raise ModelBenchmarkError(
                f"Model variant {index} require_cuda applies only to moss-tts-delay"
            )
        variants.append(
            ModelVariant(
                model_id=item["model_id"],
                backend=item["backend"],
                model=model.strip() if isinstance(model, str) else None,
                model_revision=model_revision,
                generation_profile=str(item.get("generation_profile") or "stable"),
                voice=voice.strip() if isinstance(voice, str) else None,
                terms_accepted=terms_accepted,
                require_cuda=require_cuda,
            )
        )
    return variants


def _safe_name(value):
    if not isinstance(value, str) or not value.strip() or value.strip() in {".", ".."}:
        raise ModelBenchmarkError("Model variant ID is not a safe output name")
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    if not safe or safe in {".", ".."}:
        raise ModelBenchmarkError("Model variant ID is not a safe output name")
    return safe


def _contained_child(root, name, label):
    root = Path(root).resolve()
    child = (root / name).resolve()
    try:
        child.relative_to(root)
    except ValueError as error:
        raise ModelBenchmarkError(f"{label.title()} leaves its root") from error
    if child == root:
        raise ModelBenchmarkError(f"{label.title()} must be a child directory")
    return child


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ModelBenchmarkError(f"Benchmark corpus {label} must be non-empty text")
    return value.strip()


def _required_sha256(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ModelBenchmarkError(f"Benchmark corpus {label} must be lowercase SHA-256")
    return value


def _sha256_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _mono_pcm(value):
    pcm = np.asarray(value, dtype=np.float32)
    if pcm.ndim == 1:
        mono = pcm
    elif pcm.ndim == 2 and pcm.shape[1] >= 1:
        mono = np.mean(pcm, axis=1, dtype=np.float32)
    else:
        raise ModelBenchmarkError("Typed renderer returned invalid PCM dimensions")
    if not mono.size or not np.isfinite(mono).all():
        raise ModelBenchmarkError("Typed renderer returned empty or non-finite PCM")
    return np.ascontiguousarray(mono, dtype=np.float32)


def _runtime_identity(backend):
    health = getattr(backend, "health", None)
    if isinstance(health, dict):
        return {
            "kind": "isolated-worker",
            "interpreter": health.get("interpreter"),
            "prefix": health.get("prefix"),
            "modules": health.get("modules"),
            "platform": health.get("platform"),
            "machine": health.get("machine"),
            "device": health.get("device"),
            "accelerator": health.get("accelerator"),
        }
    return {
        "kind": "current-process",
        "interpreter": str(Path(sys.executable).resolve()),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description="Benchmark typed TTS renderers on one corpus"
    )
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--pocket-samples", type=int, default=12)
    parser.add_argument("--control-samples", type=int, default=12)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--narrator-character")
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    if (arguments.corpus is None) == (arguments.queue is None):
        return cli_error("Select exactly one of --corpus or --queue")
    if arguments.state is not None and arguments.queue is None:
        return cli_error("--state requires --queue")
    manifest = arguments.manifest or find_default_voice_manifest()
    if manifest is None:
        return cli_error("No complete voice manifest is available")
    try:
        with TemporaryDirectory(prefix="vntts-model-corpus-") as temporary_corpus:
            corpus = arguments.corpus
            if corpus is None:
                corpus = Path(temporary_corpus) / "benchmark-corpus.json"
                if arguments.state is not None:
                    build_failure_comparison_corpus(
                        arguments.queue,
                        arguments.state,
                        corpus,
                        pocket_sample_size=arguments.pocket_samples,
                        control_sample_size=arguments.control_samples,
                        manifest_path=manifest,
                        narrator_character=arguments.narrator_character,
                    )
                else:
                    build_benchmark_corpus(
                        arguments.queue,
                        corpus,
                        sample_size=arguments.sample_size,
                    )
            aggregate = benchmark_model_variants(
                corpus,
                load_model_variants(arguments.models),
                CharacterVoiceRegistry.from_file(manifest),
                arguments.output,
                seed=arguments.seed,
            )
    except (ModelBenchmarkError, OSError, ValueError) as error:
        return cli_error(error)
    return cli_messages((arguments.output / "benchmark.json", *aggregate["reports"]))


if __name__ == "__main__":
    raise SystemExit(main())
