"""Replacement-reading state shared by dialogue and temporary captions."""

from collections.abc import Callable
from copy import copy

from vntts.dialog import ocr_texts_equivalent
from vntts.live_tracking import IncrementalDialogTracker


class ReadingSession:
    def __init__(
        self,
        tracker: IncrementalDialogTracker,
        tracker_factory: Callable[..., IncrementalDialogTracker],
        tracker_options: dict[str, object],
    ) -> None:
        self.tracker = tracker
        self.tracker_factory = tracker_factory
        self.tracker_options = tracker_options
        self.layout_id: str | None = None
        self.trackers: dict[str, IncrementalDialogTracker] = {}
        self.accepted_line_ids: dict[str, set[str]] = {}
        self.generation = tracker.generation
        self.restored_generation: int | None = None
        self.adjacent_tracker: IncrementalDialogTracker | None = None

    def select(self, layout_id: str, layout_kind: str) -> IncrementalDialogTracker:
        self.generation = max(self.generation, self.tracker.generation)
        if layout_id == self.layout_id:
            return self.tracker
        if self.layout_id is None:
            tracker = self.tracker
            restored = False
            self.accepted_line_ids[layout_id] = set(tracker.accepted_line_ids)
        else:
            self.adjacent_tracker = self.tracker
            restored = layout_kind == "dialogue" and layout_id in self.trackers
            tracker = (
                self.trackers[layout_id]
                if restored
                else self.tracker_factory(**self.tracker_options)
            )
            self.generation += 1
            tracker.generation = self.generation
            if not restored:
                self.accepted_line_ids[layout_id] = set()
        self.layout_id = layout_id
        self.tracker = tracker
        self.trackers[layout_id] = tracker
        # Source identity belongs to this layout occurrence, not a global text set.
        tracker.accepted_line_ids = self.accepted_line_ids[layout_id]
        self.restored_generation = tracker.generation if restored else None
        return tracker

    def coalesce(self, text: str, line_id: str | None) -> IncrementalDialogTracker:
        previous = self.adjacent_tracker
        if previous is None or self.tracker.committed_text:
            self.adjacent_tracker = None
            return self.tracker
        same_occurrence = (
            previous.canonical_line_id == line_id
            if previous.canonical_line_id is not None and line_id is not None
            else ocr_texts_equivalent(
                previous.latest_text, text, punctuation_fallback=True
            )
        )
        if not previous.committed_text or not same_occurrence:
            return self.tracker
        occurrence = copy(previous)
        occurrence.generation = self.tracker.generation
        occurrence.history = copy(previous.history)
        occurrence.pending_history = copy(previous.pending_history)
        occurrence.accepted_line_ids = set(previous.accepted_line_ids)
        self.tracker = occurrence
        assert self.layout_id is not None
        self.trackers[self.layout_id] = occurrence
        self.accepted_line_ids[self.layout_id] = occurrence.accepted_line_ids
        self.restored_generation = occurrence.generation
        self.adjacent_tracker = None
        return occurrence

    def can_advance(self, tracker: IncrementalDialogTracker) -> bool:
        # Returning unchanged lower dialogue cannot revive its revoked key lease.
        return tracker.generation != self.restored_generation
