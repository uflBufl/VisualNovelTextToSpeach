import unittest
from concurrent.futures import Future
from dataclasses import replace
from queue import Queue
from threading import Event, Lock, Thread
from unittest.mock import Mock, call, patch

from vntts.generated_audio import (
    AudioRouteTrace,
    GeneratedAudioRoute,
    PreparedGeneratedAudio,
)
from vntts.live import (
    AdaptiveCapturePolicy,
    AdaptiveSpeechBackpressure,
    AutoAdvanceAttempt,
    IncrementalDialogTracker,
    LiveDialogReader,
    SilentDialogRoute,
    SpeechChunk,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ImmediateExecutor:
    def submit(self, function, *arguments):
        future = Future()
        try:
            future.set_result(function(*arguments))
        except Exception as error:
            future.set_exception(error)
        return future


class QueuedExecutor:
    def __init__(self):
        self.jobs = []
        self.lock = Lock()
        self.available = Event()

    def submit(self, function, *arguments):
        future = Future()
        with self.lock:
            self.jobs.append((future, function, arguments))
            self.available.set()
        return future

    def run_next(self):
        if not self.available.wait(timeout=1):
            raise AssertionError("Expected an executor job")
        with self.lock:
            future, function, arguments = self.jobs.pop(0)
            if not self.jobs:
                self.available.clear()
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*arguments))
            except Exception as error:
                future.set_exception(error)
        return future


class ManualTimer:
    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.started = False
        self.cancelled = False
        self.fired = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.started or self.cancelled or self.fired:
            return False
        self.fired = True
        self.function(*self.args)
        return True


class ManualTimerScheduler:
    def __init__(self):
        self.timers = []

    def create(self, interval, function, args=()):
        timer = ManualTimer(interval, function, args)
        self.timers.append(timer)
        return timer

    def pending(self, function_name):
        return [
            timer
            for timer in self.timers
            if timer.started
            and not timer.cancelled
            and not timer.fired
            and timer.function.__name__ == function_name
        ]

    def fire_next(self, function_name):
        timers = self.pending(function_name)
        if not timers:
            raise AssertionError(f"Expected pending timer for {function_name}")
        self.assert_single_timer(timers, function_name)
        timers[0].fire()
        return timers[0]

    @staticmethod
    def assert_single_timer(timers, function_name):
        if len(timers) != 1:
            raise AssertionError(
                f"Expected one {function_name} timer, found {len(timers)}"
            )


class RecordingCapturePolicy:
    def __init__(self, completed, *, base_interval, **_options):
        self.completed = completed
        self.base_interval = base_interval

    def observe(self, character, text, *, focused=True):
        self.completed.put((character, text, focused))
        return self.base_interval


class AutoAdvanceFakeFrameHarness:
    def __init__(
        self,
        *,
        stable_frame_route=None,
        stable_frame_minimum_seconds=0.12,
        dialog_observed=None,
        frame_recheck_required=None,
        frame_recheck_interval_seconds=0.6,
        ocr_purpose=None,
        frame_observed=None,
    ):
        self.clock = FakeClock()
        self.completed_observations = Queue()
        self.speech_executor = QueuedExecutor()
        self.playback_executor = QueuedExecutor()
        self.timer_scheduler = ManualTimerScheduler()
        self.focused = True
        self.frame_owner = "event-1"
        self.advance_calls = []
        self.played = []
        self.states = []
        self.pipeline_events = []
        self.errors = []
        self.recognized_frames = []
        self.stop_event = Event()
        self.reader = LiveDialogReader(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=self.speech_executor,
            playback_executor=self.playback_executor,
            read_snapshot=Mock(),
            capture_frame=Mock(),
            recognize_frame=self._recognize,
            frame_presence=lambda frame: frame.get("visible", True),
            frame_recheck_required=frame_recheck_required,
            ocr_purpose=ocr_purpose,
            frame_observed=frame_observed,
            frame_recheck_interval_seconds=frame_recheck_interval_seconds,
            stable_frame_route=stable_frame_route,
            stable_frame_owner=lambda: self.frame_owner,
            dialog_observed=dialog_observed,
            stable_frame_minimum_seconds=stable_frame_minimum_seconds,
            stable_frame_clock=self.clock,
            speak_chunk=Mock(),
            prepare_chunk=lambda chunk: f"audio:{chunk.text}",
            play_prepared=self._play,
            report_error=self.errors.append,
            focus_probe=lambda: self.focused,
            adaptive_policy_factory=lambda **options: RecordingCapturePolicy(
                self.completed_observations,
                **options,
            ),
            tracker_options={
                "clock": self.clock,
                "idle_flush_seconds": 0.1,
            },
            auto_advance=self._advance,
            auto_advance_delay_seconds=0.2,
            auto_advance_confirmation_timeout_seconds=0.5,
            auto_advance_state_changed=lambda state, generation, attempt: (
                self.states.append((state, generation, attempt))
            ),
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                self.pipeline_events.append((stage, generation, occurred_at, details))
            ),
        )
        self.reader.stop_event = self.stop_event
        self.worker = Thread(target=self.reader._run_ocr, args=(self.stop_event,))

    def start(self):
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        with self.reader.pause_condition:
            self.reader.pause_condition.notify_all()
        self.worker.join(timeout=1)
        if self.worker.is_alive():
            raise AssertionError("OCR worker did not stop")

    def push(
        self,
        character,
        text,
        *,
        background,
        fingerprint="same-glyphs",
        visible=True,
        complete=False,
        expected=None,
    ):
        frame = {
            "character": character,
            "text": text,
            "background": background,
            "visible": visible,
        }
        with self.reader.pause_condition:
            self.reader.latest_frame = frame
            self.reader.latest_frame_fingerprint = fingerprint
            self.reader.latest_frame_visible = visible
            self.reader.latest_frame_complete = complete
            self.reader.frame_version += 1
            self.reader.pause_condition.notify_all()
        observed = self.completed_observations.get(timeout=1)
        expected = (character, text) if expected is None else expected
        if observed[:2] != expected:
            raise AssertionError(
                f"Expected processed observation {expected!r}, "
                f"received {observed[:2]!r}"
            )

    def prepare_ready_dialogue(self):
        self.push("Rhiannon", "I, erhm ...", background="first")
        self.push("Rhiannon", "I, erhm ...", background="animated-a")
        self.clock.advance(0.2)
        self.push("Rhiannon", "I, erhm ...", background="animated-b")

    def finish_playback(self):
        self.speech_executor.run_next()
        self.playback_executor.run_next()

    def _recognize(self, frame):
        self.recognized_frames.append(frame)
        return frame["character"], frame["text"]

    def _play(self, chunk, prepared):
        self.played.append((chunk, prepared))
        return True

    def _advance(self):
        self.advance_calls.append(self.reader.active_generation)
        return True


class AutoAdvanceFakeFrameEndToEndTest(unittest.TestCase):
    def test_locked_visual_frame_is_acknowledged_without_running_ocr(self):
        observed = []
        harness = AutoAdvanceFakeFrameHarness(
            stable_frame_route=lambda *_args: None,
            ocr_purpose=lambda: None,
            frame_observed=lambda _frame, fingerprint, kind: observed.append(
                (fingerprint, kind)
            ),
        )
        harness.start()
        self.addCleanup(harness.stop)

        harness.push(
            "corrupted nameplate",
            "untrusted OCR payload",
            background="locked",
            fingerprint="locked-frame",
            expected=(None, ""),
        )

        self.assertEqual(harness.recognized_frames, [])
        self.assertEqual(observed, [("locked-frame", "locked-visual-only")])

    def test_stable_frame_route_replaces_second_line_without_another_ocr_call(self):
        route_calls = []

        def stable_route(fingerprint, settled, _owner, _route_epoch):
            route_calls.append((fingerprint, settled))
            return ("Bea", "Canonical second line.") if settled else False

        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=stable_route)
        harness.start()
        self.addCleanup(harness.stop)
        harness.push(
            "Ada",
            "First line.",
            background="first",
            fingerprint="first-glyphs",
        )
        harness.push(
            "bad OCR",
            "partial second",
            background="second-a",
            fingerprint="second-glyphs",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.12)
        harness.push(
            "bad OCR",
            "still wrong",
            background="second-b",
            fingerprint="second-glyphs",
            complete=True,
            expected=("Bea", "Canonical second line."),
        )

        self.assertEqual(len(harness.recognized_frames), 1)
        self.assertEqual(
            route_calls,
            [("second-glyphs", False), ("second-glyphs", True)],
        )
        gate_events = [
            details
            for stage, _generation, _occurred_at, details in harness.pipeline_events
            if stage == "stable-frame-gate"
        ]
        self.assertEqual([event["ready"] for event in gate_events], [False, True])
        self.assertEqual(gate_events[-1]["owner"], "event-1")
        self.assertEqual(gate_events[-1]["settled_ms"], 120)
        self.assertTrue(gate_events[-1]["completion_cue"])
        self.assertNotIn("text", gate_events[-1])
        self.assertEqual(harness.errors, [])

    def test_completion_cue_change_bypasses_same_fingerprint_ocr_cache(self):
        harness = None

        def stable_route(_fingerprint, settled, _owner, _route_epoch):
            if not settled:
                return False
            if harness.reader.current_frame_has_completion_cue():
                return ("Bea", "Canonical short line.")
            return None

        harness = AutoAdvanceFakeFrameHarness(
            stable_frame_route=stable_route,
            dialog_observed=lambda _character, text: text == "First line.",
        )
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Ada", "First line.", background="first", fingerprint="first")
        harness.push(
            "bad OCR",
            "partial",
            background="candidate-a",
            fingerprint="short-line",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.12)
        harness.push(
            "bad OCR",
            "partial",
            background="candidate-b",
            fingerprint="short-line",
        )
        harness.push(
            "bad OCR",
            "partial",
            background="candidate-with-cue",
            fingerprint="short-line",
            complete=True,
            expected=("Bea", "Canonical short line."),
        )

        gate_events = [
            details
            for stage, _generation, _occurred_at, details in harness.pipeline_events
            if stage == "stable-frame-gate"
        ]
        self.assertTrue(gate_events[-1]["completion_cue"])
        self.assertEqual(harness.recognized_frames[-1]["text"], "partial")
        self.assertEqual(harness.errors, [])

    def test_pending_prefix_periodically_rechecks_same_fingerprint(self):
        pending = [True]
        canonical = "You DO have shillings, don't you, miss?"

        def observe(character, text):
            if text == canonical:
                pending[0] = False
            return character, canonical

        harness = AutoAdvanceFakeFrameHarness(
            frame_recheck_required=lambda: pending[0],
            frame_recheck_interval_seconds=0.6,
            dialog_observed=observe,
        )
        harness.start()
        self.addCleanup(harness.stop)

        harness.push(
            "Hotelier",
            "You DO have",
            background="first",
            fingerprint="same-glyphs",
            expected=("Hotelier", canonical),
        )
        self.assertEqual(len(harness.recognized_frames), 1)
        harness.clock.advance(0.4)
        harness.push(
            "Hotelier",
            "You DO have",
            background="cached",
            fingerprint="same-glyphs",
            expected=("Hotelier", canonical),
        )
        self.assertEqual(len(harness.recognized_frames), 1)
        harness.clock.advance(0.2)
        harness.push(
            "Hotelier",
            canonical,
            background="rechecked",
            fingerprint="same-glyphs",
        )
        self.assertEqual(len(harness.recognized_frames), 2)
        self.assertFalse(pending[0])

        harness.clock.advance(1.0)
        harness.push(
            "Hotelier",
            canonical,
            background="ordinary-cache",
            fingerprint="same-glyphs",
        )
        self.assertEqual(len(harness.recognized_frames), 2)
        rechecks = [
            details
            for stage, _generation, _occurred_at, details in harness.pipeline_events
            if stage == "canonical-prefix-visual-recheck"
        ]
        self.assertEqual(len(rechecks), 2)
        self.assertEqual(rechecks[-1]["recheck_interval_ms"], 600)
        self.assertNotIn("text", rechecks[-1])
        self.assertEqual(harness.errors, [])

    def test_stable_frame_route_waits_for_minimum_settled_time(self):
        route_calls = []

        def stable_route(fingerprint, settled, _owner, _route_epoch):
            route_calls.append((fingerprint, settled))
            return ("Bea", "Canonical second line.") if settled else False

        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=stable_route)
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Ada", "First line.", background="first", fingerprint="first")
        harness.push(
            "bad OCR",
            "partial",
            background="second-a",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.05)
        harness.push(
            "bad OCR",
            "partial",
            background="second-b",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.07)
        harness.push(
            "bad OCR",
            "partial",
            background="second-c",
            fingerprint="second",
            expected=("Bea", "Canonical second line."),
        )

        self.assertEqual(
            route_calls,
            [("second", False), ("second", False), ("second", True)],
        )
        self.assertEqual(len(harness.recognized_frames), 1)

    def test_stable_frame_route_requires_visible_dialogue(self):
        route_calls = []

        def stable_route(fingerprint, settled, _owner, _route_epoch):
            route_calls.append((fingerprint, settled))
            return ("Bea", "Canonical second line.") if settled else False

        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=stable_route)
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Ada", "First line.", background="first", fingerprint="first")
        harness.clock.advance(0.2)
        harness.push(
            "bad OCR",
            "popup",
            background="popup-a",
            fingerprint="popup",
            visible=False,
            expected=("Ada", "First line."),
        )
        harness.push(
            "bad OCR",
            "popup",
            background="popup-b",
            fingerprint="popup",
            visible=False,
            expected=("Ada", "First line."),
        )

        self.assertEqual(route_calls, [])
        self.assertEqual(len(harness.recognized_frames), 1)

    def test_first_invisible_frame_never_invokes_ocr(self):
        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=Mock())
        harness.start()
        self.addCleanup(harness.stop)

        harness.push(
            "bad OCR",
            "popup",
            background="popup",
            fingerprint="popup",
            visible=False,
            expected=(None, ""),
        )

        self.assertEqual(harness.recognized_frames, [])
        harness.reader.stable_frame_route.assert_not_called()

    def test_stable_frame_route_resets_when_owner_changes(self):
        route_calls = []

        def stable_route(fingerprint, settled, _owner, _route_epoch):
            route_calls.append((fingerprint, settled))
            return ("Bea", "Canonical second line.") if settled else False

        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=stable_route)
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Ada", "First line.", background="first", fingerprint="first")
        harness.push(
            "bad OCR",
            "partial",
            background="second-a",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.12)
        harness.frame_owner = "event-9"
        harness.push(
            "bad OCR",
            "partial",
            background="second-b",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.12)
        harness.push(
            "bad OCR",
            "partial",
            background="second-c",
            fingerprint="second",
            expected=("Bea", "Canonical second line."),
        )

        self.assertEqual(
            route_calls,
            [("second", False), ("second", False), ("second", True)],
        )

    def test_explicit_frame_bind_invalidates_in_flight_stable_route(self):
        harness = None

        def stable_route(_fingerprint, settled, _owner, _route_epoch):
            if settled:
                self.assertTrue(harness.reader.bind_current_frame_route())
                return ("Bea", "Stale canonical route.")
            return False

        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=stable_route)
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Ada", "First line.", background="first", fingerprint="first")
        harness.push(
            "bad OCR",
            "partial",
            background="second-a",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.12)
        harness.push(
            "bad OCR",
            "still partial",
            background="second-b",
            fingerprint="second",
            expected=("Ada", "First line."),
        )

        self.assertEqual(len(harness.recognized_frames), 1)
        self.assertEqual(harness.reader.routed_frame_fingerprint, "second")

    def test_stable_frame_route_resets_while_game_is_unfocused(self):
        route_calls = []

        def stable_route(fingerprint, settled, _owner, _route_epoch):
            route_calls.append((fingerprint, settled))
            return ("Bea", "Canonical second line.") if settled else False

        harness = AutoAdvanceFakeFrameHarness(stable_frame_route=stable_route)
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Ada", "First line.", background="first", fingerprint="first")
        harness.focused = False
        harness.clock.advance(0.2)
        harness.push(
            "bad OCR",
            "partial",
            background="second-a",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.focused = True
        harness.push(
            "bad OCR",
            "partial",
            background="second-b",
            fingerprint="second",
            expected=("Ada", "First line."),
        )
        harness.clock.advance(0.12)
        harness.push(
            "bad OCR",
            "partial",
            background="second-c",
            fingerprint="second",
            expected=("Bea", "Canonical second line."),
        )

        self.assertEqual(route_calls, [("second", False), ("second", True)])

    def test_delayed_next_screen_confirms_after_playback_without_duplicate_press(self):
        harness = AutoAdvanceFakeFrameHarness()
        with patch(
            "vntts.live.Timer",
            side_effect=harness.timer_scheduler.create,
        ):
            harness.start()
            self.addCleanup(harness.stop)
            harness.prepare_ready_dialogue()

            self.assertEqual(
                harness.timer_scheduler.pending("_run_auto_advance"),
                [],
            )
            self.assertEqual(harness.reader.dialog_ready_generation, 1)
            harness.speech_executor.run_next()
            self.assertEqual(
                harness.timer_scheduler.pending("_run_auto_advance"),
                [],
            )

            harness.playback_executor.run_next()

            self.assertEqual(len(harness.played), 1)
            self.assertIsNotNone(
                harness.reader.get_pipeline_metrics().last_playback_completed_at
            )
            self.assertEqual(
                len(harness.timer_scheduler.pending("_run_auto_advance")),
                1,
            )
            harness.timer_scheduler.fire_next("_run_auto_advance")
            self.assertEqual(harness.advance_calls, [1])
            self.assertEqual(harness.states, [("dispatched", 1, 1)])

            recognized_before_advance = len(harness.recognized_frames)
            harness.push(
                "Rhiannon",
                "I, erhm ...",
                background="delayed-transition-a",
            )
            harness.push(
                "Rhiannon",
                "I, erhm ...",
                background="delayed-transition-b",
            )
            self.assertEqual(
                len(harness.recognized_frames),
                recognized_before_advance + 2,
            )
            self.assertEqual(harness.advance_calls, [1])

            harness.push("Hotelier", "The next line.", background="next-a")
            harness.push("Hotelier", "The next line.", background="next-b")

            self.assertEqual(harness.reader.active_generation, 2)
            self.assertIsNone(harness.reader.pending_auto_advance_generation)
            self.assertEqual(harness.advance_calls, [1])
            self.assertEqual(
                harness.states,
                [("dispatched", 1, 1), ("confirmed", 1, 1)],
            )
            stages = {
                stage
                for stage, generation, _occurred_at, _details in harness.pipeline_events
                if generation == 1
            }
            self.assertTrue(
                {
                    "ocr",
                    "stable-text",
                    "generation-start",
                    "first-pcm",
                    "playback-completion",
                    "key-dispatch",
                    "confirmed-next-dialogue",
                }.issubset(stages)
            )
            self.assertEqual(
                harness.timer_scheduler.pending("_auto_advance_confirmation_expired"),
                [],
            )
            self.assertEqual(harness.errors, [])

    def test_unconfirmed_press_is_not_retried_after_focus_returns(self):
        harness = AutoAdvanceFakeFrameHarness()
        with patch(
            "vntts.live.Timer",
            side_effect=harness.timer_scheduler.create,
        ):
            harness.start()
            self.addCleanup(harness.stop)
            harness.prepare_ready_dialogue()
            harness.finish_playback()
            harness.timer_scheduler.fire_next("_run_auto_advance")

            self.assertEqual(harness.advance_calls, [1])
            harness.focused = False
            harness.timer_scheduler.fire_next("_auto_advance_confirmation_expired")
            self.assertEqual(
                len(
                    harness.timer_scheduler.pending(
                        "_auto_advance_confirmation_expired"
                    )
                ),
                1,
            )

            harness.focused = True
            harness.timer_scheduler.fire_next("_auto_advance_confirmation_expired")
            self.assertEqual(harness.advance_calls, [1])
            self.assertEqual(harness.states[-1], ("waiting", 1, 1))
            self.assertIsNone(harness.reader.failed_auto_advance_generation)
            harness.timer_scheduler.fire_next("_auto_advance_confirmation_expired")
            self.assertEqual(harness.states[-1], ("failed", 1, 1))
            self.assertEqual(harness.reader.failed_auto_advance_generation, 1)
            self.assertIsNone(harness.reader.pending_auto_advance_generation)
            self.assertEqual(harness.errors, [])

            harness.push(
                "Hotelier",
                "The recovered line.",
                background="recovery-a",
                fingerprint="recovery-a",
            )
            harness.push(
                "Hotelier",
                "The recovered line.",
                background="recovery-b",
                fingerprint="recovery-b",
            )

            self.assertEqual(harness.reader.active_generation, 2)
            self.assertEqual(
                harness.states,
                [
                    ("dispatched", 1, 1),
                    ("waiting", 1, 1),
                    ("failed", 1, 1),
                ],
            )
            self.assertEqual(harness.advance_calls, [1])
            self.assertIsNone(harness.reader.failed_auto_advance_generation)
            timeout_events = [
                event
                for event in harness.pipeline_events
                if event[0] == "auto-advance-timeout" and event[1] == 1
            ]
            self.assertEqual(len(timeout_events), 1)


class AdaptiveSpeechBackpressureTest(unittest.TestCase):
    def test_underflow_serializes_until_a_clean_cooldown_has_elapsed(self):
        clock = FakeClock()
        policy = AdaptiveSpeechBackpressure(
            normal_jobs=2,
            cooldown_seconds=10,
            clock=clock,
        )

        self.assertEqual(
            policy.observe_playback(underflowed=True),
            (1, True),
        )
        clock.advance(9)
        self.assertEqual(
            policy.observe_playback(underflowed=False),
            (1, False),
        )
        clock.advance(1)
        self.assertEqual(
            policy.observe_playback(underflowed=False),
            (2, True),
        )

    def test_another_underflow_restarts_the_cooldown(self):
        clock = FakeClock()
        policy = AdaptiveSpeechBackpressure(cooldown_seconds=10, clock=clock)

        policy.observe_playback(underflowed=True)
        clock.advance(9)
        policy.observe_playback(underflowed=True)
        clock.advance(2)

        self.assertEqual(
            policy.observe_playback(underflowed=False),
            (1, False),
        )

    def test_serial_backend_never_reports_a_backpressure_transition(self):
        policy = AdaptiveSpeechBackpressure(normal_jobs=1)

        self.assertEqual(
            policy.observe_playback(underflowed=True),
            (1, False),
        )


class IncrementalDialogTrackerTest(unittest.TestCase):
    def test_cursor_owned_silent_event_is_ready_once_without_speech(self):
        tracker = IncrementalDialogTracker()

        self.assertTrue(tracker.observe_silent("event-silent"))
        self.assertEqual(tracker.generation, 1)
        self.assertTrue(tracker.is_idle_complete())
        self.assertFalse(tracker.observe_silent("event-silent"))
        self.assertEqual(tracker.generation, 1)

        self.assertEqual(tracker.observe("Ada", "Spoken line."), [])
        self.assertEqual(tracker.generation, 2)
        self.assertFalse(tracker.is_idle_complete())

    def test_cursor_owned_canonical_line_emits_once_without_ocr_stability_delay(self):
        tracker = IncrementalDialogTracker()

        chunks = tracker.observe_canonical("Ada", "Canonical line.", "line-1")

        self.assertEqual(
            chunks,
            [
                SpeechChunk(
                    1,
                    "Ada",
                    "Canonical line.",
                    ordinal=1,
                    line_id="line-1",
                )
            ],
        )
        self.assertTrue(tracker.is_idle_complete())
        self.assertEqual(
            tracker.observe_canonical("Ada", "Canonical line.", "line-1"), []
        )
        self.assertEqual(tracker.generation, 1)

    def create_tracker(self, **options):
        self.clock = FakeClock()
        return IncrementalDialogTracker(clock=self.clock, **options)

    def test_typewriter_text_is_spoken_after_sentence_becomes_stable(self):
        tracker = self.create_tracker()

        self.assertEqual(tracker.observe("Alice", "Hello"), [])
        self.assertEqual(tracker.observe("Alice", "Hello world."), [])
        self.assertEqual(
            tracker.observe("Alice", "Hello world. How"),
            [SpeechChunk(1, "Alice", "Hello world.")],
        )

    def test_ocr_correction_in_unspoken_text_is_not_repeated(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "The qu1ck brown")
        tracker.observe("Alice", "The quick brown fox")
        self.assertEqual(tracker.observe("Alice", "The quick brown fox."), [])
        self.assertEqual(
            tracker.observe("Alice", "The quick brown fox."),
            [SpeechChunk(1, "Alice", "The quick brown fox.")],
        )

    def test_idle_text_is_flushed_without_sentence_punctuation(self):
        tracker = self.create_tracker(idle_flush_seconds=0.7)

        tracker.observe("Narrator", "A quiet morning")
        tracker.observe("Narrator", "A quiet morning")
        self.clock.advance(0.8)

        self.assertEqual(
            tracker.observe("Narrator", "A quiet morning"),
            [SpeechChunk(1, "Narrator", "A quiet morning")],
        )

    def test_exact_route_mode_emits_one_complete_multi_sentence_dialogue(self):
        tracker = self.create_tracker(
            idle_flush_seconds=0.7,
            complete_dialogue_only=True,
        )
        text = "The road was long. We should rest now."

        tracker.observe("Rhiannon", text)
        self.assertEqual(tracker.observe("Rhiannon", text), [])
        self.clock.advance(0.8)

        self.assertEqual(
            tracker.observe("Rhiannon", text),
            [SpeechChunk(1, "Rhiannon", text)],
        )

    def test_exact_route_mode_can_emit_unique_full_line_before_idle(self):
        full_text = "These old ones are enough to carry everyone."
        resolver = Mock(return_value=full_text)
        tracker = self.create_tracker(
            idle_flush_seconds=0.7,
            complete_dialogue_only=True,
            early_dialogue_resolver=resolver,
        )

        tracker.observe("Kamuta", "These old ones are enough")
        chunks = tracker.observe("Kamuta", "These old ones are enough to")

        self.assertEqual(chunks, [SpeechChunk(1, "Kamuta", full_text)])
        resolver.assert_called_once_with("Kamuta", "These old ones are enough")

    def test_exact_route_mode_expands_missing_wav_line_once_before_live_fallback(self):
        full_text = (
            "T-Two?! No, that's too much! Oh, how about this! I'll clean the "
            "room before I go. You won't find so much as a single feather—promise!"
        )
        tracker = self.create_tracker(
            idle_flush_seconds=0.7,
            complete_dialogue_only=True,
            early_dialogue_resolver=lambda _character, _text: full_text,
        )
        partial = full_text[:99]

        tracker.observe("Rhiannon", partial)
        chunks = tracker.observe("Rhiannon", partial)

        self.assertEqual(chunks, [SpeechChunk(1, "Rhiannon", full_text)])
        self.clock.advance(0.8)
        self.assertEqual(tracker.observe("Rhiannon", full_text), [])

    def test_punctuation_only_ellipsis_completes_without_speech(self):
        tracker = self.create_tracker(
            idle_flush_seconds=0.7,
            complete_dialogue_only=True,
        )

        tracker.observe("Rhiannon", "...")
        tracker.observe("Rhiannon", "...")
        self.clock.advance(0.8)

        self.assertEqual(tracker.observe("Rhiannon", "..."), [])
        self.assertTrue(tracker.is_idle_complete())

    def test_exact_route_mode_does_not_emit_unresolved_prefix(self):
        resolver = Mock(return_value=None)
        tracker = self.create_tracker(
            idle_flush_seconds=0.7,
            complete_dialogue_only=True,
            early_dialogue_resolver=resolver,
        )

        tracker.observe("Kamuta", "An ambiguous generated line")

        self.assertEqual(
            tracker.observe("Kamuta", "An ambiguous generated line grows"),
            [],
        )

    def test_exact_route_mode_does_not_idle_flush_known_incomplete_prefix(self):
        incomplete_probe = Mock(return_value=True)
        tracker = self.create_tracker(
            idle_flush_seconds=0.7,
            complete_dialogue_only=True,
            incomplete_dialogue_probe=incomplete_probe,
        )
        partial = "A single room will be four coins"

        tracker.observe("Hotelier", partial)
        tracker.observe("Hotelier", partial)
        self.clock.advance(0.8)

        self.assertEqual(tracker.observe("Hotelier", partial), [])
        self.assertEqual(tracker.committed_position, 0)
        incomplete_probe.assert_called_with("Hotelier", partial)

    def test_short_clause_waits_but_long_clause_is_emitted(self):
        tracker = self.create_tracker(
            min_chunk_characters=10,
            complete_sentences_only=False,
        )

        tracker.observe("Alice", "Wait,")
        self.assertEqual(tracker.observe("Alice", "Wait,"), [])
        tracker.observe("Alice", "Wait, this is long enough,")

        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough, next"),
            [SpeechChunk(1, "Alice", "Wait, this is long enough,")],
        )

    def test_live_quality_mode_waits_instead_of_speaking_clause_fragments(self):
        tracker = self.create_tracker(
            min_chunk_characters=10,
            idle_flush_seconds=0.7,
        )

        tracker.observe("Alice", "Wait, this is long enough,")
        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough,"),
            [],
        )
        self.clock.advance(0.8)

        self.assertEqual(
            tracker.observe("Alice", "Wait, this is long enough,"),
            [SpeechChunk(1, "Alice", "Wait, this is long enough,")],
        )

    def test_new_speaker_starts_new_generation(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "Hello.")
        tracker.observe("Alice", "Hello.")
        first_generation = tracker.generation
        self.assertEqual(tracker.observe("Bob", "Goodbye."), [])

        self.assertEqual(tracker.generation, first_generation)
        self.assertEqual(
            tracker.observe("Bob", "Goodbye."),
            [SpeechChunk(first_generation + 1, "Bob", "Goodbye.")],
        )

        self.assertGreater(tracker.generation, first_generation)

    def test_unrelated_text_from_same_speaker_starts_new_generation(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "The first dialogue is complete.")
        tracker.observe("Alice", "The first dialogue is complete.")
        first_generation = tracker.generation
        tracker.observe("Alice", "Something entirely different appears.")

        self.assertEqual(tracker.generation, first_generation)
        tracker.observe("Alice", "Something entirely different appears.")

        self.assertGreater(tracker.generation, first_generation)

    def test_empty_dialog_invalidates_previous_generation(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "Hello")
        first_generation = tracker.generation
        self.assertEqual(tracker.observe("Narrator", ""), [])

        self.assertGreater(tracker.generation, first_generation)
        self.assertEqual(tracker.latest_text, "")

    def test_flush_speaks_remaining_partial_text_once(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "One sentence. Partial ending")
        self.assertEqual(
            tracker.observe("Alice", "One sentence. Partial ending"),
            [SpeechChunk(1, "Alice", "One sentence.")],
        )
        self.assertEqual(
            tracker.flush(),
            [SpeechChunk(1, "Alice", "Partial ending")],
        )
        self.assertEqual(tracker.flush(), [])

    def test_visible_sentences_are_prepared_as_separate_chunks(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "First sentence. Second sentence.")

        self.assertEqual(
            tracker.observe("Alice", "First sentence. Second sentence."),
            [
                SpeechChunk(1, "Alice", "First sentence."),
                SpeechChunk(1, "Alice", "Second sentence."),
            ],
        )

        tracker.observe(
            "Alice",
            "First sentence. Second sentence. Third sentence.",
        )
        chunks = tracker.observe(
            "Alice",
            "First sentence. Second sentence. Third sentence.",
        )
        self.assertEqual([chunk.ordinal for chunk in chunks], [3])
        self.assertIsNotNone(chunks[0].chunk_id)

    def test_noise_only_text_is_committed_without_being_spoken(self):
        tracker = self.create_tracker()

        tracker.observe("Alice", "...")
        tracker.observe("Alice", "...")

        self.assertEqual(tracker.flush(), [])
        self.assertEqual(tracker.committed_position, 3)

    def test_idle_complete_requires_all_visible_text_to_be_committed(self):
        tracker = self.create_tracker(idle_flush_seconds=0.7)
        tracker.observe("Alice", "First sentence. Second")
        tracker.observe("Alice", "First sentence. Second")
        self.assertFalse(tracker.is_idle_complete())

        self.clock.advance(0.8)
        tracker.observe("Alice", "First sentence. Second")

        self.assertTrue(tracker.is_idle_complete())

    def test_changing_ocr_suffix_does_not_block_completed_dialog(self):
        tracker = self.create_tracker(idle_flush_seconds=0.7)

        tracker.observe("Rhiannon", "Alright, that makes five. ae")
        self.assertEqual(
            tracker.observe("Rhiannon", "Alright, that makes five. vee nee"),
            [SpeechChunk(1, "Rhiannon", "Alright, that makes five.")],
        )
        self.clock.advance(0.4)
        tracker.observe("Rhiannon", "Alright, that makes five. oo")
        self.clock.advance(0.4)
        tracker.observe("Rhiannon", "Alright, that makes five. ee")

        self.assertTrue(tracker.is_idle_complete())

    def test_short_ellipsis_line_becomes_ready_for_auto_advance(self):
        tracker = self.create_tracker(idle_flush_seconds=0.4)

        tracker.observe("Adar Llwch Gwin Fledgling", "Coo...")
        self.assertEqual(
            tracker.observe("Adar Llwch Gwin Fledgling", "Coo..."),
            [SpeechChunk(1, "Adar Llwch Gwin Fledgling", "Coo...")],
        )
        self.clock.advance(0.5)
        tracker.observe("Adar Llwch Gwin Fledgling", "Coo...")

        self.assertTrue(tracker.is_idle_complete())

    def test_short_line_is_not_repeated_by_speaker_and_background_ocr_noise(self):
        tracker = self.create_tracker(idle_flush_seconds=0.4)

        tracker.observe("Rhiannon", "I, erhm ... oe in")
        self.assertEqual(
            tracker.observe("Rhiannon", "I, erhm ..."),
            [SpeechChunk(1, "Rhiannon", "I, erhm ...")],
        )
        generation = tracker.generation

        noisy_observations = [
            ("Narrator", "Rhiannon or Coe - I, erhm ..."),
            ("Rhiannon", "I, erhm ..."),
            ("Narrator", "Rhiannon wy ie A I, ethm wd fea ey"),
            ("Rhiannon", "I, erhm ..."),
            ("Narrator", 'Rhiannon i " % a A'),
            ("Rhiannon", "I, erhm ..."),
        ]
        for character, text in noisy_observations:
            self.assertEqual(tracker.observe(character, text), [])

        self.assertEqual(tracker.generation, generation)
        self.clock.advance(0.5)
        self.assertEqual(tracker.observe("Rhiannon", "I, erhm ..."), [])
        self.assertTrue(tracker.is_idle_complete())

    def test_repeated_wrong_speaker_does_not_reopen_committed_dialog(self):
        tracker = self.create_tracker()
        tracker.observe("Rhiannon", "I, erhm ...")
        tracker.observe("Rhiannon", "I, erhm ...")
        generation = tracker.generation

        tracker.observe("Narrator", "Rhiannon - I, erhm ...")
        tracker.observe("Narrator", "Rhiannon - I, erhm ...")

        self.assertEqual(tracker.generation, generation)
        self.assertEqual(tracker.character, "Rhiannon")


class AdaptiveCapturePolicyTest(unittest.TestCase):
    def test_capture_accelerates_while_text_changes_and_slows_when_stable(self):
        policy = AdaptiveCapturePolicy(
            base_interval=0.2,
            fast_interval=0.08,
            idle_interval=0.7,
            unchanged_frames=2,
        )

        self.assertEqual(policy.observe("Alice", "H"), 0.08)
        self.assertEqual(policy.observe("Alice", "Hello"), 0.08)
        self.assertEqual(policy.observe("Alice", "Hello"), 0.2)
        self.assertEqual(policy.observe("Alice", "Hello"), 0.7)

    def test_unfocused_game_uses_slowest_interval(self):
        policy = AdaptiveCapturePolicy(
            base_interval=0.2,
            unfocused_interval=1.8,
        )

        self.assertEqual(policy.observe(None, None, focused=False), 1.8)
        self.assertEqual(policy.observe("Alice", "Welcome", focused=True), 0.1)

    def test_default_unfocused_probe_is_bounded_for_fast_focus_return(self):
        policy = AdaptiveCapturePolicy(base_interval=0.2)

        self.assertEqual(policy.observe(None, None, focused=False), 0.5)


class LiveDialogReaderTest(unittest.TestCase):
    def create_reader(self, **overrides):
        options = {
            "capture_executor": Mock(),
            "speech_executor": ImmediateExecutor(),
            "read_snapshot": Mock(return_value=("Alice", "Hello.")),
            "speak_chunk": Mock(),
            "report_error": Mock(),
            "interval_seconds": 0.001,
        }
        options.update(overrides)
        return LiveDialogReader(**options)

    def test_capture_loop_speaks_stable_text(self):
        stop_event = Event()
        snapshots = iter([("Alice", "Hello."), ("Alice", "Hello.")])

        def read_snapshot():
            snapshot = next(snapshots)
            if snapshot == ("Alice", "Hello.") and read_snapshot.calls == 1:
                stop_event.set()
            read_snapshot.calls += 1
            return snapshot

        read_snapshot.calls = 0
        speak_chunk = Mock()
        reader = self.create_reader(
            read_snapshot=read_snapshot,
            speak_chunk=speak_chunk,
        )

        reader._run(stop_event)

        speak_chunk.assert_called_once_with(SpeechChunk(1, "Alice", "Hello."))

    def test_explicit_resync_binds_latest_frame_for_locked_routing(self):
        reader = self.create_reader()

        self.assertFalse(reader.bind_current_frame_route())
        reader.latest_frame_fingerprint = "visible-dialogue"

        self.assertTrue(reader.bind_current_frame_route())
        self.assertEqual(reader.routed_frame_fingerprint, "visible-dialogue")

    def test_dialog_handler_can_route_a_cursor_owned_silent_event(self):
        reader = self.create_reader(
            dialog_observed=lambda _character, _text: SilentDialogRoute("event-silent")
        )

        routed = reader._report_observation("Rhiannon", "...")

        self.assertEqual(routed, SilentDialogRoute("event-silent"))

    def test_capture_loop_waits_for_unknown_voice_decision_before_speaking(self):
        stop_event = Event()
        observations = 0

        def read_snapshot():
            nonlocal observations
            observations += 1
            if observations == 3:
                stop_event.set()
            return "Unknown", "Hello."

        decision = Mock(side_effect=[False, True])
        speak_chunk = Mock()
        reader = self.create_reader(
            read_snapshot=read_snapshot,
            dialog_observed=decision,
            speak_chunk=speak_chunk,
        )

        reader._run(stop_event)

        self.assertEqual(decision.call_count, 2)
        speak_chunk.assert_called_once_with(SpeechChunk(1, "Unknown", "Hello."))

    def test_stale_generation_is_not_spoken(self):
        speak_chunk = Mock()
        reader = self.create_reader(speak_chunk=speak_chunk)
        reader.active_generation = 2

        reader._speak_if_current(SpeechChunk(1, "Alice", "Old text."))

        speak_chunk.assert_not_called()

    def test_speech_failure_is_reported_without_escaping_worker(self):
        report_error = Mock()
        reader = self.create_reader(
            speak_chunk=Mock(side_effect=RuntimeError("audio failed")),
            report_error=report_error,
        )
        reader.active_generation = 1

        reader._speak_if_current(SpeechChunk(1, "Alice", "Hello."))

        report_error.assert_called_once()
        self.assertEqual(str(report_error.call_args.args[0]), "audio failed")

    def test_prepared_speech_prefetches_then_plays_on_separate_executor(self):
        prepare_chunk = Mock(return_value="prepared audio")
        play_prepared = Mock()
        reader = self.create_reader(
            playback_executor=ImmediateExecutor(),
            prepare_chunk=prepare_chunk,
            play_prepared=play_prepared,
        )
        chunk = SpeechChunk(1, "Alice", "Two-stage speech.")
        reader.active_generation = 1

        reader._schedule([chunk])

        prepare_chunk.assert_called_once_with(chunk)
        play_prepared.assert_called_once_with(chunk, "prepared audio")
        self.assertEqual(reader.last_spoken_chunk, chunk)
        metrics = reader.get_pipeline_metrics()
        self.assertIsNotNone(metrics.last_generation_started_at)
        self.assertIsNotNone(metrics.last_first_pcm_at)
        self.assertIsNotNone(metrics.last_playback_started_at)
        self.assertIsNotNone(metrics.last_playback_completed_at)

    def test_duplicate_tracked_chunk_is_prepared_only_once(self):
        prepare = Mock(return_value="audio")
        events = []
        reader = self.create_reader(
            prepare_chunk=prepare,
            play_prepared=Mock(),
            playback_executor=ImmediateExecutor(),
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append((stage, generation, details))
            ),
        )
        reader.active_generation = 1
        chunk = SpeechChunk(1, "Alice", "Hello.", ordinal=1)

        self.assertEqual(reader._prepare_if_current(chunk), "audio")
        self.assertIsNone(reader._prepare_if_current(chunk))

        prepare.assert_called_once_with(chunk)
        self.assertIn(
            (
                "duplicate-chunk-suppressed",
                1,
                {
                    "chunk_id": chunk.chunk_id,
                    "chunk_ordinal": 1,
                    "chunk_characters": 6,
                },
            ),
            events,
        )

    def test_streaming_backend_records_first_pcm_from_backend_callback(self):
        reader = self.create_reader(first_pcm_on_prepare=False)

        reader.record_first_pcm(123.5)

        self.assertEqual(reader.get_pipeline_metrics().last_first_pcm_at, 123.5)

    def test_first_pcm_reports_pipeline_latency_breakdown(self):
        events = []
        reader = self.create_reader(
            first_pcm_on_prepare=False,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append((stage, generation, occurred_at, details))
            ),
        )
        reader.active_generation = 3
        reader.current_chunk = SpeechChunk(3, "Alice", "Hello.", ordinal=1)
        reader.pipeline_metrics = replace(
            reader.pipeline_metrics,
            last_text_visible_at=10.0,
            last_ocr_stable_at=10.25,
            last_generation_started_at=10.5,
            last_playback_started_at=10.75,
        )

        reader.record_first_pcm(11.0)

        details = events[-1][3]
        self.assertEqual(details["from_text_visible_ms"], 1000)
        self.assertEqual(details["from_ocr_stable_ms"], 750)
        self.assertEqual(details["from_generation_started_ms"], 500)
        self.assertEqual(details["from_playback_started_ms"], 250)

    def test_first_pcm_keeps_origins_owned_by_the_playing_chunk(self):
        events = []
        reader = None

        def play_prepared(_chunk, _prepared):
            with reader.state_lock:
                reader.pipeline_metrics = replace(
                    reader.pipeline_metrics,
                    last_text_visible_at=10.9,
                )
            reader.record_first_pcm(11.0)

        reader = self.create_reader(
            first_pcm_on_prepare=False,
            prepare_chunk=Mock(),
            play_prepared=play_prepared,
            playback_executor=ImmediateExecutor(),
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append((stage, generation, occurred_at, details))
            ),
        )
        reader.active_generation = 3
        reader.pipeline_metrics = replace(
            reader.pipeline_metrics,
            last_text_visible_at=10.0,
            last_generation_started_at=10.5,
        )
        chunk = SpeechChunk(3, "Alice", "Hello.", ordinal=1)

        with patch("vntts.live.monotonic", return_value=10.75):
            reader._play_if_current(chunk, "prepared")

        details = next(event[3] for event in events if event[0] == "first-pcm")
        self.assertEqual(details["from_text_visible_ms"], 1000)
        self.assertEqual(details["from_generation_started_ms"], 500)
        self.assertEqual(details["from_playback_started_ms"], 250)

    def test_prefix_first_pcm_and_later_full_text_report_signed_order_separately(self):
        events = []
        reader = self.create_reader(
            first_pcm_on_prepare=False,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append((stage, generation, occurred_at, details))
            ),
        )
        reader.active_generation = 3

        reader.record_first_pcm(10.0)
        reader.record_canonical_full_text(line_id="story:3", timestamp=10.4)

        full_text = next(event for event in events if event[0] == "canonical-full-text")
        self.assertEqual(full_text[1], 3)
        self.assertEqual(full_text[3]["line_id"], "story:3")
        self.assertEqual(full_text[3]["first_pcm_before_canonical_full_ms"], 400)

    def test_full_text_origin_is_added_when_first_pcm_follows_confirmation(self):
        events = []
        reader = self.create_reader(
            first_pcm_on_prepare=False,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append((stage, generation, occurred_at, details))
            ),
        )
        reader.active_generation = 3

        reader.record_canonical_full_text(timestamp=10.0)
        reader.record_first_pcm(10.25)

        first_pcm = next(event for event in events if event[0] == "first-pcm")
        self.assertEqual(first_pcm[3]["from_canonical_full_text_ms"], 250)

    def test_retroactive_first_pcm_updates_an_existing_later_full_text_event(self):
        events = []
        reader = self.create_reader(
            first_pcm_on_prepare=False,
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append((stage, generation, occurred_at, details))
            ),
        )
        reader.active_generation = 3

        reader.record_canonical_full_text(timestamp=10.4)
        reader.record_first_pcm(10.0)

        full_text = [event for event in events if event[0] == "canonical-full-text"]
        self.assertEqual(full_text[-1][3]["first_pcm_before_canonical_full_ms"], 400)

    def test_typed_route_does_not_claim_first_pcm_before_player_observes_it(self):
        events = []
        reader = self.create_reader(
            prepare_chunk=Mock(),
            play_prepared=Mock(return_value=False),
            playback_executor=ImmediateExecutor(),
            pipeline_event_handler=lambda stage, generation, occurred_at, **details: (
                events.append(stage)
            ),
            first_pcm_on_prepare=True,
        )
        reader.active_generation = 1
        route = GeneratedAudioRoute(
            PreparedGeneratedAudio("game:1", "a" * 64, (), 24_000),
            AudioRouteTrace(
                None,
                "generated",
                "exact",
                None,
                None,
                "game:1",
                "generated-audio-entry-verified",
            ),
        )

        reader._play_if_current(SpeechChunk(1, "Alice", "Hello."), route)

        self.assertNotIn("first-pcm", events)
        self.assertIn("playback-completion", events)

    def test_observation_and_stable_sentence_record_pipeline_timestamps(self):
        reader = self.create_reader()

        reader._report_observation("Alice", "Visible text")
        with reader.state_lock:
            reader._record_speech_metrics_locked(sentence_ready=True)
        metrics = reader.get_pipeline_metrics()

        self.assertIsNotNone(metrics.last_text_visible_at)
        self.assertIsNotNone(metrics.last_speaker_resolved_at)
        self.assertIsNotNone(metrics.last_ocr_stable_at)

    def test_auto_advance_is_confirmed_only_after_generation_changes(self):
        auto_advance = Mock(return_value=True)
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=auto_advance,
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer") as timer:
            reader._run_auto_advance(3)

        auto_advance.assert_called_once_with()
        self.assertEqual(reader.last_auto_advance_dispatched_generation, 3)
        self.assertEqual(reader.pending_auto_advance_generation, 3)
        self.assertIsNone(reader.get_pipeline_metrics().last_auto_advance_at)
        self.assertIsNotNone(
            reader.get_pipeline_metrics().last_auto_advance_dispatched_at
        )
        timer.return_value.start.assert_called_once_with()
        state_changed.assert_called_once_with("dispatched", 3, 1)

        reader._set_generation(4)

        self.assertIsNone(reader.pending_auto_advance_generation)
        self.assertIsNotNone(reader.get_pipeline_metrics().last_auto_advance_at)
        state_changed.assert_called_with("confirmed", 3, 1)

    def test_unconfirmed_auto_advance_never_sends_a_second_key(self):
        auto_advance = Mock(return_value=True)
        report_error = Mock()
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=auto_advance,
            report_error=report_error,
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            reader._auto_advance_confirmation_expired(3, 1)
            reader._auto_advance_confirmation_expired(3, 1, True)

        auto_advance.assert_called_once_with()
        self.assertEqual(reader.failed_auto_advance_generation, 3)
        self.assertIsNone(reader.pending_auto_advance_generation)
        self.assertIsNone(reader.auto_advance_timer)
        report_error.assert_not_called()
        state_changed.assert_any_call("waiting", 3, 1)
        state_changed.assert_any_call("failed", 3, 1)

    def test_late_next_dialogue_confirms_without_another_key(self):
        auto_advance = Mock(return_value=True)
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=auto_advance,
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            reader._auto_advance_confirmation_expired(3, 1)
            reader._set_generation(4)

        auto_advance.assert_called_once_with()
        state_changed.assert_called_with("confirmed", 3, 1)
        self.assertIsNone(reader.failed_auto_advance_generation)

    def test_disabling_auto_advance_cancels_pending_confirmation(self):
        reader = self.create_reader(auto_advance=Mock(return_value=True))
        reader.active_generation = 3
        reader.dialog_ready_generation = 3
        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            confirmation_timer = reader.auto_advance_timer

            self.assertFalse(reader.set_auto_advance(None))

        confirmation_timer.cancel.assert_called_once_with()
        self.assertIsNone(reader.pending_auto_advance_generation)
        self.assertIsNone(reader.last_auto_advance_dispatched_generation)

    def test_initial_auto_advance_waits_for_focus_without_consuming_the_press(self):
        state_changed = Mock()
        auto_advance = Mock(return_value=True)
        reader = self.create_reader(
            auto_advance=auto_advance,
            focus_probe=Mock(return_value=False),
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer") as timer:
            reader._run_auto_advance(3)

        auto_advance.assert_not_called()
        timer.assert_called_once()
        timer.return_value.start.assert_called_once_with()
        state_changed.assert_called_once_with("focus-wait", 3, 0)

    def test_guarded_auto_advance_waits_for_cursor_owned_visible_frame(self):
        state_changed = Mock()
        auto_advance = Mock(return_value=True)
        reader = self.create_reader(
            auto_advance=auto_advance,
            require_visible_auto_advance=True,
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3
        reader.latest_frame_visible = True
        reader.latest_frame_fingerprint = "newer"
        reader.routed_frame_fingerprint = "older"

        with patch("vntts.live.Timer") as timer:
            reader._run_auto_advance(3)

        auto_advance.assert_not_called()
        timer.assert_called_once()
        timer.return_value.start.assert_called_once_with()
        state_changed.assert_called_once_with("visual-wait", 3, 0)

    def test_render_completion_can_bind_quiet_owner_frame_before_visual_gate(self):
        state_changed = Mock()
        auto_advance = Mock(return_value=True)
        reader = None

        def complete_render():
            reader.routed_frame_fingerprint = reader.latest_frame_fingerprint
            return True

        reader = self.create_reader(
            auto_advance=auto_advance,
            render_completion=complete_render,
            require_visible_auto_advance=True,
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3
        reader.latest_frame_visible = True
        reader.latest_frame_fingerprint = "full-render"
        reader.routed_frame_fingerprint = "early-prefix"

        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)

        auto_advance.assert_called_once_with()
        state_changed.assert_called_once_with("dispatched", 3, 1)

    def test_render_quiet_evidence_requires_focus_visibility_owner_and_time(self):
        clock = FakeClock()
        owner = ["event-1"]
        focused = [True]
        reader = self.create_reader(
            stable_frame_clock=clock,
            stable_frame_owner=lambda: owner[0],
            focus_probe=lambda: focused[0],
            render_quiet_minimum_seconds=0.8,
        )
        reader.latest_frame_visible = True
        reader.latest_render_fingerprint = "full-glyphs"
        reader.latest_render_owner = "event-1"
        reader.latest_render_changed_at = clock()

        clock.advance(0.79)
        self.assertIsNone(
            reader.current_frame_render_quiet_ms(expected_owner="event-1")
        )
        clock.advance(0.01)
        self.assertEqual(
            reader.current_frame_render_quiet_ms(expected_owner="event-1"),
            800,
        )
        reader.latest_render_fingerprint = "more-glyphs"
        reader.latest_render_changed_at = clock()
        clock.advance(0.79)
        self.assertIsNone(
            reader.current_frame_render_quiet_ms(expected_owner="event-1")
        )
        clock.advance(0.01)
        self.assertEqual(
            reader.current_frame_render_quiet_ms(expected_owner="event-1"),
            800,
        )
        owner[0] = "event-2"
        self.assertIsNone(
            reader.current_frame_render_quiet_ms(expected_owner="event-1")
        )
        owner[0] = "event-1"
        focused[0] = False
        self.assertIsNone(
            reader.current_frame_render_quiet_ms(expected_owner="event-1")
        )

    def test_focused_callback_refusal_blocks_generation_without_retry(self):
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=Mock(return_value=False),
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer") as timer:
            reader._run_auto_advance(3)

        timer.assert_not_called()
        self.assertEqual(reader.failed_auto_advance_generation, 3)
        state_changed.assert_called_once_with("blocked", 3, 0)

    def test_focus_loss_inside_callback_window_reschedules_without_consuming_key(self):
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=Mock(return_value=False),
            focus_probe=Mock(side_effect=[True, False]),
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer") as timer:
            reader._run_auto_advance(3)

        timer.assert_called_once()
        timer.return_value.start.assert_called_once_with()
        self.assertIsNone(reader.failed_auto_advance_generation)
        state_changed.assert_called_once_with("focus-wait", 3, 0)

    def test_typed_focus_refusal_reschedules_without_an_ambiguous_second_probe(self):
        state_changed = Mock()
        focus_probe = Mock(return_value=True)
        reader = self.create_reader(
            auto_advance=Mock(return_value=AutoAdvanceAttempt(False, "focus-wait")),
            focus_probe=focus_probe,
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer") as timer:
            reader._run_auto_advance(3)

        focus_probe.assert_called_once_with()
        timer.assert_called_once()
        timer.return_value.start.assert_called_once_with()
        self.assertIsNone(reader.failed_auto_advance_generation)
        state_changed.assert_called_once_with("focus-wait", 3, 0)

    def test_cursor_visual_confirmation_cancels_terminal_retry(self):
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=Mock(return_value=True),
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            confirmation_timer = reader.auto_advance_timer

            self.assertTrue(reader.confirm_pending_auto_advance())

        confirmation_timer.cancel.assert_called_once_with()
        self.assertIsNone(reader.pending_auto_advance_generation)
        state_changed.assert_called_with("confirmed", 3, 1)

    def test_focus_probe_failure_is_fail_closed_and_reported_once(self):
        error = RuntimeError("focus unavailable")
        report_error = Mock()
        reader = self.create_reader(
            focus_probe=Mock(side_effect=error),
            report_error=report_error,
        )

        self.assertFalse(reader._is_focused())
        self.assertFalse(reader._is_focused())

        report_error.assert_called_once_with(error)

    def test_focus_wait_notice_is_not_repeated_for_the_same_generation(self):
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=Mock(return_value=True),
            focus_probe=Mock(return_value=False),
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            reader._run_auto_advance(3)

        state_changed.assert_called_once_with("focus-wait", 3, 0)

    def test_initial_auto_advance_dispatches_once_after_focus_returns(self):
        auto_advance = Mock(return_value=True)
        state_changed = Mock()
        reader = self.create_reader(
            auto_advance=auto_advance,
            focus_probe=Mock(side_effect=[False, True]),
            auto_advance_state_changed=state_changed,
        )
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        with patch("vntts.live.Timer"):
            reader._run_auto_advance(3)
            reader._run_auto_advance(3)

        auto_advance.assert_called_once_with()
        self.assertEqual(
            state_changed.call_args_list,
            [call("focus-wait", 3, 0), call("dispatched", 3, 1)],
        )

    def test_auto_advance_confirmation_options_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "timeout_seconds must be positive"):
            self.create_reader(auto_advance_confirmation_timeout_seconds=0)
        with self.assertRaisesRegex(ValueError, "must be greater"):
            self.create_reader(
                auto_advance_confirmation_timeout_seconds=2,
                auto_advance_terminal_timeout_seconds=2,
            )

    def test_source_audio_without_completion_blocks_only_current_generation(self):
        auto_advance = Mock(return_value=True)
        reader = self.create_reader(auto_advance=auto_advance)
        reader.active_generation = 3
        reader.dialog_ready_generation = 3

        self.assertTrue(
            reader.block_auto_advance_for_generation(
                3,
                "Original game audio completion is unavailable",
            )
        )
        with patch("vntts.live.Timer") as timer:
            reader._maybe_auto_advance()

        timer.assert_not_called()
        self.assertEqual(reader.auto_advance_blocked_generation, 3)
        self.assertIn("completion", reader.auto_advance_block_reason)

        reader._set_generation(4)

        self.assertIsNone(reader.auto_advance_blocked_generation)
        self.assertIsNone(reader.auto_advance_block_reason)

    def test_new_dialog_allows_active_playback_to_finish_cleanly(self):
        interrupt_speech = Mock(return_value=True)
        reader = self.create_reader(interrupt_speech=interrupt_speech)
        reader.active_generation = 1
        reader.current_chunk = SpeechChunk(1, "Alice", "Old text.")

        reader._set_generation(2)

        interrupt_speech.assert_not_called()
        self.assertEqual(reader.active_generation, 2)

    def test_new_dialog_interrupts_backend_that_supports_safe_replacement(self):
        interrupt_speech = Mock(return_value=True)
        reader = self.create_reader(
            interrupt_speech=interrupt_speech,
            interrupt_on_dialog_replacement=True,
        )
        reader.active_generation = 1
        old_chunk = SpeechChunk(1, "Alice", "Old text.")
        reader.current_chunk = old_chunk

        reader._set_generation(2)

        interrupt_speech.assert_called_once_with()
        self.assertFalse(reader.wait_until_playable(old_chunk))

    def test_new_dialog_finishes_active_non_interrupting_playback(self):
        interrupt_speech = Mock()
        reader = self.create_reader(
            interrupt_speech=interrupt_speech,
            interrupt_on_dialog_replacement=False,
        )
        reader.active_generation = 1
        old_chunk = SpeechChunk(1, "Alice", "Finish this line.")
        reader.current_chunk = old_chunk

        reader._set_generation(2)

        interrupt_speech.assert_not_called()
        self.assertTrue(reader.wait_until_playable(old_chunk))

    def test_exact_route_seals_generation_against_late_ocr_suffix(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        events = []
        reader = self.create_reader(
            speech_executor=speech_executor,
            pipeline_event_handler=(
                lambda stage, generation, occurred_at, **details: events.append(
                    (stage, generation, details)
                )
            ),
        )
        reader.active_generation = 4
        current = SpeechChunk(4, "Rhiannon", "I, erhm ...", ordinal=1)
        reader.current_chunk = current

        self.assertTrue(reader.seal_generation(4))
        reader._schedule([SpeechChunk(4, "Rhiannon", "oe in", ordinal=2)])

        speech_executor.submit.assert_not_called()
        self.assertEqual(events[0][0], "late-chunk-suppressed")
        self.assertFalse(
            reader.wait_until_playable(SpeechChunk(4, "Rhiannon", "oe in"))
        )

    def test_new_dialog_unseals_previous_exact_route(self):
        reader = self.create_reader()
        reader.active_generation = 4
        reader.sealed_generation = 4

        reader._set_generation(5)

        self.assertIsNone(reader.sealed_generation)
        self.assertTrue(reader.wait_until_playable(SpeechChunk(5, "Hotelier", "Next")))

    def test_explicit_skip_still_stops_non_interrupting_playback(self):
        reader = self.create_reader(
            interrupt_speech=Mock(return_value=True),
            interrupt_on_dialog_replacement=False,
        )
        chunk = SpeechChunk(1, "Alice", "Stop this line.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.skip_current())

        self.assertFalse(reader.wait_until_playable(chunk))

    def test_new_dialog_cancels_stale_queued_speech(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        reader = self.create_reader(speech_executor=speech_executor)
        stale_future = Future()
        reader.active_generation = 1
        reader.speech_futures[stale_future] = SpeechChunk(1, "Alice", "Old text.")

        reader._set_generation(2)

        self.assertTrue(stale_future.cancelled())

    def test_rapid_dialog_replacement_schedules_latest_after_stale_preparation(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        reader = self.create_reader(
            speech_executor=speech_executor,
            playback_executor=Mock(),
            prepare_chunk=Mock(),
            play_prepared=Mock(),
            max_speech_jobs=1,
        )
        old_chunk = SpeechChunk(1, "Alice", "Old dialogue.")
        old_preparation = Future()
        old_preparation.set_running_or_notify_cancel()
        reader.active_generation = 1
        reader.speech_futures[old_preparation] = old_chunk

        reader._set_generation(2)
        reader._schedule([SpeechChunk(2, "Bob", "Skipped dialogue.")])
        reader._set_generation(3)
        newest_chunk = SpeechChunk(3, "Carol", "Newest dialogue.")
        reader._schedule([newest_chunk])
        old_preparation.set_result("stale audio")

        reader._preparation_finished(old_preparation)

        speech_executor.submit.assert_called_once_with(
            reader._prepare_if_current,
            newest_chunk,
        )
        self.assertIsNone(reader.deferred_chunk)

    def test_pause_toggle_blocks_and_releases_the_queue(self):
        reader = self.create_reader()

        self.assertTrue(reader.toggle_pause())
        self.assertTrue(reader.paused)
        self.assertFalse(reader.toggle_pause())
        self.assertFalse(reader.paused)

    def test_pausing_current_speech_interrupts_and_replays_it_on_resume(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        interrupt_speech = Mock()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=interrupt_speech,
        )
        chunk = SpeechChunk(1, "Alice", "Continue after pause.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.toggle_pause())
        self.assertFalse(reader.toggle_pause())

        interrupt_speech.assert_called_once_with()
        speech_executor.submit.assert_called_once_with(reader._speak_if_current, chunk)

    def test_pausing_during_synthesis_resumes_without_duplicate_replay(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=Mock(return_value=False),
        )
        chunk = SpeechChunk(1, "Alice", "Still synthesizing.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.toggle_pause())
        self.assertFalse(reader.toggle_pause())

        speech_executor.submit.assert_not_called()

    def test_skip_interrupts_only_when_speech_is_active(self):
        interrupt_speech = Mock()
        reader = self.create_reader(interrupt_speech=interrupt_speech)

        self.assertFalse(reader.skip_current())
        reader.current_chunk = SpeechChunk(1, "Alice", "Current text.")
        self.assertTrue(reader.skip_current())

        interrupt_speech.assert_called_once_with()

    def test_skip_marks_synthesizing_chunk_as_unplayable(self):
        reader = self.create_reader(interrupt_speech=Mock(return_value=False))
        chunk = SpeechChunk(1, "Alice", "Do not play this.")
        reader.active_generation = 1
        reader.current_chunk = chunk

        self.assertTrue(reader.skip_current())

        self.assertFalse(reader.wait_until_playable(chunk))

    def test_repeat_queues_last_spoken_chunk_for_current_dialog(self):
        speak_chunk = Mock()
        reader = self.create_reader(speak_chunk=speak_chunk)
        reader.active_generation = 3
        reader.last_spoken_chunk = SpeechChunk(1, "Alice", "Repeat me.")

        self.assertTrue(reader.repeat_last())

        speak_chunk.assert_called_once_with(SpeechChunk(3, "Alice", "Repeat me."))

    def test_clear_cancels_pending_speech_and_suppresses_current_dialog(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        interrupt_speech = Mock()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=interrupt_speech,
        )
        pending = Future()
        reader.active_generation = 4
        reader.current_chunk = SpeechChunk(4, "Alice", "Current text.")
        reader.speech_futures[pending] = SpeechChunk(4, "Alice", "Pending text.")

        self.assertTrue(reader.clear_queue())
        reader._schedule([SpeechChunk(4, "Alice", "Queued text.")])

        self.assertTrue(pending.cancelled())
        interrupt_speech.assert_called_once_with()
        speech_executor.submit.assert_not_called()

    def test_clear_interrupts_speech_preparation_that_is_already_running(self):
        interrupt_speech = Mock()
        reader = self.create_reader(interrupt_speech=interrupt_speech)
        preparing = Future()
        preparing.set_running_or_notify_cancel()
        reader.speech_futures[preparing] = SpeechChunk(
            4,
            "Alice",
            "Still synthesizing.",
        )

        self.assertTrue(reader.clear_queue())

        self.assertFalse(preparing.cancelled())
        interrupt_speech.assert_called_once_with()

    def test_emergency_stop_blocks_pending_ocr_until_explicit_resume(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        interrupt_speech = Mock()
        reader = self.create_reader(
            speech_executor=speech_executor,
            interrupt_speech=interrupt_speech,
        )
        reader.active_generation = 4
        reader.current_chunk = SpeechChunk(4, "Alice", "Current text.")

        self.assertTrue(reader.emergency_stop())
        reader._schedule([SpeechChunk(5, "Bob", "Pending OCR result.")])

        interrupt_speech.assert_called_once_with()
        speech_executor.submit.assert_not_called()
        self.assertTrue(reader.resume_after_emergency())
        reader._schedule([SpeechChunk(5, "Bob", "Explicit new read.")])
        speech_executor.submit.assert_called_once()

    def test_emergency_stop_cancels_capture_and_delayed_auto_advance(self):
        reader = self.create_reader()
        reader.capture_future = Mock()
        reader.capture_future.done.return_value = False
        timer = Mock()
        reader.auto_advance_timer = timer

        self.assertTrue(reader.emergency_stop())

        self.assertTrue(reader.stop_event.is_set())
        timer.cancel.assert_called_once_with()
        self.assertIsNone(reader.auto_advance_timer)

    def test_emergency_stop_cancels_queued_synthesis(self):
        speech_executor = Mock()
        speech_executor.submit.return_value = Future()
        reader = self.create_reader(speech_executor=speech_executor)
        pending = Future()
        reader.speech_futures[pending] = SpeechChunk(
            3,
            "Alice",
            "Not synthesized yet.",
        )

        self.assertTrue(reader.emergency_stop())

        self.assertTrue(pending.cancelled())

    def test_emergency_stop_interrupts_playback_and_invalidates_guard(self):
        interrupt_speech = Mock(return_value=True)
        reader = self.create_reader(interrupt_speech=interrupt_speech)
        chunk = SpeechChunk(7, "Alice", "Currently playing.")
        reader.active_generation = 7
        reader.current_chunk = chunk

        self.assertTrue(reader.emergency_stop())

        interrupt_speech.assert_called_once_with()
        self.assertFalse(reader.wait_until_playable(chunk))

    def test_speech_queue_is_bounded_and_merges_overflow_text(self):
        futures = [Future(), Future(), Future()]
        speech_executor = Mock()
        speech_executor.submit.side_effect = futures
        reader = self.create_reader(
            speech_executor=speech_executor,
            max_speech_jobs=2,
        )
        reader.active_generation = 1
        chunks = [
            SpeechChunk(1, "Alice", "One."),
            SpeechChunk(1, "Alice", "Two."),
            SpeechChunk(1, "Alice", "Three."),
            SpeechChunk(1, "Alice", "Four."),
        ]

        reader._schedule(chunks)

        self.assertEqual(speech_executor.submit.call_count, 2)
        self.assertEqual(reader.deferred_chunk.text, "Three. Four.")
        self.assertEqual(reader.get_pipeline_metrics().speech_queue_depth, 3)

        reader._speech_finished(futures[0])

        self.assertEqual(speech_executor.submit.call_count, 3)
        self.assertIsNone(reader.deferred_chunk)
        self.assertEqual(
            speech_executor.submit.call_args.args[1].text,
            "Three. Four.",
        )

    def test_observed_dialog_is_reported_only_when_it_changes(self):
        dialog_observed = Mock()
        reader = self.create_reader(dialog_observed=dialog_observed)

        reader._report_observation("Alice", "Hello   world")
        reader._report_observation("Alice", "Hello world")
        reader._report_observation("Bob", "Goodbye")

        self.assertEqual(
            dialog_observed.call_args_list,
            [
                unittest.mock.call("Alice", "Hello world"),
                unittest.mock.call("Bob", "Goodbye"),
            ],
        )

    def test_observation_callback_can_replace_ocr_identity_before_tracking(self):
        dialog_observed = Mock(return_value=("Canonical", "Complete sentence."))
        reader = self.create_reader(dialog_observed=dialog_observed)

        first = reader._report_observation("OCR name", "Complete sentnce.")
        repeated = reader._report_observation("OCR name", "Complete sentnce.")

        self.assertEqual(first, ("Canonical", "Complete sentence."))
        self.assertEqual(repeated, first)
        dialog_observed.assert_called_once_with("OCR name", "Complete sentnce.")

    def test_rejected_observation_has_no_tracker_identity(self):
        reader = self.create_reader(dialog_observed=Mock(return_value=False))

        self.assertIsNone(reader._report_observation("OCR name", "Unsafe guess."))

    def test_clearing_dialog_reports_session_boundary_once(self):
        dialog_observed = Mock()
        reader = self.create_reader(dialog_observed=dialog_observed)
        reader._report_observation("Alice", "Hello")

        reader._report_observation(None, "")
        reader._report_observation(None, "")

        self.assertEqual(
            dialog_observed.call_args_list,
            [
                unittest.mock.call("Alice", "Hello"),
                unittest.mock.call("Narrator", ""),
            ],
        )

    def test_rejected_empty_transition_keeps_previous_observation(self):
        dialog_observed = Mock(side_effect=[True, False, False])
        reader = self.create_reader(dialog_observed=dialog_observed)
        reader._report_observation("Alice", "Hello")

        first_empty = reader._report_observation(None, "")
        second_empty = reader._report_observation(None, "")

        self.assertIsNone(first_empty)
        self.assertIsNone(second_empty)
        self.assertEqual(reader.last_observation, ("Alice", "Hello"))
        self.assertEqual(
            dialog_observed.call_args_list,
            [
                unittest.mock.call("Alice", "Hello"),
                unittest.mock.call("Narrator", ""),
                unittest.mock.call("Narrator", ""),
            ],
        )

    def test_unfocused_game_skips_capture_and_reports_adaptive_interval(self):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        capture_state_changed = Mock()
        reader = self.create_reader(
            focus_probe=Mock(return_value=False),
            capture_state_changed=capture_state_changed,
        )

        reader._run(stop_event)

        reader.read_snapshot.assert_not_called()
        capture_state_changed.assert_called_once_with(False, 0.0025)
        stop_event.wait.assert_called_once_with(0.0025)

    def test_capture_continues_while_speech_is_active(self):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        reader = self.create_reader(interval_seconds=0.2)
        reader.current_chunk = SpeechChunk(1, "Alice", "Speaking now.")

        reader._run(stop_event)

        reader.read_snapshot.assert_called_once_with()
        stop_event.wait.assert_called_once_with(0.1)

    def test_split_capture_keeps_only_the_latest_unprocessed_frame(self):
        stop_event = Event()
        frames = []

        def capture_frame():
            frames.append(len(frames) + 1)
            if len(frames) == 3:
                stop_event.set()
            return frames[-1]

        reader = self.create_reader(
            ocr_executor=Mock(),
            capture_frame=capture_frame,
            recognize_frame=Mock(),
            frame_fingerprint=lambda frame: frame,
        )

        reader._run_capture(stop_event)

        metrics = reader.get_pipeline_metrics()
        self.assertEqual(metrics.captured_frames, 3)
        self.assertEqual(metrics.replaced_frames, 2)
        self.assertEqual(reader.latest_frame, 3)

    def test_changed_fingerprint_bypasses_stale_idle_capture_interval(self):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, False, True]
        capture_frame = Mock(side_effect=["same frame", "changed frame"])
        reader = self.create_reader(
            interval_seconds=0.2,
            ocr_executor=Mock(),
            capture_frame=capture_frame,
            recognize_frame=Mock(),
            frame_fingerprint=lambda frame: frame,
        )
        reader.latest_frame_fingerprint = "same frame"
        reader.next_capture_interval = 0.6

        reader._run_capture(stop_event)

        self.assertEqual(
            stop_event.wait.call_args_list,
            [call(0.6), call(0.1)],
        )

    def test_unchanged_fingerprint_preserves_adaptive_idle_interval(self):
        stop_event = Mock()
        stop_event.is_set.side_effect = [False, True]
        reader = self.create_reader(
            interval_seconds=0.2,
            ocr_executor=Mock(),
            capture_frame=Mock(return_value="same frame"),
            recognize_frame=Mock(),
            frame_fingerprint=lambda frame: frame,
        )
        reader.latest_frame_fingerprint = "same frame"
        reader.next_capture_interval = 0.6

        reader._run_capture(stop_event)

        stop_event.wait.assert_called_once_with(0.6)

    def test_split_ocr_reuses_unchanged_frame_until_auto_advance(self):
        stop_event = Event()
        first_observed = Event()
        second_observed = Event()

        class Tracker:
            generation = 1

            def __init__(self):
                self.observations = 0

            def observe(self, _character, _text):
                self.observations += 1
                if self.observations == 1:
                    first_observed.set()
                elif self.observations == 2:
                    second_observed.set()
                else:
                    stop_event.set()
                return []

            def flush(self):
                return []

            def is_idle_complete(self):
                return False

        recognize_frame = Mock(return_value=("Alice", "Unchanged text."))
        reader = self.create_reader(
            ocr_executor=Mock(),
            capture_frame=Mock(),
            recognize_frame=recognize_frame,
            frame_fingerprint=Mock(),
            tracker_factory=Tracker,
        )
        worker = Thread(target=reader._run_ocr, args=(stop_event,))
        worker.start()
        with reader.pause_condition:
            reader.latest_frame = "frame one"
            reader.latest_frame_fingerprint = "same text"
            reader.frame_version = 1
            reader.pause_condition.notify_all()
        self.assertTrue(first_observed.wait(timeout=1))
        with reader.pause_condition:
            reader.latest_frame = "frame two with animated background"
            reader.latest_frame_fingerprint = "same text"
            reader.frame_version = 2
            reader.pause_condition.notify_all()
        self.assertTrue(second_observed.wait(timeout=1))
        with reader.pause_condition:
            reader.pending_auto_advance_generation = reader.active_generation
            reader.latest_frame = "first frame after auto advance"
            reader.latest_frame_fingerprint = "same text"
            reader.frame_version = 3
            reader.pause_condition.notify_all()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(
            recognize_frame.call_args_list,
            [
                unittest.mock.call("frame one"),
                unittest.mock.call("first frame after auto advance"),
            ],
        )
        metrics = reader.get_pipeline_metrics()
        self.assertEqual(metrics.recognized_frames, 2)
        self.assertEqual(metrics.reused_frames, 1)


if __name__ == "__main__":
    unittest.main()
