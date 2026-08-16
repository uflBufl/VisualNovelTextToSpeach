"""Generate a small MOSS cloning matrix without touching VNTTS caches/manifests."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import mlx.core as mx
import numpy as np
from mlx_audio.audio_io import write as write_audio
from mlx_audio.tts import load

from vntts.moss_compat import install_moss_quantized_codec_compat


def _collect(results) -> np.ndarray:
    chunks = []
    for result in results:
        audio = result.audio
        if hasattr(audio, "tolist") and not hasattr(audio, "__array__"):
            audio = np.asarray(audio.tolist(), dtype=np.float32)
        else:
            audio = np.asarray(audio, dtype=np.float32)
        audio = np.squeeze(audio)
        if audio.size:
            chunks.append(audio)
    if not chunks:
        raise RuntimeError("MOSS generated no audio")
    return np.concatenate(chunks, axis=0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cached-prompt", type=Path)
    options = parser.parse_args()

    options.output.mkdir(parents=True, exist_ok=True)
    install_moss_quantized_codec_compat()
    model = load(str(options.model.expanduser().resolve()), lazy=False)
    codes = model.encode_reference_audio(str(options.reference.expanduser().resolve()))
    mx.eval(codes)
    mx.save_safetensors(
        str(options.output / "fresh-prompt-codes.safetensors"),
        {"prompt_audio_codes": codes},
    )

    prompt_comparison = None
    if options.cached_prompt is not None:
        cached_codes = mx.load(str(options.cached_prompt.expanduser().resolve()))[
            "prompt_audio_codes"
        ]
        mx.eval(cached_codes)
        fresh_values = np.asarray(codes.tolist(), dtype=np.int32)
        cached_values = np.asarray(cached_codes.tolist(), dtype=np.int32)
        prompt_comparison = {
            "cached_prompt": str(options.cached_prompt.expanduser().resolve()),
            "cached_shape": list(cached_values.shape),
            "same_shape": cached_values.shape == fresh_values.shape,
            "matching_fraction": (
                round(float(np.mean(cached_values == fresh_values)), 6)
                if cached_values.shape == fresh_values.shape
                else 0.0
            ),
        }

    roundtrip = model.decode_audio_token_ids(codes)
    mx.eval(roundtrip)
    write_audio(
        options.output / "00-reference-codec-roundtrip.wav",
        roundtrip,
        model.sample_rate,
    )

    variants = (
        (
            "01-current-streaming-temp-1.7.wav",
            {"mode": "generation", "stream": True, "audio_temperature": 1.7},
        ),
        (
            "02-generation-nonstream-temp-1.7.wav",
            {"mode": "generation", "stream": False, "audio_temperature": 1.7},
        ),
        (
            "03-generation-nonstream-temp-0.8.wav",
            {"mode": "generation", "stream": False, "audio_temperature": 0.8},
        ),
        (
            "04-continuation-nonstream-temp-0.8.wav",
            {
                "mode": "continuation",
                "stream": False,
                "audio_temperature": 0.8,
                "ref_text": options.reference_text,
            },
        ),
    )
    report = {
        "model": str(options.model.expanduser().resolve()),
        "reference": str(options.reference.expanduser().resolve()),
        "reference_text": options.reference_text,
        "text": options.text,
        "sample_rate": model.sample_rate,
        "prompt_code_shape": list(codes.shape),
        "cached_prompt_comparison": prompt_comparison,
        "variants": [],
    }
    for index, (name, variant) in enumerate(variants):
        mx.random.seed(0)
        started = time.monotonic()
        audio = _collect(
            model.generate(
                text=options.text,
                prompt_audio_codes=codes,
                language="English",
                max_tokens=512,
                do_sample=True,
                **variant,
            )
        )
        write_audio(options.output / name, audio, model.sample_rate)
        report["variants"].append(
            {
                "name": name,
                "seconds": round(len(audio) / model.sample_rate, 3),
                "generation_seconds": round(time.monotonic() - started, 3),
            }
        )
        print(f"[{index + 1}/{len(variants)}] wrote {name}", flush=True)

    (options.output / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
