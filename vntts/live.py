import os
from collections import deque
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from hashlib import sha256
from threading import Condition, Event, RLock, Timer
from time import monotonic


@dataclass(frozen=True)
class SpeechChunk:
    generation: int
    character: str
    text: str
    ordinal: int | None = field(default=None, compare=False)
    line_id: str | None = field(default=None, compare=False)

    @property
    def chunk_id(self):
        if self.ordinal is None:
            return None
        character = " ".join((self.character or "Narrator").casefold().split())
        text = " ".join((self.text or "").casefold().split())
        payload = (
            f"{self.generation}\0{self.ordinal}\0{character}\0{text}\0"
            f"{self.line_id or ''}"
        )
        return sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SilentDialogRoute:
    """A cursor-owned visible dialogue event that intentionally has no speech."""

    event_id: str


@dataclass(frozen=True)
class AutoAdvanceAttempt:
    """Typed callback result so a safe wait is not mistaken for a hard block."""

    dispatched: bool
    reason: str

    def __bool__(self):
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
    last_playback_started_at: float | None = None
    last_playback_completed_at: float | None = None


class AdaptiveSpeechBackpressure:
    """Temporarily serialize speech after an output underrun."""

    def __init__(self, *, normal_jobs=2, cooldown_seconds=10.0, clock=monotonic):
        if normal_jobs < 1:
            raise ValueError("normal_jobs must be positive")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        self.normal_jobs = int(normal_jobs)
        self.cooldown_seconds = float(cooldown_seconds)
        self.clock = clock
        self.current_jobs = self.normal_jobs
        self.last_underflow_at = None

    def reset(self):
        self.current_jobs = self.normal_jobs
        self.last_underflow_at = None
        return self.current_jobs

    def observe_playback(self, *, underflowed):
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
        base_interval=0.2,
        fast_interval=None,
        idle_interval=None,
        unfocused_interval=None,
        unchanged_frames=3,
    ):
        if base_interval <= 0:
            raise ValueError("base_interval must be positive")
        if unchanged_frames < 1:
            raise ValueError("unchanged_frames must be positive")
        self.base_interval = base_interval
        self.fast_interval = fast_interval or max(0.05, base_interval / 2)
        self.idle_interval = idle_interval or min(1.5, base_interval * 3)
        self.unfocused_interval = unfocused_interval or min(0.5, base_interval * 2.5)
        self.unchanged_frames = unchanged_frames
        self.last_observation = None
        self.unchanged_count = 0
        self.was_focused = True

    def observe(self, character, text, *, focused=True):
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


class IncrementalDialogTracker:
    def __init__(
        self,
        *,
        stability_frames=2,
        idle_flush_seconds=0.7,
        min_chunk_characters=20,
        complete_sentences_only=True,
        complete_dialogue_only=False,
        early_dialogue_resolver=None,
        incomplete_dialogue_probe=None,
        clock=monotonic,
    ):
        if stability_frames < 2:
            raise ValueError("stability_frames must be at least 2")
        if idle_flush_seconds <= 0:
            raise ValueError("idle_flush_seconds must be positive")
        if min_chunk_characters <= 0:
            raise ValueError("min_chunk_characters must be positive")

        self.stability_frames = stability_frames
        self.idle_flush_seconds = idle_flush_seconds
        self.min_chunk_characters = min_chunk_characters
        self.complete_sentences_only = complete_sentences_only
        self.complete_dialogue_only = bool(complete_dialogue_only)
        self.early_dialogue_resolver = early_dialogue_resolver
        self.incomplete_dialogue_probe = incomplete_dialogue_probe
        self.clock = clock
        self.generation = 0
        self.character = None
        self.latest_text = ""
        self.committed_position = 0
        self.last_change_at = None
        self.stable_text = ""
        self.last_stable_change_at = None
        self.history = deque(maxlen=stability_frames)
        self.pending_character = None
        self.pending_history = deque(maxlen=stability_frames)
        self.next_chunk_ordinal = 1
        self.silent_event_id = None
        self.canonical_line_id = None

    def observe(self, character, text):
        now = self.clock()
        character = (character or "Narrator").strip() or "Narrator"
        text = self._normalize(text)

        if not text:
            if self.latest_text:
                self._clear_dialog()
            return []

        if self._is_speaker_noise_over_committed_text(character, text):
            self._clear_pending_dialog()
            return []

        if self._is_new_dialog(character, text):
            if self.latest_text:
                return self._observe_new_dialog_candidate(character, text, now)
            self._start_dialog(character, text, now)
            return []

        self._clear_pending_dialog()
        if text != self.latest_text:
            self.last_change_at = now
        self.character = character
        self.latest_text = text
        self.history.append(text)

        if len(self.history) < self.stability_frames:
            return []

        stable_text = os.path.commonprefix(list(self.history))
        if stable_text != self.stable_text:
            self.stable_text = stable_text
            self.last_stable_change_at = now
        idle = now - self.last_change_at >= self.idle_flush_seconds
        if (
            self.complete_dialogue_only
            and not idle
            and self.committed_position == 0
            and self.early_dialogue_resolver is not None
        ):
            resolved_text = self.early_dialogue_resolver(character, stable_text)
            if resolved_text:
                return self._emit(resolved_text, flush=True)
        if (
            self.complete_dialogue_only
            and idle
            and self.committed_position == 0
            and self.incomplete_dialogue_probe is not None
            and self.incomplete_dialogue_probe(character, stable_text)
        ):
            return []
        return self._emit(stable_text, flush=idle)

    def observe_silent(self, event_id):
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("silent event_id must be non-empty")
        if self.silent_event_id == event_id:
            return False
        self.generation += 1
        self.character = None
        self.latest_text = ""
        self.committed_position = 0
        self.last_change_at = None
        self.stable_text = ""
        self.last_stable_change_at = self.clock()
        self.history.clear()
        self.next_chunk_ordinal = 1
        self._clear_pending_dialog()
        self.silent_event_id = event_id
        self.canonical_line_id = None
        return True

    def observe_canonical(self, character, text, line_id):
        character = (character or "Narrator").strip() or "Narrator"
        text = self._normalize(text)
        line_id = str(line_id).strip()
        if not text or not line_id:
            raise ValueError("canonical dialogue requires text and line_id")
        if self.canonical_line_id == line_id:
            return []
        now = self.clock()
        self.generation += 1
        self.character = character
        self.latest_text = text
        self.committed_position = len(text)
        self.last_change_at = now
        self.stable_text = text
        self.last_stable_change_at = now
        self.history.clear()
        self.history.extend([text] * self.stability_frames)
        self.next_chunk_ordinal = 2
        self._clear_pending_dialog()
        self.silent_event_id = None
        self.canonical_line_id = line_id
        return [
            SpeechChunk(
                self.generation,
                character,
                text,
                ordinal=1,
                line_id=line_id,
            )
        ]

    def flush(self):
        if not self.latest_text:
            return []
        return self._emit(self.latest_text, flush=True)

    def is_idle_complete(self):
        if self.silent_event_id is not None:
            return True
        if self.canonical_line_id is not None:
            return True
        if (
            not self.latest_text
            or len(self.history) < self.stability_frames
            or self.last_stable_change_at is None
            or not self.stable_text.strip()
        ):
            return False
        stable_length = len(self.stable_text.rstrip())
        return (
            self.committed_position >= stable_length
            and self.clock() - self.last_stable_change_at >= self.idle_flush_seconds
        )

    def _is_new_dialog(self, character, text):
        if not self.latest_text:
            return True
        if character != self.character:
            return True
        if text == self.latest_text or text.startswith(self.latest_text):
            return False

        common_prefix = os.path.commonprefix([self.latest_text, text])
        if len(text) < self.committed_position and len(common_prefix) < len(text):
            return True

        similarity = SequenceMatcher(None, self.latest_text, text).ratio()
        meaningful_prefix = min(8, max(1, len(self.latest_text) // 3))
        return len(common_prefix) < meaningful_prefix and similarity < 0.5

    def _is_speaker_noise_over_committed_text(self, character, text):
        """Ignore a speaker wobble that still contains the spoken dialogue.

        The nameplate is a small OCR target and can temporarily be recognized
        as Narrator while the dialogue crop also picks up background glyphs.
        Once the stable dialogue has been committed, that must not create a new
        speech generation for the same words.
        """
        if character == self.character or self.committed_position == 0:
            return False
        committed = self.latest_text[: self.committed_position]
        committed_key = self._comparison_key(committed)
        text_key = self._comparison_key(text)
        if len(committed_key) < 4 or not text_key:
            return False
        return committed_key in text_key or text_key in committed_key

    def _observe_new_dialog_candidate(self, character, text, now):
        if not self._matches_pending_dialog(character, text):
            self.pending_character = character
            self.pending_history.clear()
        self.pending_history.append(text)

        if len(self.pending_history) < self.stability_frames:
            return []

        candidate_history = list(self.pending_history)
        self._start_dialog(character, text, now)
        self.history.clear()
        self.history.extend(candidate_history)
        stable_text = os.path.commonprefix(candidate_history)
        self.stable_text = stable_text
        self.last_stable_change_at = now
        self._clear_pending_dialog()
        return self._emit(stable_text, flush=False)

    def _matches_pending_dialog(self, character, text):
        if character != self.pending_character or not self.pending_history:
            return False
        previous = self.pending_history[-1]
        if text == previous or text.startswith(previous) or previous.startswith(text):
            return True
        common_prefix = os.path.commonprefix([previous, text])
        meaningful_prefix = min(8, max(1, len(previous) // 3))
        similarity = SequenceMatcher(None, previous, text).ratio()
        return len(common_prefix) >= meaningful_prefix or similarity >= 0.5

    def _clear_pending_dialog(self):
        self.pending_character = None
        self.pending_history.clear()

    @staticmethod
    def _comparison_key(text):
        return "".join(
            character.casefold() for character in text if character.isalnum()
        )

    def _start_dialog(self, character, text, now):
        self.generation += 1
        self.character = character
        self.latest_text = text
        self.committed_position = 0
        self.last_change_at = now
        self.stable_text = ""
        self.last_stable_change_at = now
        self.history.clear()
        self.history.append(text)
        self.next_chunk_ordinal = 1
        self.silent_event_id = None
        self.canonical_line_id = None
        self._clear_pending_dialog()

    def _clear_dialog(self):
        self.generation += 1
        self.character = None
        self.latest_text = ""
        self.committed_position = 0
        self.last_change_at = None
        self.stable_text = ""
        self.last_stable_change_at = None
        self.history.clear()
        self.next_chunk_ordinal = 1
        self.silent_event_id = None
        self.canonical_line_id = None
        self._clear_pending_dialog()

    def _emit(self, stable_text, *, flush):
        if self.complete_dialogue_only and not flush:
            return []
        if len(stable_text) <= self.committed_position:
            return []

        unspoken_text = stable_text[self.committed_position :]
        boundary = len(unspoken_text) if flush else self._find_boundary(unspoken_text)
        if boundary == 0:
            return []

        text = unspoken_text[:boundary].strip()
        self.committed_position += boundary
        if not text or not any(character.isalnum() for character in text):
            return []

        sentences = (
            [text] if self.complete_dialogue_only else self._split_sentences(text)
        )
        chunks = []
        for sentence in sentences:
            if not any(character.isalnum() for character in sentence):
                continue
            chunks.append(
                SpeechChunk(
                    self.generation,
                    self.character,
                    sentence,
                    ordinal=self.next_chunk_ordinal,
                )
            )
            self.next_chunk_ordinal += 1
        return chunks

    @staticmethod
    def _split_sentences(text):
        sentences = []
        start = 0
        for position, character in enumerate(text):
            if character not in ".!?":
                continue
            next_position = position + 1
            if next_position != len(text) and not text[next_position].isspace():
                continue
            sentence = text[start:next_position].strip()
            if sentence:
                sentences.append(sentence)
            start = next_position
        remainder = text[start:].strip()
        if remainder:
            sentences.append(remainder)
        return sentences

    def _find_boundary(self, text):
        sentence_boundary = self._last_punctuation_boundary(text, ".!?")
        if sentence_boundary:
            return sentence_boundary

        # Short clause fragments make XTTS try to continue from a comma or
        # semicolon and can produce buzzing, repeated syllables, or unstable
        # prosody. Live reading therefore waits for a complete sentence or an
        # idle flush by default. The old clause behavior remains opt-in for
        # integrations that explicitly prioritize latency over voice quality.
        if self.complete_sentences_only:
            return 0

        if len(text.strip()) < self.min_chunk_characters:
            return 0
        return self._last_punctuation_boundary(text, ",;:")

    @staticmethod
    def _last_punctuation_boundary(text, punctuation):
        boundary = 0
        for position, character in enumerate(text):
            if character not in punctuation:
                continue
            next_position = position + 1
            if next_position == len(text) or text[next_position].isspace():
                boundary = next_position
        return boundary

    @staticmethod
    def _normalize(text):
        return " ".join((text or "").split())


class LiveDialogReader:
    def __init__(
        self,
        *,
        capture_executor,
        speech_executor,
        read_snapshot,
        speak_chunk,
        report_error,
        ocr_executor=None,
        capture_frame=None,
        recognize_frame=None,
        frame_fingerprint=None,
        frame_presence=None,
        stable_frame_route=None,
        stable_frame_owner=None,
        frame_routed=None,
        line_id_resolver=None,
        stable_frame_minimum_seconds=0.12,
        stable_frame_clock=monotonic,
        playback_executor=None,
        prepare_chunk=None,
        play_prepared=None,
        interrupt_speech=None,
        dialog_observed=None,
        interval_seconds=0.2,
        tracker_factory=IncrementalDialogTracker,
        tracker_options=None,
        focus_probe=None,
        capture_state_changed=None,
        adaptive_policy_factory=AdaptiveCapturePolicy,
        adaptive_options=None,
        auto_advance=None,
        require_visible_auto_advance=False,
        auto_advance_delay_seconds=0.35,
        auto_advance_confirmation_timeout_seconds=2.0,
        auto_advance_terminal_timeout_seconds=10.0,
        auto_advance_state_changed=None,
        pipeline_event_handler=None,
        max_speech_jobs=2,
        interrupt_on_dialog_replacement=False,
        first_pcm_on_prepare=True,
    ):
        self.capture_executor = capture_executor
        self.speech_executor = speech_executor
        self.ocr_executor = ocr_executor
        self.playback_executor = playback_executor
        self.read_snapshot = read_snapshot
        self.capture_frame = capture_frame
        self.recognize_frame = recognize_frame
        self.frame_fingerprint = frame_fingerprint or (lambda _frame: None)
        self.frame_presence = frame_presence or (lambda _frame: True)
        self.stable_frame_route = stable_frame_route
        self.stable_frame_owner = stable_frame_owner or (lambda: None)
        self.frame_routed = frame_routed or (
            lambda _frame, _fingerprint, _route_kind, _character, _text: None
        )
        self.line_id_resolver = line_id_resolver or (lambda _character, _text: None)
        if stable_frame_minimum_seconds < 0:
            raise ValueError("stable_frame_minimum_seconds must not be negative")
        self.stable_frame_minimum_seconds = float(stable_frame_minimum_seconds)
        self.stable_frame_clock = stable_frame_clock
        self.speak_chunk = speak_chunk
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
        self.capture_future = None
        self.ocr_future = None
        self.active_generation = 0
        self.suppressed_generation = None
        self.speech_futures = {}
        self.paused_chunks = []
        self.deferred_chunk = None
        self.current_chunk = None
        self.current_chunk_pipeline_origins = None
        self.last_spoken_chunk = None
        self.cancelled_chunk_ids = set()
        self.prepared_chunk_ids = set()
        self.sealed_generation = None
        self.paused = False
        self.emergency_stopped = False
        self.last_observation = None
        self.last_accepted_observation = None
        self.deferred_observation = None
        self.dialog_ready_generation = None
        self.last_auto_advance_dispatched_generation = None
        self.pending_auto_advance_generation = None
        self.failed_auto_advance_generation = None
        self.auto_advance_attempts = 0
        self.auto_advance_blocked_generation = None
        self.auto_advance_block_reason = None
        self.auto_advance_focus_wait_generation = None
        self.auto_advance_visual_wait_generation = None
        self.auto_advance_timer = None
        self.focus_probe_failed = False
        self.latest_frame = None
        self.latest_frame_fingerprint = None
        self.latest_frame_visible = False
        self.routed_frame_fingerprint = None
        self.frame_route_epoch = 0
        self.candidate_frame_fingerprint = object()
        self.candidate_frame_count = 0
        self.candidate_frame_owner = None
        self.candidate_frame_started_at = None
        self.frame_version = 0
        self.processed_frame_version = 0
        self.next_capture_interval = interval_seconds
        self.pipeline_metrics = LivePipelineMetrics()

        if (prepare_chunk is None) != (play_prepared is None):
            raise ValueError(
                "prepare_chunk and play_prepared must be provided together"
            )
        if prepare_chunk is not None and playback_executor is None:
            raise ValueError("playback_executor is required for prepared speech")
        split_capture_options = (ocr_executor, capture_frame, recognize_frame)
        if any(value is not None for value in split_capture_options) and not all(
            value is not None for value in split_capture_options
        ):
            raise ValueError(
                "ocr_executor, capture_frame and recognize_frame must be provided together"
            )

    @property
    def is_running(self):
        with self.state_lock:
            return self.capture_future is not None and not self.capture_future.done()

    def start(self):
        with self.state_lock:
            if self.capture_future is not None and not self.capture_future.done():
                return False
        self.clear_queue()
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
            self.routed_frame_fingerprint = None
            self.frame_route_epoch += 1
            self._reset_stable_frame_candidate_locked()
            self.frame_version = 0
            self.processed_frame_version = 0
            self.next_capture_interval = self.interval_seconds
            self.pipeline_metrics = LivePipelineMetrics()
            if self.capture_frame is None:
                self.capture_future = self.capture_executor.submit(
                    self._run,
                    self.stop_event,
                )
            else:
                self.ocr_future = self.ocr_executor.submit(
                    self._run_ocr,
                    self.stop_event,
                )
                self.capture_future = self.capture_executor.submit(
                    self._run_capture,
                    self.stop_event,
                )
        return True

    def stop(self):
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

    def set_auto_advance(self, callback):
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

    def block_auto_advance_for_generation(self, generation, reason):
        with self.state_lock:
            if generation != self.active_generation:
                return False
            self._cancel_auto_advance_locked()
            self.auto_advance_blocked_generation = generation
            self.auto_advance_block_reason = str(reason).strip() or None
        return True

    def confirm_pending_auto_advance(self):
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

    def toggle(self):
        if self.is_running:
            self.stop()
            return False
        return self.start()

    def toggle_pause(self):
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
                    if future.running() and chunk == self.current_chunk:
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

    def enqueue(self, character, text, *, line_id=None):
        with self.state_lock:
            generation = self.active_generation + 1
        self._set_generation(generation)
        self._schedule([SpeechChunk(generation, character, text, line_id=line_id)])
        return True

    def bind_current_frame_route(self):
        """Bind explicit cursor recovery to the latest captured dialogue frame."""
        with self.state_lock:
            fingerprint = self.latest_frame_fingerprint
            if fingerprint is None:
                return False
            self._accept_routed_frame_locked(fingerprint)
        return True

    def frame_route_epoch_is_current(self, epoch):
        with self.state_lock:
            return epoch == self.frame_route_epoch

    def skip_current(self):
        with self.state_lock:
            has_current_speech = self.current_chunk is not None
            if has_current_speech:
                self.cancelled_chunk_ids.add(id(self.current_chunk))
        if has_current_speech:
            self._interrupt_speech()
        return has_current_speech

    def repeat_last(self):
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
                )
            ]
        )
        return True

    def clear_queue(self):
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

    def emergency_stop(self):
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

    def resume_after_emergency(self):
        with self.state_lock:
            was_stopped = self.emergency_stopped
            self.emergency_stopped = False
        return was_stopped

    def release_waiters(self):
        with self.pause_condition:
            self.paused = False
            self.pause_condition.notify_all()

    def wait_until_playable(self, chunk):
        with self.pause_condition:
            finish_active_playback = bool(
                self.current_chunk == chunk and not self.interrupt_on_dialog_replacement
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
                    self.sealed_generation != chunk.generation or finish_active_playback
                )
                and self.suppressed_generation != chunk.generation
                and id(chunk) not in self.cancelled_chunk_ids
            )

    def seal_generation(self, generation):
        """Suppress OCR suffix chunks after an exact full-line route completed."""
        stale_futures = []
        with self.pause_condition:
            if generation != self.active_generation:
                return False
            self.sealed_generation = generation
            stale_futures = [
                future
                for future, chunk in self.speech_futures.items()
                if chunk != self.current_chunk and chunk.generation == generation
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

    def wait(self):
        with self.state_lock:
            capture_future = self.capture_future
            ocr_future = self.ocr_future
        if capture_future is not None:
            capture_future.result()
        if ocr_future is not None:
            ocr_future.result()

    def get_pipeline_metrics(self):
        with self.state_lock:
            return self.pipeline_metrics

    def _run_capture(self, stop_event):
        policy = self.adaptive_policy_factory(
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        while not stop_event.is_set():
            focused = self._is_focused()
            if not focused:
                interval = policy.observe(None, None, focused=False)
                self.capture_state_changed(False, interval)
                stop_event.wait(interval)
                continue
            try:
                frame = self.capture_frame()
                fingerprint = self.frame_fingerprint(frame)
                visible = bool(self.frame_presence(frame))
                with self.pause_condition:
                    fingerprint_changed = fingerprint != self.latest_frame_fingerprint
                    replaced = self.frame_version > self.processed_frame_version
                    self.latest_frame = frame
                    self.latest_frame_fingerprint = fingerprint
                    self.latest_frame_visible = visible
                    self.frame_version += 1
                    metrics = self.pipeline_metrics
                    self.pipeline_metrics = replace(
                        metrics,
                        captured_frames=metrics.captured_frames + 1,
                        replaced_frames=metrics.replaced_frames + int(replaced),
                        last_capture_at=monotonic(),
                    )
                    interval = self.next_capture_interval
                    if fingerprint_changed:
                        # The OCR worker updates the adaptive interval after it
                        # consumes this frame. Do not sleep once more on the
                        # previous static-dialogue interval before giving the
                        # stability gate its confirming frame.
                        interval = min(interval, policy.fast_interval)
                    self.pause_condition.notify_all()
            except Exception as error:
                self.report_error(error)
                interval = self.interval_seconds
            self.capture_state_changed(True, interval)
            stop_event.wait(interval)

    def _run_ocr(self, stop_event):
        tracker = self.tracker_factory(**self.tracker_options)
        policy = self.adaptive_policy_factory(
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        cached_fingerprint = object()
        cached_observation = (None, "")
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
                self.processed_frame_version = self.frame_version
            try:
                with self.state_lock:
                    awaiting_post_advance_dialog = (
                        self.pending_auto_advance_generation == self.active_generation
                    )
                if (
                    fingerprint == cached_fingerprint
                    and not awaiting_post_advance_dialog
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
                    )
                if frame_route is False:
                    character, text = cached_observation
                    interval = policy.observe(character, text, focused=True)
                    with self.state_lock:
                        self.next_capture_interval = interval
                    continue
                if isinstance(frame_route, SilentDialogRoute):
                    character, text = None, ""
                    route_kind = "canonical"
                    cached_fingerprint = fingerprint
                    cached_observation = (character, text)
                elif isinstance(frame_route, tuple) and len(frame_route) == 2:
                    character, text = frame_route
                    route_kind = "canonical"
                    cached_fingerprint = fingerprint
                    cached_observation = (character, text)
                elif frame_route is None and (
                    fingerprint != cached_fingerprint or awaiting_post_advance_dialog
                ):
                    character, text = self.recognize_frame(frame)
                    route_kind = "ocr"
                    cached_fingerprint = fingerprint
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
                        "a SilentDialogRoute/(character, text) route"
                    )
                routed_observation = (
                    frame_route
                    if isinstance(frame_route, SilentDialogRoute)
                    else self._report_observation(character, text)
                )
                if routed_observation is None:
                    interval = policy.observe(character, text, focused=True)
                    with self.state_lock:
                        self.next_capture_interval = interval
                    continue
                silent_route = (
                    routed_observation
                    if isinstance(routed_observation, SilentDialogRoute)
                    else None
                )
                if silent_route is None:
                    character, text = routed_observation
                else:
                    character, text = None, ""
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
                        chunks = tracker.observe(character, text)
                    else:
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

    def _stable_frame_route_decision(self, fingerprint, visible=True):
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
            settled_for = now - self.candidate_frame_started_at
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
    def _privacy_safe_fingerprint(fingerprint):
        if isinstance(fingerprint, bytes):
            return fingerprint.hex()[:16]
        return str(fingerprint)[:64]

    def _accept_routed_frame(self, fingerprint):
        with self.state_lock:
            self._accept_routed_frame_locked(fingerprint)

    def _accept_routed_frame_locked(self, fingerprint):
        self.routed_frame_fingerprint = fingerprint
        self.frame_route_epoch += 1
        self._reset_stable_frame_candidate_locked()

    def _reset_stable_frame_candidate(self):
        with self.state_lock:
            self._reset_stable_frame_candidate_locked()

    def _reset_stable_frame_candidate_locked(self):
        self.candidate_frame_fingerprint = object()
        self.candidate_frame_count = 0
        self.candidate_frame_owner = None
        self.candidate_frame_started_at = None

    def _run(self, stop_event):
        tracker = self.tracker_factory(**self.tracker_options)
        policy = self.adaptive_policy_factory(
            base_interval=self.interval_seconds,
            **self.adaptive_options,
        )
        while not stop_event.is_set():
            focused = self._is_focused()
            if not focused:
                interval = policy.observe(None, None, focused=False)
                self.capture_state_changed(False, interval)
                stop_event.wait(interval)
                continue
            try:
                character, text = self.read_snapshot()
                routed_observation = self._report_observation(character, text)
                if routed_observation is None:
                    interval = policy.observe(character, text, focused=True)
                    self.capture_state_changed(True, interval)
                    stop_event.wait(interval)
                    continue
                character, text = routed_observation
                chunks = tracker.observe(character, text)
                self._set_generation(tracker.generation)
                self._schedule(chunks)
                self._update_dialog_ready(tracker)
                interval = policy.observe(character, text, focused=True)
            except Exception as error:
                self.report_error(error)
                interval = self.interval_seconds
            self.capture_state_changed(True, interval)
            stop_event.wait(interval)

        self._set_generation(tracker.generation)
        self._schedule(tracker.flush())
        self._update_dialog_ready(tracker)

    def _speech_is_active(self):
        with self.state_lock:
            return self.current_chunk is not None

    def _is_focused(self):
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

    def _set_generation(self, generation):
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
                    if chunk != self.current_chunk and chunk.generation != generation
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

    def _schedule(self, chunks):
        for chunk in chunks:
            with self.pause_condition:
                if self.emergency_stopped:
                    continue
                if self.sealed_generation == chunk.generation:
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
            target = (
                self._prepare_if_current
                if self.prepare_chunk is not None
                else self._speak_if_current
            )
            future = self.speech_executor.submit(target, chunk)
            with self.state_lock:
                self.speech_futures[future] = chunk
                self._record_speech_metrics_locked(sentence_ready=True)
            callback = (
                self._preparation_finished
                if self.prepare_chunk is not None
                else self._speech_finished
            )
            future.add_done_callback(callback)

    def _speech_finished(self, future):
        with self.state_lock:
            self.speech_futures.pop(future, None)
            self._record_speech_metrics_locked(playback=True)
        self._schedule_deferred_if_possible()
        self._maybe_auto_advance()

    def _prepare_if_current(self, chunk):
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

    def _preparation_finished(self, future):
        with self.state_lock:
            chunk = self.speech_futures.pop(future, None)
            self._record_speech_metrics_locked(
                synthesis=True,
                first_pcm=self.first_pcm_on_prepare,
            )
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
                self.speech_futures[playback_future] = chunk
                self._record_speech_metrics_locked()
            playback_future.add_done_callback(self._speech_finished)
        finally:
            # A rapidly advancing game can replace a dialogue while its audio
            # is still being prepared. The old result is then intentionally
            # discarded, but the newest deferred dialogue must still be
            # scheduled or live mode remains running with a permanently stuck
            # queue.
            self._schedule_deferred_if_possible()

    def _play_if_current(self, chunk, prepared):
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
                if self.current_chunk == chunk:
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

    def _speak_if_current(self, chunk):
        if not self.wait_until_playable(chunk):
            return
        with self.state_lock:
            self.current_chunk = chunk
            self.last_spoken_chunk = chunk
            self._record_speech_metrics_locked(
                generation_started=True,
                playback_started=True,
            )
        now = monotonic()
        chunk_details = {
            "chunk_id": chunk.chunk_id,
            "chunk_ordinal": chunk.ordinal,
            "chunk_characters": len(chunk.text),
        }
        self._report_pipeline_event(
            "generation-start", chunk.generation, now, **chunk_details
        )
        self._report_pipeline_event("first-pcm", chunk.generation, now, **chunk_details)

        try:
            self.speak_chunk(chunk)
        except Exception as error:
            self.report_error(error)
        finally:
            with self.state_lock:
                self._record_speech_metrics_locked(playback_completed=True)
                if self.current_chunk == chunk:
                    self.current_chunk = None
                self.cancelled_chunk_ids.discard(id(chunk))
            self._report_pipeline_event(
                "playback-completion",
                chunk.generation,
                monotonic(),
                **chunk_details,
            )

    def _report_observation(self, character, text):
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
        if isinstance(decision, SilentDialogRoute):
            routed = decision
        elif isinstance(decision, tuple) and len(decision) == 2:
            routed = (decision[0], " ".join((decision[1] or "").split()))
        else:
            routed = observation
        self.deferred_observation = None
        self.last_accepted_observation = routed
        return routed

    def _interrupt_speech(self):
        try:
            return bool(self.interrupt_speech())
        except Exception as error:
            self.report_error(error)
            return False

    def _update_dialog_ready(self, tracker):
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

    def _maybe_auto_advance(self):
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

    def _run_auto_advance(self, generation):
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
            advanced = attempt.dispatched
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
        generation,
        attempt,
        terminal=False,
    ):
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
        generation,
        attempt,
        *,
        delay_seconds=None,
        terminal=False,
    ):
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

    def _resume_auto_advance_confirmation(self):
        with self.state_lock:
            generation = self.pending_auto_advance_generation
            attempt = self.auto_advance_attempts
        if generation is not None and attempt:
            self._schedule_auto_advance_confirmation(generation, attempt)

    def _report_auto_advance_state(self, state, generation, attempt):
        try:
            self.auto_advance_state_changed(state, generation, attempt)
        except Exception as error:
            self.report_error(error)

    def _report_pipeline_event(
        self,
        stage,
        generation,
        occurred_at=None,
        **details,
    ):
        try:
            self.pipeline_event_handler(
                stage,
                generation,
                monotonic() if occurred_at is None else occurred_at,
                **details,
            )
        except Exception as error:
            self.report_error(error)

    def _cancel_auto_advance_locked(self):
        timer = self.auto_advance_timer
        self.auto_advance_timer = None
        if timer is not None:
            timer.cancel()

    def _record_speech_metrics_locked(
        self,
        *,
        sentence_ready=False,
        synthesis=False,
        playback=False,
        text_visible=False,
        generation_started=False,
        first_pcm=False,
        playback_started=False,
        playback_completed=False,
    ):
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
            last_playback_started_at=(
                now if playback_started else metrics.last_playback_started_at
            ),
            last_playback_completed_at=(
                now if playback_completed else metrics.last_playback_completed_at
            ),
        )

    def record_first_pcm(self, timestamp=None):
        occurred_at = monotonic() if timestamp is None else timestamp
        with self.state_lock:
            previous = self.pipeline_metrics
            self.pipeline_metrics = replace(
                previous,
                last_first_pcm_at=occurred_at,
            )
            generation = self.active_generation
            chunk = self.current_chunk
            origins = self.current_chunk_pipeline_origins or {
                "from_text_visible_ms": previous.last_text_visible_at,
                "from_ocr_stable_ms": previous.last_ocr_stable_at,
                "from_generation_started_ms": previous.last_generation_started_at,
                "from_playback_started_ms": previous.last_playback_started_at,
            }
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

    def _defer_chunk_locked(self, chunk):
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

    def _schedule_deferred_if_possible(self):
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
