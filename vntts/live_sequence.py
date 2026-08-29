"""Session cursor for the shared checksum-bound live-sequence contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from vntts_artifacts.live_sequence import (
    LIVE_SEQUENCE_SCHEMA,
    LIVE_SEQUENCE_SCHEMA_VERSION,
    LiveSequenceChapter,
    LiveSequenceEvent,
    LiveSequencePlan,
    LiveSequencePlanError,
    load_live_sequence_plan,
    write_live_sequence_plan,
)

__all__ = [
    "LIVE_SEQUENCE_SCHEMA",
    "LIVE_SEQUENCE_SCHEMA_VERSION",
    "LiveSequenceChapter",
    "LiveSequenceEvent",
    "LiveSequencePlan",
    "LiveSequencePlanError",
    "StoryCursor",
    "StoryCursorError",
    "StoryCursorSnapshot",
    "StoryCursorState",
    "load_live_sequence_plan",
    "write_live_sequence_plan",
]


class StoryCursorError(RuntimeError):
    """Raised when a cursor transition violates the sequence plan."""


class StoryCursorState(str, Enum):
    UNSYNCHRONIZED = "unsynchronized"
    ANCHORING = "anchoring"
    LOCKED = "locked"
    PLAYING = "playing"
    WAITING_TRANSITION = "waiting-transition"
    DESYNCHRONIZED = "desynchronized"
    MANUAL = "manual"


@dataclass(frozen=True)
class StoryCursorSnapshot:
    state: StoryCursorState
    current_event_id: str | None
    current_line_id: str | None
    expected_successor_ids: tuple[str, ...]
    reason: str | None


class StoryCursor:
    """Session-only cursor that fails closed on unexpected story transitions."""

    def __init__(self, plan):
        if not isinstance(plan, LiveSequencePlan):
            raise TypeError("StoryCursor requires a LiveSequencePlan")
        self.plan = plan
        self.state = StoryCursorState.UNSYNCHRONIZED
        self.current_event_id = None
        self.reason = None

    @property
    def current_event(self):
        if self.current_event_id is None:
            return None
        return self.plan.events[self.current_event_id]

    @property
    def expected_successors(self):
        event = self.current_event
        if event is None:
            return ()
        return tuple(self.plan.events[event_id] for event_id in event.successors)

    @property
    def can_auto_advance(self):
        event = self.current_event
        return bool(
            self.state == StoryCursorState.LOCKED
            and event is not None
            and event.control == "automatic"
            and len(event.successors) == 1
            and (
                (event.is_speech and self.reason == "playback-completed")
                or (
                    event.kind == "silent"
                    and self.reason == "visual-transition-confirmed"
                )
            )
        )

    @property
    def can_confirm_visual_transition(self):
        event = self.current_event
        return bool(
            event is not None
            and (
                self.state == StoryCursorState.WAITING_TRANSITION
                or (
                    self.state == StoryCursorState.LOCKED
                    and (
                        self.reason == "playback-completed"
                        or (
                            event.kind == "silent"
                            and self.reason == "visual-transition-confirmed"
                        )
                    )
                )
            )
        )

    def deterministic_manual_successor(self):
        """Return the unique upcoming manual boundary, if it precedes dialogue."""
        if self.state != StoryCursorState.WAITING_TRANSITION:
            return None
        current = self._require_current()
        visited = {current.event_id}
        while len(current.successors) == 1:
            candidate = self.plan.events[current.successors[0]]
            if candidate.event_id in visited:
                return None
            visited.add(candidate.event_id)
            if candidate.kind in {"choice", "wait"} or candidate.control == "manual":
                return candidate
            if candidate.kind in {"speech", "silent"} or candidate.control not in {
                "automatic",
                "passive",
            }:
                return None
            current = candidate
        return None

    def snapshot(self):
        event = self.current_event
        return StoryCursorSnapshot(
            state=self.state,
            current_event_id=self.current_event_id,
            current_line_id=event.line_id if event is not None else None,
            expected_successor_ids=(event.successors if event is not None else ()),
            reason=self.reason,
        )

    def reset(self, reason=None):
        self.state = StoryCursorState.UNSYNCHRONIZED
        self.current_event_id = None
        self.reason = _optional_text(reason)
        return self.snapshot()

    def begin_anchoring(self, reason=None):
        if self.state in {
            StoryCursorState.PLAYING,
            StoryCursorState.WAITING_TRANSITION,
        }:
            raise StoryCursorError(f"Cannot anchor while cursor is {self.state.value}")
        self.state = StoryCursorState.ANCHORING
        self.reason = _optional_text(reason)
        return self.snapshot()

    def anchor_event(self, event_id, reason=None):
        event = self._event(event_id)
        self.current_event_id = event.event_id
        self.state = self._resting_state(event)
        self.reason = _optional_text(reason) or "explicit-anchor"
        return self.snapshot()

    def anchor_line(self, line_id, reason=None):
        event = self.plan.event_for_line(str(line_id))
        if event is None:
            raise StoryCursorError(f"No live sequence event binds line {line_id!r}")
        return self.anchor_event(event.event_id, reason or "line-anchor")

    def begin_playback(self):
        event = self._require_current()
        if self.state != StoryCursorState.LOCKED:
            raise StoryCursorError(f"Cannot play while cursor is {self.state.value}")
        if not event.is_speech:
            raise StoryCursorError(f"Event {event.event_id!r} is not speakable")
        self.state = StoryCursorState.PLAYING
        self.reason = "playback-started"
        return self.snapshot()

    def finish_playback(self, *, successful=True):
        event = self._require_current()
        if self.state != StoryCursorState.PLAYING:
            raise StoryCursorError(
                f"Cannot finish playback while cursor is {self.state.value}"
            )
        self.state = self._resting_state(event)
        self.reason = "playback-completed" if successful else "playback-failed"
        return self.snapshot()

    def deterministic_visual_successor(self):
        """Return one visible successor without guessing across a branch."""
        if not self.can_confirm_visual_transition:
            return None
        current = self._require_current()
        visited = {current.event_id}
        while True:
            if len(current.successors) != 1:
                return None
            candidate = self.plan.events[current.successors[0]]
            if candidate.event_id in visited:
                return None
            visited.add(candidate.event_id)
            if candidate.kind in {"speech", "silent"}:
                return candidate
            if candidate.kind in {"choice", "wait"} or candidate.control not in {
                "automatic",
                "passive",
            }:
                return None
            current = candidate

    def confirm_visual_transition(self):
        candidate = self.deterministic_visual_successor()
        if candidate is None:
            return None
        self.anchor_event(candidate.event_id, "visual-transition-confirmed")
        return candidate

    def dispatch_advance(self):
        event = self._require_current()
        if not self.can_auto_advance:
            raise StoryCursorError(
                f"Event {event.event_id!r} is not ready for one automatic advance"
            )
        self.state = StoryCursorState.WAITING_TRANSITION
        self.reason = "advance-dispatched"
        return self.snapshot()

    def confirm_transition(self, event_id):
        current = self._require_current()
        event_id = str(event_id)
        if self.state != StoryCursorState.WAITING_TRANSITION:
            raise StoryCursorError(
                f"Cannot confirm a transition while cursor is {self.state.value}"
            )
        if event_id not in current.successors:
            return self.desynchronize(
                f"unexpected-transition:{current.event_id}->{event_id}"
            )
        return self.anchor_event(event_id, "transition-confirmed")

    def confirm_passive_transition(self, event_id):
        current = self._require_current()
        event_id = str(event_id)
        if self.state != StoryCursorState.LOCKED or current.control != "passive":
            raise StoryCursorError(
                f"Cannot confirm a passive transition while cursor is {self.state.value}"
            )
        if event_id not in current.successors:
            return self.desynchronize(
                f"unexpected-passive-transition:{current.event_id}->{event_id}"
            )
        return self.anchor_event(event_id, "passive-transition-confirmed")

    def observe_line(self, line_id):
        """Update a cursor from an exact canonical line observation."""
        if self.state == StoryCursorState.DESYNCHRONIZED:
            return self.snapshot()
        event = self.plan.event_for_line(str(line_id))
        if event is None:
            return self.desynchronize(f"unplanned-line:{line_id}")
        current = self.current_event
        if current is None or self.state in {
            StoryCursorState.UNSYNCHRONIZED,
            StoryCursorState.ANCHORING,
        }:
            return self.anchor_event(event.event_id, "observation-anchor")
        if event.event_id == current.event_id:
            self.reason = "observation-current-event"
            return self.snapshot()
        if event.event_id in current.successors:
            return self.anchor_event(event.event_id, "observation-successor")
        if self._is_linear_observed_successor(current, event.event_id):
            return self.anchor_event(event.event_id, "observation-successor-chain")
        return self.desynchronize(
            f"observation-unexpected:{current.event_id}->{event.event_id}"
        )

    def bounded_visible_successors(self, *, maximum_visible_depth=3, maximum_nodes=24):
        """Return explicit visible lookahead without inferring undeclared edges."""
        current = self.current_event
        if current is None or maximum_visible_depth < 1 or maximum_nodes < 1:
            return ()
        pending = [(event_id, 0) for event_id in current.successors]
        visited_depth = {}
        visible = []
        emitted_event_ids = set()
        while pending and len(visited_depth) < maximum_nodes:
            event_id, visible_depth = pending.pop(0)
            previous_depth = visited_depth.get(event_id)
            if previous_depth is not None and previous_depth <= visible_depth:
                continue
            if previous_depth is None and len(visited_depth) >= maximum_nodes:
                break
            visited_depth[event_id] = visible_depth
            event = self.plan.events[event_id]
            next_visible_depth = visible_depth
            if event.kind in {"speech", "silent"}:
                next_visible_depth += 1
                if event.event_id not in emitted_event_ids:
                    emitted_event_ids.add(event.event_id)
                    visible.append(event)
                if next_visible_depth >= maximum_visible_depth:
                    continue
            if event.kind == "wait":
                continue
            pending.extend(
                (successor_id, next_visible_depth) for successor_id in event.successors
            )
        return tuple(visible)

    def observe_bounded_line(self, line_id, allowed_event_ids):
        """Recover only to a line proven to be in the supplied graph window."""
        event = self.plan.event_for_line(str(line_id))
        if event is None:
            return self.desynchronize(f"unplanned-line:{line_id}")
        current = self.current_event
        if current is not None and event.event_id == current.event_id:
            self.state = self._resting_state(event)
            self.reason = "observation-current-event"
            return self.snapshot()
        allowed = {str(event_id) for event_id in allowed_event_ids}
        if event.event_id not in allowed:
            return self.desynchronize(
                f"observation-outside-bounded-window:{event.event_id}"
            )
        return self.anchor_event(event.event_id, "observation-bounded-lookahead")

    def desynchronize(self, reason):
        self.state = StoryCursorState.DESYNCHRONIZED
        self.reason = _required_text(reason, "desynchronization reason")
        return self.snapshot()

    def _event(self, event_id):
        event_id = str(event_id)
        try:
            return self.plan.events[event_id]
        except KeyError as error:
            raise StoryCursorError(
                f"Unknown live sequence event {event_id!r}"
            ) from error

    def _require_current(self):
        event = self.current_event
        if event is None:
            raise StoryCursorError("Story cursor has no current event")
        return event

    def _is_linear_observed_successor(self, current, target_event_id):
        pending = current
        visited = set()
        while pending.event_id not in visited:
            visited.add(pending.event_id)
            if (
                pending.control not in {"automatic", "passive"}
                or len(pending.successors) != 1
            ):
                return False
            successor = self.plan.events[pending.successors[0]]
            if successor.event_id == target_event_id:
                return True
            if successor.is_speech:
                return False
            pending = successor
        return False

    @staticmethod
    def _resting_state(event):
        return (
            StoryCursorState.MANUAL
            if event.control == "manual" or event.kind in {"choice", "wait"}
            else StoryCursorState.LOCKED
        )


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise StoryCursorError(f"{label} must be non-empty text")
    return value.strip()


def _optional_text(value):
    if value is None:
        return None
    return str(value).strip() or None
