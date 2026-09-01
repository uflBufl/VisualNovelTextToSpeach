"""In-memory heard evidence for one authoring review session."""


class ReviewPlaybackEvidence:
    def __init__(self):
        self.active = None
        self.heard = set()

    @staticmethod
    def identity(item):
        authority = item.authority
        if authority is None:
            return None
        return item.queue_id, authority.state_sha256, authority.audio_sha256

    def begin(self, item):
        self.active = self.identity(item)

    def cancel(self):
        self.active = None

    def complete(self):
        if self.active is not None:
            self.heard.add(self.active)
        self.cancel()

    def allows(self, item):
        return item is not None and self.identity(item) in self.heard
