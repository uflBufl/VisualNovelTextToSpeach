"""Typed runtime contract shared by concrete speech backends."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeGuard

from vntts.playback import PlaybackOutcome, PreparedPlayback
from vntts.synthesis import SynthesisChunkStream, SynthesisRequest


@dataclass(frozen=True)
class SpeechBackendCapabilities:
    voice_cloning: bool
    streaming: bool
    concurrent_prepare_and_play: bool
    interrupt_on_dialog_replacement: bool = False


class SpeechBackend(Protocol):
    name: str
    capabilities: SpeechBackendCapabilities

    def prepare_playback(self, character: str, text: str) -> PreparedPlayback: ...

    def play_prepared(
        self,
        prepared: PreparedPlayback,
        *,
        playback_guard: Callable[[], bool] | None = None,
    ) -> PlaybackOutcome: ...

    def stop(self) -> bool: ...


class RenderableBackend(Protocol):
    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...


def is_renderable_backend(value: object) -> TypeGuard[RenderableBackend]:
    return callable(getattr(value, "render", None))
