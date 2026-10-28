"""Typed runtime contract shared by concrete speech backends."""

from dataclasses import dataclass
from typing import Protocol, TypeGuard

from vntts.playback import TypedPlaybackBackend
from vntts.synthesis import SynthesisChunkStream, SynthesisRequest


@dataclass(frozen=True)
class SpeechBackendCapabilities:
    voice_cloning: bool
    streaming: bool
    concurrent_prepare_and_play: bool


class SpeechBackend(TypedPlaybackBackend, Protocol):
    name: str
    capabilities: SpeechBackendCapabilities

    def stop(self) -> bool: ...


class RenderableBackend(Protocol):
    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...


def is_renderable_backend(value: object) -> TypeGuard[RenderableBackend]:
    return callable(getattr(value, "render", None))
