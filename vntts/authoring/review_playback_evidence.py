"""In-memory heard evidence for one authoring review session."""

from typing import TypeAlias

from vntts.authoring.workbench_contracts import ReviewItem

PlaybackIdentity: TypeAlias = tuple[str, str, str]


class ReviewPlaybackEvidence:
    def __init__(self) -> None:
        self.active: PlaybackIdentity | None = None
        self.heard: set[PlaybackIdentity] = set()

    @staticmethod
    def identity(item: ReviewItem) -> PlaybackIdentity | None:
        authority = item.authority
        if authority is None:
            return None
        return item.queue_id, authority.state_sha256, authority.audio_sha256

    def begin(self, item: ReviewItem) -> None:
        self.active = self.identity(item)

    def cancel(self) -> None:
        self.active = None

    def complete(self) -> None:
        if self.active is not None:
            self.heard.add(self.active)
        self.cancel()

    def allows(self, item: ReviewItem | None) -> bool:
        return item is not None and self.identity(item) in self.heard
