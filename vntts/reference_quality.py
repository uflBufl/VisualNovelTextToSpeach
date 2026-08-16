"""Objective preflight for voice-cloning reference WAV files."""

from __future__ import annotations

import argparse
import hashlib
import wave
from pathlib import Path

import numpy as np
from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import cli_error, cli_messages


def analyze_reference(path, *, silence_db=-40.0, window_ms=20.0):
    path = Path(path).expanduser().resolve()
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            raw = source.readframes(frame_count)
    except (OSError, EOFError, wave.Error) as error:
        raise ValueError(f"Unable to read reference WAV {path}: {error}") from error
    if channels != 1 or sample_width != 2 or sample_rate <= 0 or frame_count <= 0:
        raise ValueError(
            f"Reference must be non-empty PCM16 mono WAV: {path} "
            f"({channels} channels, {sample_width * 8}-bit, {sample_rate} Hz)"
        )
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    absolute = np.abs(samples)
    duration_seconds = len(samples) / sample_rate
    peak = float(np.max(absolute))
    rms = float(np.sqrt(np.mean(np.square(samples))))
    clipping_fraction = float(np.mean(absolute >= 0.999))
    dc_offset = float(abs(np.mean(samples)))
    window_samples = max(1, round(sample_rate * window_ms / 1000))
    padded = np.pad(samples, (0, (-len(samples)) % window_samples))
    windows = padded.reshape(-1, window_samples)
    window_rms = np.sqrt(np.mean(np.square(windows), axis=1))
    active = window_rms >= 10 ** (silence_db / 20)
    active_indices = np.flatnonzero(active)
    if len(active_indices):
        leading_silence_seconds = active_indices[0] * window_samples / sample_rate
        trailing_silence_seconds = (
            (len(active) - 1 - active_indices[-1]) * window_samples / sample_rate
        )
    else:
        leading_silence_seconds = duration_seconds
        trailing_silence_seconds = duration_seconds
    inactive_fraction = float(np.mean(~active))

    rejection_reasons = []
    if duration_seconds < 1.0:
        rejection_reasons.append("duration-under-1-second")
    if duration_seconds > 30.0:
        rejection_reasons.append("duration-over-30-seconds")
    if peak < 0.02 or rms < 0.005:
        rejection_reasons.append("signal-too-quiet")
    if clipping_fraction > 0.001:
        rejection_reasons.append("excessive-clipping")
    if leading_silence_seconds > 1.0:
        rejection_reasons.append("excessive-leading-silence")
    if trailing_silence_seconds > 1.0:
        rejection_reasons.append("excessive-trailing-silence")
    if dc_offset > 0.02:
        rejection_reasons.append("excessive-dc-offset")

    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sample_rate": sample_rate,
        "duration_seconds": round(duration_seconds, 3),
        "peak": round(peak, 6),
        "rms": round(rms, 6),
        "clipping_fraction": round(clipping_fraction, 8),
        "inactive_window_fraction": round(inactive_fraction, 6),
        "leading_silence_seconds": round(leading_silence_seconds, 3),
        "trailing_silence_seconds": round(trailing_silence_seconds, 3),
        "dc_offset": round(dc_offset, 8),
        "objective_preflight": "pass" if not rejection_reasons else "reject",
        "rejection_reasons": rejection_reasons,
        "manual_review_required": [
            "single-speaker-identity",
            "music-or-background-audio",
            "spoken-content-and-pronunciation",
        ],
    }


def analyze_reference_set(paths):
    references = [analyze_reference(path) for path in paths]
    objective_ranking = sorted(
        range(len(references)),
        key=lambda index: (
            references[index]["objective_preflight"] != "pass",
            references[index]["clipping_fraction"],
            references[index]["leading_silence_seconds"]
            + references[index]["trailing_silence_seconds"],
            references[index]["inactive_window_fraction"],
            index,
        ),
    )
    return {
        "schema_version": 1,
        "references": references,
        "objective_ranking": [index + 1 for index in objective_ranking],
        "selection_policy": (
            "Objective metrics reject unusable files but do not choose speaker "
            "similarity. Keep the configured order until the passing references "
            "complete a blinded listening comparison."
        ),
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description="Preflight PCM16 mono WAV voice references"
    )
    parser.add_argument("reference", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        report = analyze_reference_set(arguments.reference)
        atomic_write_json(arguments.output, report)
    except (OSError, TypeError, ValueError) as error:
        return cli_error(error)
    rejected = sum(
        reference["objective_preflight"] == "reject"
        for reference in report["references"]
    )
    return cli_messages(
        (
            f"Reference preflight: {len(report['references']) - rejected} passed, "
            f"{rejected} rejected",
            arguments.output,
        ),
        exit_code=1 if rejected else 0,
        error=bool(rejected),
    )


if __name__ == "__main__":
    raise SystemExit(main())
