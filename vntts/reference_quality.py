"""Objective preflight for voice-cloning reference WAV files."""

from __future__ import annotations

import argparse
import hashlib
import io
import wave
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.atomic_io import atomic_write_json

from vntts.cli import cli_error, cli_messages


class ReferenceQualityReport(TypedDict):
    path: str
    sha256: str
    sample_rate: int
    duration_seconds: float
    peak: float
    rms: float
    clipping_fraction: float
    inactive_window_fraction: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    dc_offset: float
    objective_preflight: Literal["pass", "reject"]
    rejection_reasons: list[str]
    manual_review_required: list[str]


class ReferenceQualitySet(TypedDict):
    schema_version: int
    references: list[ReferenceQualityReport]
    objective_ranking: list[int]
    selection_policy: str


@dataclass(frozen=True)
class ReferenceMetrics:
    duration_seconds: float
    peak: float
    rms: float
    clipping_fraction: float
    inactive_fraction: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    dc_offset: float


def analyze_reference(
    path: str | Path,
    *,
    silence_db: float = -40.0,
    window_ms: float = 20.0,
) -> ReferenceQualityReport:
    path = Path(path).expanduser().resolve()
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"Unable to read reference WAV {path}: {error}") from error
    return analyze_reference_bytes(
        payload,
        path=path,
        silence_db=silence_db,
        window_ms=window_ms,
    )


def analyze_reference_bytes(
    payload: bytes,
    *,
    path: str | Path,
    silence_db: float = -40.0,
    window_ms: float = 20.0,
) -> ReferenceQualityReport:
    """Analyze one immutable byte snapshot and bind the report to its digest."""
    path = Path(path).expanduser().resolve()
    if not isinstance(payload, bytes):
        raise ValueError("Reference WAV payload must be bytes")
    samples, sample_rate = _read_pcm16_mono_samples(payload, path)
    metrics = _reference_metrics(samples, sample_rate, silence_db, window_ms)
    rejection_reasons = _rejection_reasons(metrics)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "sample_rate": sample_rate,
        "duration_seconds": round(metrics.duration_seconds, 3),
        "peak": round(metrics.peak, 6),
        "rms": round(metrics.rms, 6),
        "clipping_fraction": round(metrics.clipping_fraction, 8),
        "inactive_window_fraction": round(metrics.inactive_fraction, 6),
        "leading_silence_seconds": round(metrics.leading_silence_seconds, 3),
        "trailing_silence_seconds": round(metrics.trailing_silence_seconds, 3),
        "dc_offset": round(metrics.dc_offset, 8),
        "objective_preflight": "pass" if not rejection_reasons else "reject",
        "rejection_reasons": rejection_reasons,
        "manual_review_required": [
            "single-speaker-identity",
            "music-or-background-audio",
            "spoken-content-and-pronunciation",
        ],
    }


def _read_pcm16_mono_samples(
    payload: bytes,
    path: Path,
) -> tuple[NDArray[np.float32], int]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
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
    if len(raw) != frame_count * channels * sample_width:
        raise ValueError(f"Reference WAV contains truncated PCM data: {path}")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0, sample_rate


def _reference_metrics(
    samples: NDArray[np.float32],
    sample_rate: int,
    silence_db: float,
    window_ms: float,
) -> ReferenceMetrics:
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
    return ReferenceMetrics(
        duration_seconds=duration_seconds,
        peak=peak,
        rms=rms,
        clipping_fraction=clipping_fraction,
        inactive_fraction=float(np.mean(~active)),
        leading_silence_seconds=leading_silence_seconds,
        trailing_silence_seconds=trailing_silence_seconds,
        dc_offset=dc_offset,
    )


def _rejection_reasons(metrics: ReferenceMetrics) -> list[str]:
    return [
        reason
        for rejected, reason in (
            (metrics.duration_seconds < 1.0, "duration-under-1-second"),
            (metrics.duration_seconds > 30.0, "duration-over-30-seconds"),
            (
                metrics.peak < 0.02 or metrics.rms < 0.005,
                "signal-too-quiet",
            ),
            (metrics.clipping_fraction > 0.001, "excessive-clipping"),
            (metrics.leading_silence_seconds > 1.0, "excessive-leading-silence"),
            (metrics.trailing_silence_seconds > 1.0, "excessive-trailing-silence"),
            (metrics.dc_offset > 0.02, "excessive-dc-offset"),
        )
        if rejected
    ]


def analyze_reference_set(paths: Iterable[str | Path]) -> ReferenceQualitySet:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preflight PCM16 mono WAV voice references"
    )
    parser.add_argument("reference", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = analyze_reference_set(arguments.reference)
        atomic_write_json(arguments.output, report)
    except (OSError, TypeError, ValueError) as error:
        return int(cli_error(error))
    rejected = sum(
        reference["objective_preflight"] == "reject"
        for reference in report["references"]
    )
    return int(
        cli_messages(
            (
                f"Reference preflight: {len(report['references']) - rejected} passed, "
                f"{rejected} rejected",
                arguments.output,
            ),
            exit_code=1 if rejected else 0,
            error=bool(rejected),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
