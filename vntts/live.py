from collections.abc import Callable, Iterable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from threading import Condition, Event, RLock, Timer
from time import monotonic
from typing import Literal, ParamSpec, Protocol, TypeAlias, TypeVar

from vntts.live_tracking import IncrementalDialogTracker, SpeechChunk
from vntts.live_tracking import TrackerProbe as TrackerProbe
from vntts.live_tracking import TrackerResolver as TrackerResolver

Frame: TypeAlias = object
FrameFingerprint: TypeAlias = object
Observation: TypeAlias = tuple[str | None, str]
_P = ParamSpec("_P")
_R = TypeVar("_R")


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


class AdaptiveSpeechBackpressure:
    """Temporarily serialize speech after an output underrun."""

    def __init__(
        self,
        *,
        normal_jobs: int = 2,
        cooldown_seconds: float = 10.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if normal_jobs < 1:
            raise ValueError("normal_jobs must be positive")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self.normal_jobs = int(normal_jobs)
        self.cooldown_seconds = float(cooldown_seconds)
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
        if base_interval <= 0:
            raise ValueError("base_interval must be positive")
        if unchanged_frames < 1:
            raise ValueError("unchanged_frames must be positive")
        self.base_interval = base_interval
        self.fast_interval = fast_interval or max(0.05, base_interval / 2)
        self.idle_interval = idle_interval or min(1.5, base_interval * 3)
        self.unfocused_interval = unfocused_interval or min(0.5, base_interval * 2.5)
        self.unchanged_frames = unchanged_frames
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
        recognize_frame: Callable[[Frame], Observation],
        prepare_chunk: Callable[[SpeechChunk], object],
        play_prepared: Callable[[SpeechChunk, object], object],
        report_error: Callable[[Exception], object],
        frame_fingerprint: Callable[[Frame], FrameFingerprint] | None = None,
        frame_render_fingerprint: Callable[[Frame], FrameFingerprint] | None = None,
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
        pipeline_event_handler: Callable[..., None] | None = None,
        max_speech_jobs: int = 2,
        interrupt_on_dialog_replacement: bool = False,
        first_pcm_on_prepare: bool = True,
    ) -> None:
        self.capture_executor = capture_executor
        self.speech_executor = speech_executor
        self.ocr_executor = ocr_executor
        self.playback_executor = playback_executor
        self.capture_frame = capture_frame
        self.recognize_frame = recognize_frame
        self.frame_fingerprint = frame_fingerprint or (lambda _frame: None)
        # None means the identity fingerprint is also sufficient for render
        # activity. Keep that as a sentinel so capture does not compute an
        # expensive fingerprint twice for replay readers.
        self.frame_render_fingerprint = frame_render_fingerprint
        self.frame_presence = frame_presence or (lambda _frame: True)
        self.frame_completion = frame_completion or (lambda _frame: False)
        self.frame_recheck_required = frame_recheck_required or (lambda: False)
        self.ocr_purpose = ocr_purpose or (lambda: "legacy")
        if frame_recheck_interval_seconds <= 0:
            raise ValueError("frame_recheck_interval_seconds must be positive")
        self.frame_recheck_interval_seconds = float(frame_recheck_interval_seconds)
        self.render_completion = render_completion or (lambda: False)
        if render_quiet_minimum_seconds <= 0:
            raise ValueError("render_quiet_minimum_seconds must be positive")
        self.render_quiet_minimum_seconds = float(render_quiet_minimum_seconds)
        self.stable_frame_route = stable_frame_route
        self.stable_frame_owner = stable_frame_owner or (lambda: None)
        self.frame_routed = frame_routed or (
            lambda _frame, _fingerprint, _route_kind, _character, _text: None
        )
        self.frame_observed = frame_observed or (
            lambda _frame, _fingerprint, _observation_kind: None
        )
        self.line_id_resolver = line_id_resolver or (lambda _character, _text: None)
        if stable_frame_minimum_seconds < 0:
            raise ValueError("stable_frame_minimum_seconds must not be negative")
        self.stable_frame_minimum_seconds = float(stable_frame_minimum_seconds)
        self.stable_frame_clock = stable_frame_clock
        self.prepare_chunk = prepare_chunk
        self.play_prepared = play_prepared
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
        if auto_advance_confirmation_timeout_seconds <= 0:
            raise ValueError(
                "auto_advance_confirmation_timeout_seconds must be positive"
            )
        self.auto_advance_confirmation_timeout_seconds = float(
            auto_advance_confirmation_timeout_seconds
        )
        if (
            auto_advance_terminal_timeout_seconds
            <= auto_advance_confirmation_timeout_seconds
        ):
            raise ValueError(
                "auto_advance_terminal_timeout_seconds must be greater than "
                "auto_advance_confirmation_timeout_seconds"
            )
        self.auto_advance_terminal_timeout_seconds = float(
            auto_advance_terminal_timeout_seconds
        )
        self.auto_advance_state_changed = auto_advance_state_changed or (
            lambda _state, _generation, _attempt: None
        )
        self.pipeline_event_handler = pipeline_event_handler or (
            lambda _stage, _generation, _occurred_at, **_details: None
        )
        if max_speech_jobs < 1:
            raise ValueError("max_speech_jobs must be positive")
        self.max_speech_jobs = max_speech_jobs
        self.interrupt_on_dialog_replacement = bool(interrupt_on_dialog_replacement)
        self.first_pcm_on_prepare = bool(first_pcm_on_prepare)
        self.state_lock = RLock()
        self.pause_condition = Condition(self.state_lock)
        self.stop_event = Event()
        self.capture_future: Future[object] | None = None
        self.ocr_future: Future[object] | None = None
        self.active_generation = 0
        self.suppressed_generation: int | None = None
        self.speech_futures: dict[
            Future[object | None] | Future[None], SpeechChunk
        ] = {}
        self.paused_chunks: list[SpeechChunk] = []
        self.deferred_chunk: SpeechChunk | None = None
        self.current_chunk: SpeechChunk | None = None
        self.current_chunk_pipeline_origins: dict[str, float | None] | None = None
        self.last_spoken_chunk: SpeechChunk | None = None
        self.cancelled_chunk_ids: set[int] = set()
        self.prepared_chunk_ids: set[str] = set()
        self.sealed_generation: int | None = None
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
        self.focus_probe_failed = False
        self.latest_frame: Frame | None = None
        self.latest_frame_fingerprint: FrameFingerprint | None = None
        self.latest_frame_visible = False
        self.latest_frame_complete = False
        self.latest_render_fingerprint: FrameFingerprint | None = None
        self.latest_render_owner: object | None = None
        self.latest_render_changed_at: float | None = None
        self.routed_frame_fingerprint: FrameFingerprint | None = None
        self.frame_route_epoch = 0
        self.candidate_frame_fingerprint = object()
        self.candidate_frame_count = 0
        self.candidate_frame_owner: object | None = None
        self.candidate_frame_started_at: float | None = None
        self.frame_version = 0
        self.processed_frame_version = 0
        self.next_capture_interval = interval_seconds
        self.pipeline_metrics = LivePipelineMetrics()

    @property
    def is_running(self) -> bool:
        with self.state_lock:
            return self.capture_future is not None and not self.capture_future.done()

    def runtime_control_snapshot(self) -> dict[str, bool]:
        """Return the lock-consistent playback facts used by all UI transports."""
        with self.state_lock:
            active_futures = any(not future.done() for future in self.speech_futures)
            queued = bool(
                active_futures or self.paused_chunks or self.deferred_chunk is not None
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
        with self.state_lock:
            if self.capture_future is not None and not self.capture_future.done():
                return False
            restarting = self.capture_future is not None
        self.clear_queue()
        if restarting:
            try:
                self.wait(timeout_seconds=5.0)
            except FutureTimeoutError as error:
                self.report_error(error)
                return False
        with self.state_lock:
            self.emergency_stopped = False
            self.stop_event = Event()
            self.active_generation = 0
            self.suppressed_generation = None
            self.last_observation = None
            self.last_accepted_observation = None
            self.deferred_observation = None
            self.prepared_chunk_ids.clear()
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
            self.latest_frame = None
            self.latest_frame_fingerprint = None
            self.latest_frame_visible = False
            self.latest_frame_complete = False
            self.latest_render_fingerprint = None
            self.latest_render_owner = None
            self.latest_render_changed_at = None
            self.routed_frame_fingerprint = None
            self.frame_route_epoch += 1
            self._reset_stable_frame_candidate_locked()
            self.frame_version = 0
            self.processed_frame_version = 0
            self.next_capture_interval = self.interval_seconds
            self.pipeline_metrics = LivePipelineMetrics()
            self.ocr_future = self.ocr_executor.submit(
                self._run_ocr,
                self.stop_event,
            )
            self.capture_future = self.capture_executor.submit(
                self._run_capture,
                self.stop_event,
            )
        return True

    def stop(self) -> bool:
        with self.state_lock:
            if self.capture_future is None or self.capture_future.done():
                return False
            self.stop_event.set()
            self._cancel_auto_advance_locked()
            self.pending_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
            self.pause_condition.notify_all()
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
        chunks_to_resume = []
        current_chunk = None
        with self.pause_condition:
            if self.paused:
                self.paused = False
                chunks_to_resume = self.paused_chunks
                self.paused_chunks = []
                self.pause_condition.notify_all()
            else:
                self.paused = True
                self._cancel_auto_advance_locked()
                if self.current_chunk is not None:
                    current_chunk = self.current_chunk
                for future, chunk in tuple(self.speech_futures.items()):
                    if future.running() and chunk is self.current_chunk:
                        continue
                    if future.cancel():
                        self.paused_chunks.append(chunk)
        if current_chunk is not None and self._interrupt_speech():
            with self.pause_condition:
                if (
                    self.paused
                    and current_chunk.generation == self.active_generation
                    and self.suppressed_generation != current_chunk.generation
                ):
                    self.paused_chunks.insert(0, current_chunk)
        if chunks_to_resume:
            self._schedule(chunks_to_resume)
        self._schedule_deferred_if_possible()
        if not self.paused:
            self._resume_auto_advance_confirmation()
            self._maybe_auto_advance()
        return self.paused

    def enqueue(self, character: str, text: str, *, line_id: str | None = None) -> bool:
        with self.state_lock:
            generation = self.active_generation + 1
        self._set_generation(generation)
        self._schedule([SpeechChunk(generation, character, text, line_id=line_id)])
        return True

    def bind_current_frame_route(self) -> bool:
        """Bind explicit cursor recovery to the latest captured dialogue frame."""
        with self.state_lock:
            fingerprint = self.latest_frame_fingerprint
            if fingerprint is None:
                return False
            self._accept_routed_frame_locked(fingerprint)
        return True

    def frame_route_epoch_is_current(self, epoch: int) -> bool:
        with self.state_lock:
            return epoch == self.frame_route_epoch

    def skip_current(self) -> bool:
        with self.state_lock:
            has_current_speech = self.current_chunk is not None
            if has_current_speech:
                self.cancelled_chunk_ids.add(id(self.current_chunk))
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

    def clear_queue(self) -> bool:
        with self.pause_condition:
            self.suppressed_generation = self.active_generation
            futures = tuple(self.speech_futures)
            had_paused_chunks = bool(self.paused_chunks)
            had_deferred_chunk = self.deferred_chunk is not None
            self.paused_chunks = []
            self.deferred_chunk = None
            has_current_speech = self.current_chunk is not None
            self.pause_condition.notify_all()
            self._cancel_auto_advance_locked()
            self.pending_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_blocked_generation = None
            self.auto_advance_block_reason = None
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
        for future in futures:
            future.cancel()
        # A preparation future may already be running before it becomes
        # ``current_chunk``. Future.cancel() cannot stop that work, so notify
        # the backend as well; otherwise application shutdown can wait forever
        # for the speech executor after the user presses Quit.
        if has_current_speech or futures:
            self._interrupt_speech()
        return (
            has_current_speech
            or bool(futures)
            or had_paused_chunks
            or had_deferred_chunk
        )

    def emergency_stop(self) -> bool:
        with self.pause_condition:
            was_running = (
                self.capture_future is not None and not self.capture_future.done()
            )
            self.emergency_stopped = True
            self.stop_event.set()
            self._cancel_auto_advance_locked()
            self.pending_auto_advance_generation = None
            self.auto_advance_attempts = 0
            self.auto_advance_focus_wait_generation = None
            self.auto_advance_visual_wait_generation = None
            self.pause_condition.notify_all()
        cleared = self.clear_queue()
        self.release_waiters()
        return was_running or cleared

    def resume_after_emergency(self) -> bool:
        with self.state_lock:
            was_stopped = self.emergency_stopped
            self.emergency_stopped = False
        return was_stopped

    def release_waiters(self) -> None:
        with self.pause_condition:
            self.paused = False
            self.pause_condition.notify_all()

    def wait_until_playable(self, chunk: SpeechChunk) -> bool:
        with self.pause_condition:
            finish_active_playback = bool(
                self.current_chunk is chunk and not self.interrupt_on_dialog_replacement
            )
            while (
                self.paused
                and (
                    chunk.generation == self.active_generation or finish_active_playback
                )
                and self.suppressed_generation != chunk.generation
            ):
                self.pause_condition.wait()
            return (
                (chunk.generation == self.active_generation or finish_active_playback)
                and (
                    self.sealed_generation != chunk.generation
                    or finish_active_playback
                    or chunk.explicit_replay
                )
                and self.suppressed_generation != chunk.generation
                and id(chunk) not in self.cancelled_chunk_ids
            )

    def seal_generation(self, generation: int) -> bool:
        """Suppress OCR suffix chunks after an exact full-line route completed."""
        stale_futures = []
        with self.pause_condition:
            if generation != self.active_generation:
                return False
            self.sealed_generation = generation
            stale_futures = [
                future
                for future, chunk in self.speech_futures.items()
                if chunk is not self.current_chunk and chunk.generation == generation
            ]
            self.paused_chunks = [
                chunk for chunk in self.paused_chunks if chunk.generation != generation
            ]
            if (
                self.deferred_chunk is not None
                and self.deferred_chunk.generation == generation
            ):
                self.deferred_chunk = None
            self.pause_condition.notify_all()
        for future in stale_futures:
            future.cancel()
        return True

    def wait(self, *, timeout_seconds: float | None = None) -> None:
        deadline = (
            None
            if timeout_seconds is None
            else monotonic() + max(0.0, float(timeout_seconds))
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
            future.result(timeout=remaining)

        with self.state_lock:
            capture_future = self.capture_future
            ocr_future = self.ocr_future
        if capture_future is not None:
            wait_for(capture_future)
        if ocr_future is not None:
            wait_for(ocr_future)
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

    def _run_capture(self, stop_event: Event) -> None:
        policy = self.adaptive_policy_factory(
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        while not stop_event.is_set():
            focused = self._is_focused()
            if not focused:
                with self.state_lock:
                    self.latest_render_fingerprint = None
                    self.latest_render_owner = None
                    self.latest_render_changed_at = None
                interval = policy.observe(None, None, focused=False)
                self.capture_state_changed(False, interval)
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
                with self.pause_condition:
                    fingerprint_changed = fingerprint != self.latest_frame_fingerprint
                    replaced = self.frame_version > self.processed_frame_version
                    self.latest_frame = frame
                    self.latest_frame_fingerprint = fingerprint
                    self.latest_frame_visible = visible
                    self.latest_frame_complete = complete
                    if not visible:
                        self.latest_render_fingerprint = None
                        self.latest_render_owner = None
                        self.latest_render_changed_at = None
                    elif (
                        render_fingerprint != self.latest_render_fingerprint
                        or render_owner != self.latest_render_owner
                    ):
                        self.latest_render_fingerprint = render_fingerprint
                        self.latest_render_owner = render_owner
                        self.latest_render_changed_at = captured_at
                    self.frame_version += 1
                    metrics = self.pipeline_metrics
                    self.pipeline_metrics = replace(
                        metrics,
                        captured_frames=metrics.captured_frames + 1,
                        replaced_frames=metrics.replaced_frames + int(replaced),
                        last_capture_at=monotonic(),
                    )
                    interval = self.next_capture_interval
                    if fingerprint_changed or recheck_required:
                        # The OCR worker updates the adaptive interval after it
                        # consumes this frame. Do not sleep once more on the
                        # previous static-dialogue interval before giving the
                        # stability gate its confirming frame. The same fast
                        # cadence must stay active while early canonical audio
                        # is waiting for the typewriter render to finish;
                        # otherwise the render-quiet fallback can expire before
                        # capture ever sees the next visible prefix.
                        interval = min(interval, policy.fast_interval)
                    self.pause_condition.notify_all()
            except Exception as error:
                self.report_error(error)
                interval = self.interval_seconds
            self.capture_state_changed(True, interval)
            stop_event.wait(interval)

    def _run_ocr(self, stop_event: Event) -> None:
        tracker = self.tracker_factory(**self.tracker_options)
        policy = self.adaptive_policy_factory(
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        cached_fingerprint = object()
        cached_completion = object()
        cached_observation: Observation = (None, "")
        last_frame_recheck_at = None
        while True:
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
                visible = self.latest_frame_visible
                complete = self.latest_frame_complete
                self.processed_frame_version = self.frame_version
            try:
                recheck_required = bool(self.frame_recheck_required())
                focused = self._is_focused() if recheck_required else False
                recheck_now = False
                if recheck_required and visible and focused:
                    now = self.stable_frame_clock()
                    recheck_now = bool(
                        last_frame_recheck_at is None
                        or now - last_frame_recheck_at
                        >= self.frame_recheck_interval_seconds
                    )
                    if recheck_now:
                        last_frame_recheck_at = now
                        self._report_pipeline_event(
                            "canonical-prefix-visual-recheck",
                            self.active_generation,
                            fingerprint=self._privacy_safe_fingerprint(fingerprint),
                            visible=True,
                            focused=True,
                            owner=self.stable_frame_owner(),
                            recheck_interval_ms=round(
                                self.frame_recheck_interval_seconds * 1000
                            ),
                        )
                else:
                    last_frame_recheck_at = None
                with self.state_lock:
                    awaiting_post_advance_dialog = (
                        self.pending_auto_advance_generation == self.active_generation
                    )
                if (
                    fingerprint == cached_fingerprint
                    and complete == cached_completion
                    and not awaiting_post_advance_dialog
                    and not recheck_now
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
                    if self.ocr_purpose() is None:
                        self.frame_observed(
                            frame,
                            fingerprint,
                            "locked-visual-only",
                        )
                    character, text = cached_observation
                    interval = policy.observe(character, text, focused=True)
                    with self.state_lock:
                        self.next_capture_interval = interval
                    continue
                if isinstance(frame_route, SilentDialogRoute):
                    character, text = None, ""
                    route_kind = "canonical"
                    cached_fingerprint = fingerprint
                    cached_completion = complete
                    cached_observation = (character, text)
                elif isinstance(frame_route, CanonicalDialogRoute):
                    character, text = frame_route.character, frame_route.text
                    route_kind = "canonical"
                    cached_fingerprint = fingerprint
                    cached_completion = complete
                    cached_observation = (character, text)
                elif isinstance(frame_route, tuple) and len(frame_route) == 2:
                    character, text = frame_route
                    route_kind = "canonical"
                    cached_fingerprint = fingerprint
                    cached_completion = complete
                    cached_observation = (character, text)
                elif frame_route is None and (
                    fingerprint != cached_fingerprint
                    or awaiting_post_advance_dialog
                    or recheck_now
                ):
                    if self.ocr_purpose() is None:
                        self.frame_observed(
                            frame,
                            fingerprint,
                            "locked-visual-only",
                        )
                        character, text = cached_observation
                        interval = policy.observe(character, text, focused=True)
                        with self.state_lock:
                            self.next_capture_interval = interval
                        continue
                    character, text = self.recognize_frame(frame)
                    route_kind = "ocr"
                    cached_fingerprint = fingerprint
                    cached_completion = complete
                    cached_observation = (character, text)
                    with self.state_lock:
                        metrics = self.pipeline_metrics
                        now = monotonic()
                        self.pipeline_metrics = replace(
                            metrics,
                            recognized_frames=metrics.recognized_frames + 1,
                            last_ocr_at=now,
                            last_speaker_resolved_at=now,
                        )
                elif frame_route is not None:
                    raise TypeError(
                        "stable_frame_route must return None, False or "
                        "a SilentDialogRoute/CanonicalDialogRoute/"
                        "(character, text) route"
                    )
                routed_observation = (
                    frame_route
                    if isinstance(
                        frame_route,
                        (SilentDialogRoute, CanonicalDialogRoute),
                    )
                    else self._report_observation(character, text)
                )
                if routed_observation is None:
                    interval = policy.observe(character, text, focused=True)
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
                with self.state_lock:
                    frame_already_routed = fingerprint == self.routed_frame_fingerprint
                if route_kind != "cached" and not frame_already_routed:
                    self.frame_routed(
                        frame,
                        fingerprint,
                        route_kind,
                        character,
                        text,
                    )
                if self.stable_frame_route is not None:
                    self._accept_routed_frame(fingerprint)
                if silent_route is not None:
                    tracker.observe_silent(silent_route.event_id)
                    chunks = []
                else:
                    line_id = self.line_id_resolver(character, text)
                    if line_id is None:
                        expect_new_dialog = getattr(tracker, "expect_new_dialog", None)
                        if callable(expect_new_dialog) and awaiting_post_advance_dialog:
                            expect_new_dialog()
                        tracker_character = (
                            "Narrator" if character is None else character
                        )
                        chunks = tracker.observe(tracker_character, text)
                    else:
                        assert character is not None
                        chunks = tracker.observe_canonical(character, text, line_id)
                self._set_generation(tracker.generation)
                self._schedule(chunks)
                self._update_dialog_ready(tracker)
                interval = policy.observe(character, text, focused=True)
                with self.state_lock:
                    self.next_capture_interval = interval
            except Exception as error:
                self.report_error(error)
                with self.state_lock:
                    self.next_capture_interval = self.interval_seconds

        self._set_generation(tracker.generation)
        self._schedule(tracker.flush())
        self._update_dialog_ready(tracker)

    def _stable_frame_route_decision(
        self,
        fingerprint: FrameFingerprint,
        visible: bool = True,
        complete: bool = False,
    ) -> StableFrameRoute:
        if self.stable_frame_route is None:
            return None
        focused = self._is_focused()
        if not visible or not focused:
            with self.state_lock:
                self._reset_stable_frame_candidate_locked()
            self._report_pipeline_event(
                "stable-frame-gate",
                self.active_generation,
                fingerprint=self._privacy_safe_fingerprint(fingerprint),
                visible=bool(visible),
                focused=focused,
                owner=self.stable_frame_owner(),
                completion_cue=bool(complete),
                candidate_frames=0,
                settled_ms=0,
                ready=False,
            )
            return False
        owner = self.stable_frame_owner()
        now = self.stable_frame_clock()
        with self.state_lock:
            if self.routed_frame_fingerprint is None:
                return None
            if fingerprint == self.routed_frame_fingerprint:
                return None
            route_epoch = self.frame_route_epoch
            if (
                fingerprint == self.candidate_frame_fingerprint
                and owner == self.candidate_frame_owner
            ):
                self.candidate_frame_count += 1
            else:
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
        self._report_pipeline_event(
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
        route = self.stable_frame_route(
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
            return fingerprint.hex()[:16]
        return str(fingerprint)[:64]

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

    def _accept_routed_frame_locked(self, fingerprint: FrameFingerprint) -> None:
        self.routed_frame_fingerprint = fingerprint
        self.frame_route_epoch += 1
        self._reset_stable_frame_candidate_locked()

    def _reset_stable_frame_candidate_locked(self) -> None:
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
        stale_futures = []
        interrupt_current = False
        confirmed_advance = None
        with self.pause_condition:
            previous_generation = self.active_generation
            changed = generation != self.active_generation
            self.active_generation = generation
            if self.suppressed_generation != generation:
                self.suppressed_generation = None
            if changed:
                self.prepared_chunk_ids.clear()
                self.sealed_generation = None
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
                        self.pipeline_metrics,
                        last_auto_advance_at=monotonic(),
                    )
                interrupt_current = bool(
                    self.interrupt_on_dialog_replacement
                    and self.current_chunk is not None
                )
                if interrupt_current:
                    self.cancelled_chunk_ids.add(id(self.current_chunk))
                self.dialog_ready_generation = None
                self.auto_advance_blocked_generation = None
                self.auto_advance_block_reason = None
                self._cancel_auto_advance_locked()
                stale_futures = [
                    future
                    for future, chunk in self.speech_futures.items()
                    if chunk is not self.current_chunk
                    and chunk.generation != generation
                ]
                self.paused_chunks = [
                    chunk
                    for chunk in self.paused_chunks
                    if chunk.generation == generation
                ]
                if (
                    self.deferred_chunk is not None
                    and self.deferred_chunk.generation != generation
                ):
                    self.deferred_chunk = None
                self.pause_condition.notify_all()
            metrics = self.pipeline_metrics
        # Most backends finish active playback to avoid clicks. Streaming
        # backends that cooperatively cancel generation opt into interruption.
        for future in stale_futures:
            future.cancel()
        if interrupt_current:
            self._interrupt_speech()
        if changed and generation > 0:
            for stage, occurred_at in (
                ("capture", metrics.last_capture_at),
                ("ocr", metrics.last_ocr_at),
                ("stable-text", monotonic()),
            ):
                if occurred_at is not None:
                    self._report_pipeline_event(stage, generation, occurred_at)
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

    def _schedule(self, chunks: Iterable[SpeechChunk]) -> None:
        for chunk in chunks:
            with self.pause_condition:
                if self.emergency_stopped:
                    continue
                if (
                    self.sealed_generation == chunk.generation
                    and not chunk.explicit_replay
                ):
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
                    self.paused_chunks.append(chunk)
                    continue
                if len(self.speech_futures) >= self.max_speech_jobs:
                    self._defer_chunk_locked(chunk)
                    self._record_speech_metrics_locked(sentence_ready=True)
                    continue
            future = self.speech_executor.submit(self._prepare_if_current, chunk)
            with self.state_lock:
                self.speech_futures[future] = chunk
                self._record_speech_metrics_locked(sentence_ready=True)
            future.add_done_callback(self._preparation_finished)

    def _speech_finished(self, future: Future[None]) -> None:
        with self.state_lock:
            self.speech_futures.pop(future, None)
            self._record_speech_metrics_locked(playback=True)
        self._schedule_deferred_if_possible()
        self._maybe_auto_advance()

    def _prepare_if_current(self, chunk: SpeechChunk) -> object | None:
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
        try:
            return self.prepare_chunk(chunk)
        except Exception as error:
            self.report_error(error)
            return None

    def _preparation_finished(self, future: Future[object | None]) -> None:
        with self.state_lock:
            chunk = self.speech_futures.get(future)
            self._record_speech_metrics_locked(
                synthesis=True,
                first_pcm=self.first_pcm_on_prepare,
            )
        replaced = False
        try:
            if chunk is None or future.cancelled():
                return
            try:
                prepared = future.result()
            except Exception as error:
                self.report_error(error)
                return
            if prepared is None or not self.wait_until_playable(chunk):
                return
            playback_future = self.playback_executor.submit(
                self._play_if_current,
                chunk,
                prepared,
            )
            with self.state_lock:
                self.speech_futures.pop(future, None)
                self.speech_futures[playback_future] = chunk
                self._record_speech_metrics_locked()
                replaced = True
            playback_future.add_done_callback(self._speech_finished)
        finally:
            if not replaced:
                with self.state_lock:
                    self.speech_futures.pop(future, None)
            # A rapidly advancing game can replace a dialogue while its audio
            # is still being prepared. The old result is then intentionally
            # discarded, but the newest deferred dialogue must still be
            # scheduled or live mode remains running with a permanently stuck
            # queue.
            self._schedule_deferred_if_possible()

    def _play_if_current(self, chunk: SpeechChunk, prepared: object) -> None:
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
        except Exception as error:
            self.report_error(error)
        finally:
            with self.state_lock:
                self._record_speech_metrics_locked(playback_completed=True)
                if self.current_chunk is chunk:
                    self.current_chunk = None
                    self.current_chunk_pipeline_origins = None
                self.cancelled_chunk_ids.discard(id(chunk))
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
        if isinstance(decision, SilentDialogRoute):
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

    def _update_dialog_ready(self, tracker: IncrementalDialogTracker) -> None:
        with self.state_lock:
            self.dialog_ready_generation = (
                tracker.generation if tracker.is_idle_complete() else None
            )
            if (
                self.dialog_ready_generation is None
                and self.pending_auto_advance_generation is None
            ):
                self._cancel_auto_advance_locked()
        self._maybe_auto_advance()

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
                or self.deferred_chunk is not None
                or self.paused
                or self.suppressed_generation == generation
                or self.stop_event.is_set()
            ):
                return
            timer = Timer(
                self.auto_advance_delay_seconds,
                self._run_auto_advance,
                args=(generation,),
            )
            timer.daemon = True
            self.auto_advance_timer = timer
        timer.start()

    def _run_auto_advance(self, generation: int) -> None:
        with self.state_lock:
            self.auto_advance_timer = None
            if (
                self.auto_advance is None
                or generation != self.active_generation
                or self.dialog_ready_generation != generation
                or self.failed_auto_advance_generation == generation
                or self.last_auto_advance_dispatched_generation == generation
                or self.current_chunk is not None
                or self.speech_futures
                or self.deferred_chunk is not None
                or self.paused
                or self.suppressed_generation == generation
                or self.stop_event.is_set()
            ):
                return
        if not self._is_focused():
            # A nonmodal voice prompt can own focus exactly when speech ends.
            # Keep a delayed attempt alive so returning to the game cannot
            # strand a ready dialogue forever.
            with self.state_lock:
                report_focus_wait = (
                    self.auto_advance_focus_wait_generation != generation
                )
                self.auto_advance_focus_wait_generation = generation
            if report_focus_wait:
                self._report_pipeline_event(
                    "auto-advance-withheld",
                    generation,
                    reason="game-focus-not-owned",
                )
                self._report_auto_advance_state("focus-wait", generation, 0)
            self._maybe_auto_advance()
            return
        try:
            self.render_completion()
        except Exception as error:
            self.report_error(error)
        with self.state_lock:
            visual_ready = bool(
                not self.require_visible_auto_advance
                or (
                    self.latest_frame_visible
                    and self.routed_frame_fingerprint == self.latest_frame_fingerprint
                )
            )
            report_visual_wait = (
                not visual_ready
                and self.auto_advance_visual_wait_generation != generation
            )
            if not visual_ready:
                self.auto_advance_visual_wait_generation = generation
        if not visual_ready:
            if report_visual_wait:
                self._report_pipeline_event(
                    "auto-advance-withheld",
                    generation,
                    reason="owned-frame-not-visible-and-stable",
                )
                self._report_auto_advance_state("visual-wait", generation, 0)
            self._maybe_auto_advance()
            return
        try:
            attempt = self.auto_advance()
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
            if refusal_reason == "focus-wait" or (
                refusal_reason is None and not self._is_focused()
            ):
                with self.state_lock:
                    report_focus_wait = (
                        self.auto_advance_focus_wait_generation != generation
                    )
                    self.auto_advance_focus_wait_generation = generation
                if report_focus_wait:
                    self._report_pipeline_event(
                        "auto-advance-withheld",
                        generation,
                        reason="game-focus-lost-before-dispatch",
                    )
                    self._report_auto_advance_state("focus-wait", generation, 0)
                self._maybe_auto_advance()
            elif refusal_reason == "visual-wait":
                with self.state_lock:
                    report_visual_wait = (
                        self.auto_advance_visual_wait_generation != generation
                    )
                    self.auto_advance_visual_wait_generation = generation
                if report_visual_wait:
                    self._report_pipeline_event(
                        "auto-advance-withheld",
                        generation,
                        reason="canonical-full-text-not-confirmed",
                    )
                    self._report_auto_advance_state("visual-wait", generation, 0)
                self._maybe_auto_advance()
            else:
                self._report_pipeline_event(
                    "auto-advance-withheld",
                    generation,
                    reason=refusal_reason or "callback-blocked",
                )
                with self.state_lock:
                    if generation == self.active_generation:
                        self.failed_auto_advance_generation = generation
                self._report_auto_advance_state("blocked", generation, 0)
            return
        if advanced is not False:
            dispatched = None
            with self.state_lock:
                if generation == self.active_generation:
                    self.auto_advance_focus_wait_generation = None
                    self.auto_advance_visual_wait_generation = None
                    self.auto_advance_attempts = 1
                    attempt = 1
                    self.last_auto_advance_dispatched_generation = generation
                    self.pending_auto_advance_generation = generation
                    self.pipeline_metrics = replace(
                        self.pipeline_metrics,
                        last_auto_advance_dispatched_at=monotonic(),
                    )
                    timer = Timer(
                        self.auto_advance_confirmation_timeout_seconds,
                        self._auto_advance_confirmation_expired,
                        args=(generation, attempt, False),
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
                )
                self._report_auto_advance_state("dispatched", generation, attempt)
                timer.start()

    def _auto_advance_confirmation_expired(
        self,
        generation: int,
        attempt: int,
        terminal: bool = False,
    ) -> None:
        with self.state_lock:
            self.auto_advance_timer = None
            if (
                generation != self.active_generation
                or self.pending_auto_advance_generation != generation
                or self.auto_advance_attempts != attempt
                or self.stop_event.is_set()
            ):
                return
            paused = self.paused
        if paused or not self._is_focused():
            self._schedule_auto_advance_confirmation(
                generation,
                attempt,
                terminal=terminal,
            )
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
        self._report_auto_advance_state("failed", generation, attempt)

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
                or self.stop_event.is_set()
            ):
                return
            timer = Timer(
                (
                    self.auto_advance_confirmation_timeout_seconds
                    if delay_seconds is None
                    else delay_seconds
                ),
                self._auto_advance_confirmation_expired,
                args=(generation, attempt, terminal),
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
        try:
            self.pipeline_event_handler(
                stage,
                generation,
                monotonic() if occurred_at is None else occurred_at,
                **details,
            )
        except Exception as error:
            self.report_error(error)

    def _cancel_auto_advance_locked(self) -> None:
        timer = self.auto_advance_timer
        self.auto_advance_timer = None
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
        depth = len(self.speech_futures) + int(self.deferred_chunk is not None)
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
                now if text_visible else metrics.last_text_visible_at
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

    def record_first_pcm(self, timestamp: float | None = None) -> None:
        occurred_at = monotonic() if timestamp is None else timestamp
        with self.state_lock:
            previous = self.pipeline_metrics
            generation = self.active_generation
            self.pipeline_metrics = replace(
                previous,
                last_first_pcm_at=occurred_at,
                last_first_pcm_generation=generation,
            )
            chunk = self.current_chunk
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
        self._report_pipeline_event("first-pcm", generation, occurred_at, **details)
        if canonical_full_at is not None and canonical_full_at > occurred_at:
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
        deferred = self.deferred_chunk
        if deferred is None or deferred.generation != chunk.generation:
            self.deferred_chunk = chunk
            return
        separator = "" if deferred.text.endswith((" ", "\n")) else " "
        self.deferred_chunk = SpeechChunk(
            chunk.generation,
            chunk.character,
            f"{deferred.text}{separator}{chunk.text}",
            ordinal=deferred.ordinal,
            line_id=(deferred.line_id if deferred.line_id == chunk.line_id else None),
        )

    def _schedule_deferred_if_possible(self) -> None:
        with self.pause_condition:
            if (
                self.deferred_chunk is None
                or self.paused
                or len(self.speech_futures) >= self.max_speech_jobs
            ):
                return
            chunk = self.deferred_chunk
            self.deferred_chunk = None
        self._schedule([chunk])
