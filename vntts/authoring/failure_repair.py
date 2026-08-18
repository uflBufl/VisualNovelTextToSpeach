"""Conservative, deterministic building blocks for bounded authoring repairs."""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from vntts.synthesis import (
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)

SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.!?])\s+(?=[\"'“‘A-Z])")
WORD_PATTERN = re.compile(r"\b[\w'’]+\b")
DEFAULT_MIN_SEGMENT_WORDS = 3
DEFAULT_EDGE_TRIGGER_SECONDS = 0.8
DEFAULT_EDGE_PADDING_SECONDS = 0.08
DEFAULT_SILENCE_DBFS = -45.0
DEFAULT_SEGMENT_PAUSE_MS = 180
DEFAULT_MAX_REPAIRED_AUDIO_SECONDS = 20.0
FAILURE_REPAIR_POLICY_VERSION = 3
LEGACY_FAILURE_REPAIR_POLICY_VERSION = 1
BOUNDED_SEED_POLICY_VERSION = 2
SENTENCE_BOUNDARY_SEGMENTATION = "sentence_boundary_segmentation"
EDGE_SILENCE_TRIM = "edge_silence_trim"
BOUNDED_SEED_RETRY = "bounded_seed_retry"
OFFLINE_FALLBACK_BACKEND = "offline_fallback_backend"
MAX_BOUNDED_TOTAL_ATTEMPTS = 3


class FailureRepairPolicyError(ValueError):
    """A bounded failure-repair policy is unsafe or malformed."""


@dataclass(frozen=True)
class FailureRepairPolicy:
    """Exact queue IDs authorized for one deterministic repair strategy."""

    sentence_segment_queue_ids: tuple[str, ...] = ()
    edge_silence_queue_ids: tuple[str, ...] = ()
    segment_pause_ms: int = DEFAULT_SEGMENT_PAUSE_MS
    bounded_seed_retry_queue_ids: tuple[str, ...] = ()
    offline_fallback_queue_ids: tuple[str, ...] = ()

    def __post_init__(self):
        sentence = _canonical_queue_ids(
            self.sentence_segment_queue_ids, "Sentence-segment queue IDs"
        )
        edge = _canonical_queue_ids(
            self.edge_silence_queue_ids, "Edge-silence queue IDs"
        )
        seed = _canonical_queue_ids(
            self.bounded_seed_retry_queue_ids, "Bounded-seed queue IDs"
        )
        fallback = _canonical_queue_ids(
            self.offline_fallback_queue_ids, "Offline-fallback queue IDs"
        )
        overlap = (
            (set(sentence) & set(edge))
            | (set(sentence) & set(seed))
            | (set(edge) & set(seed))
            | (set(sentence) & set(fallback))
            | (set(edge) & set(fallback))
            | (set(seed) & set(fallback))
        )
        if overlap:
            raise FailureRepairPolicyError(
                "A queue ID cannot use two failure-repair strategies: "
                + ", ".join(sorted(overlap))
            )
        if (
            not isinstance(self.segment_pause_ms, int)
            or isinstance(self.segment_pause_ms, bool)
            or not 0 <= self.segment_pause_ms <= 1000
        ):
            raise FailureRepairPolicyError(
                "Sentence-segment pause must be an integer from 0 to 1000 ms"
            )
        if not sentence and self.segment_pause_ms != DEFAULT_SEGMENT_PAUSE_MS:
            raise FailureRepairPolicyError(
                "A custom sentence pause requires a sentence-repair queue ID"
            )
        object.__setattr__(self, "sentence_segment_queue_ids", sentence)
        object.__setattr__(self, "edge_silence_queue_ids", edge)
        object.__setattr__(self, "bounded_seed_retry_queue_ids", seed)
        object.__setattr__(self, "offline_fallback_queue_ids", fallback)

    @property
    def queue_ids(self):
        return tuple(
            sorted(
                self.sentence_segment_queue_ids
                + self.edge_silence_queue_ids
                + self.bounded_seed_retry_queue_ids
                + self.offline_fallback_queue_ids
            )
        )

    @property
    def is_empty(self):
        return not self.queue_ids

    def strategy_for(self, queue_id):
        if queue_id in self.sentence_segment_queue_ids:
            return SENTENCE_BOUNDARY_SEGMENTATION
        if queue_id in self.edge_silence_queue_ids:
            return EDGE_SILENCE_TRIM
        if queue_id in self.bounded_seed_retry_queue_ids:
            return BOUNDED_SEED_RETRY
        if queue_id in self.offline_fallback_queue_ids:
            return OFFLINE_FALLBACK_BACKEND
        return None

    def to_document(self):
        document = {
            "schema_version": LEGACY_FAILURE_REPAIR_POLICY_VERSION,
            "sentence_segment_queue_ids": list(self.sentence_segment_queue_ids),
            "edge_silence_queue_ids": list(self.edge_silence_queue_ids),
            "segment_pause_ms": self.segment_pause_ms,
        }
        if self.bounded_seed_retry_queue_ids:
            document["schema_version"] = BOUNDED_SEED_POLICY_VERSION
            document["bounded_seed_retry_queue_ids"] = list(
                self.bounded_seed_retry_queue_ids
            )
        if self.offline_fallback_queue_ids:
            document["schema_version"] = FAILURE_REPAIR_POLICY_VERSION
            document["bounded_seed_retry_queue_ids"] = list(
                self.bounded_seed_retry_queue_ids
            )
            document["offline_fallback_queue_ids"] = list(
                self.offline_fallback_queue_ids
            )
        return document

    @classmethod
    def from_document(cls, document):
        if document is None:
            return cls()
        if not isinstance(document, dict):
            raise FailureRepairPolicyError("Failure-repair policy is malformed")
        version = document.get("schema_version")
        legacy_fields = {
            "schema_version",
            "sentence_segment_queue_ids",
            "edge_silence_queue_ids",
            "segment_pause_ms",
        }
        bounded_fields = legacy_fields | {"bounded_seed_retry_queue_ids"}
        current_fields = bounded_fields | {"offline_fallback_queue_ids"}
        if (
            (
                version == LEGACY_FAILURE_REPAIR_POLICY_VERSION
                and set(document) != legacy_fields
            )
            or (
                version == BOUNDED_SEED_POLICY_VERSION
                and set(document) != bounded_fields
            )
            or (
                version == FAILURE_REPAIR_POLICY_VERSION
                and set(document) != current_fields
            )
        ):
            raise FailureRepairPolicyError("Failure-repair policy is malformed")
        if version not in {
            LEGACY_FAILURE_REPAIR_POLICY_VERSION,
            BOUNDED_SEED_POLICY_VERSION,
            FAILURE_REPAIR_POLICY_VERSION,
        }:
            raise FailureRepairPolicyError("Unsupported failure-repair policy version")
        sentence = document.get("sentence_segment_queue_ids")
        edge = document.get("edge_silence_queue_ids")
        if not isinstance(sentence, list) or not isinstance(edge, list):
            raise FailureRepairPolicyError(
                "Failure-repair queue IDs must be JSON lists"
            )
        return cls(
            tuple(sentence),
            tuple(edge),
            document.get("segment_pause_ms"),
            tuple(document.get("bounded_seed_retry_queue_ids") or ()),
            tuple(document.get("offline_fallback_queue_ids") or ()),
        )


@dataclass(frozen=True)
class EdgeSilenceTrim:
    pcm: np.ndarray
    leading_trimmed_samples: int
    trailing_trimmed_samples: int


def _canonical_queue_ids(values, label):
    if not isinstance(values, (tuple, list)):
        raise FailureRepairPolicyError(f"{label} must be a list")
    canonical = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise FailureRepairPolicyError(f"{label} must contain non-empty text")
        canonical.append(value)
    if len(set(canonical)) != len(canonical):
        raise FailureRepairPolicyError(f"{label} must not contain duplicates")
    return tuple(sorted(canonical))


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


def render_sentence_segments(
    render,
    request,
    segments,
    *,
    pause_ms,
    max_audio_seconds=DEFAULT_MAX_REPAIRED_AUDIO_SECONDS,
):
    """Render exact safe segments and combine only typed COMPLETE results."""
    segments = tuple(segments)
    if len(segments) < 2 or any(
        not isinstance(value, str) or not value for value in segments
    ):
        raise ValueError("Sentence repair requires at least two exact text segments")
    if (
        not isinstance(pause_ms, int)
        or isinstance(pause_ms, bool)
        or not 0 <= pause_ms <= 1000
    ):
        raise ValueError("Sentence repair pause must be an integer from 0 to 1000 ms")
    if (
        not isinstance(max_audio_seconds, (int, float))
        or isinstance(max_audio_seconds, bool)
        or not np.isfinite(max_audio_seconds)
        or max_audio_seconds <= 0
    ):
        raise ValueError("Sentence repair audio limit must be finite and positive")
    results = []
    for index, text in enumerate(segments):
        segment_request = SynthesisRequest(
            voice=request.voice,
            text=text,
            seed=None if request.seed is None else request.seed + index,
            generation_profile=request.generation_profile,
            cancellation=request.cancellation,
            cache_policy=request.cache_policy,
        )
        result = render(segment_request).collect()
        if (
            result.diagnostics.seed != segment_request.seed
            or result.diagnostics.generation_profile
            != segment_request.generation_profile
        ):
            raise ValueError(
                "Sentence-segment render diagnostics conflict with its request"
            )
        results.append(result)
        if result.completion is not SynthesisCompletion.COMPLETE:
            return _combined_segment_result(
                results,
                request,
                pause_ms=0,
                max_audio_seconds=max_audio_seconds,
            )
    return _combined_segment_result(
        results,
        request,
        pause_ms=pause_ms,
        max_audio_seconds=max_audio_seconds,
    )


def _combined_segment_result(results, request, *, pause_ms, max_audio_seconds):
    sample_rates = {value.sample_rate for value in results}
    backends = {value.diagnostics.backend for value in results}
    profiles = {value.diagnostics.generation_profile for value in results}
    if (
        len(sample_rates) != 1
        or len(backends) != 1
        or profiles != {request.generation_profile}
    ):
        raise ValueError("Sentence-segment render results have conflicting provenance")
    shapes = {value.pcm.shape[1:] for value in results}
    if len(shapes) != 1:
        raise ValueError("Sentence-segment render results have incompatible channels")
    sample_rate = next(iter(sample_rates))
    parts = []
    pause_samples = int(round(sample_rate * pause_ms / 1000.0))
    for index, value in enumerate(results):
        parts.append(np.asarray(value.pcm))
        if index + 1 < len(results) and pause_samples:
            parts.append(
                np.zeros((pause_samples, *value.pcm.shape[1:]), dtype=value.pcm.dtype)
            )
    pcm = np.concatenate(parts, axis=0)
    completions = [value.completion for value in results]
    completion = next(
        (value for value in completions if value is not SynthesisCompletion.COMPLETE),
        SynthesisCompletion.COMPLETE,
    )
    if len(pcm) / sample_rate > max_audio_seconds:
        completion = SynthesisCompletion.LIMITED
    cache_sources = {value.diagnostics.cache_source for value in results}
    return SynthesisResult(
        pcm=pcm,
        sample_rate=sample_rate,
        completion=completion,
        limits=SynthesisLimits(
            max_tokens=_sum_optional(value.limits.max_tokens for value in results),
            max_audio_seconds=max_audio_seconds,
        ),
        timing=SynthesisTiming(
            first_chunk_ms=results[0].timing.first_chunk_ms,
            total_ms=sum(value.timing.total_ms for value in results),
        ),
        diagnostics=SynthesisDiagnostics(
            backend=next(iter(backends)),
            cache_source=(
                next(iter(cache_sources)) if len(cache_sources) == 1 else "mixed"
            ),
            generation_profile=request.generation_profile,
            seed=request.seed,
            chunk_count=sum(value.diagnostics.chunk_count for value in results),
            sample_count=len(pcm),
        ),
    )


def _sum_optional(values):
    values = tuple(values)
    return None if any(value is None for value in values) else sum(values)
