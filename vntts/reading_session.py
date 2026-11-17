"""Replacement-reading state shared by dialogue and temporary captions."""

from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from vntts.dialog import ocr_texts_equivalent
from vntts.live_tracking import IncrementalDialogTracker


@dataclass
class _AdvanceLease:
    attempted: bool = False
    blocked: bool = False
    origin_generation: int | None = None

    def release(self) -> None:
        self.attempted = False


@dataclass
class _TrackerAdvance:
    generation: int
    live_generation: int | None = None
    lease: _AdvanceLease = field(default_factory=_AdvanceLease)


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
        self.advance_states: WeakKeyDictionary[
            IncrementalDialogTracker, _TrackerAdvance
        ] = WeakKeyDictionary()

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
            lease = self._advance_state(tracker).lease
            self.generation += 1
            tracker.generation = self.generation
            self.advance_states[tracker] = _TrackerAdvance(
                tracker.generation, lease=lease
            )
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
        lease = self._advance_state(previous).lease
        occurrence.generation = self.tracker.generation
        self.advance_states[occurrence] = _TrackerAdvance(
            occurrence.generation, lease=lease
        )
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

    def _advance_state(self, tracker: IncrementalDialogTracker) -> _TrackerAdvance:
        state = self.advance_states.get(tracker)
        if state is None or state.generation != tracker.generation:
            state = _TrackerAdvance(tracker.generation)
            self.advance_states[tracker] = state
        return state

    def bind_generation(
        self, tracker: IncrementalDialogTracker, generation: int
    ) -> None:
        state = self._advance_state(tracker)
        state.live_generation = generation
        if state.lease.origin_generation is None:
            state.lease.origin_generation = generation

    def block_advance(self, generation: int, *, current: bool = False) -> None:
        if current:
            self._advance_state(self.tracker).lease.blocked = True
        for state in tuple(self.advance_states.values()):
            if generation in (state.live_generation, state.lease.origin_generation):
                state.lease.blocked = True

    def can_advance(
        self, tracker: IncrementalDialogTracker, *, fresh: bool = False
    ) -> bool:
        lease = self._advance_state(tracker).lease
        return (
            not lease.attempted
            and not lease.blocked
            and (tracker.generation != self.restored_generation or fresh)
        )

    def claim_advance(
        self, tracker: IncrementalDialogTracker, *, fresh: bool = False
    ) -> Callable[[], None] | None:
        if not self.can_advance(tracker, fresh=fresh):
            return None
        lease = self._advance_state(tracker).lease
        lease.attempted = True
        return lease.release
