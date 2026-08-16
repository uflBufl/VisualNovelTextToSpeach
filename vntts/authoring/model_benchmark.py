"""Generic, device-independent model comparison for authoring."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.voice_generation_queue import VoiceGenerationQueue

from vntts.cli import cli_error, cli_messages
from vntts.settings import get_local_data_directory
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
)
from vntts.tts_benchmark import create_backend, load_tts_benchmark_corpus
from vntts.voices import CharacterVoiceRegistry, find_default_voice_manifest

CORPUS_SCHEMA = "vntts.tts-benchmark-corpus"
MODEL_REPORT_SCHEMA = "vntts.voice-model-report"
BENCHMARK_SCHEMA = "vntts.voice-model-benchmark"
SCHEMA_VERSION = 1
default_output = get_local_data_directory() / "authoring" / "model-benchmark"


class ModelBenchmarkError(RuntimeError):
    """A generic model benchmark cannot be completed safely."""


@dataclass(frozen=True)
class ModelVariant:
    model_id: str
    backend: str
    model: str | None = None
    generation_profile: str = "stable"


def select_representative_items(items, sample_size=24):
    """Round-robin generation-ready records by delivery/emotion label."""
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 1:
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
        character = str(item.get("voice_character") or item.get("speaker") or "Narrator")
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


def benchmark_renderer(
    variant,
    backend,
    samples,
    output_directory,
    *,
    seed=0,
):
    """Render a common corpus through one typed backend without playback."""
    render = getattr(backend, "render", None)
    if not callable(render):
        raise ModelBenchmarkError(
            f"Model {variant.model_id!r} does not implement the typed render API"
        )
    output_directory = Path(output_directory).expanduser().resolve()
    audio_root = output_directory / "audio"
    audio_root.mkdir(parents=True, exist_ok=True)
    rendered_samples = []
    for index, sample in enumerate(samples, start=1):
        request = SynthesisRequest(
            voice=sample["character"],
            text=sample["text"],
            seed=seed,
            generation_profile=variant.generation_profile,
            cache_policy=SynthesisCachePolicy.BYPASS,
        )
        result = render(request).collect()
        if result.completion is not SynthesisCompletion.COMPLETE:
            raise ModelBenchmarkError(
                f"Model {variant.model_id!r} did not complete sample {sample['id']!r}: "
                f"{result.completion.value}"
            )
        safe_id = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(sample["id"])).strip("-")
        audio_path = write_pcm16_wav(
            audio_root / f"{index:04d}-{safe_id or 'sample'}.wav",
            result.pcm,
            result.sample_rate,
        )
        info = probe_pcm16_mono_wav(audio_path)
        rendered_samples.append(
            {
                **sample,
                "audio": str(audio_path),
                "sample_rate": info.sample_rate,
                "sample_count": info.sample_count,
                "duration_seconds": round(info.duration_seconds, 6),
                "peak": round(info.peak, 6),
                "first_pcm_ms": result.timing.first_chunk_ms,
                "wall_ms": result.timing.total_ms,
                "seed": result.diagnostics.seed,
                "generation_profile": result.diagnostics.generation_profile,
            }
        )
    report = {
        "schema": MODEL_REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_id": variant.model_id,
        "provider": variant.backend,
        "backend": variant.backend,
        "model": variant.model or variant.backend,
        "generation_profile": variant.generation_profile,
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
    corpus = load_tts_benchmark_corpus(corpus_path)
    variants = tuple(variants)
    if len(variants) < 2:
        raise ModelBenchmarkError("At least two model variants are required")
    model_ids = [variant.model_id for variant in variants]
    if len(model_ids) != len(set(model_ids)):
        raise ModelBenchmarkError("Model variant IDs must be unique")
    safe_model_ids = [_safe_name(model_id) for model_id in model_ids]
    if len(safe_model_ids) != len(set(safe_model_ids)):
        raise ModelBenchmarkError("Model variant IDs collide as output directory names")
    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    reports = []
    with TemporaryDirectory() as cache:
        for variant in variants:
            try:
                backend = backend_factory(
                    variant.backend,
                    registry,
                    Path(cache) / _safe_name(variant.model_id),
                    model_name=variant.model,
                )
            except (TypeError, ValueError) as error:
                raise ModelBenchmarkError(str(error)) from error
            try:
                benchmark_renderer(
                    variant,
                    backend,
                    corpus["samples"],
                    output_directory / _safe_name(variant.model_id),
                    seed=seed,
                )
                reports.append(
                    str(output_directory / _safe_name(variant.model_id) / "report.json")
                )
            finally:
                stop = getattr(backend, "stop", None)
                if callable(stop):
                    stop()
    aggregate = {
        "schema": BENCHMARK_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": str(Path(corpus_path).expanduser().resolve()),
        "sample_count": len(corpus["samples"]),
        "manual_review_required": True,
        "reports": reports,
    }
    atomic_write_json(output_directory / "benchmark.json", aggregate, sort_keys=True)
    return aggregate


def load_model_variants(path):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelBenchmarkError(f"Unable to read model variants {path}: {error}") from error
    if not isinstance(document, list) or len(document) < 2:
        raise ModelBenchmarkError("Model variants must be a list with at least two entries")
    variants = []
    for index, item in enumerate(document):
        if not isinstance(item, dict) or not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("model_id", "backend")
        ):
            raise ModelBenchmarkError(f"Model variant {index} is invalid")
        variants.append(
            ModelVariant(
                model_id=item["model_id"],
                backend=item["backend"],
                model=item.get("model"),
                generation_profile=str(item.get("generation_profile") or "stable"),
            )
        )
    return variants


def _safe_name(value):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "model"


def create_parser():
    parser = argparse.ArgumentParser(description="Benchmark typed TTS renderers on one corpus")
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--queue", type=Path)
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    if (arguments.corpus is None) == (arguments.queue is None):
        return cli_error("Select exactly one of --corpus or --queue")
    manifest = arguments.manifest or find_default_voice_manifest()
    if manifest is None:
        return cli_error("No complete voice manifest is available")
    try:
        corpus = arguments.corpus
        if corpus is None:
            corpus = arguments.output / "benchmark-corpus.json"
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
