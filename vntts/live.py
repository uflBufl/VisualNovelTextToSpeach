import os
from collections import deque
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from threading import Condition, Event, RLock, Timer
from time import monotonic


@dataclass(frozen=True)
class SpeechChunk:
    generation: int
    character: str
    text: str


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
        self.unfocused_interval = unfocused_interval or min(2.5, base_interval * 8)
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
        self.clock = clock
        self.generation = 0
        self.character = None
        self.latest_text = ""
        self.committed_position = 0
        self.last_change_at = None
        self.history = deque(maxlen=stability_frames)

    def observe(self, character, text):
        now = self.clock()
        character = (character or "Narrator").strip() or "Narrator"
        text = self._normalize(text)

        if not text:
            if self.latest_text:
                self._clear_dialog()
            return []

        if self._is_new_dialog(character, text):
            self._start_dialog(character, text, now)
            return []

        if text != self.latest_text:
            self.last_change_at = now
        self.character = character
        self.latest_text = text
        self.history.append(text)

        if len(self.history) < self.stability_frames:
            return []

        stable_text = os.path.commonprefix(list(self.history))
        idle = now - self.last_change_at >= self.idle_flush_seconds
        return self._emit(stable_text, flush=idle)

    def flush(self):
        if not self.latest_text:
            return []
        return self._emit(self.latest_text, flush=True)

    def is_idle_complete(self):
        if not self.latest_text or self.last_change_at is None:
            return False
        return (
            self.committed_position >= len(self.latest_text)
            and self.clock() - self.last_change_at >= self.idle_flush_seconds
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

    def _start_dialog(self, character, text, now):
        self.generation += 1
        self.character = character
        self.latest_text = text
        self.committed_position = 0
        self.last_change_at = now
        self.history.clear()
        self.history.append(text)

    def _clear_dialog(self):
        self.generation += 1
        self.character = None
        self.latest_text = ""
        self.committed_position = 0
        self.last_change_at = None
        self.history.clear()

    def _emit(self, stable_text, *, flush):
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

        return [
            SpeechChunk(self.generation, self.character, sentence)
            for sentence in self._split_sentences(text)
            if any(character.isalnum() for character in sentence)
        ]

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
        auto_advance_delay_seconds=0.35,
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
        self.auto_advance_delay_seconds = auto_advance_delay_seconds
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
        self.last_spoken_chunk = None
        self.cancelled_chunk_ids = set()
        self.paused = False
        self.emergency_stopped = False
        self.last_observation = None
        self.dialog_ready_generation = None
        self.advanced_generation = None
        self.auto_advance_timer = None
        self.latest_frame = None
        self.latest_frame_fingerprint = None
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
            self.dialog_ready_generation = None
            self.advanced_generation = None
            self._cancel_auto_advance_locked()
            self.latest_frame = None
            self.latest_frame_fingerprint = None
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
            self.pause_condition.notify_all()
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
        return self.paused

    def enqueue(self, character, text):
        with self.state_lock:
            generation = self.active_generation + 1
        self._set_generation(generation)
        self._schedule([SpeechChunk(generation, character, text)])
        return True

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
        self._schedule([SpeechChunk(generation, chunk.character, chunk.text)])
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
        for future in futures:
            future.cancel()
        if has_current_speech:
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
            while (
                self.paused
                and chunk.generation == self.active_generation
                and self.suppressed_generation != chunk.generation
            ):
                self.pause_condition.wait()
            return (
                chunk.generation == self.active_generation
                and self.suppressed_generation != chunk.generation
                and id(chunk) not in self.cancelled_chunk_ids
            )

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
                with self.pause_condition:
                    replaced = self.frame_version > self.processed_frame_version
                    self.latest_frame = frame
                    self.latest_frame_fingerprint = fingerprint
                    self.frame_version += 1
                    metrics = self.pipeline_metrics
                    self.pipeline_metrics = replace(
                        metrics,
                        captured_frames=metrics.captured_frames + 1,
                        replaced_frames=metrics.replaced_frames + int(replaced),
                        last_capture_at=monotonic(),
                    )
                    interval = self.next_capture_interval
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
                self.processed_frame_version = self.frame_version
            try:
                with self.state_lock:
                    awaiting_post_advance_dialog = (
                        self.advanced_generation == self.active_generation
                    )
                if (
                    fingerprint == cached_fingerprint
                    and not awaiting_post_advance_dialog
                ):
                    character, text = cached_observation
                    with self.state_lock:
                        metrics = self.pipeline_metrics
                        self.pipeline_metrics = replace(
                            metrics,
                            reused_frames=metrics.reused_frames + 1,
                        )
                else:
                    character, text = self.recognize_frame(frame)
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
                self._report_observation(character, text)
                chunks = tracker.observe(character, text)
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
                self._report_observation(character, text)
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
            return bool(self.focus_probe())
        except Exception as error:
            self.report_error(error)
            return True

    def _set_generation(self, generation):
        stale_futures = []
        interrupt_current = False
        with self.pause_condition:
            changed = generation != self.active_generation
            self.active_generation = generation
            if self.suppressed_generation != generation:
                self.suppressed_generation = None
            if changed:
                interrupt_current = bool(
                    self.interrupt_on_dialog_replacement
                    and self.current_chunk is not None
                )
                if interrupt_current:
                    self.cancelled_chunk_ids.add(id(self.current_chunk))
                self.dialog_ready_generation = None
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
        # Most backends finish active playback to avoid clicks. Streaming
        # backends that cooperatively cancel generation opt into interruption.
        for future in stale_futures:
            future.cancel()
        if interrupt_current:
            self._interrupt_speech()

    def _schedule(self, chunks):
        for chunk in chunks:
            with self.pause_condition:
                if self.emergency_stopped:
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
        with self.state_lock:
            self._record_speech_metrics_locked(generation_started=True)
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
        if not self.wait_until_playable(chunk):
            return
        with self.state_lock:
            self.current_chunk = chunk
            self.last_spoken_chunk = chunk
            self._record_speech_metrics_locked(playback_started=True)
        try:
            self.play_prepared(chunk, prepared)
        except Exception as error:
            self.report_error(error)
        finally:
            with self.state_lock:
                self._record_speech_metrics_locked(playback_completed=True)
                if self.current_chunk == chunk:
                    self.current_chunk = None
                self.cancelled_chunk_ids.discard(id(chunk))

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

    def _report_observation(self, character, text):
        if character is None and not text:
            if self.last_observation is not None:
                self.dialog_observed("Narrator", "")
            self.last_observation = None
            return
        observation = (character, " ".join((text or "").split()))
        if observation == self.last_observation:
            return
        self.last_observation = observation
        if text:
            with self.state_lock:
                self._record_speech_metrics_locked(text_visible=True)
        self.dialog_observed(*observation)

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
            if self.dialog_ready_generation is None:
                self._cancel_auto_advance_locked()
        self._maybe_auto_advance()

    def _maybe_auto_advance(self):
        with self.state_lock:
            generation = self.active_generation
            if (
                self.auto_advance is None
                or self.dialog_ready_generation != generation
                or self.advanced_generation == generation
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
                generation != self.active_generation
                or self.dialog_ready_generation != generation
                or self.advanced_generation == generation
                or self.current_chunk is not None
                or self.speech_futures
                or self.deferred_chunk is not None
                or self.paused
                or self.suppressed_generation == generation
                or self.stop_event.is_set()
            ):
                return
        if not self._is_focused():
            return
        try:
            advanced = self.auto_advance()
        except Exception as error:
            self.report_error(error)
            return
        if advanced is not False:
            with self.state_lock:
                if generation == self.active_generation:
                    self.advanced_generation = generation
                    self.pipeline_metrics = replace(
                        self.pipeline_metrics,
                        last_auto_advance_at=monotonic(),
                    )

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
        with self.state_lock:
            self.pipeline_metrics = replace(
                self.pipeline_metrics,
                last_first_pcm_at=monotonic() if timestamp is None else timestamp,
            )

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
