"""Typed runtime contract shared by concrete speech backends."""

from dataclasses import dataclass
from typing import Protocol

from vntts.playback import PlaybackOutcome, PreparedPlayback


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
        playback_guard=None,
    ) -> PlaybackOutcome: ...

    def stop(self) -> bool: ...
