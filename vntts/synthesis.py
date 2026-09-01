import re
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

_SHORT_TRAILING_ELLIPSIS = re.compile(
    r"^\s*(?P<spoken>[\w'’]+(?:\s+[\w'’]+)?)\s*(?:\.{3}|…)\s*$"
)


def normalize_short_trailing_ellipsis(text: str | None) -> str:
    """Give one/two-word ellipses an audible terminal boundary for MOSS."""
    match = _SHORT_TRAILING_ELLIPSIS.fullmatch(str(text or ""))
    return str(text) if match is None else match.group("spoken") + "."


def moss_generation_limits(text: str | None) -> tuple[int, float]:
    """Bound missed-EOS output while leaving room for normal speech cadence."""
    word_count = max(1, len(re.findall(r"[\w']+", str(text or ""), flags=re.UNICODE)))
    # Short hesitations keep the strict 3s guard; longer text gets a 90wpm
    # allowance plus lead/tail reserve, capped at the existing absolute 20s.
    max_audio_seconds = (
        3.0 if word_count <= 2 else min(20.0, max(4.0, 2.5 + word_count / 1.5))
    )
    return min(2048, max(256, round(max_audio_seconds * 100))), max_audio_seconds


class SynthesisCachePolicy(str, Enum):
    """Controls whether a render may read or write the speech caches."""

    USE = "use"
    REFRESH = "refresh"
    BYPASS = "bypass"


class SynthesisCompletion(str, Enum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    LIMITED = "limited"


@dataclass(frozen=True)
class SynthesisRequest:
    """All inputs that can change a rendered waveform."""

    voice: str
    text: str
    seed: int | None = None
    generation_profile: str = "stable"
    cancellation: Callable[[], bool] | Any | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    cache_policy: SynthesisCachePolicy = SynthesisCachePolicy.USE

    def cancellation_requested(self) -> bool:
        if self.cancellation is None:
            return False
        is_set = getattr(self.cancellation, "is_set", None)
        if callable(is_set):
            return bool(is_set())
        if callable(self.cancellation):
            return bool(self.cancellation())
        raise TypeError("Synthesis cancellation must be callable or Event-like")


@dataclass(frozen=True)
class SynthesisLimits:
    max_tokens: int | None
    max_audio_seconds: float | None


@dataclass(frozen=True)
class SynthesisTiming:
    first_chunk_ms: float | None
    total_ms: float


@dataclass(frozen=True)
class SynthesisDiagnostics:
    backend: str
    cache_source: str
    generation_profile: str
    seed: int | None
    chunk_count: int
    sample_count: int


@dataclass(frozen=True)
class SynthesisChunk:
    pcm: np.ndarray
    sample_rate: int
    index: int
    elapsed_ms: float


@dataclass(frozen=True)
class SynthesisResult:
    pcm: np.ndarray
    sample_rate: int
    completion: SynthesisCompletion
    limits: SynthesisLimits
    timing: SynthesisTiming
    diagnostics: SynthesisDiagnostics


class SynthesisChunkStream(Iterator[SynthesisChunk]):
    """A render iterator whose typed result becomes available after exhaustion."""

    def __init__(self, producer: Generator[SynthesisChunk, None, SynthesisResult]):
        self._producer = producer
        self._result: SynthesisResult | None = None

    def __iter__(self) -> Iterator[SynthesisChunk]:
        return self

    def __next__(self) -> SynthesisChunk:
        try:
            return next(self._producer)
        except StopIteration as stopped:
            if self._result is None:
                self._result = stopped.value
            raise

    @property
    def result(self) -> SynthesisResult:
        if self._result is None:
            raise RuntimeError(
                "Synthesis result is available only after stream exhaustion"
            )
        return self._result

    def collect(self) -> SynthesisResult:
        for _chunk in self:
            pass
        return self.result

    def close(self) -> None:
        close = getattr(self._producer, "close", None)
        if callable(close):
            close()
