import os
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from threading import Condition, Event, RLock
from time import monotonic


@dataclass(frozen=True)
class SpeechChunk:
    generation: int
    character: str
    text: str


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
        if not text:
            return []

        return [SpeechChunk(self.generation, self.character, text)]

    def _find_boundary(self, text):
        sentence_boundary = self._last_punctuation_boundary(text, ".!?")
        if sentence_boundary:
            return sentence_boundary

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
        interrupt_speech=None,
        dialog_observed=None,
        interval_seconds=0.2,
        tracker_factory=IncrementalDialogTracker,
        tracker_options=None,
        focus_probe=None,
        capture_state_changed=None,
        adaptive_policy_factory=AdaptiveCapturePolicy,
        adaptive_options=None,
    ):
        self.capture_executor = capture_executor
        self.speech_executor = speech_executor
        self.read_snapshot = read_snapshot
        self.speak_chunk = speak_chunk
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
        self.state_lock = RLock()
        self.pause_condition = Condition(self.state_lock)
        self.stop_event = Event()
        self.capture_future = None
        self.active_generation = 0
        self.suppressed_generation = None
        self.speech_futures = {}
        self.paused_chunks = []
        self.current_chunk = None
        self.last_spoken_chunk = None
        self.cancelled_chunk_ids = set()
        self.paused = False
        self.last_observation = None

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
            self.stop_event = Event()
            self.active_generation = 0
            self.suppressed_generation = None
            self.last_observation = None
            self.capture_future = self.capture_executor.submit(
                self._run,
                self.stop_event,
            )
        return True

    def stop(self):
        with self.state_lock:
            if self.capture_future is None or self.capture_future.done():
                return False
            self.stop_event.set()
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
            self.paused_chunks = []
            has_current_speech = self.current_chunk is not None
            self.pause_condition.notify_all()
        for future in futures:
            future.cancel()
        if has_current_speech:
            self._interrupt_speech()
        return has_current_speech or bool(futures) or had_paused_chunks

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
        if capture_future is not None:
            capture_future.result()

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
                interval = policy.observe(character, text, focused=True)
            except Exception as error:
                self.report_error(error)
                interval = self.interval_seconds
            self.capture_state_changed(True, interval)
            stop_event.wait(interval)

        self._set_generation(tracker.generation)
        self._schedule(tracker.flush())

    def _is_focused(self):
        try:
            return bool(self.focus_probe())
        except Exception as error:
            self.report_error(error)
            return True

    def _set_generation(self, generation):
        interrupt = False
        with self.pause_condition:
            changed = generation != self.active_generation
            if changed and self.current_chunk is not None:
                interrupt = self.current_chunk.generation != generation
            self.active_generation = generation
            if self.suppressed_generation != generation:
                self.suppressed_generation = None
            if changed:
                self.paused_chunks = [
                    chunk
                    for chunk in self.paused_chunks
                    if chunk.generation == generation
                ]
                self.pause_condition.notify_all()
        if interrupt:
            self._interrupt_speech()

    def _schedule(self, chunks):
        for chunk in chunks:
            with self.pause_condition:
                if self.suppressed_generation == chunk.generation:
                    continue
                if self.paused:
                    self.paused_chunks.append(chunk)
                    continue
            future = self.speech_executor.submit(self._speak_if_current, chunk)
            with self.state_lock:
                self.speech_futures[future] = chunk
            future.add_done_callback(self._speech_finished)

    def _speech_finished(self, future):
        with self.state_lock:
            self.speech_futures.pop(future, None)

    def _speak_if_current(self, chunk):
        if not self.wait_until_playable(chunk):
            return
        with self.state_lock:
            self.current_chunk = chunk
            self.last_spoken_chunk = chunk

        try:
            self.speak_chunk(chunk)
        except Exception as error:
            self.report_error(error)
        finally:
            with self.state_lock:
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
        self.dialog_observed(*observation)

    def _interrupt_speech(self):
        try:
            return bool(self.interrupt_speech())
        except Exception as error:
            self.report_error(error)
            return False
