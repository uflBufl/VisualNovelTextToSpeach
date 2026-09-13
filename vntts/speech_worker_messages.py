"""Typed messages shared across the isolated speech-worker boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, TypedDict

from vntts.synthesis import SynthesisCachePolicy


@dataclass(frozen=True)
class RemotePreparedSpeech:
    voice: str
    voice_key: str
    text: str
    generation_profile: str
    cache_policy: SynthesisCachePolicy


FrameDocument: TypeAlias = dict[str, object]


class VoiceDocument(TypedDict):
    character: str
    speaker: str
    aliases: list[str]
    references: list[str]
    reference_root: str | None


class RegistryDocument(TypedDict):
    voices: list[VoiceDocument]
    assignments: dict[str, VoiceDocument | None]


class SynthesisLimitsDocument(TypedDict):
    max_tokens: int | None
    max_audio_seconds: float | None


class SynthesisTimingDocument(TypedDict):
    first_chunk_ms: float | None
    total_ms: float


class SynthesisDiagnosticsDocument(TypedDict):
    backend: str
    cache_source: str
    generation_profile: str
    seed: int | None
    chunk_count: int
    sample_count: int


class SynthesisResultDocument(TypedDict):
    sample_rate: int
    completion: str
    limits: SynthesisLimitsDocument
    timing: SynthesisTimingDocument
    diagnostics: SynthesisDiagnosticsDocument
