from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from hashlib import sha256
from threading import TIMEOUT_MAX, Condition, Event, Lock, RLock, Timer
from time import monotonic
from typing import Literal, ParamSpec, Protocol, TypeAlias, TypeVar, cast
from uuid import uuid4

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.audio_output import PcmAdmission, PcmAdmissionRejected, pcm_admission_context
from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.dialog_capture import OCRUncertainError
from vntts.live_tracking import IncrementalDialogTracker, SpeechChunk
from vntts.live_tracking import TrackerProbe as TrackerProbe
from vntts.live_tracking import TrackerResolver as TrackerResolver
from vntts.numeric_values import optional_finite_number
from vntts.ocr import OCRPreviewCancelled
from vntts.reading_diagnostics import retain_visual_wait_frames
from vntts.support_timeline_acquisition import CONTINUATION_GATE_REASONS

Frame: TypeAlias = object
FrameFingerprint: TypeAlias = object
Observation: TypeAlias = tuple[str | None, str]
_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass(frozen=True)
class RecognizedDialog:
    character: str
    text: str
    line_id: str
    exact_match: bool = True
    allow_unheard_replacement: bool = True


@dataclass(frozen=True)
class _CanonicalRecognition:
    dialog: RecognizedDialog
    run_id: str
    epoch: int
    is_current: Callable[[int], bool]
    observations: int = 1


class _Executor(Protocol):
    def submit(
        self,
        function: Callable[_P, _R],
        /,
        *arguments: _P.args,
        **keywords: _P.kwargs,
    ) -> Future[_R]: ...


class _CapturePolicy(Protocol):
    fast_interval: float

    def observe(
        self, character: str | None, text: str | None, *, focused: bool = True
    ) -> float: ...


def _frame_capture_evidence(frame: Frame) -> tuple[str | None, float | None]:
    try:
        provenance = getattr(frame, "provenance", None)
        capture_id = getattr(provenance, "capture_id", None)
        captured_at = optional_finite_number(
            getattr(provenance, "completed_monotonic", None)
        )
        return capture_id if isinstance(capture_id, str) else None, captured_at
    except Exception:
        return None, None


class _TrackerAdmissionDiagnostics:
    def __init__(self, report: Callable[..., object]) -> None:
        self.report = report
        self.identity: tuple[str, int, str, str] | None = None
        self.started_at = self.last_at = 0.0
        self.observations = 0
        self.details: dict[str, object] = {}

    def observe(
        self,
        tracker: IncrementalDialogTracker,
        run_id: str,
        route_kind: str,
        frame: Frame,
        frame_version: int,
        reason: str | None = None,
    ) -> None:
        try:
            tracker_reason = getattr(tracker, "admission_reason", None)
            if not isinstance(tracker_reason, str):
                return
            details: dict[str, object] = {
                "capture_id": _frame_capture_evidence(frame)[0],
                "frame_version": frame_version,
                "committed_characters": len(tracker.committed_text),
                "latest_characters": len(tracker.latest_text),
                "stable_characters": len(tracker.stable_text),
                "transition_expected": tracker.transition_expected,
            }
            identity = run_id, tracker.generation, reason or tracker_reason, route_kind
        except Exception:
            return
        now = monotonic()
        changed = identity != self.identity
        if changed:
            self.finish()
            self.identity = identity
            self.started_at = now
        self.last_at = now
        self.observations += 1
        self.details = details
        if changed:
            self._emit("started")

    def _emit(self, phase: str) -> None:
        assert self.identity is not None
        run_id, generation, reason, route_kind = self.identity
        try:
            self.report(
                "tracker-admission",
                generation,
                self.last_at,
                run_id=run_id,
                reason=reason,
                route_kind=route_kind,
                phase=phase,
                observations=self.observations,
                started_monotonic=self.started_at,
                completed_monotonic=self.last_at,
                span_ms=(self.last_at - self.started_at) * 1000,
                **self.details,
            )
        except Exception:
            pass

    def finish(self) -> None:
        if self.identity is not None and self.observations > 1:
            # The summary includes the first observation; it is not another attempt.
            self._emit("summary")
        self.identity = None
        self.observations = 0


@dataclass(frozen=True)
class SilentDialogRoute:
    """A cursor-owned visible dialogue event that intentionally has no speech."""

    event_id: str


@dataclass(frozen=True)
class CanonicalDialogRoute:
    """A cursor-owned line whose text was not established by this frame's OCR."""

    character: str
    text: str


@dataclass(frozen=True)
class AutoAdvanceAttempt:
    """Typed callback result so a safe wait is not mistaken for a hard block."""

    dispatched: bool
    reason: str

    def __bool__(self) -> bool:
        return self.dispatched


@dataclass(frozen=True)
class LivePipelineMetrics:
    captured_frames: int = 0
    replaced_frames: int = 0
    recognized_frames: int = 0
    reused_frames: int = 0
    speech_queue_depth: int = 0
    max_speech_queue_depth: int = 0
    last_capture_at: float | None = None
    last_ocr_at: float | None = None
    last_ocr_capture_at: float | None = None
    last_ocr_capture_id: str | None = None
    last_ocr_frame_version: int | None = None
    last_route_capture_at: float | None = None
    last_route_capture_id: str | None = None
    last_route_frame_version: int | None = None
    last_route_ocr_at: float | None = None
    last_route_kind: str | None = None
    last_sentence_ready_at: float | None = None
    last_synthesis_at: float | None = None
    last_playback_at: float | None = None
    last_auto_advance_at: float | None = None
    last_auto_advance_dispatched_at: float | None = None
    last_text_visible_at: float | None = None
    last_ocr_stable_at: float | None = None
    last_speaker_resolved_at: float | None = None
    last_generation_started_at: float | None = None
    last_first_pcm_at: float | None = None
    last_first_pcm_generation: int | None = None
    last_canonical_full_text_at: float | None = None
    last_canonical_full_text_generation: int | None = None
    last_playback_started_at: float | None = None
    last_playback_completed_at: float | None = None


DialogRoute: TypeAlias = SilentDialogRoute | CanonicalDialogRoute | Observation
DialogObservationDecision: TypeAlias = DialogRoute | bool | None
StableFrameRoute: TypeAlias = DialogRoute | None | Literal[False]


def _canonical_frame_observation(route: StableFrameRoute) -> Observation | None:
    if isinstance(route, SilentDialogRoute):
        return None, ""
    if isinstance(route, CanonicalDialogRoute):
        return route.character, route.text
    if isinstance(route, tuple) and len(route) == 2:
        return route
    return None


def _finite_seconds(value: object, name: str) -> float:
    seconds = optional_finite_number(int(value) if isinstance(value, bool) else value)
    if seconds is None:
        raise ValueError(f"{name} must be finite")
    return seconds


def _positive_seconds(value: object, name: str) -> float:
    seconds = _finite_seconds(value, name)
    if seconds <= 0:
        raise ValueError(f"{name} must be positive")
    return seconds


def _nonnegative_seconds(value: object, name: str) -> float:
    seconds = _finite_seconds(value, name)
    if seconds < 0:
        raise ValueError(f"{name} must not be negative")
    return seconds


def _thread_seconds(seconds: float, name: str) -> float:
    if seconds > TIMEOUT_MAX:
        raise ValueError(f"{name} exceeds the thread timeout range")
    return seconds


def _positive_wait_seconds(value: object, name: str) -> float:
    return _thread_seconds(_positive_seconds(value, name), name)


def _wait_timeout_seconds(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("timeout_seconds must be finite") from error
    return _thread_seconds(
        max(0.0, _finite_seconds(timeout, "timeout_seconds")), "timeout_seconds"
    )


def _positive_count(value: object, name: str) -> int:
    if isinstance(value, int):
        count = int(value)
    elif isinstance(value, float) and value.is_integer():
        count = int(value)
    else:
        raise ValueError(f"{name} must be an integer")
    if count < 1:
        raise ValueError(f"{name} must be positive")
    return count


def _recheck_interval_milliseconds(seconds: float) -> int | None:
    milliseconds = optional_finite_number(seconds * 1000)
    return round(milliseconds) if milliseconds is not None else None


def _drain_reported_worker(
    future: Future[object], wait_for: Callable[[Future[object]], None]
) -> None:
    if future.cancelled():
        return
    try:
        wait_for(future)
    except FutureTimeoutError as error:
        if not future.done() or future.cancelled() or future.exception() is not error:
            raise
    except Exception:
        # Failure was reported by the worker; still drain its paired worker.
        pass


class AdaptiveSpeechBackpressure:
    """Temporarily serialize speech after an output underrun."""

    def __init__(
        self,
        *,
        normal_jobs: int = 2,
        cooldown_seconds: float = 10.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.normal_jobs = _positive_count(normal_jobs, "normal_jobs")
        self.cooldown_seconds = _positive_seconds(cooldown_seconds, "cooldown_seconds")
        self.clock = clock
        self.current_jobs = self.normal_jobs
        self.last_underflow_at: float | None = None

    def reset(self) -> int:
        self.current_jobs = self.normal_jobs
        self.last_underflow_at = None
        return self.current_jobs

    def observe_playback(self, *, underflowed: bool) -> tuple[int, bool]:
        previous_jobs = self.current_jobs
        now = self.clock()
        if underflowed:
            self.last_underflow_at = now
            self.current_jobs = 1
        elif (
            self.current_jobs < self.normal_jobs
            and self.last_underflow_at is not None
            and now - self.last_underflow_at >= self.cooldown_seconds
        ):
            self.current_jobs = self.normal_jobs
            self.last_underflow_at = None
        return self.current_jobs, self.current_jobs != previous_jobs


class AdaptiveCapturePolicy:
    def __init__(
        self,
        *,
        base_interval: float = 0.2,
        fast_interval: float | None = None,
        idle_interval: float | None = None,
        unfocused_interval: float | None = None,
        unchanged_frames: int = 3,
    ) -> None:
        self.base_interval = _positive_wait_seconds(base_interval, "base_interval")
        self.fast_interval = _positive_wait_seconds(
            max(0.05, self.base_interval / 2)
            if fast_interval is None
            else fast_interval,
            "fast_interval",
        )
        self.idle_interval = _positive_wait_seconds(
            min(1.5, self.base_interval * 3)
            if idle_interval is None
            else idle_interval,
            "idle_interval",
        )
        self.unfocused_interval = _positive_wait_seconds(
            min(0.5, self.base_interval * 2.5)
            if unfocused_interval is None
            else unfocused_interval,
            "unfocused_interval",
        )
        self.unchanged_frames = _positive_count(unchanged_frames, "unchanged_frames")
        self.last_observation: tuple[str, str] | None = None
        self.unchanged_count = 0
        self.was_focused = True

    def observe(
        self, character: str | None, text: str | None, *, focused: bool = True
    ) -> float:
        if not focused:
            self.was_focused = False
            self.unchanged_count = 0
            return self.unfocused_interval

        observation = (
            (character or "Narrator").strip() or "Narrator",
            " ".join((text or "").split()),
        )
        focus_returned = not self.was_focused
        self.was_focused = True
        if observation == self.last_observation:
            self.unchanged_count += 1
        else:
            self.last_observation = observation
            self.unchanged_count = 0

        if focus_returned or self.unchanged_count == 0:
            return self.fast_interval
        if self.unchanged_count >= self.unchanged_frames:
            return self.idle_interval
        return self.base_interval


class LiveDialogReader:
    def __init__(
        self,
        *,
        capture_executor: _Executor,
        ocr_executor: _Executor,
        speech_executor: _Executor,
        playback_executor: _Executor,
        capture_frame: Callable[[], Frame],
        recognize_frame: Callable[[Frame], Observation | RecognizedDialog],
        ocr_uncertain: Callable[[OCRUncertainError, Callable[[], bool]], object]
        | None = None,
        prepare_chunk: Callable[[SpeechChunk], object],
        play_prepared: Callable[[SpeechChunk, object], object],
        report_error: Callable[[Exception], object],
        accept_chunk: Callable[[SpeechChunk], SpeechChunk | None] | None = None,
        reject_chunk: Callable[[SpeechChunk], object] | None = None,
        frame_fingerprint: Callable[[Frame], FrameFingerprint] | None = None,
        frame_render_fingerprint: Callable[[Frame], FrameFingerprint] | None = None,
        frame_equivalent: Callable[[Frame, Frame], bool] | None = None,
        frame_render_equivalent: Callable[[Frame, Frame], bool] | None = None,
        frame_presence: Callable[[Frame], bool] | None = None,
        frame_completion: Callable[[Frame], bool] | None = None,
        frame_recheck_required: Callable[[], bool] | None = None,
        ocr_purpose: Callable[[], str | None] | None = None,
        frame_recheck_interval_seconds: float = 0.6,
        render_completion: Callable[[], object] | None = None,
        render_quiet_minimum_seconds: float = 0.8,
        stable_frame_route: Callable[
            [FrameFingerprint, bool, str | None, int], StableFrameRoute
        ]
        | None = None,
        stable_frame_owner: Callable[[], str | None] | None = None,
        frame_routed: Callable[[Frame, FrameFingerprint, str, str | None, str], None]
        | None = None,
        frame_observed: Callable[[Frame, FrameFingerprint, str], None] | None = None,
        line_id_resolver: Callable[[str | None, str], str | None] | None = None,
        stable_frame_minimum_seconds: float = 0.12,
        stable_frame_clock: Callable[[], float] = monotonic,
        interrupt_speech: Callable[[], object] | None = None,
        dialog_observed: Callable[[str | None, str], DialogObservationDecision]
        | None = None,
        interval_seconds: float = 0.2,
        tracker_factory: Callable[
            ..., IncrementalDialogTracker
        ] = IncrementalDialogTracker,
        tracker_options: dict[str, object] | None = None,
        focus_probe: Callable[[], bool] | None = None,
        capture_state_changed: Callable[[bool, float], None] | None = None,
        adaptive_policy_factory: Callable[..., _CapturePolicy] = AdaptiveCapturePolicy,
        adaptive_options: dict[str, object] | None = None,
        auto_advance: Callable[[], object] | None = None,
        require_visible_auto_advance: bool = False,
        auto_advance_delay_seconds: float = 0.35,
        auto_advance_confirmation_timeout_seconds: float = 2.0,
        auto_advance_terminal_timeout_seconds: float = 10.0,
        auto_advance_state_changed: Callable[[str, int, int], None] | None = None,
        pipeline_event_handler: Callable[..., object] | None = None,
        max_speech_jobs: int = 2,
        first_pcm_on_prepare: bool = True,
        shutdown_requested: Event | None = None,
    ) -> None:
        self.capture_executor = capture_executor
        self.speech_executor = speech_executor
        self.ocr_executor = ocr_executor
        self.playback_executor = playback_executor
        self.capture_frame = capture_frame
        self._initialize_recognition(recognize_frame, ocr_uncertain)
        self.frame_fingerprint = frame_fingerprint or (lambda _frame: None)
        # None means the identity fingerprint is also sufficient for render
        # activity. Keep that as a sentinel so capture does not compute an
        # expensive fingerprint twice for replay readers.
        self.frame_render_fingerprint = frame_render_fingerprint
        self.frame_equivalent = frame_equivalent or (lambda _before, _after: False)
        self.frame_render_equivalent = frame_render_equivalent
        self.frame_presence = frame_presence or (lambda _frame: True)
        self.frame_completion = frame_completion or (lambda _frame: False)
        self.frame_recheck_required = frame_recheck_required or (lambda: False)
        self.ocr_purpose = ocr_purpose or (lambda: "legacy")
        self.frame_recheck_interval_seconds = _positive_seconds(
            frame_recheck_interval_seconds, "frame_recheck_interval_seconds"
        )
        self.render_completion = render_completion or (lambda: False)
        self.render_quiet_minimum_seconds = _positive_seconds(
            render_quiet_minimum_seconds, "render_quiet_minimum_seconds"
        )
        self.stable_frame_route = stable_frame_route
        self.stable_frame_owner = stable_frame_owner or (lambda: None)
        self.frame_routed = frame_routed or (
            lambda _frame, _fingerprint, _route_kind, _character, _text: None
        )
        self.frame_observed = frame_observed or (
            lambda _frame, _fingerprint, _observation_kind: None
        )
        self.line_id_resolver = line_id_resolver or (lambda _character, _text: None)
        self.stable_frame_minimum_seconds = _nonnegative_seconds(
            stable_frame_minimum_seconds, "stable_frame_minimum_seconds"
        )
        self.stable_frame_clock = stable_frame_clock
        self.prepare_chunk = prepare_chunk
        self.play_prepared = play_prepared
        self.accept_chunk = accept_chunk or (lambda chunk: chunk)
        self.reject_chunk = reject_chunk or (lambda _chunk: None)
        self.report_error = report_error
        self.interrupt_speech = interrupt_speech or (lambda: None)
        self.dialog_observed = dialog_observed or (lambda _character, _text: None)
        self.interval_seconds = interval_seconds
        self.tracker_factory = tracker_factory
        self.tracker_options = tracker_options or {}
        self.focus_probe = focus_probe or (lambda: True)
        self.capture_state_changed = capture_state_changed or (
            lambda _focused, _interval: None
        )
        self.adaptive_policy_factory = adaptive_policy_factory
        self.adaptive_options = adaptive_options or {}
        self.auto_advance = auto_advance
        self.require_visible_auto_advance = bool(require_visible_auto_advance)
        self.auto_advance_delay_seconds = auto_advance_delay_seconds
        self.auto_advance_confirmation_timeout_seconds = _positive_wait_seconds(
            auto_advance_confirmation_timeout_seconds,
            "auto_advance_confirmation_timeout_seconds",
        )
        auto_advance_terminal_timeout_seconds = _finite_seconds(
            auto_advance_terminal_timeout_seconds,
            "auto_advance_terminal_timeout_seconds",
        )
        if (
            auto_advance_terminal_timeout_seconds
            <= self.auto_advance_confirmation_timeout_seconds
        ):
            raise ValueError(
                "auto_advance_terminal_timeout_seconds must be greater than "
                "auto_advance_confirmation_timeout_seconds"
            )
        _thread_seconds(
            auto_advance_terminal_timeout_seconds
            - self.auto_advance_confirmation_timeout_seconds,
            "auto_advance_terminal_timeout_seconds",
        )
        self.auto_advance_terminal_timeout_seconds = (
            auto_advance_terminal_timeout_seconds
        )
        self.auto_advance_state_changed = auto_advance_state_changed or (
            lambda _state, _generation, _attempt: None
        )
        self.pipeline_event_handler = pipeline_event_handler or (
            lambda _stage, _generation, _occurred_at, **_details: None
        )
        self.max_speech_jobs = max_speech_jobs
        self.first_pcm_on_prepare = bool(first_pcm_on_prepare)
        self._initialize_worker_state(shutdown_requested)
        self.run_id = uuid4().hex
        self.run_started_at = monotonic()
        self.active_generation = 0
        self._explicit_generation_offset = 0
        self.suppressed_generation: int | None = None
        self._initialize_speech_queue()
        self.paused = False
        self.emergency_stopped = False
        self.last_observation: Observation | None = None
        self.last_accepted_observation: DialogRoute | None = None
        self.deferred_observation: Observation | None = None
        self.dialog_ready_generation: int | None = None
        self.last_auto_advance_dispatched_generation: int | None = None
        self.pending_auto_advance_generation: int | None = None
        self.failed_auto_advance_generation: int | None = None
        self.auto_advance_attempts = 0
        self.auto_advance_blocked_generation: int | None = None
        self.auto_advance_block_reason: str | None = None
        self.auto_advance_focus_wait_generation: int | None = None
        self.auto_advance_visual_wait_generation: int | None = None
        self.auto_advance_timer: Timer | None = None
        self._auto_advance_timer_serial = 0
        self._auto_advance_gate_reports: dict[str, tuple[object, int, float]] = {}
        self.focus_probe_failed = False
        self.ocr_worker_stage = "not-started"
        self.ocr_worker_progress_at = monotonic()
        self._reset_pending_transition(None)
        self._reset_frame_observations()
        self.frame_route_epoch = 0
        self.candidate_frame_fingerprint = object()
        self.candidate_frame_count = 0
        self.candidate_frame_owner: object | None = None
        self.candidate_frame_started_at: float | None = None
        self.frame_version = 0
        self.processed_frame_version = 0
        self.next_capture_interval = self.interval_seconds
        self.pipeline_metrics = LivePipelineMetrics()

    def _initialize_speech_queue(self) -> None:
        self.speech_futures: dict[
            Future[object | None] | Future[None], SpeechChunk
        ] = {}
        self.paused_chunks: list[SpeechChunk] = []
        self.deferred_chunks: deque[SpeechChunk] = deque()
        self.accepted_chunks: dict[str, SpeechChunk] = {}
        self.queue_epoch = 0
        self.pending_admission_ids: set[str] = set()
        self.scheduled_playback_ids: set[str] = set()
        self.completed_playback_ids: set[str] = set()
        self.prepared_results: dict[Future, object] = {}
        self.playback_future: Future | None = None
        self.pause_retry_chunks: dict[str, SpeechChunk] = {}
        self.current_chunk: SpeechChunk | None = None
        self.unheard_candidate: SpeechChunk | None = None
        self.pcm_admitted_generations: set[int] = set()
        self.current_chunk_pipeline_origins: dict[str, float | None] | None = None
        self.last_spoken_chunk: SpeechChunk | None = None
        self.cancelled_chunk_ids: set[str] = set()
        self.pause_interrupted_chunk_ids: set[str] = set()
        self.prepared_chunk_ids: set[str] = set()
        self.sealed_generations: set[int] = set()

    def _initialize_recognition(
        self,
        recognize_frame: Callable[[Frame], Observation | RecognizedDialog],
        ocr_uncertain: Callable[[OCRUncertainError, Callable[[], bool]], object] | None,
    ) -> None:
        self.recognize_frame = recognize_frame
        self.ocr_uncertain = ocr_uncertain or (lambda _error, _is_current: None)

    def _reset_pending_transition(self, frame: Frame | None) -> None:
        self.pending_transition_frame = frame
        self.pending_transition_progress_at: float | None = None
        self.pending_transition_seen = False
        self.pending_transition_typing = False
        self.pending_transition_text = ""

    def _reset_render_observation(self) -> None:
        self.render_anchor_frame: Frame | None = None
        self.render_anchor_fingerprint: FrameFingerprint | None = None
        self.latest_render_fingerprint: FrameFingerprint | None = None
        self.latest_render_owner: object | None = None
        self.latest_render_changed_at: float | None = None

    def _reset_frame_observations(self) -> None:
        self.observation_uncertain = False
        self.observation_frame: Frame | None = None
        self.latest_frame_captured_at: float | None = None
        self.latest_frame: Frame | None = None
        self.latest_frame_fingerprint: FrameFingerprint | None = None
        self.latest_frame_visible = False
        self.latest_frame_complete = False
        self._reset_render_observation()
        self.routed_frame: Frame | None = None
        self.candidate_frame: Frame | None = None
        self.routed_frame_fingerprint: FrameFingerprint | None = None

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @interval_seconds.setter
    def interval_seconds(self, value: float) -> None:
        self._interval_seconds = _positive_wait_seconds(value, "interval_seconds")

    @property
    def auto_advance_delay_seconds(self) -> float:
        return self._auto_advance_delay_seconds

    @auto_advance_delay_seconds.setter
    def auto_advance_delay_seconds(self, value: float) -> None:
        self._auto_advance_delay_seconds = _thread_seconds(
            _nonnegative_seconds(value, "auto_advance_delay_seconds"),
            "auto_advance_delay_seconds",
        )

    @property
    def max_speech_jobs(self) -> int:
        return self._max_speech_jobs

    @max_speech_jobs.setter
    def max_speech_jobs(self, value: int) -> None:
        self._max_speech_jobs = _positive_count(value, "max_speech_jobs")

    def _initialize_worker_state(self, shutdown_requested: Event | None) -> None:
        self.state_lock = RLock()
        self.start_lock = Lock()
        self.pause_condition = Condition(self.state_lock)
        self.stop_event = Event()
        self.shutdown_requested = shutdown_requested or Event()
        self.shutdown_started = False
        self.capture_future: Future[object] | None = None
        self.ocr_future: Future[object] | None = None

    @property
    def is_shutting_down(self) -> bool:
        return self.shutdown_started or self.shutdown_requested.is_set()

    @property
    def is_running(self) -> bool:
        with self.state_lock:
            return self.capture_future is not None and not self.capture_future.done()

    def runtime_control_snapshot(self) -> dict[str, bool]:
        """Return the lock-consistent playback facts used by all UI transports."""
        with self.state_lock:
            active_futures = any(not future.done() for future in self.speech_futures)
            queued = bool(
                active_futures or self.paused_chunks or bool(self.deferred_chunks)
            )
            replayable = bool(
                self.last_spoken_chunk is not None
                and self.suppressed_generation != self.active_generation
            )
            return {
                "paused": self.paused,
                "speaking": self.current_chunk is not None,
                "queued": queued,
                "replayable": replayable,
            }

    def start(self) -> bool:
        if not self.start_lock.acquire(blocking=False):
            return False
        try:
            return self._start_capture()
        finally:
            self.start_lock.release()

    def _start_capture(self) -> bool:
        with self.state_lock:
            if self.is_shutting_down:
                return False
            if self.capture_future is not None and not self.capture_future.done():
                return False
            restarting = self.capture_future is not None or self.ocr_future is not None
        self.clear_queue(reason="start-capture")
        if restarting:
            try:
                self.wait(timeout_seconds=5.0)
            except FutureTimeoutError as error:
                self.report_error(error)
                return False
        with self.state_lock:
            if self.is_shutting_down:
                return False
            self.emergency_stopped = False
            self.stop_event = Event()
            self.run_id = uuid4().hex
            self.run_started_at = monotonic()
            self.active_generation = 0
            self._explicit_generation_offset = 0
            self.suppressed_generation = None
            self.last_observation = None
            self.last_accepted_observation = None
            self.deferred_observation = None
            self._reset_speech_history()
            self.dialog_ready_generation = None
            self.last_auto_advance_dispatched_generation = None
            self.pending_auto_advance_generation = None
            self.failed_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_blocked_generation = None
            self.auto_advance_block_reason = None
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
            self.focus_probe_failed = False
            self._cancel_auto_advance_locked()
            self._reset_pending_transition(None)
            self._reset_frame_observations()
            self.frame_route_epoch += 1
            self._reset_stable_frame_candidate_locked()
            self.frame_version = 0
            self.processed_frame_version = 0
            self.next_capture_interval = self.interval_seconds
            self.pipeline_metrics = LivePipelineMetrics()
            self._ocr_progress("queued")
            self._submit_capture_workers()
        return True

    def _reset_speech_history(self) -> None:
        self.prepared_chunk_ids.clear()
        self.cancelled_chunk_ids.clear()
        self.pause_interrupted_chunk_ids.clear()
        self.scheduled_playback_ids.clear()
        self.completed_playback_ids.clear()
        self.prepared_results.clear()
        self.playback_future = None
        self.pause_retry_chunks.clear()
        self.sealed_generations.clear()
        self.pcm_admitted_generations.clear()

    def _submit_capture_workers(self) -> None:
        """Signal the paired workers on partial submission; retain them for wait."""
        try:
            self.ocr_future = self.ocr_executor.submit(
                self._run_capture_worker,
                self._run_ocr,
                self.stop_event,
            )
            self.capture_future = self.capture_executor.submit(
                self._run_capture_worker,
                self._run_capture,
                self.stop_event,
            )
        except BaseException:
            self.stop_event.set()
            self.pause_condition.notify_all()
            raise

    def _run_capture_worker(
        self, worker: Callable[[Event], None], stop_event: Event
    ) -> None:
        try:
            worker(stop_event)
        except BaseException as error:
            # Setup failures happen outside the workers' per-frame error handlers.
            with self.pause_condition:
                stop_event.set()
                self.pause_condition.notify_all()
            if isinstance(error, Exception):
                attempt_cleanup(
                    partial(self.report_error, error),
                    description="Reading worker failure reporting",
                    primary_error=error,
                )
            raise

    def stop(self) -> bool:
        with self.state_lock:
            if not any(
                future is not None and not future.done()
                for future in (self.capture_future, self.ocr_future)
            ):
                return False
            self.stop_event.set()
            self.frame_route_epoch += 1
            self.paused = False
            self.pending_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
            self.pause_condition.notify_all()
            self._cancel_auto_advance_locked()
        self._report_pipeline_event(
            "reading-control", self.active_generation, action="stop-capture"
        )
        return True

    def set_auto_advance(self, callback: Callable[[], object] | None) -> bool:
        with self.state_lock:
            self.auto_advance = callback
            if callback is None:
                self._cancel_auto_advance_locked()
                self.pending_auto_advance_generation = None
                self.failed_auto_advance_generation = None
                self.last_auto_advance_dispatched_generation = None
                self.auto_advance_attempts = 0
                self.auto_advance_focus_wait_generation = None
                self.auto_advance_visual_wait_generation = None
                return False
        self._maybe_auto_advance()
        return True

    def block_auto_advance_for_generation(
        self, generation: int, reason: object
    ) -> bool:
        with self.state_lock:
            if generation != self.active_generation:
                return False
            self._cancel_auto_advance_locked()
            self.auto_advance_blocked_generation = generation
            self.auto_advance_block_reason = str(reason).strip() or None
        return True

    def confirm_pending_auto_advance(self) -> bool:
        """Confirm one dispatched key from cursor-owned visual evidence."""
        with self.state_lock:
            generation = self.pending_auto_advance_generation
            attempt = self.auto_advance_attempts
            if generation is None or not attempt:
                return False
            self._cancel_auto_advance_locked()
            self.pending_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
            self.pipeline_metrics = replace(
                self.pipeline_metrics,
                last_auto_advance_at=monotonic(),
            )
        self._report_pipeline_event(
            "confirmed-next-dialogue",
            generation,
            monotonic(),
            attempt=attempt,
        )
        self._report_auto_advance_state("confirmed", generation, attempt)
        return True

    def toggle(self) -> bool:
        if self.is_running:
            self.stop()
            return False
        return self.start()

    def toggle_pause(self) -> bool:
        current_chunk = None
        with self.pause_condition:
            if self.paused:
                self.paused = False
                for chunk in self.paused_chunks:
                    self._defer_chunk_locked(chunk)
                self.paused_chunks = []
                self.pause_condition.notify_all()
            else:
                self.paused = True
                self.frame_route_epoch += 1
                self._cancel_auto_advance_locked()
                current_chunk = self.current_chunk
                if current_chunk is not None:
                    self.pause_interrupted_chunk_ids.add(current_chunk.playback_id)
        if current_chunk is not None:
            interrupted = self._interrupt_speech()
            with self.pause_condition:
                if not interrupted:
                    self.pause_interrupted_chunk_ids.discard(current_chunk.playback_id)
                elif (
                    self.accepted_chunks.get(current_chunk.playback_id) is current_chunk
                    and current_chunk.run_id == self.run_id
                    and current_chunk.playback_id not in self.cancelled_chunk_ids
                ):
                    self.pause_retry_chunks[current_chunk.playback_id] = current_chunk
                    if not any(
                        pending.playback_id == current_chunk.playback_id
                        for pending in self.speech_futures.values()
                    ):
                        self._queue_pause_retry_locked(current_chunk)
        self._schedule_prepared_if_possible()
        self._schedule_deferred_if_possible()
        if not self.paused:
            self._resume_auto_advance_confirmation()
            self._maybe_auto_advance()
        self._report_pipeline_event(
            "reading-control",
            self.active_generation,
            action="pause" if self.paused else "resume",
        )
        return self.paused

    def _queue_pause_retry_locked(self, chunk: SpeechChunk) -> None:
        self.pause_retry_chunks.pop(chunk.playback_id, None)
        self.pause_interrupted_chunk_ids.discard(chunk.playback_id)
        self.scheduled_playback_ids.discard(chunk.playback_id)
        self.completed_playback_ids.discard(chunk.playback_id)
        self.prepared_chunk_ids.discard(chunk.chunk_id)
        self._defer_chunk_locked(chunk)

    def enqueue(
        self,
        character: str,
        text: str,
        *,
        line_id: str | None = None,
        auto_advance_block_reason: str | None = None,
    ) -> bool:
        with self.state_lock:
            if self.is_shutting_down:
                return False
            generation = self.active_generation + 1
            # Explicit lines advance the shared queue without advancing the OCR tracker.
            self._explicit_generation_offset += 1
            self._set_generation(generation)
            if auto_advance_block_reason is not None:
                self.auto_advance_blocked_generation = generation
                self.auto_advance_block_reason = auto_advance_block_reason
        self._schedule([SpeechChunk(generation, character, text, line_id=line_id)])
        return True

    def bind_current_frame_route(
        self, *, expected_render_owner: str | None = None
    ) -> bool:
        """Bind recovery, optionally requiring current owner-bound render quiet."""
        if expected_render_owner is not None:
            if not self._is_focused():
                return False
            owner = self.stable_frame_owner()
            now = self.stable_frame_clock()
        with self.state_lock:
            fingerprint = self.latest_frame_fingerprint
            if fingerprint is None:
                return False
            if expected_render_owner is not None and (
                self.stop_event.is_set()
                or self.paused
                or self.emergency_stopped
                or self.is_shutting_down
                or self.observation_uncertain
                or self._render_quiet_ms_locked(now, owner, expected_render_owner)
                is None
            ):
                return False
            self._accept_routed_frame_locked(fingerprint)
        return True

    def frame_route_epoch_is_current(self, epoch: int) -> bool:
        with self.state_lock:
            return epoch == self.frame_route_epoch

    def skip_current(self) -> bool:
        with self.pause_condition:
            current = self.current_chunk
            has_current_speech = current is not None
            if current is not None:
                self.cancelled_chunk_ids.add(current.playback_id)
                self.pause_condition.notify_all()
        if has_current_speech:
            self._interrupt_speech()
        return has_current_speech

    def repeat_last(self) -> bool:
        with self.state_lock:
            chunk = self.last_spoken_chunk
            generation = self.active_generation
            suppressed = self.suppressed_generation == generation
        if chunk is None or suppressed:
            return False
        self._schedule(
            [
                SpeechChunk(
                    generation,
                    chunk.character,
                    chunk.text,
                    line_id=chunk.line_id,
                    explicit_replay=True,
                )
            ]
        )
        return True

    def clear_queue(self, *, reason: str = "clear-queue") -> bool:
        with ExitStack() as cleanup:
            with self.pause_condition:
                return self._clear_queue_locked(cleanup, reason=reason)

    def _clear_queue_locked(
        self, cleanup: ExitStack, *, reason: str = "clear-queue"
    ) -> bool:
        chunk = self.current_chunk or next(iter(self.speech_futures.values()), None)
        context = {} if chunk is None else self._chunk_context(chunk)
        context.pop("generation", None)
        cancelled_at = monotonic()
        self.frame_route_epoch += 1
        self.queue_epoch += 1
        self.suppressed_generation = self.active_generation
        futures = tuple(self.speech_futures)
        for future in self.prepared_results:
            self.speech_futures.pop(future, None)
        self.prepared_results.clear()
        self.pause_retry_chunks.clear()
        had_paused_chunks = bool(self.paused_chunks)
        had_deferred_chunk = bool(self.deferred_chunks)
        self.paused_chunks = []
        self.deferred_chunks.clear()
        self.cancelled_chunk_ids.update(
            chunk.playback_id for chunk in self.accepted_chunks.values()
        )
        self.accepted_chunks.clear()
        current = self.current_chunk
        has_current_speech = current is not None
        if current is not None:
            self.cancelled_chunk_ids.add(current.playback_id)
        self.pending_auto_advance_generation = None
        self.auto_advance_attempts = 0
        self.auto_advance_blocked_generation = None
        self.auto_advance_block_reason = None
        self.auto_advance_focus_wait_generation = None
        self.auto_advance_visual_wait_generation = None
        self.pause_condition.notify_all()
        # Cancellation cannot stop running synthesis. Interrupt it after
        # attempting every queued future, outside the reader lock.
        if has_current_speech or futures:
            cleanup.enter_context(
                cleanup_on_exit(
                    self._interrupt_speech, description="Speech interruption"
                )
            )
        for future in reversed(futures):
            cleanup.enter_context(
                cleanup_on_exit(future.cancel, description="Queued speech cancellation")
            )
        self._cancel_auto_advance_locked()
        # Capture the cause before cleanup can finish or replace current playback.
        cleanup.callback(
            self._report_pipeline_event,
            "queue-cancelled",
            self.active_generation,
            cancelled_at,
            reason=reason,
            speech_future_count=len(futures),
            current_chunk_present=has_current_speech,
            suppressed_generation=self.suppressed_generation,
            **context,
        )
        return (
            has_current_speech
            or bool(futures)
            or had_paused_chunks
            or had_deferred_chunk
        )

    def invalidate_observation(self) -> None:
        """Withhold visual continuation until fresh OCR rebinds the current owner."""
        with self.state_lock:
            self.observation_uncertain = True
            self.frame_route_epoch += 1
            self.last_observation = None
            self.last_accepted_observation = None
            self._reset_stable_frame_candidate_locked()
            self._cancel_auto_advance_locked()

    def shutdown(self) -> None:
        """Seal this reader permanently before attempting fallible cancellation."""
        with cleanup_on_exit(self.emergency_stop, description="Reader emergency stop"):
            with self.pause_condition:
                self.shutdown_started = True
                self.stop_event.set()

    def emergency_stop(self) -> bool:
        with cleanup_on_exit(self.release_waiters, description="Reader waiter release"):
            with self.pause_condition:
                was_running = (
                    self.capture_future is not None and not self.capture_future.done()
                )
                self.emergency_stopped = True
                self.stop_event.set()
                self.pause_condition.notify_all()
            cleared = self.clear_queue(reason="emergency-stop")
        self._report_pipeline_event(
            "reading-control", self.active_generation, action="emergency-stop"
        )
        return was_running or cleared

    def resume_after_emergency(self) -> bool:
        with self.state_lock:
            if self.is_shutting_down:
                return False
            was_stopped = self.emergency_stopped
            self.emergency_stopped = False
        return was_stopped

    def release_waiters(self) -> None:
        with self.pause_condition:
            self.paused = False
            self.pause_condition.notify_all()

    def was_playback_paused(self, chunk: SpeechChunk) -> bool:
        with self.state_lock:
            return (
                chunk.playback_id in self.pause_interrupted_chunk_ids
                and chunk.playback_id not in self.cancelled_chunk_ids
                and self._owns_chunk(chunk)
                and self.suppressed_generation != chunk.generation
                and not self.emergency_stopped
                and not self.is_shutting_down
            )

    def allow_unheard_replacement(self, chunk: SpeechChunk) -> bool:
        """The route owner has qualified this chunk as raw live/no-match."""
        with self.state_lock:
            if (
                chunk.generation != self.active_generation
                or chunk.run_id != self.run_id
                or chunk.line_id is not None
                or chunk.explicit_replay
                or chunk.generation in self.pcm_admitted_generations
                or chunk.playback_id in self.cancelled_chunk_ids
                or self.suppressed_generation == chunk.generation
                or self.stop_event.is_set()
                or self.is_shutting_down
            ):
                return False
            self.unheard_candidate = chunk
            return True

    def claim_first_pcm(self, chunk: SpeechChunk) -> bool:
        with self.pause_condition:
            if (
                not self.wait_until_playable(chunk)
                or self.current_chunk is not chunk
                or chunk.run_id not in (None, self.run_id)
            ):
                return False
            self.pcm_admitted_generations.add(chunk.generation)
            return True

    def _replace_unheard_candidate(
        self,
        tracker: IncrementalDialogTracker,
        recognition: _CanonicalRecognition | None,
        character: str | None,
        text: str,
    ) -> list[SpeechChunk] | None:
        if (
            recognition is None
            or not recognition.dialog.allow_unheard_replacement
            or (
                not recognition.dialog.exact_match
                and recognition.observations < tracker.stability_frames
            )
            or (character, text)
            != (
                recognition.dialog.character,
                recognition.dialog.text,
            )
        ):
            return None
        with self.state_lock:
            version = self.frame_version
            epoch = self.frame_route_epoch
        if (
            recognition.run_id != self.run_id
            or recognition.epoch != epoch
            or not recognition.is_current(epoch)
        ):
            return None
        with self.pause_condition:
            candidate = self.unheard_candidate
            if (
                candidate is None
                or self.paused
                or self.emergency_stopped
                or self.is_shutting_down
                or self.stop_event.is_set()
                or self.frame_version != version
                or self.frame_route_epoch != epoch
                or self.run_id != recognition.run_id
                or candidate.run_id != self.run_id
                or candidate.generation != self.active_generation
                or self.active_generation
                != tracker.generation + self._explicit_generation_offset
                or self.suppressed_generation == self.active_generation
                or self.active_generation in self.sealed_generations
                or self.active_generation in self.pcm_admitted_generations
                or candidate.playback_id in self.cancelled_chunk_ids
                or tracker.canonical_line_id is not None
                or tracker.committed_text != candidate.text
            ):
                return None
            self.cancelled_chunk_ids.add(candidate.playback_id)
            self.unheard_candidate = None
            superseded = [
                future
                for future, chunk in self.speech_futures.items()
                if chunk is candidate
            ]
            chunks = tracker.observe_canonical(
                recognition.dialog.character,
                recognition.dialog.text,
                recognition.dialog.line_id,
            )
        for future in superseded:
            future.cancel()
        return chunks

    def wait_until_playable(self, chunk: SpeechChunk) -> bool:
        with self.pause_condition:
            while (
                self.paused
                and not self.emergency_stopped
                and not self.is_shutting_down
                and self._owns_chunk(chunk)
                and self.suppressed_generation != chunk.generation
                and chunk.playback_id not in self.cancelled_chunk_ids
            ):
                self.pause_condition.wait()
            return (
                not self.emergency_stopped
                and not self.is_shutting_down
                and self._owns_chunk(chunk)
                and (
                    chunk.generation not in self.sealed_generations
                    or self.current_chunk is chunk
                    or chunk.explicit_replay
                )
                and self.suppressed_generation != chunk.generation
                and chunk.playback_id not in self.cancelled_chunk_ids
            )

    def _owns_chunk(self, chunk: SpeechChunk) -> bool:
        return (
            chunk.run_id in (None, self.run_id)
            and chunk.playback_id not in self.completed_playback_ids
            and (
                self.accepted_chunks.get(chunk.playback_id) is chunk
                or chunk.generation == self.active_generation
            )
        )

    def seal_generation(self, generation: int) -> bool:
        """Suppress OCR suffix chunks after an exact full-line route completed."""
        stale_futures = []
        with self.pause_condition:
            if generation != self.active_generation and not any(
                chunk.generation == generation
                for chunk in self.accepted_chunks.values()
            ):
                return False
            self.sealed_generations.add(generation)
            stale_ids = {
                chunk.playback_id
                for chunk in self.accepted_chunks.values()
                if chunk is not self.current_chunk
                and chunk.generation == generation
                and not chunk.explicit_replay
            }
            self.completed_playback_ids.update(stale_ids)
            self.cancelled_chunk_ids.update(stale_ids)
            self.pause_interrupted_chunk_ids.difference_update(stale_ids)
            for playback_id in stale_ids:
                self.pause_retry_chunks.pop(playback_id, None)
            stale_futures = [
                future
                for future, chunk in self.speech_futures.items()
                if chunk.playback_id in stale_ids
            ]
            self.paused_chunks = [
                chunk
                for chunk in self.paused_chunks
                if chunk.playback_id not in stale_ids
            ]
            self.deferred_chunks = deque(
                chunk
                for chunk in self.deferred_chunks
                if chunk.playback_id not in stale_ids
            )
            self.pause_condition.notify_all()
        for future in stale_futures:
            future.cancel()
        self._schedule_prepared_if_possible()
        self._schedule_deferred_if_possible()
        return True

    def wait(
        self, *, timeout_seconds: float | None = None, include_speech: bool = True
    ) -> None:
        deadline = (
            None
            if timeout_seconds is None
            else monotonic() + _wait_timeout_seconds(timeout_seconds)
        )

        def wait_for(
            future: Future[object] | Future[object | None] | Future[None],
        ) -> None:
            if deadline is None:
                future.result()
                return
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise FutureTimeoutError("Live reader did not quiesce before timeout")
            future.result(timeout=min(remaining, TIMEOUT_MAX))

        with self.state_lock:
            capture_future = self.capture_future
            ocr_future = self.ocr_future
        for worker_future in (capture_future, ocr_future):
            if worker_future is not None:
                _drain_reported_worker(worker_future, wait_for)
        if not include_speech:
            return
        while True:
            with self.state_lock:
                speech_futures = tuple(self.speech_futures)
            if not speech_futures:
                return
            for future in speech_futures:
                wait_for(future)

    def get_pipeline_metrics(self) -> LivePipelineMetrics:
        with self.state_lock:
            return self.pipeline_metrics

    def _observe_transition_frame(
        self,
        frame: Frame,
        fingerprint: FrameFingerprint,
        visible: bool,
        changed: bool,
        previous_frame: Frame | None,
        owner: object | None,
    ) -> None:
        with self.state_lock:
            generation = self.active_generation
            if (
                self.pending_auto_advance_generation != generation
                or not visible
                or fingerprint == self.routed_frame_fingerprint
            ):
                return
            anchor = self.pending_transition_frame
            route_epoch = self.frame_route_epoch
            frame_version = self.frame_version
        if anchor is not None and self.frame_equivalent(anchor, frame):
            return
        progressed = changed and (
            previous_frame is None or not self.frame_equivalent(previous_frame, frame)
        )
        focused = self._is_focused()
        current_owner = self.stable_frame_owner()
        with self.state_lock:
            if (
                self.active_generation != generation
                or self.pending_auto_advance_generation != generation
                or route_epoch != self.frame_route_epoch
                or frame_version != self.frame_version
                or self.latest_frame is not frame
                or self.pending_transition_frame is not anchor
                or owner != current_owner
                or not focused
                or self.stop_event.is_set()
                or self.paused
                or self.emergency_stopped
                or self.is_shutting_down
            ):
                return
            self.pending_transition_seen = True
            if progressed:
                self.pending_transition_progress_at = monotonic()

    def _run_capture(self, stop_event: Event) -> None:
        policy = self.adaptive_policy_factory(
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        while not stop_event.is_set():
            focused = self._is_focused()
            if not focused:
                with self.state_lock:
                    self._reset_render_observation()
                interval = policy.observe(None, None, focused=False)
                self._notify_capture_state(False, interval)
                stop_event.wait(interval)
                continue
            try:
                frame = self.capture_frame()
                fingerprint = self.frame_fingerprint(frame)
                render_fingerprint = (
                    fingerprint
                    if self.frame_render_fingerprint is None
                    else self.frame_render_fingerprint(frame)
                )
                visible = bool(self.frame_presence(frame))
                complete = bool(self.frame_completion(frame))
                render_owner = self.stable_frame_owner()
                recheck_required = bool(self.frame_recheck_required())
                captured_at = self.stable_frame_clock()
                with self.state_lock:
                    render_anchor = self.render_anchor_frame
                    anchor_fingerprint = self.render_anchor_fingerprint
                    anchor_owner = self.latest_render_owner
                same_render = bool(
                    visible
                    and render_anchor is not None
                    and render_owner == anchor_owner
                    and (
                        self.frame_render_equivalent(render_anchor, frame)
                        if self.frame_render_equivalent is not None
                        else render_fingerprint == anchor_fingerprint
                    )
                )
                with self.pause_condition:
                    fingerprint_changed = fingerprint != self.latest_frame_fingerprint
                    render_changed = (
                        render_fingerprint != self.latest_render_fingerprint
                    )
                    replaced = self.frame_version > self.processed_frame_version
                    previous_frame = self.latest_frame
                    self.latest_frame = frame
                    self.latest_frame_captured_at = monotonic()
                    self.latest_frame_fingerprint = fingerprint
                    self.latest_frame_visible = visible
                    self.latest_frame_complete = complete
                    if not visible:
                        self._reset_render_observation()
                    else:
                        if (
                            not same_render
                            or render_anchor is not self.render_anchor_frame
                            or render_owner != self.latest_render_owner
                        ):
                            # Compare every frame to this fixed anchor, never to
                            # successive tolerated edges that could slowly drift.
                            self.render_anchor_frame = frame
                            self.render_anchor_fingerprint = render_fingerprint
                            self.latest_render_changed_at = captured_at
                        self.latest_render_fingerprint = render_fingerprint
                        self.latest_render_owner = render_owner
                    self.frame_version += 1
                    metrics = self.pipeline_metrics
                    self.pipeline_metrics = replace(
                        metrics,
                        captured_frames=metrics.captured_frames + 1,
                        replaced_frames=metrics.replaced_frames + int(replaced),
                        last_capture_at=monotonic(),
                    )
                    interval = self.next_capture_interval
                    if (
                        fingerprint_changed
                        or recheck_required
                        or (
                            visible
                            and render_changed
                            and self.tracker_options.get("complete_dialogue_only")
                        )
                    ):
                        # The OCR worker updates the adaptive interval after it
                        # consumes this frame. Do not sleep once more on the
                        # previous static-dialogue interval before giving the
                        # stability gate its confirming frame. The same fast
                        # cadence must stay active while early canonical audio
                        # is waiting for the typewriter render to finish;
                        # otherwise the render-quiet fallback can expire before
                        # capture ever sees the next visible prefix.
                        interval = min(interval, policy.fast_interval)
                self._observe_transition_frame(
                    frame,
                    fingerprint,
                    visible,
                    fingerprint_changed or render_changed,
                    previous_frame,
                    render_owner,
                )
                with self.pause_condition:
                    self.pause_condition.notify_all()
            except Exception as error:
                self.report_error(error)
                interval = self.interval_seconds
            self._notify_capture_state(True, interval)
            stop_event.wait(interval)

    def _notify_capture_state(self, focused: bool, interval: float) -> None:
        if not focused:
            with self.state_lock:
                self.frame_route_epoch += 1
        try:
            self.capture_state_changed(focused, interval)
        except Exception as error:
            self.report_error(error)

    def _record_ocr_completion(
        self,
        frame: Frame | None = None,
        frame_version: int | None = None,
        captured_at: float | None = None,
    ) -> None:
        with self.state_lock:
            metrics = self.pipeline_metrics
            now = monotonic()
            self.pipeline_metrics = replace(
                metrics,
                recognized_frames=metrics.recognized_frames + 1,
                last_ocr_at=now,
                last_ocr_capture_at=captured_at,
                last_ocr_capture_id=getattr(
                    getattr(frame, "provenance", None), "capture_id", None
                ),
                last_ocr_frame_version=frame_version,
                last_speaker_resolved_at=now,
            )

    def _record_route_observation(
        self,
        frame: Frame,
        frame_version: int,
        captured_at: float | None,
        route_kind: str,
    ) -> None:
        with self.state_lock:
            self.observation_frame = frame
            self.pipeline_metrics = replace(
                self.pipeline_metrics,
                last_route_capture_at=captured_at,
                last_route_capture_id=getattr(
                    getattr(frame, "provenance", None), "capture_id", None
                ),
                last_route_frame_version=frame_version,
                last_route_ocr_at=(
                    self.pipeline_metrics.last_ocr_at
                    if route_kind in {"ocr", "cached"}
                    else None
                ),
                last_route_kind=route_kind,
            )

    def _recognition_is_current(
        self,
        purpose: str,
        recognition_owner: str | None,
        recognition_epoch: int,
        fingerprint: object,
        visible: bool,
        complete: bool,
        render_fingerprint: object,
        frame: Frame | None = None,
        run_id: str | None = None,
    ) -> bool:
        # Ownership/focus are barriers even when pixels happen to match.
        if (
            self._ocr_callback("validating-purpose", self.ocr_purpose) != purpose
            or recognition_owner
            != self._ocr_callback("validating-owner", self.stable_frame_owner)
            or not self._ocr_callback("validating-focus", self._is_focused)
        ):
            return False
        with self.state_lock:
            if (
                self.stop_event.is_set()
                or self.paused
                or self.emergency_stopped
                or self.is_shutting_down
                or recognition_epoch != self.frame_route_epoch
                or (run_id is not None and run_id != self.run_id)
                or visible != self.latest_frame_visible
            ):
                return False
            if purpose == "expected-successor-prefix":
                return bool(self.latest_frame_visible and not self.stop_event.is_set())
            changed = (
                fingerprint != self.latest_frame_fingerprint
                or complete != self.latest_frame_complete
                or (
                    self.tracker_options.get("complete_dialogue_only")
                    and render_fingerprint != self.latest_render_fingerprint
                )
            )
            latest = self.latest_frame
        if not changed:
            return True
        # Compare the entire name/text evidence; a new glyph must invalidate OCR.
        if (
            frame is None
            or latest is None
            or not self._ocr_callback(
                "validating-frame", self.frame_equivalent, frame, latest
            )
        ):
            return False
        focused = self._ocr_callback("validating-focus", self._is_focused)
        purpose_current = self._ocr_callback("validating-purpose", self.ocr_purpose)
        owner_current = self._ocr_callback("validating-owner", self.stable_frame_owner)
        with self.state_lock:
            return (
                focused
                and purpose_current == purpose
                and owner_current == recognition_owner
                and not self.stop_event.is_set()
                and not self.paused
                and not self.emergency_stopped
                and not self.is_shutting_down
                and latest is self.latest_frame
                and recognition_epoch == self.frame_route_epoch
                and (run_id is None or run_id == self.run_id)
                and visible == self.latest_frame_visible
            )

    def worker_health_snapshot(self) -> dict[str, object]:
        with self.state_lock:
            future = self.ocr_future
            error = None
            if future is None:
                state = "not-started"
            elif not future.done():
                state = "running" if future.running() else "queued"
            elif future.cancelled():
                state = "cancelled"
            else:
                error = future.exception()
                state = "failed" if error is not None else "finished"
            return {
                "worker_state": state,
                "worker_stage": self.ocr_worker_stage,
                "worker_error_type": type(error).__name__ if error else None,
                "worker_progress_age_ms": round(
                    (monotonic() - self.ocr_worker_progress_at) * 1000
                ),
                "worker_frame_version": self.processed_frame_version,
                "latest_frame_version": self.frame_version,
            }

    def _ocr_progress(self, stage: str) -> None:
        with self.state_lock:
            self.ocr_worker_stage = stage
            self.ocr_worker_progress_at = monotonic()

    def _ocr_callback(
        self,
        stage: str,
        callback: Callable[_P, _R],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        self._ocr_progress(stage)
        return callback(*args, **kwargs)

    def _run_ocr(self, stop_event: Event) -> None:
        tracker = self._ocr_callback(
            "initializing-tracker", self.tracker_factory, **self.tracker_options
        )
        policy = self._ocr_callback(
            "initializing-policy",
            self.adaptive_policy_factory,
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        admission = _TrackerAdmissionDiagnostics(self._report_pipeline_event)
        try:
            self._consume_ocr_frames(stop_event, tracker, policy, admission)
        finally:
            admission.finish()
        self._ocr_progress("flushing")
        if not self.stop_event.is_set() and not self.is_shutting_down:
            self._ocr_callback(
                "scheduling-flush",
                self._schedule_tracked_chunks,
                tracker,
                self._ocr_callback("flushing-tracker", tracker.flush),
            )
        self._ocr_progress("finished")

    def _consume_ocr_frames(
        self,
        stop_event: Event,
        tracker: IncrementalDialogTracker,
        policy: _CapturePolicy,
        admission: _TrackerAdmissionDiagnostics,
    ) -> None:
        cached_fingerprint = object()
        cached_render_fingerprint = object()
        cached_completion = object()
        cached_observation: Observation = (None, "")
        cached_canonical: _CanonicalRecognition | None = None
        last_frame_recheck_at = None
        while True:
            self._ocr_progress("waiting-for-frame")
            with self.pause_condition:
                while (
                    self.processed_frame_version >= self.frame_version
                    and not stop_event.is_set()
                ):
                    self.pause_condition.wait(timeout=self.interval_seconds)
                if (
                    self.processed_frame_version >= self.frame_version
                    and stop_event.is_set()
                ):
                    break
                frame = self.latest_frame
                fingerprint = self.latest_frame_fingerprint
                render_fingerprint = self.latest_render_fingerprint
                visible = self.latest_frame_visible
                complete = self.latest_frame_complete
                self.processed_frame_version = self.frame_version
                recognition_version = self.frame_version
                recognition_capture_at = self.latest_frame_captured_at
            try:
                recheck_required = bool(
                    self._ocr_callback("checking-recheck", self.frame_recheck_required)
                )
                focused = (
                    self._ocr_callback("checking-focus", self._is_focused)
                    if recheck_required
                    else False
                )
                recheck_now = False
                if recheck_required and visible and focused:
                    now = self._ocr_callback("reading-clock", self.stable_frame_clock)
                    recheck_now = bool(
                        last_frame_recheck_at is None
                        or now - last_frame_recheck_at
                        >= self.frame_recheck_interval_seconds
                    )
                    if recheck_now:
                        last_frame_recheck_at = now
                        self._ocr_callback(
                            "reporting-prefix-recheck",
                            self._report_pipeline_event,
                            "canonical-prefix-visual-recheck",
                            self.active_generation,
                            fingerprint=self._privacy_safe_fingerprint(fingerprint),
                            visible=True,
                            focused=True,
                            owner=self._ocr_callback(
                                "reading-owner", self.stable_frame_owner
                            ),
                            recheck_interval_ms=_recheck_interval_milliseconds(
                                self.frame_recheck_interval_seconds
                            ),
                        )
                else:
                    last_frame_recheck_at = None
                with self.state_lock:
                    # Keep recognizing a dispatched transition after its timeout.
                    awaiting_post_advance_dialog = (
                        self.pending_auto_advance_generation == self.active_generation
                        or (
                            self.last_auto_advance_dispatched_generation
                            == self.active_generation
                            and self.failed_auto_advance_generation
                            == self.active_generation
                        )
                    )
                render_changed = bool(
                    self.tracker_options.get("complete_dialogue_only")
                    and self.frame_render_fingerprint is not None
                    and render_fingerprint != cached_render_fingerprint
                )
                confirm_identity = bool(
                    cached_canonical is not None
                    and not cached_canonical.dialog.exact_match
                    and tracker.canonical_line_id != cached_canonical.dialog.line_id
                )
                if (
                    fingerprint == cached_fingerprint
                    and complete == cached_completion
                    and not awaiting_post_advance_dialog
                    and not recheck_now
                    and not render_changed
                    and not self.observation_uncertain
                    and not confirm_identity
                ):
                    character, text = cached_observation
                    route_kind = "cached"
                    with self.state_lock:
                        metrics = self.pipeline_metrics
                        self.pipeline_metrics = replace(
                            metrics,
                            reused_frames=metrics.reused_frames + 1,
                        )
                    frame_route = None
                else:
                    frame_route = self._stable_frame_route_decision(
                        fingerprint,
                        visible,
                        complete,
                    )
                if frame_route is False:
                    admission.observe(
                        tracker,
                        self.run_id,
                        "visual",
                        frame,
                        recognition_version,
                        "visual-only",
                    )
                    if self._ocr_callback("reading-purpose", self.ocr_purpose) is None:
                        self._ocr_callback(
                            "observing-frame",
                            self.frame_observed,
                            frame,
                            fingerprint,
                            "locked-visual-only",
                        )
                    character, text = cached_observation
                    interval = self._ocr_callback(
                        "updating-policy", policy.observe, character, text, focused=True
                    )
                    with self.state_lock:
                        self.next_capture_interval = interval
                    continue
                canonical_observation = _canonical_frame_observation(frame_route)
                if canonical_observation is not None:
                    character, text = canonical_observation
                    route_kind = "canonical"
                    cached_fingerprint = fingerprint
                    cached_render_fingerprint = render_fingerprint
                    cached_completion = complete
                    cached_observation = (character, text)
                    cached_canonical = None
                elif frame_route is None and (
                    fingerprint != cached_fingerprint
                    or awaiting_post_advance_dialog
                    or recheck_now
                    or render_changed
                    or self.observation_uncertain
                    or confirm_identity
                ):
                    purpose = self._ocr_callback("reading-purpose", self.ocr_purpose)
                    if purpose is None:
                        admission.observe(
                            tracker,
                            self.run_id,
                            "visual",
                            frame,
                            recognition_version,
                            "visual-only",
                        )
                        self._ocr_callback(
                            "observing-frame",
                            self.frame_observed,
                            frame,
                            fingerprint,
                            "locked-visual-only",
                        )
                        character, text = cached_observation
                        interval = self._ocr_callback(
                            "updating-policy",
                            policy.observe,
                            character,
                            text,
                            focused=True,
                        )
                        with self.state_lock:
                            self.next_capture_interval = interval
                        continue
                    observation = self._recognize_current_frame(
                        frame,
                        purpose,
                        fingerprint,
                        visible,
                        complete,
                        render_fingerprint,
                        policy,
                        recognition_version,
                        recognition_capture_at,
                    )
                    if observation is None:
                        continue
                    previous_canonical = cached_canonical
                    cached_canonical = (
                        observation
                        if isinstance(observation, _CanonicalRecognition)
                        else None
                    )
                    if (
                        cached_canonical is not None
                        and previous_canonical is not None
                        and cached_canonical.dialog == previous_canonical.dialog
                        and cached_canonical.run_id == previous_canonical.run_id
                        and cached_canonical.epoch == previous_canonical.epoch
                    ):
                        cached_canonical = replace(
                            cached_canonical,
                            observations=previous_canonical.observations + 1,
                        )
                    character, text = (
                        (observation.dialog.character, observation.dialog.text)
                        if isinstance(observation, _CanonicalRecognition)
                        else observation
                    )
                    route_kind = "ocr"
                    cached_fingerprint = fingerprint
                    cached_render_fingerprint = render_fingerprint
                    cached_completion = complete
                    cached_observation = (character, text)
                    self._record_ocr_completion(
                        frame, recognition_version, recognition_capture_at
                    )
                elif frame_route is not None:
                    raise TypeError(
                        "stable_frame_route must return None, False or "
                        "a SilentDialogRoute/CanonicalDialogRoute/"
                        "(character, text) route"
                    )
                self._record_route_observation(
                    frame, recognition_version, recognition_capture_at, route_kind
                )
                self._ocr_progress("routing")
                if (
                    cached_canonical is not None
                    and not cached_canonical.dialog.exact_match
                    and cached_canonical.observations < tracker.stability_frames
                ):
                    admission.observe(
                        tracker,
                        self.run_id,
                        route_kind,
                        frame,
                        recognition_version,
                        "waiting-stability",
                    )
                    interval = self._ocr_callback(
                        "updating-policy", policy.observe, character, text, focused=True
                    )
                    with self.state_lock:
                        self.next_capture_interval = interval
                    continue
                self._ocr_callback(
                    "observing-transition",
                    self._observe_pending_transition_text,
                    character,
                    text,
                )
                routed_observation = (
                    frame_route
                    if isinstance(
                        frame_route,
                        (SilentDialogRoute, CanonicalDialogRoute),
                    )
                    else self._ocr_callback(
                        "routing-observation", self._report_observation, character, text
                    )
                )
                if routed_observation is None:
                    admission.observe(
                        tracker,
                        self.run_id,
                        route_kind,
                        frame,
                        recognition_version,
                        "routing-held",
                    )
                    interval = self._ocr_callback(
                        "updating-policy", policy.observe, character, text, focused=True
                    )
                    with self.state_lock:
                        self.next_capture_interval = interval
                    continue
                if isinstance(routed_observation, SilentDialogRoute):
                    silent_route = routed_observation
                    character, text = None, ""
                elif isinstance(routed_observation, CanonicalDialogRoute):
                    silent_route = None
                    character = routed_observation.character
                    text = routed_observation.text
                else:
                    silent_route = None
                    character, text = routed_observation
                frame_already_routed, cached_canonical = self._bind_observed_route(
                    tracker,
                    character,
                    text,
                    cached_canonical,
                    frame,
                    fingerprint,
                    route_kind,
                )
                if route_kind != "cached" and not frame_already_routed:
                    self._ocr_callback(
                        "publishing-route",
                        self.frame_routed,
                        frame,
                        fingerprint,
                        route_kind,
                        character,
                        text,
                    )
                replacement = (
                    self._replace_unheard_candidate(
                        tracker, cached_canonical, character, text
                    )
                    if silent_route is None
                    and not isinstance(routed_observation, CanonicalDialogRoute)
                    else None
                )
                if silent_route is not None:
                    self._ocr_callback(
                        "tracking-silent", tracker.observe_silent, silent_route.event_id
                    )
                    chunks: list[SpeechChunk] | None = []
                elif replacement is not None:
                    chunks = replacement
                else:
                    chunks = self._track_routed_dialog(
                        tracker,
                        character,
                        text,
                        cached_canonical,
                        route_kind,
                        awaiting_post_advance_dialog,
                    )
                admission.observe(
                    tracker, self.run_id, route_kind, frame, recognition_version
                )
                if chunks is not None:
                    self._ocr_callback(
                        "scheduling-speech",
                        self._schedule_tracked_chunks,
                        tracker,
                        chunks,
                    )
                interval = self._ocr_callback(
                    "updating-policy", policy.observe, character, text, focused=True
                )
                with self.state_lock:
                    self.next_capture_interval = interval
            except Exception as error:
                self._ocr_callback("reporting-error", self.report_error, error)
                with self.state_lock:
                    self.next_capture_interval = self.interval_seconds

    def _bind_observed_route(
        self,
        tracker: IncrementalDialogTracker,
        character: str | None,
        text: str,
        canonical: _CanonicalRecognition | None,
        frame: Frame,
        fingerprint: FrameFingerprint,
        route_kind: str,
    ) -> tuple[bool, _CanonicalRecognition | None]:
        tracker_character = "Narrator" if character is None and text else character
        with self.state_lock:
            already_routed = fingerprint == self.routed_frame_fingerprint
            changed_identity = (
                (tracker_character, text) != (tracker.character, tracker.latest_text)
                or canonical is not None
                and canonical.dialog.line_id != tracker.canonical_line_id
            )
            if changed_identity and (not already_routed or route_kind != "cached"):
                self.dialog_ready_generation = None
                self._cancel_auto_advance_locked()
            if self.stable_frame_route is not None:
                previous_epoch = self.frame_route_epoch
                self._accept_routed_frame_locked(fingerprint, frame)
                canonical = (
                    replace(canonical, epoch=self.frame_route_epoch)
                    if canonical is not None and canonical.epoch == previous_epoch
                    else canonical
                )
        return already_routed, canonical

    def _track_routed_dialog(
        self,
        tracker: IncrementalDialogTracker,
        character: str | None,
        text: str,
        canonical: _CanonicalRecognition | None,
        route_kind: str,
        awaiting_post_advance_dialog: bool,
    ) -> list[SpeechChunk] | None:
        line_id = self._ocr_callback(
            "resolving-line", self.line_id_resolver, character, text
        )
        if (
            line_id is None
            and canonical is not None
            and (character, text)
            == (
                canonical.dialog.character,
                canonical.dialog.text,
            )
        ):
            with self.state_lock:
                epoch = self.frame_route_epoch
            current_identity = (
                canonical.run_id == self.run_id
                and canonical.epoch == epoch
                and canonical.is_current(epoch)
            )
            with self.state_lock:
                pending_correction = bool(
                    canonical.dialog.allow_unheard_replacement
                    and tracker.canonical_line_id is None
                    and tracker.committed_text
                    and tracker._texts_are_related(tracker.committed_text, text)
                    and self.active_generation not in self.pcm_admitted_generations
                    and any(
                        chunk.generation == self.active_generation
                        and chunk.line_id is None
                        for chunk in self.speech_futures.values()
                    )
                )
            if (
                current_identity
                and not pending_correction
                and (route_kind != "cached" or canonical.dialog.exact_match)
            ):
                if (
                    canonical.dialog.line_id in tracker.accepted_line_ids
                    and canonical.dialog.line_id != tracker.canonical_line_id
                ):
                    return None
                return self._ocr_callback(
                    "tracking-identified",
                    tracker.observe_identified,
                    canonical.dialog.character,
                    text,
                    canonical.dialog.line_id,
                    exact=True,
                )
            else:
                return None
        elif line_id is None:
            expect_new_dialog = getattr(tracker, "expect_new_dialog", None)
            if callable(expect_new_dialog) and awaiting_post_advance_dialog:
                self._ocr_callback("tracking-transition", expect_new_dialog)
            tracker_character = "Narrator" if character is None else character
            return self._ocr_callback(
                "tracking-dialogue",
                tracker.observe,
                tracker_character,
                text,
            )
        else:
            assert character is not None
            return self._ocr_callback(
                "tracking-canonical",
                tracker.observe_canonical,
                character,
                text,
                line_id,
            )

    def _recognize_current_frame(
        self,
        frame: Frame,
        purpose: str,
        fingerprint: FrameFingerprint,
        visible: bool,
        complete: bool,
        render_fingerprint: FrameFingerprint,
        policy: _CapturePolicy,
        frame_version: int,
        captured_at: float | None,
    ) -> Observation | _CanonicalRecognition | None:
        recognition_owner = self._ocr_callback("reading-owner", self.stable_frame_owner)
        with self.state_lock:
            recognition_epoch = self.frame_route_epoch
            recognition_run_id = self.run_id
            generation = self.active_generation
        self._ocr_progress("recognizing")
        started = monotonic()
        capture_id, provenance_time = _frame_capture_evidence(frame)
        captured_at = (
            provenance_time
            if provenance_time is not None
            else optional_finite_number(captured_at)
        )
        outcome, reason, error_type = "error", None, None
        uncertainty = None
        recognized: Observation | RecognizedDialog = (None, "")
        try:
            try:
                recognized = self.recognize_frame(frame)
                character, text = (
                    (recognized.character, recognized.text)
                    if isinstance(recognized, RecognizedDialog)
                    else recognized
                )
            except OCRPreviewCancelled as error:
                outcome, error_type = "cancelled", type(error).__name__
                return None
            except OCRUncertainError as error:
                uncertainty = error
            finally:
                ocr_call_ms = (monotonic() - started) * 1000
            self._ocr_progress("validating-freshness")

            def is_current_at_epoch(epoch: int) -> bool:
                return self._recognition_is_current(
                    purpose,
                    recognition_owner,
                    epoch,
                    fingerprint,
                    visible,
                    complete,
                    render_fingerprint,
                    frame,
                    recognition_run_id,
                )

            is_current = partial(is_current_at_epoch, recognition_epoch)
            if not is_current():
                outcome, reason = "stale", "ocr-not-current"
                return None
            if uncertainty is not None:
                self._ocr_callback(
                    "reporting-uncertainty", self.ocr_uncertain, uncertainty, is_current
                )
                interval = self._ocr_callback(
                    "updating-policy", policy.observe, None, "", focused=True
                )
                with self.state_lock:
                    self.next_capture_interval = interval
                outcome = "uncertain"
                return None
            # Accepted here means current OCR evidence, not tracker/speech admission.
            outcome, reason = "accepted", "ocr-current"
            if isinstance(recognized, RecognizedDialog):
                return _CanonicalRecognition(
                    recognized,
                    recognition_run_id,
                    recognition_epoch,
                    is_current_at_epoch,
                )
            return character, text
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            completed = monotonic()
            self._report_pipeline_event(
                "ocr-attempt",
                generation,
                completed,
                run_id=recognition_run_id,
                capture_id=capture_id,
                frame_version=frame_version,
                started_monotonic=started,
                completed_monotonic=completed,
                frame_age_ms=(started - captured_at) * 1000
                if captured_at is not None
                else None,
                ocr_call_ms=ocr_call_ms,
                attempt_ms=(completed - started) * 1000,
                outcome=outcome,
                reason=reason,
                error_type=error_type,
                ocr_confidence=uncertainty.result.confidence if uncertainty else None,
                minimum_confidence=uncertainty.minimum_confidence
                if uncertainty
                else None,
            )

    def _stable_frame_route_decision(
        self,
        fingerprint: FrameFingerprint,
        visible: bool = True,
        complete: bool = False,
    ) -> StableFrameRoute:
        if self.stable_frame_route is None:
            return None
        focused = self._ocr_callback("checking-focus", self._is_focused)
        if not visible or not focused:
            with self.state_lock:
                self._reset_stable_frame_candidate_locked()
            self._ocr_callback(
                "reporting-stable-gate",
                self._report_pipeline_event,
                "stable-frame-gate",
                self.active_generation,
                fingerprint=self._privacy_safe_fingerprint(fingerprint),
                visible=bool(visible),
                focused=focused,
                owner=self._ocr_callback("reading-owner", self.stable_frame_owner),
                completion_cue=bool(complete),
                candidate_frames=0,
                settled_ms=0,
                ready=False,
            )
            return False
        owner = self._ocr_callback("reading-owner", self.stable_frame_owner)
        expected_prefix = (
            self._ocr_callback("reading-purpose", self.ocr_purpose)
            == "expected-successor-prefix"
        )
        now = self._ocr_callback("reading-clock", self.stable_frame_clock)
        with self.state_lock:
            if self.observation_uncertain:
                return None
            if self.routed_frame_fingerprint is None:
                return None
            route_epoch = self.frame_route_epoch
            frame_version = self.frame_version
            latest_frame = self.latest_frame
            routed_frame = self.routed_frame
            candidate_frame = self.candidate_frame
            same_routed = fingerprint == self.routed_frame_fingerprint
            same_candidate = fingerprint == self.candidate_frame_fingerprint
        # Image comparisons can block; keep capture, health and Stop independent.
        same_routed = same_routed or (
            routed_frame is not None
            and latest_frame is not None
            and self._ocr_callback(
                "comparing-routed-frame",
                self.frame_equivalent,
                routed_frame,
                latest_frame,
            )
        )
        if not same_routed:
            same_candidate = same_candidate or (
                candidate_frame is not None
                and latest_frame is not None
                and self._ocr_callback(
                    "comparing-candidate-frame",
                    self.frame_equivalent,
                    candidate_frame,
                    latest_frame,
                )
            )
        current_owner = self._ocr_callback("validating-owner", self.stable_frame_owner)
        focused = self._ocr_callback("validating-focus", self._is_focused)
        with self.state_lock:
            if (
                route_epoch != self.frame_route_epoch
                or frame_version != self.frame_version
                or latest_frame is not self.latest_frame
                or owner != current_owner
                or not focused
                or self.observation_uncertain
                or self.stop_event.is_set()
                or self.paused
                or self.emergency_stopped
                or self.is_shutting_down
            ):
                return False
            if same_routed:
                return False if expected_prefix else None
            if same_candidate and owner == self.candidate_frame_owner:
                self.candidate_frame_count += 1
            else:
                self.candidate_frame = self.latest_frame
                self.candidate_frame_fingerprint = fingerprint
                self.candidate_frame_count = 1
                self.candidate_frame_owner = owner
                self.candidate_frame_started_at = now
            candidate_frames = self.candidate_frame_count
            candidate_started_at = self.candidate_frame_started_at
            assert candidate_started_at is not None
            settled_for = now - candidate_started_at
            ready = (
                candidate_frames >= 2
                and settled_for >= self.stable_frame_minimum_seconds
            )
        self._ocr_callback(
            "reporting-stable-gate",
            self._report_pipeline_event,
            "stable-frame-gate",
            self.active_generation,
            fingerprint=self._privacy_safe_fingerprint(fingerprint),
            visible=True,
            focused=True,
            owner=owner,
            completion_cue=bool(complete),
            candidate_frames=candidate_frames,
            settled_ms=round(settled_for * 1000),
            ready=ready,
        )
        route = self._ocr_callback(
            "routing-stable-frame",
            self.stable_frame_route,
            fingerprint,
            ready,
            owner,
            route_epoch,
        )
        with self.state_lock:
            if route_epoch != self.frame_route_epoch:
                return False
        return route

    @staticmethod
    def _privacy_safe_fingerprint(fingerprint: FrameFingerprint) -> str:
        if isinstance(fingerprint, bytes):
            payload = fingerprint
        elif isinstance(fingerprint, (str, int, float)):
            payload = str(fingerprint).encode("utf-8", errors="replace")
        else:
            return "unavailable"
        return sha256(payload).hexdigest()[:16]

    def _accept_routed_frame(self, fingerprint: FrameFingerprint) -> None:
        with self.state_lock:
            self._accept_routed_frame_locked(fingerprint)

    def current_frame_has_completion_cue(self) -> bool:
        with self.state_lock:
            return bool(self.latest_frame_visible and self.latest_frame_complete)

    def current_frame_render_quiet_ms(
        self, *, expected_owner: object | None = None
    ) -> int | None:
        """Return owner-bound render quiet time, or None when evidence is unsafe."""
        if not self._is_focused():
            return None
        owner = self.stable_frame_owner()
        now = self.stable_frame_clock()
        with self.state_lock:
            return self._render_quiet_ms_locked(now, owner, expected_owner)

    def _render_quiet_ms_locked(
        self, now: float, owner: object, expected_owner: object | None
    ) -> int | None:
        if (
            not self.latest_frame_visible
            or self.latest_render_fingerprint is None
            or self.latest_render_changed_at is None
            or owner != self.latest_render_owner
            or (expected_owner is not None and owner != expected_owner)
        ):
            return None
        quiet_seconds = now - self.latest_render_changed_at
        if quiet_seconds < self.render_quiet_minimum_seconds:
            return None
        return round(quiet_seconds * 1000)

    def _accept_routed_frame_locked(
        self, fingerprint: FrameFingerprint, frame: Frame | None = None
    ) -> None:
        self.observation_uncertain = False
        self.routed_frame = self.latest_frame if frame is None else frame
        self.routed_frame_fingerprint = fingerprint
        self.frame_route_epoch += 1
        self._reset_stable_frame_candidate_locked()

    def _reset_stable_frame_candidate_locked(self) -> None:
        self.candidate_frame = None
        self.candidate_frame_fingerprint = object()
        self.candidate_frame_count = 0
        self.candidate_frame_owner = None
        self.candidate_frame_started_at = None

    def _is_focused(self) -> bool:
        try:
            focused = bool(self.focus_probe())
        except Exception as error:
            with self.state_lock:
                report_failure = not self.focus_probe_failed
                self.focus_probe_failed = True
            if report_failure:
                self.report_error(error)
            return False
        with self.state_lock:
            self.focus_probe_failed = False
        return focused

    def _set_generation(self, generation: int) -> None:
        confirmed_advance = None
        with self.pause_condition:
            previous_generation = self.active_generation
            changed = generation != previous_generation
            self.active_generation = generation
            if self.suppressed_generation != generation:
                self.suppressed_generation = None
            if changed:
                self.unheard_candidate = None
                self.failed_auto_advance_generation = None
                self.auto_advance_focus_wait_generation = None
                self.auto_advance_visual_wait_generation = None
                if self.pending_auto_advance_generation == previous_generation:
                    confirmed_advance = (
                        previous_generation,
                        self.auto_advance_attempts,
                    )
                    self.pending_auto_advance_generation = None
                    self.auto_advance_attempts = 0
                    self.pipeline_metrics = replace(
                        self.pipeline_metrics, last_auto_advance_at=monotonic()
                    )
                self.dialog_ready_generation = None
                self.auto_advance_blocked_generation = None
                self.auto_advance_block_reason = None
                self._cancel_auto_advance_locked()
                self.pause_condition.notify_all()
            metrics = self.pipeline_metrics
        if changed and generation > 0:
            for stage, occurred_at in (
                ("capture", metrics.last_route_capture_at or metrics.last_capture_at),
                (
                    "ocr",
                    metrics.last_route_ocr_at
                    if metrics.last_route_kind is not None
                    else metrics.last_ocr_at,
                ),
                ("stable-text", monotonic()),
            ):
                if occurred_at is not None:
                    self._report_pipeline_event(
                        stage,
                        generation,
                        occurred_at,
                        capture_id=(
                            metrics.last_ocr_capture_id
                            if stage == "ocr"
                            else metrics.last_route_capture_id
                        ),
                        frame_version=(
                            metrics.last_ocr_frame_version
                            if stage == "ocr"
                            else metrics.last_route_frame_version
                        ),
                        visibility_provenance=(
                            "canonical-visual-frame"
                            if metrics.last_route_kind == "canonical"
                            else "recognized-captured-frame"
                        ),
                    )
        if confirmed_advance is not None:
            confirmed_generation, attempt = confirmed_advance
            self._report_pipeline_event(
                "confirmed-next-dialogue",
                confirmed_generation,
                monotonic(),
                attempt=attempt,
            )
            self._report_auto_advance_state(
                "confirmed",
                confirmed_generation,
                attempt,
            )

    def _admit_chunk(self, chunk: SpeechChunk) -> SpeechChunk | None:
        with self.state_lock:
            admitted = self.accepted_chunks.get(chunk.playback_id)
            if admitted is not None:
                return admitted
            if (
                chunk.playback_id in self.pending_admission_ids
                or chunk.playback_id in self.cancelled_chunk_ids
                or chunk.run_id not in (None, self.run_id)
                or chunk.generation < self.active_generation
                or self.suppressed_generation == chunk.generation
                or self.emergency_stopped
                or self.is_shutting_down
            ):
                return None
            queue_epoch = self.queue_epoch
            self.pending_admission_ids.add(chunk.playback_id)
            chunk = replace(
                chunk,
                run_id=self.run_id,
                capture_id=chunk.capture_id
                or self.pipeline_metrics.last_route_capture_id,
            )
        # Controller callbacks may acquire the story lock.
        playback_id = chunk.playback_id
        try:
            accepted = self.accept_chunk(chunk)
        except BaseException:
            with self.state_lock:
                self.pending_admission_ids.discard(playback_id)
            raise
        with self.state_lock:
            self.pending_admission_ids.discard(playback_id)
            if accepted is None:
                return None
            chunk = accepted
            rejected = (
                queue_epoch != self.queue_epoch
                or chunk.run_id != self.run_id
                or chunk.generation < self.active_generation
                or self.suppressed_generation == chunk.generation
                or self.emergency_stopped
                or self.is_shutting_down
            )
            if not rejected:
                self.accepted_chunks[chunk.playback_id] = chunk
        if rejected:
            self.reject_chunk(chunk)
            return None
        return chunk

    def _schedule(self, chunks: Iterable[SpeechChunk]) -> None:
        for candidate in chunks:
            chunk = self._admit_chunk(candidate)
            if chunk is None:
                continue
            with self.pause_condition:
                if (
                    self.emergency_stopped
                    or self.is_shutting_down
                    or chunk.playback_id in self.cancelled_chunk_ids
                    or chunk.playback_id in self.scheduled_playback_ids
                ):
                    continue
                if (
                    chunk.generation in self.sealed_generations
                    and not chunk.explicit_replay
                ):
                    self.completed_playback_ids.add(chunk.playback_id)
                    self._report_pipeline_event(
                        "late-chunk-suppressed",
                        chunk.generation,
                        monotonic(),
                        chunk_id=chunk.chunk_id,
                        chunk_ordinal=chunk.ordinal,
                        chunk_characters=len(chunk.text),
                    )
                    continue
                if self.suppressed_generation == chunk.generation:
                    continue
                if self.paused:
                    if not any(
                        pending.playback_id == chunk.playback_id
                        for pending in self.paused_chunks
                    ):
                        self.paused_chunks.append(chunk)
                else:
                    self._defer_chunk_locked(chunk)
                self._record_speech_metrics_locked(sentence_ready=True)
        self._schedule_prepared_if_possible()
        self._schedule_deferred_if_possible()

    def _speech_finished(self, future: Future[None]) -> None:
        with self.state_lock:
            chunk = self.speech_futures.pop(future, None)
            if self.playback_future is future:
                self.playback_future = None
            if chunk is not None:
                if chunk.playback_id in self.pause_retry_chunks:
                    self._queue_pause_retry_locked(chunk)
                else:
                    self.completed_playback_ids.add(chunk.playback_id)
            self._record_speech_metrics_locked(playback=True)
        self._schedule_prepared_if_possible()
        self._schedule_deferred_if_possible()
        self._maybe_auto_advance()

    def _prepare_if_current(self, chunk: SpeechChunk) -> object | None:
        token = audio_lifecycle_context.set(self._chunk_context(chunk))
        try:
            return self._prepare_current_chunk(chunk)
        finally:
            audio_lifecycle_context.reset(token)

    def _chunk_context(self, chunk: SpeechChunk) -> dict[str, object]:
        return {
            "run_id": chunk.run_id or self.run_id,
            "playback_id": chunk.playback_id,
            "generation": chunk.generation,
            "chunk_id": chunk.chunk_id,
            "capture_id": chunk.capture_id,
        }

    def _prepare_current_chunk(self, chunk: SpeechChunk) -> object | None:
        if not self.wait_until_playable(chunk):
            return None
        if chunk.chunk_id is not None:
            with self.state_lock:
                if chunk.chunk_id in self.prepared_chunk_ids:
                    duplicate = True
                else:
                    self.prepared_chunk_ids.add(chunk.chunk_id)
                    duplicate = False
            if duplicate:
                self._report_pipeline_event(
                    "duplicate-chunk-suppressed",
                    chunk.generation,
                    monotonic(),
                    chunk_id=chunk.chunk_id,
                    chunk_ordinal=chunk.ordinal,
                    chunk_characters=len(chunk.text),
                )
                return None
        with self.state_lock:
            self._record_speech_metrics_locked(generation_started=True)
        self._report_pipeline_event("generation-start", chunk.generation, monotonic())
        for attempt in (1, 2):
            try:
                return self.prepare_chunk(chunk)
            except Exception as error:
                self.report_error(error)
            if attempt == 1 and self.wait_until_playable(chunk):
                self._report_pipeline_event(
                    "generation-start",
                    chunk.generation,
                    attempt=2,
                    outcome="retrying",
                    chunk_id=chunk.chunk_id,
                )
                continue
            with self.state_lock:
                self.prepared_chunk_ids.discard(chunk.chunk_id)
            reason = (
                "Audio preparation failed after retry; restart reading to retry "
                "this dialogue"
            )
            if attempt == 2 and self.block_auto_advance_for_generation(
                chunk.generation, reason
            ):
                self._report_pipeline_event(
                    "auto-advance-withheld",
                    chunk.generation,
                    attempt=2,
                    outcome="failed",
                    reason="audio-preparation-failed",
                    chunk_id=chunk.chunk_id,
                )
                self._report_auto_advance_state(
                    "preparation-failed", chunk.generation, 2
                )
            return None

        return None

    def _preparation_finished(self, future: Future[object | None]) -> None:
        with self.state_lock:
            chunk = self.speech_futures.get(future)
            self._record_speech_metrics_locked(
                synthesis=True,
                first_pcm=self.first_pcm_on_prepare,
            )
        prepared = None
        try:
            if chunk is not None and not future.cancelled():
                prepared = future.result()
        except Exception as error:
            self.report_error(error)
        with self.state_lock:
            if (
                chunk is not None
                and prepared is not None
                and self._owns_chunk(chunk)
                and chunk.playback_id not in self.cancelled_chunk_ids
                and self.suppressed_generation != chunk.generation
            ):
                self.prepared_results[future] = prepared
            else:
                self.speech_futures.pop(future, None)
                if chunk is not None:
                    self.completed_playback_ids.add(chunk.playback_id)
        self._schedule_prepared_if_possible()
        self._schedule_deferred_if_possible()

    def _schedule_prepared_if_possible(self) -> None:
        with self.pause_condition:
            for future in tuple(self.prepared_results):
                pending = self.speech_futures.get(future)
                if pending is None or (
                    pending.playback_id in self.cancelled_chunk_ids
                    or not self._owns_chunk(pending)
                    or self.suppressed_generation == pending.generation
                    or (
                        pending.generation in self.sealed_generations
                        and not pending.explicit_replay
                    )
                ):
                    self.prepared_results.pop(future, None)
                    self.speech_futures.pop(future, None)
                    if pending is not None:
                        self.completed_playback_ids.add(pending.playback_id)
            if self.paused or self.playback_future is not None:
                return
            chunk = next(
                (
                    pending
                    for pending in self.accepted_chunks.values()
                    if (
                        pending.playback_id not in self.completed_playback_ids
                        or pending.playback_id in self.pause_interrupted_chunk_ids
                    )
                    and pending.playback_id not in self.cancelled_chunk_ids
                ),
                None,
            )
            if chunk is None or chunk.playback_id in self.pause_interrupted_chunk_ids:
                return
            prepared_future = next(
                (
                    future
                    for future, pending in self.speech_futures.items()
                    if pending is chunk and future in self.prepared_results
                ),
                None,
            )
            if prepared_future is None:
                return
            prepared = self.prepared_results.pop(prepared_future)
            self.speech_futures.pop(prepared_future, None)
            failure = None
            try:
                future = self.playback_executor.submit(
                    self._play_if_current, chunk, prepared
                )
            except Exception as error:
                self.completed_playback_ids.add(chunk.playback_id)
                failure = error
            else:
                self.playback_future = future
                self.speech_futures[future] = chunk
            self._record_speech_metrics_locked()
        if failure is not None:
            self.reject_chunk(chunk)
            self.report_error(failure)
            self._schedule_prepared_if_possible()
            self._schedule_deferred_if_possible()
        else:
            future.add_done_callback(self._speech_finished)

    def _play_if_current(self, chunk: SpeechChunk, prepared: object) -> None:
        token = audio_lifecycle_context.set(self._chunk_context(chunk))
        admission_token = pcm_admission_context.set(
            PcmAdmission(lambda: self.claim_first_pcm(chunk))
        )
        try:
            self._play_current_chunk(chunk, prepared)
        finally:
            pcm_admission_context.reset(admission_token)
            audio_lifecycle_context.reset(token)

    def _play_current_chunk(self, chunk: SpeechChunk, prepared: object) -> None:
        from vntts.generated_audio import (
            GeneratedAudioRoute,
            LiveFallbackRoute,
            LiveTTSRoute,
            SourceAudioRoute,
        )
        from vntts.playback import PreparedPlayback

        if not self.wait_until_playable(chunk):
            return
        with self.state_lock:
            self.current_chunk = chunk
            self.last_spoken_chunk = chunk
            metrics = self.pipeline_metrics
            self.current_chunk_pipeline_origins = {
                "from_text_visible_ms": metrics.last_text_visible_at,
                "from_ocr_stable_ms": metrics.last_ocr_stable_at,
                "from_generation_started_ms": metrics.last_generation_started_at,
                "from_playback_started_ms": monotonic(),
                "from_canonical_full_text_ms": (
                    metrics.last_canonical_full_text_at
                    if metrics.last_canonical_full_text_generation == chunk.generation
                    else None
                ),
            }
            self._record_speech_metrics_locked(playback_started=True)
        self._report_pipeline_event(
            "playback-start",
            chunk.generation,
            measurement="dispatch",
            timing_provenance="measured",
        )
        if self.first_pcm_on_prepare and not isinstance(
            prepared,
            (
                GeneratedAudioRoute,
                LiveFallbackRoute,
                LiveTTSRoute,
                SourceAudioRoute,
                PreparedPlayback,
            ),
        ):
            self._report_pipeline_event(
                "first-pcm",
                chunk.generation,
                monotonic(),
                chunk_id=chunk.chunk_id,
                chunk_ordinal=chunk.ordinal,
                chunk_characters=len(chunk.text),
            )
        try:
            self.play_prepared(chunk, prepared)
        except PcmAdmissionRejected:
            pass
        except Exception as error:
            self.report_error(error)
        finally:
            with self.state_lock:
                self._record_speech_metrics_locked(playback_completed=True)
                if self.current_chunk is chunk:
                    self.current_chunk = None
                    self.current_chunk_pipeline_origins = None
            self._report_pipeline_event(
                "playback-completion",
                chunk.generation,
                monotonic(),
                chunk_id=chunk.chunk_id,
                chunk_ordinal=chunk.ordinal,
                chunk_characters=len(chunk.text),
            )

    def _report_observation(
        self, character: str | None, text: str
    ) -> DialogRoute | None:
        if character is None and not text:
            if self.last_observation is not None:
                decision = self.dialog_observed("Narrator", "")
                if decision is False:
                    self.deferred_observation = (None, "")
                    self.last_accepted_observation = None
                    return None
            self.last_observation = None
            self.last_accepted_observation = None
            self.deferred_observation = None
            return (None, "")
        observation = (character, " ".join((text or "").split()))
        if (
            observation == self.last_observation
            and observation != self.deferred_observation
        ):
            return self.last_accepted_observation or observation
        self.last_observation = observation
        if text:
            with self.state_lock:
                self._record_speech_metrics_locked(text_visible=True)
        decision = self.dialog_observed(*observation)
        if decision is False:
            self.deferred_observation = observation
            self.last_accepted_observation = None
            return None
        routed: DialogRoute
        if isinstance(decision, (SilentDialogRoute, CanonicalDialogRoute)):
            routed = decision
        elif isinstance(decision, tuple) and len(decision) == 2:
            routed = (decision[0], " ".join((decision[1] or "").split()))
        else:
            routed = observation
        self.deferred_observation = None
        self.last_accepted_observation = routed
        return routed

    def _interrupt_speech(self) -> bool:
        try:
            return bool(self.interrupt_speech())
        except Exception as error:
            self.report_error(error)
            return False

    def _schedule_tracked_chunks(
        self, tracker: IncrementalDialogTracker, chunks: Iterable[SpeechChunk]
    ) -> None:
        with self.state_lock:
            if self.stop_event.is_set() or self.is_shutting_down:
                return
            offset = self._explicit_generation_offset
            generation = tracker.generation + offset
            self._set_generation(generation)
        self._schedule(
            replace(chunk, generation=chunk.generation + offset) for chunk in chunks
        )
        self._update_dialog_ready(tracker, generation)

    def _update_dialog_ready(
        self, tracker: IncrementalDialogTracker, generation: int
    ) -> None:
        with self.state_lock:
            if generation != self.active_generation:
                return
            self.dialog_ready_generation = (
                generation if tracker.is_idle_complete() else None
            )
            if (
                self.dialog_ready_generation is None
                and self.pending_auto_advance_generation is None
            ):
                self._cancel_auto_advance_locked()
        self._maybe_auto_advance()

    def _auto_advance_gate_details_locked(
        self, timer_serial: int | None = None
    ) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "active_generation": self.active_generation,
            "dialog_ready_generation": self.dialog_ready_generation,
            "pending_auto_advance_generation": self.pending_auto_advance_generation,
            "failed_auto_advance_generation": self.failed_auto_advance_generation,
            "blocked_generation": self.auto_advance_blocked_generation,
            "suppressed_generation": self.suppressed_generation,
            "auto_advance_enabled": self.auto_advance is not None,
            "already_dispatched": (
                self.last_auto_advance_dispatched_generation == self.active_generation
            ),
            "timer_pending": self.auto_advance_timer is not None,
            "timer_serial": timer_serial,
            "current_timer_serial": self._auto_advance_timer_serial,
            "frame_version": self.frame_version,
            "processed_frame_version": self.processed_frame_version,
            "frame_route_epoch": self.frame_route_epoch,
            "routed_latest_equal": (
                self.routed_frame_fingerprint == self.latest_frame_fingerprint
            ),
            "routed_frame_present": self.routed_frame_fingerprint is not None,
            "latest_frame_present": self.latest_frame_fingerprint is not None,
            "visible": self.latest_frame_visible,
            "completion_cue": self.latest_frame_complete,
            "observation_uncertain": self.observation_uncertain,
            "candidate_frames": self.candidate_frame_count,
            "candidate_owner_present": self.candidate_frame_owner is not None,
            "candidate_started_at": self.candidate_frame_started_at,
            "stable_minimum_ms": self.stable_frame_minimum_seconds * 1000,
            "render_owner_present": self.latest_render_owner is not None,
            "render_changed_at": self.latest_render_changed_at,
            "render_quiet_minimum_ms": self.render_quiet_minimum_seconds * 1000,
            "current_chunk_present": self.current_chunk is not None,
            "speech_future_count": len(self.speech_futures),
            "deferred_chunk_present": bool(self.deferred_chunks),
            "paused": self.paused,
            "emergency_stopped": self.emergency_stopped,
            "shutting_down": self.is_shutting_down,
            "stop_requested": self.stop_event.is_set(),
        }

    def _report_auto_advance_gate(
        self,
        generation: int,
        reason: str,
        details: dict[str, object] | None = None,
        *,
        focused: bool | None = None,
    ) -> None:
        reason = reason if reason in CONTINUATION_GATE_REASONS else "callback-blocked"
        now = monotonic()
        with self.state_lock:
            if details is None:
                details = self._auto_advance_gate_details_locked()
            previous = self._auto_advance_gate_reports.get(reason)
            if (
                previous is not None
                and previous[:2] == (details["run_id"], generation)
                and now - previous[2] < 1.0
            ):
                return
            self._auto_advance_gate_reports[reason] = details["run_id"], generation, now
            if len(self._auto_advance_gate_reports) > 16:
                oldest = next(iter(self._auto_advance_gate_reports))
                del self._auto_advance_gate_reports[oldest]
        self._report_pipeline_event(
            "auto-advance-withheld",
            generation,
            now,
            reason=reason,
            focused=focused,
            **details,
        )

    def record_prefix_confirmation_wait(
        self,
        reason: str,
        *,
        event_id: str | None,
        target_event_id: str,
        quiet_ms: int | None = None,
    ) -> None:
        with self.state_lock:
            generation = self.active_generation
            details = self._auto_advance_gate_details_locked()
        details.update(
            event_id=event_id,
            target_event_id=target_event_id,
            prefix_confirmation_pending=True,
            prefix_owner_current=event_id == target_event_id,
            render_quiet_ms=quiet_ms,
        )
        self._report_auto_advance_gate(generation, reason, details)

    def _maybe_auto_advance(self) -> None:
        with self.state_lock:
            generation = self.active_generation
            if (
                self.auto_advance is None
                or self.dialog_ready_generation != generation
                or self.pending_auto_advance_generation == generation
                or self.failed_auto_advance_generation == generation
                or self.auto_advance_blocked_generation == generation
                or self.auto_advance_timer is not None
                or self.current_chunk is not None
                or self.speech_futures
                or bool(self.deferred_chunks)
                or self.paused
                or self.suppressed_generation == generation
                or self.observation_uncertain
                or self.stop_event.is_set()
            ):
                details = (
                    self._auto_advance_gate_details_locked()
                    if self.auto_advance is not None
                    else None
                )
                timer = None
            else:
                details = None
                self._auto_advance_timer_serial += 1
                timer = Timer(
                    self.auto_advance_delay_seconds,
                    self._run_auto_advance,
                    args=(generation, self._auto_advance_timer_serial),
                )
                timer.daemon = True
                self.auto_advance_timer = timer
        if timer is None:
            if self.auto_advance is not None:
                self._report_auto_advance_gate(
                    generation, "schedule-precondition", details
                )
            return
        timer.start()

    def _run_auto_advance(
        self, generation: int, timer_serial: int | None = None
    ) -> None:
        reason = None
        with self.state_lock:
            if (
                timer_serial is not None
                and timer_serial != self._auto_advance_timer_serial
            ):
                reason = "stale-auto-advance-timer"
            else:
                self.auto_advance_timer = None
                if (
                    self.auto_advance is None
                    or generation != self.active_generation
                    or self.dialog_ready_generation != generation
                    or self.failed_auto_advance_generation == generation
                    or self.last_auto_advance_dispatched_generation == generation
                    or self.current_chunk is not None
                    or self.speech_futures
                    or bool(self.deferred_chunks)
                    or self.paused
                    or self.suppressed_generation == generation
                    or self.observation_uncertain
                    or self.stop_event.is_set()
                ):
                    reason = "dispatch-precondition"
            details = (
                self._auto_advance_gate_details_locked(timer_serial)
                if reason is not None
                else None
            )
        if reason is not None:
            self._report_auto_advance_gate(generation, reason, details)
            return
        focused = self._is_focused()
        with self.state_lock:
            if (
                (
                    timer_serial is not None
                    and timer_serial != self._auto_advance_timer_serial
                )
                or generation != self.active_generation
                or self.paused
                or self.stop_event.is_set()
            ):
                invalidated = self._auto_advance_gate_details_locked(timer_serial)
            else:
                invalidated = None
        if invalidated is not None:
            self._report_auto_advance_gate(
                generation, "ownership-changed-after-focus", invalidated
            )
            return
        if not focused:
            # A nonmodal voice prompt can own focus exactly when speech ends.
            # Keep a delayed attempt alive so returning to the game cannot
            # strand a ready dialogue forever.
            with self.state_lock:
                report_focus_wait = (
                    self.auto_advance_focus_wait_generation != generation
                )
                self.auto_advance_focus_wait_generation = generation
                focus_details = self._auto_advance_gate_details_locked(timer_serial)
            self._report_auto_advance_gate(
                generation, "game-focus-not-owned", focus_details, focused=False
            )
            if report_focus_wait:
                self._report_auto_advance_state("focus-wait", generation, 0)
            self._maybe_auto_advance()
            return
        try:
            self.render_completion()
        except Exception as error:
            self.report_error(error)
        with self.state_lock:
            routed_frame = self.routed_frame
            latest_frame = self.latest_frame
            frame_version = self.frame_version
            route_epoch = self.frame_route_epoch
            run_id = self.run_id
            same_routed = self.routed_frame_fingerprint == self.latest_frame_fingerprint
        same_fingerprint = same_routed
        comparison_started = monotonic()
        # Keep the original route anchor: accepting successive offsets would drift.
        if (
            self.require_visible_auto_advance
            and not same_routed
            and routed_frame is not None
            and latest_frame is not None
        ):
            try:
                same_routed = self.frame_equivalent(routed_frame, latest_frame)
            except Exception as error:
                self.report_error(error)
        frame_equivalence_ms = (monotonic() - comparison_started) * 1000
        focused = self._is_focused()
        with self.state_lock:
            if (
                (
                    timer_serial is not None
                    and timer_serial != self._auto_advance_timer_serial
                )
                or generation != self.active_generation
                or run_id != self.run_id
                or self.dialog_ready_generation != generation
                or self.failed_auto_advance_generation == generation
                or self.last_auto_advance_dispatched_generation == generation
                or self.current_chunk is not None
                or self.speech_futures
                or bool(self.deferred_chunks)
                or self.suppressed_generation == generation
                or self.observation_uncertain
                or self.paused
                or self.emergency_stopped
                or self.is_shutting_down
                or self.stop_event.is_set()
            ):
                invalidated = self._auto_advance_gate_details_locked(timer_serial)
            else:
                invalidated = None
            if invalidated is None:
                visual_ready = bool(
                    not self.require_visible_auto_advance
                    or (
                        focused
                        and self.latest_frame_visible
                        and same_routed
                        and frame_version == self.frame_version
                        and route_epoch == self.frame_route_epoch
                        and latest_frame is self.latest_frame
                        and routed_frame is self.routed_frame
                        and not self.observation_uncertain
                    )
                )
                report_visual_wait = (
                    not visual_ready
                    and self.auto_advance_visual_wait_generation != generation
                )
                if not visual_ready:
                    self.auto_advance_visual_wait_generation = generation
                visual_details = (
                    self._auto_advance_gate_details_locked(timer_serial)
                    if not visual_ready
                    else None
                )
        if invalidated is not None:
            self._report_auto_advance_gate(
                generation, "ownership-changed-after-render", invalidated
            )
            return
        if not focused:
            self._handle_auto_advance_refusal(generation, "focus-wait")
            return
        if not visual_ready:
            retain_visual_wait_frames(
                routed_frame,
                latest_frame,
                run_id=run_id,
                generation=generation,
                frame_version=frame_version,
            )
            self._report_auto_advance_gate(
                generation,
                "owned-frame-not-visible-and-stable",
                visual_details,
                focused=True,
            )
            if report_visual_wait:
                self._report_auto_advance_state("visual-wait", generation, 0)
            self._maybe_auto_advance()
            return
        try:
            attempt = cast(Callable[[], object], self.auto_advance)()
        except Exception as error:
            self.report_error(error)
            return
        if isinstance(attempt, AutoAdvanceAttempt):
            advanced: object = attempt.dispatched
            refusal_reason = attempt.reason
        else:
            advanced = attempt
            refusal_reason = None
        if advanced is False:
            self._handle_auto_advance_refusal(generation, refusal_reason)
            return
        dispatched = None
        with self.state_lock:
            if generation == self.active_generation:
                self.auto_advance_focus_wait_generation = None
                self.auto_advance_visual_wait_generation = None
                self.auto_advance_attempts = 1
                attempt = 1
                self.last_auto_advance_dispatched_generation = generation
                self._reset_pending_transition(self.latest_frame)
                self.pending_auto_advance_generation = generation
                self.pipeline_metrics = replace(
                    self.pipeline_metrics,
                    last_auto_advance_dispatched_at=monotonic(),
                )
                self._auto_advance_timer_serial += 1
                timer = Timer(
                    self.auto_advance_confirmation_timeout_seconds,
                    self._auto_advance_confirmation_expired,
                    args=(
                        generation,
                        attempt,
                        False,
                        self._auto_advance_timer_serial,
                    ),
                )
                timer.daemon = True
                self.auto_advance_timer = timer
                dispatched = attempt, timer
        if dispatched is not None:
            attempt, timer = dispatched
            self._report_pipeline_event(
                "key-dispatch",
                generation,
                monotonic(),
                attempt=attempt,
                routed_frame_equivalent=bool(same_routed and not same_fingerprint),
                frame_equivalence_ms=frame_equivalence_ms,
            )
            self._report_auto_advance_state("dispatched", generation, attempt)
            timer.start()

    def _handle_auto_advance_refusal(
        self, generation: int, refusal_reason: str | None
    ) -> None:
        if refusal_reason == "reading-control-barrier":
            self._report_auto_advance_gate(generation, "reading-control-barrier")
            # Resume rearms Pause; Clear and Stop retain their own barriers.
            return
        if refusal_reason == "focus-wait" or (
            refusal_reason is None and not self._is_focused()
        ):
            with self.state_lock:
                report_focus_wait = (
                    self.auto_advance_focus_wait_generation != generation
                )
                self.auto_advance_focus_wait_generation = generation
            self._report_auto_advance_gate(
                generation, "game-focus-lost-before-dispatch", focused=False
            )
            if report_focus_wait:
                self._report_auto_advance_state("focus-wait", generation, 0)
            self._maybe_auto_advance()
        elif refusal_reason == "visual-wait":
            with self.state_lock:
                report_visual_wait = (
                    self.auto_advance_visual_wait_generation != generation
                )
                self.auto_advance_visual_wait_generation = generation
            self._report_auto_advance_gate(
                generation, "canonical-full-text-not-confirmed"
            )
            if report_visual_wait:
                self._report_auto_advance_state("visual-wait", generation, 0)
            self._maybe_auto_advance()
        else:
            self._report_auto_advance_gate(
                generation, refusal_reason or "callback-blocked"
            )
            with self.state_lock:
                if generation == self.active_generation:
                    self.failed_auto_advance_generation = generation
            self._report_auto_advance_state("blocked", generation, 0)

    def _auto_advance_confirmation_expired(
        self,
        generation: int,
        attempt: int,
        terminal: bool = False,
        timer_serial: int | None = None,
    ) -> None:
        with self.state_lock:
            if (
                timer_serial is not None
                and timer_serial != self._auto_advance_timer_serial
            ):
                return
            self.auto_advance_timer = None
            if (
                generation != self.active_generation
                or self.pending_auto_advance_generation != generation
                or self.auto_advance_attempts != attempt
                or self.stop_event.is_set()
            ):
                return
            paused = self.paused
        focused = False if paused else self._is_focused()
        with self.state_lock:
            if (
                (
                    timer_serial is not None
                    and timer_serial != self._auto_advance_timer_serial
                )
                or generation != self.active_generation
                or self.pending_auto_advance_generation != generation
                or self.auto_advance_attempts != attempt
                or self.stop_event.is_set()
            ):
                return
            paused = self.paused
        if paused or not focused:
            self._schedule_auto_advance_confirmation(
                generation,
                attempt,
                terminal=terminal,
            )
            return
        with self.state_lock:
            visual_transition = self.pending_transition_seen
            typing = self.pending_transition_typing
            progress_at = self.pending_transition_progress_at
        progress_recent = (
            progress_at is None
            or monotonic() - progress_at < self.auto_advance_terminal_timeout_seconds
        )
        if visual_transition and progress_recent:
            self._report_auto_advance_state(
                "typing" if typing else "recognition-wait", generation, attempt
            )
            self._schedule_auto_advance_confirmation(generation, attempt, terminal=True)
            return
        if not terminal:
            self._report_auto_advance_state("waiting", generation, attempt)
            self._schedule_auto_advance_confirmation(
                generation,
                attempt,
                delay_seconds=(
                    self.auto_advance_terminal_timeout_seconds
                    - self.auto_advance_confirmation_timeout_seconds
                ),
                terminal=True,
            )
            return
        with self.state_lock:
            if (
                generation != self.active_generation
                or self.pending_auto_advance_generation != generation
                or self.auto_advance_attempts != attempt
            ):
                return
            self.failed_auto_advance_generation = generation
            self.pending_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
        self._report_pipeline_event(
            "auto-advance-timeout",
            generation,
            monotonic(),
            attempt=attempt,
        )
        self._report_auto_advance_state(
            "successor-unavailable" if visual_transition else "failed",
            generation,
            attempt,
        )

    def _observe_pending_transition_text(
        self, character: str | None, text: str
    ) -> None:
        with self.state_lock:
            if self.pending_auto_advance_generation != self.active_generation:
                return
            if not text:
                return
            if (character, text) == self.last_observation:
                if self.pending_transition_text == "":
                    self.pending_transition_seen = False
                return
            previous = self.pending_transition_text
            self.pending_transition_typing = bool(
                previous and text.startswith(previous) and len(text) > len(previous)
            )
            if text != previous:
                self.pending_transition_progress_at = monotonic()
            self.pending_transition_text = text
            self.pending_transition_seen = True

    def _schedule_auto_advance_confirmation(
        self,
        generation: int,
        attempt: int,
        *,
        delay_seconds: float | None = None,
        terminal: bool = False,
    ) -> None:
        with self.state_lock:
            if (
                generation != self.active_generation
                or self.pending_auto_advance_generation != generation
                or self.auto_advance_timer is not None
                or self.paused
                or self.stop_event.is_set()
            ):
                return
            self._auto_advance_timer_serial += 1
            timer = Timer(
                (
                    self.auto_advance_confirmation_timeout_seconds
                    if delay_seconds is None
                    else delay_seconds
                ),
                self._auto_advance_confirmation_expired,
                args=(generation, attempt, terminal, self._auto_advance_timer_serial),
            )
            timer.daemon = True
            self.auto_advance_timer = timer
        timer.start()

    def _resume_auto_advance_confirmation(self) -> None:
        with self.state_lock:
            generation = self.pending_auto_advance_generation
            attempt = self.auto_advance_attempts
        if generation is not None and attempt:
            self._schedule_auto_advance_confirmation(generation, attempt)

    def _report_auto_advance_state(
        self, state: str, generation: int, attempt: int
    ) -> None:
        try:
            self.auto_advance_state_changed(state, generation, attempt)
        except Exception as error:
            self.report_error(error)

    def _report_pipeline_event(
        self,
        stage: str,
        generation: int,
        occurred_at: float | None = None,
        **details: object,
    ) -> None:
        context = audio_lifecycle_context.get() or {}
        if not context:
            chunk = self.last_spoken_chunk
            if (
                chunk is not None
                and chunk.generation == generation
                and chunk.run_id == self.run_id
                and stage
                in {
                    "key-dispatch",
                    "confirmed-next-dialogue",
                    "auto-advance-withheld",
                    "auto-advance-timeout",
                    "reading-control",
                }
            ):
                context = self._chunk_context(chunk)
        details = {
            "run_id": self.run_id,
            "run_started_at": self.run_started_at,
            **context,
            **details,
        }
        details.pop("generation", None)
        try:
            self.pipeline_event_handler(
                stage,
                generation,
                monotonic() if occurred_at is None else occurred_at,
                **details,
            )
        except Exception as error:
            try:
                self.report_error(error)
            except Exception:
                # Diagnostic sinks must not change reading or mask its failure.
                pass

    def _cancel_auto_advance_locked(self) -> None:
        timer = self.auto_advance_timer
        self.auto_advance_timer = None
        self._auto_advance_timer_serial += 1
        if timer is not None:
            timer.cancel()

    def _record_speech_metrics_locked(
        self,
        *,
        sentence_ready: bool = False,
        synthesis: bool = False,
        playback: bool = False,
        text_visible: bool = False,
        generation_started: bool = False,
        first_pcm: bool = False,
        playback_started: bool = False,
        playback_completed: bool = False,
    ) -> None:
        now = monotonic()
        depth = len(self.speech_futures) + len(self.deferred_chunks)
        metrics = self.pipeline_metrics
        self.pipeline_metrics = replace(
            metrics,
            speech_queue_depth=depth,
            max_speech_queue_depth=max(metrics.max_speech_queue_depth, depth),
            last_sentence_ready_at=(
                now if sentence_ready else metrics.last_sentence_ready_at
            ),
            last_synthesis_at=now if synthesis else metrics.last_synthesis_at,
            last_playback_at=now if playback else metrics.last_playback_at,
            last_text_visible_at=(
                (
                    metrics.last_ocr_capture_at
                    if metrics.last_ocr_capture_at is not None
                    else now
                )
                if text_visible
                else metrics.last_text_visible_at
            ),
            last_speaker_resolved_at=(
                now if text_visible else metrics.last_speaker_resolved_at
            ),
            last_ocr_stable_at=(now if sentence_ready else metrics.last_ocr_stable_at),
            last_generation_started_at=(
                now if generation_started else metrics.last_generation_started_at
            ),
            last_first_pcm_at=now if first_pcm else metrics.last_first_pcm_at,
            last_first_pcm_generation=(
                self.active_generation
                if first_pcm
                else metrics.last_first_pcm_generation
            ),
            last_playback_started_at=(
                now if playback_started else metrics.last_playback_started_at
            ),
            last_playback_completed_at=(
                now if playback_completed else metrics.last_playback_completed_at
            ),
        )

    def record_first_pcm(
        self,
        timestamp: float | None = None,
        *,
        provenance: str = "measured",
        chunk: SpeechChunk | None = None,
    ) -> None:
        occurred_at = monotonic() if timestamp is None else timestamp
        with self.state_lock:
            previous = self.pipeline_metrics
            chunk = chunk or self.current_chunk
            generation = (
                chunk.generation if chunk is not None else self.active_generation
            )
            if provenance == "measured" and generation == self.active_generation:
                self.pipeline_metrics = replace(
                    previous,
                    last_first_pcm_at=occurred_at,
                    last_first_pcm_generation=generation,
                )
            origins = self.current_chunk_pipeline_origins or {
                "from_text_visible_ms": previous.last_text_visible_at,
                "from_ocr_stable_ms": previous.last_ocr_stable_at,
                "from_generation_started_ms": previous.last_generation_started_at,
                "from_playback_started_ms": previous.last_playback_started_at,
            }
            origins = dict(origins)
            if previous.last_canonical_full_text_generation == generation:
                origins["from_canonical_full_text_ms"] = (
                    previous.last_canonical_full_text_at
                )
            canonical_full_at = (
                previous.last_canonical_full_text_at
                if previous.last_canonical_full_text_generation == generation
                else None
            )
        details = (
            {
                "run_id": chunk.run_id or self.run_id,
                "playback_id": chunk.playback_id,
                "chunk_id": chunk.chunk_id,
                "chunk_ordinal": chunk.ordinal,
                "chunk_characters": len(chunk.text),
            }
            if chunk is not None
            else {}
        )
        for name, origin in origins.items():
            if origin is not None and origin <= occurred_at:
                details[name] = round((occurred_at - origin) * 1000)
        self._report_pipeline_event(
            "first-pcm" if provenance == "measured" else "audio-onset-estimate",
            generation,
            occurred_at,
            timing_provenance=provenance,
            measurement="first-pcm-write-returned"
            if provenance == "measured"
            else "backend-offset-from-controller-dispatch",
            **details,
        )
        if (
            provenance == "measured"
            and canonical_full_at is not None
            and canonical_full_at > occurred_at
        ):
            self._report_pipeline_event(
                "canonical-full-text",
                generation,
                canonical_full_at,
                first_pcm_before_canonical_full_ms=round(
                    (canonical_full_at - occurred_at) * 1000
                ),
            )

    def record_canonical_full_text(
        self,
        *,
        line_id: str | None = None,
        timestamp: float | None = None,
        reason: str | None = None,
        settled_ms: int | None = None,
    ) -> bool:
        """Record full-text confirmation after an early canonical prefix route."""
        occurred_at = monotonic() if timestamp is None else timestamp
        with self.state_lock:
            generation = self.active_generation
            if generation < 1:
                return False
            previous = self.pipeline_metrics
            self.pipeline_metrics = replace(
                previous,
                last_canonical_full_text_at=occurred_at,
                last_canonical_full_text_generation=generation,
            )
            first_pcm_at = (
                previous.last_first_pcm_at
                if previous.last_first_pcm_generation == generation
                else None
            )
        details: dict[str, object] = {"line_id": line_id} if line_id is not None else {}
        if reason is not None:
            details["reason"] = reason
        if settled_ms is not None:
            details["settled_ms"] = settled_ms
        if first_pcm_at is not None and first_pcm_at <= occurred_at:
            details["first_pcm_before_canonical_full_ms"] = round(
                (occurred_at - first_pcm_at) * 1000
            )
        self._report_pipeline_event(
            "canonical-full-text",
            generation,
            occurred_at,
            **details,
        )
        return True

    def _defer_chunk_locked(self, chunk: SpeechChunk) -> None:
        if not any(
            pending.playback_id == chunk.playback_id for pending in self.deferred_chunks
        ):
            self.deferred_chunks.append(chunk)
            admission_order = {
                playback_id: index
                for index, playback_id in enumerate(self.accepted_chunks)
            }
            self.deferred_chunks = deque(
                sorted(
                    self.deferred_chunks,
                    key=lambda pending: admission_order[pending.playback_id],
                )
            )

    def _schedule_deferred_if_possible(self) -> None:
        callbacks = []
        rejected = []
        with self.pause_condition:
            while (
                self.deferred_chunks
                and not self.paused
                and len(self.speech_futures) < self.max_speech_jobs
            ):
                chunk = self.deferred_chunks.popleft()
                if (
                    chunk.playback_id in self.scheduled_playback_ids
                    or chunk.playback_id in self.cancelled_chunk_ids
                    or not self._owns_chunk(chunk)
                    or self.suppressed_generation == chunk.generation
                    or self.emergency_stopped
                    or self.is_shutting_down
                ):
                    continue
                self.scheduled_playback_ids.add(chunk.playback_id)
                try:
                    future = self.speech_executor.submit(
                        self._prepare_if_current, chunk
                    )
                except Exception as error:
                    self.completed_playback_ids.add(chunk.playback_id)
                    rejected.append((chunk, error))
                else:
                    self.speech_futures[future] = chunk
                    callbacks.append(future)
                self._record_speech_metrics_locked()
        for future in callbacks:
            future.add_done_callback(self._preparation_finished)
        for chunk, rejected_error in rejected:
            self.reject_chunk(chunk)
            self.report_error(rejected_error)
