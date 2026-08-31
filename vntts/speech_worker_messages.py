"""Typed messages shared across the isolated speech-worker boundary."""

from dataclasses import dataclass

from vntts.synthesis import SynthesisCachePolicy


@dataclass(frozen=True)
class RemotePreparedSpeech:
    voice: str
    voice_key: str
    text: str
    generation_profile: str
    cache_policy: SynthesisCachePolicy
