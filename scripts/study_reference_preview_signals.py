"""Offline diagnostics for checksum-bound, matched typical/steady preview WAVs.

Run with python -m scripts.study_reference_preview_signals MANIFEST > report.json.
The manifest contains a ``samples`` list with character, method, text_index, text,
seed, wav and wav_sha256. Optional transcript is compared using the app's ASR
normalization; optional asr_exact is a cached independent ASR result. WAV paths
are relative to the manifest. Uses the application's pinned pitch runtime.
This study never changes production ranking.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from vntts_artifacts.audio import read_pcm16_mono_wav

from vntts.authoring.speech_quality import (
    SpeechSilenceValidationError,
    inspect_generated_speech_samples,
)
from vntts.path_safety import read_regular_bytes
from vntts.reference_selection import delivery_features
from vntts.speech_content_advice import normalize_speech_text


def terminal_pitch_change(payload: bytes) -> float | None:
    """Final voiced 300 ms minus preceding voiced 300 ms, in semitones.

    This measures contour only: English questions need not rise, and a rise
    cannot establish that a speaker sounds questioning.
    """
    import parselmouth
    import soundfile as sf

    audio, rate = sf.read(io.BytesIO(payload), always_2d=True)
    pitch = parselmouth.Sound(audio.mean(axis=1), sampling_frequency=rate).to_pitch(
        time_step=0.01, pitch_floor=65, pitch_ceiling=650
    )
    frequencies = pitch.selected_array["frequency"]
    voiced = frequencies > 0
    times, frequencies = pitch.xs()[voiced], frequencies[voiced]
    if len(times) < 20:
        return None
    end = times[-1]
    final = frequencies[times >= end - 0.3]
    preceding = frequencies[(times >= end - 0.6) & (times < end - 0.3)]
    if min(len(final), len(preceding)) < 5:
        return None
    return float(12 * np.log2(np.median(final) / np.median(preceding)))


def measure_sample(row: dict[str, Any], root: Path) -> dict[str, Any]:
    payload = read_regular_bytes(root / row["wav"])
    digest = hashlib.sha256(payload).hexdigest()
    if digest != row["wav_sha256"]:
        raise ValueError("Preview WAV checksum mismatch")
    samples, info = read_pcm16_mono_wav(io.BytesIO(payload))
    failures: list[str] = []
    pause_diagnosis = None
    try:
        quality = inspect_generated_speech_samples(
            samples,
            sample_rate=info.sample_rate,
            duration_seconds=info.duration_seconds,
            text=row["text"],
        )
    except SpeechSilenceValidationError as error:
        failures = list(error.failures)
        quality = error.quality
        if error.diagnosis is not None:
            pause_diagnosis = asdict(error.diagnosis)
    asr_exact = row.get("asr_exact")
    if "transcript" in row:
        asr_exact = normalize_speech_text(row["transcript"]) == normalize_speech_text(
            row["text"]
        )
    if asr_exact is not None and not isinstance(asr_exact, bool):
        raise ValueError("asr_exact must be a boolean or absent")
    amplitudes = np.abs(np.asarray(samples, dtype=np.float64))
    peak = float(np.max(amplitudes)) / 32768
    return {
        **row,
        "asr_exact": asr_exact,
        "duration_seconds": info.duration_seconds,
        "words_per_second": len(re.findall(r"\b[\w']+\b", row["text"]))
        / info.duration_seconds,
        "peak": peak,
        "full_scale_fraction": float(np.mean(amplitudes >= 32766)),
        "silence": asdict(quality),
        "qa_failures": failures,
        "pause_diagnosis": pause_diagnosis,
        "delivery": delivery_features(payload),
        "terminal_pitch_change_st": terminal_pitch_change(payload),
    }


def diagnostic_direction(differences: list[float], margin: float) -> str:
    """Preserve ties and mixed runs rather than average away reversals.

    Positive differences mean typical is lower. Margin is a descriptive
    measurement tolerance, never a learned quality weight.
    """
    signs = {
        1 if value > margin else -1 if value < -margin else 0 for value in differences
    }
    if not signs or signs == {0}:
        return "tie"
    if 1 in signs and -1 in signs:
        return "mixed"
    return "typical_lower" if 1 in signs else "steady_lower"


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen = set()
    for row in samples:
        if row["method"] not in {"typical", "steady"}:
            raise ValueError("Only typical and steady methods belong in this study")
        identity = row["character"], row["method"], row["text_index"], row["seed"]
        if identity in seen:
            raise ValueError("Duplicate matched preview identity")
        seen.add(identity)
        groups[row["character"]].append(row)
    reports = {}
    for character, rows in groups.items():
        arms = {
            method: {
                (r["text_index"], r["seed"]): r for r in rows if r["method"] == method
            }
            for method in ("typical", "steady")
        }
        if not arms["typical"] or arms["typical"].keys() != arms["steady"].keys():
            raise ValueError("Every text and seed must have both preview methods")
        pairs = [
            (arms["typical"][key], arms["steady"][key])
            for key in sorted(arms["typical"])
        ]
        if any(a["text"] != b["text"] for a, b in pairs):
            raise ValueError("Matched previews must use identical text")
        asr = [
            (a, b)
            for a, b in pairs
            if a["asr_exact"] is not None and b["asr_exact"] is not None
        ]
        pitch = [
            (a, b)
            for a, b in pairs
            if a["delivery"] is not None and b["delivery"] is not None
        ]
        method_stats = {}
        for method, keyed in arms.items():
            values = list(keyed.values())
            seed_ranges = {}
            for field in (
                "duration_seconds",
                "words_per_second",
                "pitch_hz",
                "pitch_span_st",
            ):
                ranges = {}
                for text_index in sorted({key[0] for key in keyed}):
                    same_text = [r for r in values if r["text_index"] == text_index]
                    measured = [
                        r[field] if field in r else (r["delivery"] or {}).get(field)
                        for r in same_text
                    ]
                    valid = [float(value) for value in measured if value is not None]
                    if len(valid) > 1:
                        ranges[str(text_index)] = max(valid) - min(valid)
                seed_ranges[field] = ranges
            method_stats[method] = {
                "qa_failed_samples": sum(bool(r["qa_failures"]) for r in values),
                "asr_mismatches": sum(r["asr_exact"] is False for r in values),
                "asr_measured_samples": sum(r["asr_exact"] is not None for r in values),
                "mean_pitch_span_st": float(
                    np.mean(
                        [
                            r["delivery"]["pitch_span_st"]
                            for r in values
                            if r["delivery"]
                        ]
                    )
                )
                if any(r["delivery"] for r in values)
                else None,
                "mean_words_per_second": float(
                    np.mean([r["words_per_second"] for r in values])
                ),
                "seed_ranges_by_text": seed_ranges,
            }
        reports[character] = {
            "methods": method_stats,
            "matched_qa_failure_direction": diagnostic_direction(
                [
                    float(bool(b["qa_failures"])) - float(bool(a["qa_failures"]))
                    for a, b in pairs
                ],
                0,
            ),
            "matched_asr_error_direction": diagnostic_direction(
                [float(not b["asr_exact"]) - float(not a["asr_exact"]) for a, b in asr],
                0,
            )
            if len(asr) == len(pairs)
            else "unmeasured",
            "matched_pitch_span_direction": diagnostic_direction(
                [
                    b["delivery"]["pitch_span_st"] - a["delivery"]["pitch_span_st"]
                    for a, b in pitch
                ],
                1,
            )
            if len(pitch) == len(pairs)
            else "unmeasured",
        }
    return reports


def study(manifest: Path) -> dict[str, Any]:
    payload = read_regular_bytes(manifest)
    document = json.loads(payload)
    samples = [measure_sample(row, manifest.parent) for row in document["samples"]]
    return {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "rules": {
            "qa": "Existing generated-speech silence gate, analysis version 2",
            "asr": "Normalized exact text match; mismatch is advisory, not proven synthesis error",
            "pitch_span": "Existing Praat 10th-90th percentile span; 1 semitone descriptive tie margin",
            "terminal_pitch": "Median pitch of final voiced 300ms minus preceding voiced 300ms; contour only, not question classification",
            "direction": "Every matched text/seed retained; opposite directions yield mixed",
            "interpretation": "No signal establishes listener preference or neutral/questioning appropriateness; no automated ranking",
        },
        "characters": summarize(samples),
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(study(arguments.manifest), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
