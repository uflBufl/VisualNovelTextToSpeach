"""Checksum-bound CUDA benchmark for MOSS-SoundEffect v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import numpy as np

from vntts.cuda_probe import CudaProbeError, inspect_cuda

CORPUS_SCHEMA = "vntts.sound-effect-benchmark-corpus"
REPORT_SCHEMA = "vntts.sound-effect-benchmark"
SCHEMA_VERSION = 1
DEFAULT_MODEL = "OpenMOSS-Team/MOSS-SoundEffect-v2.0"
DEFAULT_MODEL_REVISION = "e35df4d82fbe87fcd5d14e5d100e349c0c3c076d"
UPSTREAM_CODE_REVISION = "c0880299e8b8d0f7119efab17e4e776fffe7b8fa"


class SoundEffectBenchmarkError(RuntimeError):
    """The bounded sound-effect experiment cannot be published safely."""


def load_sound_effect_corpus(path):
    path = Path(path).expanduser().resolve()
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise SoundEffectBenchmarkError(f"Unable to read corpus: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema") != CORPUS_SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(document.get("samples"), list)
        or not document["samples"]
    ):
        raise SoundEffectBenchmarkError("Sound-effect corpus is invalid")
    samples = []
    seen_ids = set()
    for index, item in enumerate(document["samples"]):
        sample = _validate_sample(item, index)
        if sample["id"].casefold() in seen_ids:
            raise SoundEffectBenchmarkError("Sound-effect sample IDs must be unique")
        seen_ids.add(sample["id"].casefold())
        samples.append(sample)
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "name": str(document.get("name") or path.stem),
        "samples": samples,
    }


def _validate_sample(item, index):
    if not isinstance(item, dict):
        raise SoundEffectBenchmarkError(f"Sound-effect sample {index} is invalid")
    sample_id = item.get("id")
    prompt = item.get("prompt")
    kind = item.get("kind")
    seconds = item.get("seconds")
    if not isinstance(sample_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]*", sample_id
    ):
        raise SoundEffectBenchmarkError(f"Sound-effect sample {index} ID is invalid")
    if not isinstance(kind, str) or not kind.strip():
        raise SoundEffectBenchmarkError(
            f"Sound-effect sample {sample_id} kind is invalid"
        )
    if not isinstance(prompt, str) or not prompt.strip():
        raise SoundEffectBenchmarkError(
            f"Sound-effect sample {sample_id} prompt is invalid"
        )
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise SoundEffectBenchmarkError(
            f"Sound-effect sample {sample_id} duration is invalid"
        )
    seconds = round(float(seconds), 1)
    if not 0 < seconds <= 30:
        raise SoundEffectBenchmarkError(
            f"Sound-effect sample {sample_id} duration is invalid"
        )
    return {
        "id": sample_id,
        "kind": kind.strip(),
        "prompt": prompt.strip(),
        "seconds": seconds,
    }


def benchmark_sound_effects(
    corpus_path,
    output_directory,
    *,
    model=DEFAULT_MODEL,
    model_revision=DEFAULT_MODEL_REVISION,
    seeds=(0, 1, 2),
    num_inference_steps=100,
    cfg_scale=4.0,
    sigma_shift=5.0,
    torch_module=None,
    pipeline_factory=None,
    clock=monotonic,
):
    corpus = load_sound_effect_corpus(corpus_path)
    seeds = _validate_controls(seeds, num_inference_steps, cfg_scale, sigma_shift)
    if re.fullmatch(r"[0-9a-f]{40,64}", model_revision) is None:
        raise SoundEffectBenchmarkError("Model revision must be an exact commit")
    cuda = inspect_cuda(torch_module)
    if cuda["bf16_supported"] is not True:
        raise SoundEffectBenchmarkError(
            "MOSS-SoundEffect v2 requires CUDA BF16 support; model weights were not loaded"
        )
    if torch_module is None:
        import torch

        torch_module = torch
    if pipeline_factory is None:
        try:
            from moss_soundeffect_v2 import MossSoundEffectPipeline
        except ImportError as error:
            raise SoundEffectBenchmarkError(
                "MOSS-SoundEffect v2 is not installed in this runtime"
            ) from error
        pipeline_factory = MossSoundEffectPipeline.from_pretrained
    output_directory = Path(output_directory).expanduser().resolve()
    if output_directory.exists():
        raise SoundEffectBenchmarkError(
            f"Benchmark output already exists; refusing to overwrite: {output_directory}"
        )
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        pipeline = pipeline_factory(
            model,
            revision=model_revision,
            torch_dtype=torch_module.bfloat16,
            device="cuda",
        )
    except Exception as error:
        raise SoundEffectBenchmarkError(
            f"Unable to load MOSS-SoundEffect v2: {error}"
        ) from error
    sample_rate = int(getattr(pipeline, "sample_rate", 0))
    if sample_rate <= 0:
        raise SoundEffectBenchmarkError(
            "MOSS-SoundEffect returned an invalid sample rate"
        )
    with TemporaryDirectory(
        prefix=f".{output_directory.name}-", dir=output_directory.parent
    ) as staging:
        root = Path(staging) / output_directory.name
        audio_directory = root / "audio"
        audio_directory.mkdir(parents=True)
        results = []
        for sample in corpus["samples"]:
            for seed in seeds:
                results.append(
                    _render_sample(
                        pipeline,
                        sample,
                        seed,
                        audio_directory,
                        sample_rate,
                        num_inference_steps,
                        cfg_scale,
                        sigma_shift,
                        torch_module,
                        clock,
                    )
                )
        report = {
            "schema": REPORT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "model_revision": model_revision,
            "configured_upstream_code_revision": UPSTREAM_CODE_REVISION,
            "corpus": str(corpus["path"]),
            "corpus_sha256": corpus["sha256"],
            "corpus_name": corpus["name"],
            "cuda": cuda,
            "controls": {
                "seeds": list(seeds),
                "num_inference_steps": num_inference_steps,
                "cfg_scale": cfg_scale,
                "sigma_shift": sigma_shift,
            },
            "manual_review_required": True,
            "speaker_identity_claim": False,
            "samples": results,
        }
        (root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.rename(root, output_directory)
    return report


def _validate_controls(seeds, steps, cfg_scale, sigma_shift):
    seeds = tuple(seeds)
    if not seeds or any(
        isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds
    ):
        raise SoundEffectBenchmarkError("At least one integer seed is required")
    if len(seeds) != len(set(seeds)):
        raise SoundEffectBenchmarkError("Sound-effect seeds must be unique")
    if isinstance(steps, bool) or not isinstance(steps, int) or not 10 <= steps <= 150:
        raise SoundEffectBenchmarkError("Inference steps must be between 10 and 150")
    if not 1 <= float(cfg_scale) <= 8:
        raise SoundEffectBenchmarkError("CFG scale must be between 1 and 8")
    if not 0 <= float(sigma_shift) <= 10:
        raise SoundEffectBenchmarkError("Sigma shift must be between 0 and 10")
    return seeds


def _render_sample(
    pipeline,
    sample,
    seed,
    audio_directory,
    sample_rate,
    steps,
    cfg_scale,
    sigma_shift,
    torch_module,
    clock,
):
    cuda = torch_module.cuda
    if callable(getattr(cuda, "reset_peak_memory_stats", None)):
        cuda.reset_peak_memory_stats()
    started = clock()
    try:
        audio = pipeline(
            prompt=sample["prompt"],
            seconds=sample["seconds"],
            num_inference_steps=steps,
            cfg_scale=cfg_scale,
            sigma_shift=sigma_shift,
            seed=seed,
        )
        if callable(getattr(cuda, "synchronize", None)):
            cuda.synchronize()
        elapsed = clock() - started
        pcm = _mono_pcm(audio)
    except Exception as error:
        raise SoundEffectBenchmarkError(
            f"Sound-effect render failed for {sample['id']} seed {seed}: {error}"
        ) from error
    filename = f"{sample['id']}-seed-{seed}.wav"
    path = audio_directory / filename
    _write_pcm16_mono(path, pcm, sample_rate)
    payload = path.read_bytes()
    peak_memory = (
        int(cuda.max_memory_allocated())
        if callable(getattr(cuda, "max_memory_allocated", None))
        else None
    )
    return {
        **sample,
        "seed": seed,
        "audio": f"audio/{filename}",
        "wav_sha256": hashlib.sha256(payload).hexdigest(),
        "wav_size": len(payload),
        "sample_rate": sample_rate,
        "sample_count": int(pcm.size),
        "actual_seconds": pcm.size / sample_rate,
        "render_seconds": elapsed,
        "realtime_factor": elapsed / (pcm.size / sample_rate),
        "peak": float(np.max(np.abs(pcm))),
        "near_silence_ratio": float(np.mean(np.abs(pcm) < 1e-4)),
        "peak_cuda_memory_bytes": peak_memory,
        "human_review": {
            "adherence": "pending",
            "unwanted_speech": "pending",
            "artifacts": "pending",
        },
    }


def _mono_pcm(value):
    if hasattr(value, "detach"):
        value = value.detach().float().cpu().numpy()
    pcm = np.asarray(value, dtype=np.float32)
    if pcm.ndim == 3 and pcm.shape[0] == 1:
        pcm = pcm[0]
    if pcm.ndim == 2:
        pcm = np.mean(pcm, axis=0, dtype=np.float32)
    if pcm.ndim != 1 or not pcm.size or not np.isfinite(pcm).all():
        raise SoundEffectBenchmarkError("MOSS-SoundEffect returned invalid PCM")
    return np.ascontiguousarray(pcm, dtype=np.float32)


def _write_pcm16_mono(path, pcm, sample_rate):
    encoded = np.rint(np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(encoded)


def create_parser():
    parser = argparse.ArgumentParser(
        description="Benchmark MOSS-SoundEffect v2 on an exact CUDA corpus"
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--model-revision", default=DEFAULT_MODEL_REVISION)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--num-inference-steps", type=int, default=100)
    parser.add_argument("--cfg-scale", type=float, default=4.0)
    parser.add_argument("--sigma-shift", type=float, default=5.0)
    return parser


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    try:
        benchmark_sound_effects(
            arguments.corpus,
            arguments.output,
            model=arguments.model,
            model_revision=arguments.model_revision,
            seeds=arguments.seeds or (0, 1, 2),
            num_inference_steps=arguments.num_inference_steps,
            cfg_scale=arguments.cfg_scale,
            sigma_shift=arguments.sigma_shift,
        )
    except (CudaProbeError, SoundEffectBenchmarkError, OSError, ValueError) as error:
        print(f"Sound-effect benchmark failed: {error}", file=sys.stderr)
        return 2
    print(arguments.output.expanduser().resolve() / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
