"""Shared synthetic bulk-generation inputs and backend for tests."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Generator, Sequence
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from vntts.synthesis import (
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)

PcmSamples = NDArray[np.float32]
RendererOutcome = SynthesisCompletion | BaseException


def queue_item(
    name: str = "one",
    *,
    action: str = "generate",
    character: str = "Hero",
    text: str | None = None,
) -> dict[str, object]:
    value = text or f"Exact text for {name}."
    text_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return {
        "record_type": "generation_item",
        "queue_id": f"line:{name}:{text_hash[:16]}",
        "line_id": f"line:{name}",
        "text_sha256": text_hash,
        "text": value,
        "speaker": character,
        "voice_character": character,
        "action": action,
        "prompt_adapters": {"generic": f"Delivery for {name}"},
    }


def write_queue(path: Path, items: Sequence[dict[str, object]]) -> Path:
    destination: object = write_voice_generation_queue(
        path,
        {"game": "Synthetic Game", "language": "en"},
        items,
    )
    if not isinstance(destination, Path):
        raise TypeError("Queue fixture writer returned an invalid path")
    return destination


def audio_samples(sample_rate: int = 16_000) -> PcmSamples:
    indexes = np.arange(sample_rate // 4, dtype=np.float32)
    return (0.25 * np.sin(2 * math.pi * 220 * indexes / sample_rate)).astype(np.float32)


class SyntheticRenderer:
    name = "synthetic"
    model_name = "synthetic-v1"

    def __init__(
        self,
        outcomes: Sequence[RendererOutcome] | None = None,
        *,
        inspect_state: Callable[[SynthesisRequest], None] | None = None,
        diagnostics_backend: str | None = None,
        pcm: PcmSamples | None = None,
        result_sample_rate: int = 16_000,
    ) -> None:
        self.outcomes = list(outcomes or [SynthesisCompletion.COMPLETE])
        self.requests: list[SynthesisRequest] = []
        self.inspect_state = inspect_state
        self.diagnostics_backend = diagnostics_backend
        self.pcm = pcm
        self.result_sample_rate = result_sample_rate
        self.stop_calls = 0

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream:
        self.requests.append(request)
        if self.inspect_state is not None:
            self.inspect_state(request)
        outcome = (
            self.outcomes.pop(0) if self.outcomes else SynthesisCompletion.COMPLETE
        )
        if isinstance(outcome, BaseException):
            raise outcome
        pcm = audio_samples() if self.pcm is None else self.pcm

        def produce() -> Generator[SynthesisChunk, None, SynthesisResult]:
            yield SynthesisChunk(pcm, 16_000, 0, 1.0)
            return SynthesisResult(
                pcm=pcm,
                sample_rate=self.result_sample_rate,
                completion=outcome,
                limits=SynthesisLimits(256, 180.0),
                timing=SynthesisTiming(1.0, 2.0),
                diagnostics=SynthesisDiagnostics(
                    backend=self.diagnostics_backend or self.name,
                    cache_source="fresh-generation",
                    generation_profile=request.generation_profile,
                    seed=request.seed,
                    chunk_count=1,
                    sample_count=len(pcm),
                ),
            )

        return SynthesisChunkStream(produce())

    def stop(self) -> None:
        self.stop_calls += 1
