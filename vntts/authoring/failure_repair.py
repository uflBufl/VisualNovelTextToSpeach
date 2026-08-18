"""Conservative, deterministic building blocks for bounded authoring repairs."""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘A-Z])")
WORD_PATTERN = re.compile(r"\b[\w'’]+\b")
DEFAULT_MIN_SEGMENT_WORDS = 3
DEFAULT_EDGE_TRIGGER_SECONDS = 0.8
DEFAULT_EDGE_PADDING_SECONDS = 0.08
DEFAULT_SILENCE_DBFS = -45.0


@dataclass(frozen=True)
class EdgeSilenceTrim:
    pcm: np.ndarray
    leading_trimmed_samples: int
    trailing_trimmed_samples: int


def safe_sentence_segments(text, *, minimum_words=DEFAULT_MIN_SEGMENT_WORDS):
    """Split only between complete, substantial sentences; otherwise do nothing."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Repair text must be non-empty text")
    if not isinstance(minimum_words, int) or isinstance(minimum_words, bool):
        raise ValueError("Minimum segment words must be an integer")
    if minimum_words < 1:
        raise ValueError("Minimum segment words must be positive")
    original = text.strip()
    segments = tuple(
        value.strip()
        for value in SENTENCE_BOUNDARY_PATTERN.split(original)
        if value.strip()
    )
    if len(segments) < 2:
        return (original,)
    if any(len(WORD_PATTERN.findall(value)) < minimum_words for value in segments):
        return (original,)
    return segments


def trim_excess_edge_silence(
    pcm,
    sample_rate,
    *,
    trigger_seconds=DEFAULT_EDGE_TRIGGER_SECONDS,
    padding_seconds=DEFAULT_EDGE_PADDING_SECONDS,
    silence_dbfs=DEFAULT_SILENCE_DBFS,
):
    """Trim only long leading/trailing silence while retaining boundary padding."""
    samples = np.asarray(pcm, dtype=np.float32)
    if samples.ndim != 1 or not samples.size or not np.isfinite(samples).all():
        raise ValueError("Repair PCM must be finite, non-empty mono samples")
    if (
        not isinstance(sample_rate, int)
        or isinstance(sample_rate, bool)
        or sample_rate <= 0
    ):
        raise ValueError("Repair sample rate must be a positive integer")
    if (
        trigger_seconds <= 0
        or padding_seconds < 0
        or padding_seconds >= trigger_seconds
    ):
        raise ValueError("Edge silence timing is invalid")
    threshold = 10.0 ** (float(silence_dbfs) / 20.0)
    active = np.flatnonzero(np.abs(samples) > threshold)
    if not active.size:
        return EdgeSilenceTrim(samples.copy(), 0, 0)
    first = int(active[0])
    last = int(active[-1])
    trigger = int(round(trigger_seconds * sample_rate))
    padding = int(round(padding_seconds * sample_rate))
    leading = max(0, first - padding) if first > trigger else 0
    trailing_silence = len(samples) - last - 1
    trailing = max(0, trailing_silence - padding) if trailing_silence > trigger else 0
    end = len(samples) - trailing if trailing else len(samples)
    return EdgeSilenceTrim(samples[leading:end].copy(), leading, trailing)
