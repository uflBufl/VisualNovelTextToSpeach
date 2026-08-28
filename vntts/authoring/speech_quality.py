"""Versioned speech-silence measurement independent of generation orchestration."""

from __future__ import annotations

import io
import re
import wave
from dataclasses import dataclass

import numpy as np
from vntts_artifacts.audio import Pcm16MonoWavError, read_pcm16_mono_wav

from vntts.authoring.failure_repair import safe_sentence_segments
from vntts.authoring.generation_lease import BulkGenerationError

SILENCE_DBFS = -45.0
SILENCE_FRAME_MS = 80
MAX_LEADING_SILENCE_SECONDS = 0.8
MAX_TRAILING_SILENCE_SECONDS = 0.8
MAX_INTERNAL_SILENCE_SECONDS = 1.2
MAX_SILENCE_RATIO = 0.5
NOTABLE_SILENCE_SPAN_SECONDS = 0.5
PAUSE_DIAGNOSIS_VERSION = 1
LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION = 1
SPEECH_QUALITY_ANALYSIS_VERSION = 2


class SpeechSilenceValidationError(BulkGenerationError):
    """Generated speech contains unsafe silence spans."""

    def __init__(self, quality, failures, diagnosis=None):
        self.quality = quality
        self.failures = tuple(failures)
        self.diagnosis = diagnosis
        super().__init__(
            "Generated WAV failed speech-silence validation: "
            + ", ".join(self.failures)
        )


@dataclass(frozen=True)
class SpeechQuality:
    silence_ratio: float
    leading_silence_seconds: float
    trailing_silence_seconds: float
    longest_internal_silence_seconds: float
    analysis_version: int = SPEECH_QUALITY_ANALYSIS_VERSION


@dataclass(frozen=True)
class SpeechSilenceSpan:
    kind: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class SpeechPauseDiagnosis:
    schema_version: int
    analysis_version: int
    classification: str
    threshold_seconds: float
    sentence_boundary_count: int
    repairable_by_safe_segmentation: bool
    spans: tuple[SpeechSilenceSpan, ...]


for _compatibility_type in (
    SpeechSilenceValidationError,
    SpeechQuality,
    SpeechSilenceSpan,
    SpeechPauseDiagnosis,
):
    _compatibility_type.__module__ = "vntts.authoring.bulk_generation"


def measure_generated_speech(path, *, analysis_version=SPEECH_QUALITY_ANALYSIS_VERSION):
    """Measure speech pauses with an explicitly versioned PCM interpretation."""
    if analysis_version not in {
        LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
        SPEECH_QUALITY_ANALYSIS_VERSION,
    }:
        raise BulkGenerationError(
            f"Unsupported speech-quality analysis version: {analysis_version!r}"
        )
    try:
        samples, info = read_pcm16_mono_wav(path)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Unable to analyze generated speech: {error}"
        ) from error
    quality, _spans = analyze_generated_speech_samples(
        samples,
        sample_rate=info.sample_rate,
        duration_seconds=info.duration_seconds,
        analysis_version=analysis_version,
    )
    return quality


def measure_generated_speech_bytes(
    content, *, analysis_version=SPEECH_QUALITY_ANALYSIS_VERSION
):
    """Measure one already-captured PCM16 WAV payload without reopening a path."""
    if not isinstance(content, bytes):
        raise BulkGenerationError("Generated speech payload must be bytes")
    try:
        with wave.open(io.BytesIO(content), "rb") as source:
            if source.getcomptype() != "NONE":
                raise Pcm16MonoWavError("compressed WAV is not supported")
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise Pcm16MonoWavError("expected mono 16-bit PCM WAV")
            sample_rate = source.getframerate()
            sample_count = source.getnframes()
            samples = np.frombuffer(source.readframes(sample_count), dtype="<i2")
    except (EOFError, OSError, ValueError, wave.Error, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Unable to analyze generated speech: {error}"
        ) from error
    if sample_rate < 1 or len(samples) != sample_count:
        raise BulkGenerationError(
            "Unable to analyze generated speech: invalid WAV data"
        )
    quality, _spans = analyze_generated_speech_samples(
        samples,
        sample_rate=sample_rate,
        duration_seconds=sample_count / sample_rate,
        analysis_version=analysis_version,
    )
    return quality


def measure_generated_speech_samples(
    samples, *, sample_rate, duration_seconds, analysis_version
):
    quality, _spans = analyze_generated_speech_samples(
        samples,
        sample_rate=sample_rate,
        duration_seconds=duration_seconds,
        analysis_version=analysis_version,
    )
    return quality


def analyze_generated_speech_samples(
    samples, *, sample_rate, duration_seconds, analysis_version
):
    if analysis_version not in {
        LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION,
        SPEECH_QUALITY_ANALYSIS_VERSION,
    }:
        raise BulkGenerationError(
            f"Unsupported speech-quality analysis version: {analysis_version!r}"
        )
    samples = np.asarray(samples, dtype=np.float32)
    if analysis_version == SPEECH_QUALITY_ANALYSIS_VERSION:
        # PCM16 sample values require [-1, 1] units for dBFS thresholds.
        # Version 1 remains available only to validate published legacy state.
        samples /= 32768.0
    frame_samples = max(1, round(sample_rate * SILENCE_FRAME_MS / 1000))
    frame_rms = np.asarray(
        [
            np.sqrt(np.mean(samples[start : start + frame_samples] ** 2))
            for start in range(0, len(samples), frame_samples)
        ]
    )
    silent = frame_rms <= 10 ** (SILENCE_DBFS / 20.0)
    active_indices = np.flatnonzero(~silent)
    if not len(active_indices):
        quality = SpeechQuality(
            1.0,
            duration_seconds,
            duration_seconds,
            0.0,
            analysis_version,
        )
    else:
        first_active = int(active_indices[0])
        last_active = int(active_indices[-1])
        longest_internal = 0
        current_internal = 0
        for is_silent in silent[first_active + 1 : last_active]:
            if is_silent:
                current_internal += 1
                longest_internal = max(longest_internal, current_internal)
            else:
                current_internal = 0
        frame_seconds = frame_samples / sample_rate
        quality = SpeechQuality(
            silence_ratio=round(float(np.mean(silent)), 4),
            leading_silence_seconds=round(first_active * frame_seconds, 3),
            trailing_silence_seconds=round(
                (len(silent) - last_active - 1) * frame_seconds, 3
            ),
            longest_internal_silence_seconds=round(longest_internal * frame_seconds, 3),
            analysis_version=analysis_version,
        )
    frame_seconds = frame_samples / sample_rate
    spans = []
    start = None
    for index, is_silent in enumerate(silent):
        if is_silent and start is None:
            start = index
        if start is None or (is_silent and index + 1 < len(silent)):
            continue
        end = index if is_silent else index - 1
        start_seconds = start * frame_seconds
        end_seconds = min((end + 1) * frame_seconds, duration_seconds)
        span_duration = end_seconds - start_seconds
        if span_duration >= NOTABLE_SILENCE_SPAN_SECONDS:
            if not len(active_indices):
                kind = "all_silent"
            elif end < int(active_indices[0]):
                kind = "leading"
            elif start > int(active_indices[-1]):
                kind = "trailing"
            else:
                kind = "internal"
            spans.append(
                SpeechSilenceSpan(
                    kind=kind,
                    start_seconds=round(start_seconds, 3),
                    end_seconds=round(end_seconds, 3),
                    duration_seconds=round(span_duration, 3),
                )
            )
        start = None
    return quality, tuple(spans)


def speech_pause_diagnosis(text, quality, spans):
    features = text_failure_features(text)
    sentence_boundary_count = features["sentence_boundary_count"]
    internal_exceeded = (
        quality.longest_internal_silence_seconds > MAX_INTERNAL_SILENCE_SECONDS
    )
    classification = (
        "sentence_boundary_pause_candidate"
        if internal_exceeded and sentence_boundary_count >= 2
        else "speech_silence"
    )
    try:
        repairable = len(safe_sentence_segments(text)) >= 2
    except ValueError:
        repairable = False
    return SpeechPauseDiagnosis(
        schema_version=PAUSE_DIAGNOSIS_VERSION,
        analysis_version=quality.analysis_version,
        classification=classification,
        threshold_seconds=NOTABLE_SILENCE_SPAN_SECONDS,
        sentence_boundary_count=sentence_boundary_count,
        repairable_by_safe_segmentation=repairable,
        spans=tuple(spans),
    )


def inspect_generated_speech(
    path, *, analysis_version=SPEECH_QUALITY_ANALYSIS_VERSION, text=""
):
    """Reject long silence spans that pass basic peak/duration validation."""
    try:
        samples, info = read_pcm16_mono_wav(path)
    except (OSError, Pcm16MonoWavError) as error:
        raise BulkGenerationError(
            f"Unable to analyze generated speech: {error}"
        ) from error
    quality, spans = analyze_generated_speech_samples(
        samples,
        sample_rate=info.sample_rate,
        duration_seconds=info.duration_seconds,
        analysis_version=analysis_version,
    )
    failures = []
    if quality.leading_silence_seconds > MAX_LEADING_SILENCE_SECONDS:
        failures.append(f"{quality.leading_silence_seconds:.2f}s leading silence")
    if quality.trailing_silence_seconds > MAX_TRAILING_SILENCE_SECONDS:
        failures.append(f"{quality.trailing_silence_seconds:.2f}s trailing silence")
    if quality.longest_internal_silence_seconds > MAX_INTERNAL_SILENCE_SECONDS:
        failures.append(
            f"{quality.longest_internal_silence_seconds:.2f}s internal silence"
        )
    if quality.silence_ratio > MAX_SILENCE_RATIO:
        failures.append(f"{quality.silence_ratio:.0%} silent frames")
    if failures:
        raise SpeechSilenceValidationError(
            quality,
            failures,
            speech_pause_diagnosis(text, quality, spans),
        )
    return quality


def text_failure_features(text):
    value = str(text or "")
    return {
        "character_count": len(value),
        "word_count": len(re.findall(r"\b[\w'’]+\b", value)),
        "comma_count": value.count(","),
        "ellipsis_count": value.count("...") + value.count("…"),
        "sentence_boundary_count": sum(value.count(mark) for mark in ".!?"),
    }


__all__ = [
    "LEGACY_SPEECH_QUALITY_ANALYSIS_VERSION",
    "MAX_INTERNAL_SILENCE_SECONDS",
    "MAX_LEADING_SILENCE_SECONDS",
    "MAX_SILENCE_RATIO",
    "MAX_TRAILING_SILENCE_SECONDS",
    "NOTABLE_SILENCE_SPAN_SECONDS",
    "PAUSE_DIAGNOSIS_VERSION",
    "SPEECH_QUALITY_ANALYSIS_VERSION",
    "SILENCE_DBFS",
    "SILENCE_FRAME_MS",
    "SpeechPauseDiagnosis",
    "SpeechQuality",
    "SpeechSilenceSpan",
    "SpeechSilenceValidationError",
    "analyze_generated_speech_samples",
    "inspect_generated_speech",
    "measure_generated_speech",
    "measure_generated_speech_bytes",
    "measure_generated_speech_samples",
    "speech_pause_diagnosis",
    "text_failure_features",
]
