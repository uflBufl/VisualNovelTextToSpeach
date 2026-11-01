import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from tests import test_controller_transition_observation as sequence_fixtures
from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.auto_advance import DialogueAdvancer
from vntts.controller import AppController
from vntts.playback import PlaybackOutcome, PlaybackStatus
from vntts.settings import AppSettings
from vntts.support import GenerationTimelineLog


class InputDispatchDiagnosticsTest(unittest.TestCase):
    @contextmanager
    def ordinary(self, *, broken_probe=False, broken_sink=False):
        harness = AutoAdvanceFakeFrameHarness()
        events, keys = [], []
        state = {"pressed": False, "foreground_pid": 11}
        timeline = GenerationTimelineLog()

        def snapshot():
            if broken_probe:
                raise RuntimeError("PRIVATE focus failure")
            return {
                "selected_window_id": 7,
                "selected_pid": 11,
                "foreground_pid": state["foreground_pid"],
                "focused": state["foreground_pid"] == 11,
            }

        def record(stage, generation, occurred_at, **details):
            if stage == "key-input":
                self.assertFalse(state["pressed"])
                if broken_sink:
                    raise RuntimeError("PRIVATE diagnostic sink failure")
            events.append((stage, generation, details))
            timeline.record(stage, generation, occurred_at, **details)

        def press(key):
            state["pressed"] = True
            keys.append(("press", key))
            state["foreground_pid"] = 22

        def release(key):
            keys.append(("release", key))
            state["pressed"] = False

        def advancer(key, **options):
            return DialogueAdvancer(
                key,
                platform="linux",
                controller_factory=lambda: SimpleNamespace(
                    press=press, release=release
                ),
                **options,
            )

        controller = AppController(
            AppSettings(capture_mode="window", auto_advance_enabled=True),
            pipeline_event_handler=record,
        )
        controller.capture_target = SimpleNamespace(
            input_focus_snapshot=snapshot, is_focused=lambda: True
        )
        controller.live_reader = harness.reader
        harness.reader.auto_advance = controller._auto_advance_dialog
        harness.reader.pipeline_event_handler = record
        with (
            patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create),
            patch("vntts.controller.DialogueAdvancer", side_effect=advancer),
        ):
            harness.start()
            try:
                harness.prepare_ready_dialogue()
                harness.finish_playback()
                yield controller, harness, events, keys, timeline
            finally:
                harness.stop()

    def test_input_dispatch_and_confirmation_share_id_with_native_focus(self):
        with self.ordinary() as (_, harness, events, keys, timeline):
            harness.fire_advance()
            self.assertTrue(harness.reader.confirm_pending_auto_advance())
            relevant = [
                event
                for event in events
                if event[0] in {"key-input", "key-dispatch", "confirmed-next-dialogue"}
            ]
            self.assertEqual(
                [e[0] for e in relevant],
                ["key-input", "key-dispatch", "confirmed-next-dialogue"],
            )
            ids = {e[2]["dispatch_id"] for e in relevant}
            self.assertEqual(len(ids), 1)
            UUID(next(iter(ids)))
            native = relevant[0][2]
            self.assertEqual(native["before_foreground_pid"], 11)
            self.assertEqual(native["after_foreground_pid"], 22)
            self.assertTrue(native["before_focused"])
            self.assertFalse(native["after_focused"])
            self.assertEqual(native["input_origin"], "automatic")
            self.assertLessEqual(
                native["press_completed_monotonic"], native["release_started_monotonic"]
            )
            self.assertEqual([e[0] for e in keys], ["press", "release"])
            stored = [
                e
                for t in timeline.snapshot()
                for e in t["events"]
                if e["stage"]
                in {"key-input", "key-dispatch", "confirmed-next-dialogue"}
            ]
            self.assertEqual({e["dispatch_id"] for e in stored}, ids)
            self.assertEqual(harness.errors, [])

    def test_timeout_keeps_native_dispatch_id_without_a_second_key(self):
        with self.ordinary() as (_, harness, events, keys, _):
            harness.fire_advance()
            harness.timer_scheduler.fire_next("_auto_advance_confirmation_expired")
            harness.timer_scheduler.fire_next("_auto_advance_confirmation_expired")
            native = next(e[2] for e in events if e[0] == "key-input")
            timeout = next(e[2] for e in events if e[0] == "auto-advance-timeout")
            self.assertEqual(timeout["dispatch_id"], native["dispatch_id"])
            self.assertEqual(len(keys), 2)

    def test_operator_request_cancels_timer_and_still_requires_fresh_ocr(self):
        with self.ordinary() as (_, harness, events, keys, _):
            reader = harness.reader
            timer = harness.timer_scheduler.pending("_run_auto_advance")[0]
            reader._run_auto_advance(
                reader.active_generation,
                reader._auto_advance_timer_serial,
                input_origin="user-diagnostic",
            )
            self.assertTrue(timer.cancelled)
            self.assertEqual(keys, [])
            timer.function(*timer.args)
            self.assertEqual(reader._advance_input_origin, "user-diagnostic")
            frame = reader.latest_frame
            harness.push(frame["character"], frame["text"], background="fresh")
            native = next(e[2] for e in events if e[0] == "key-input")
            self.assertEqual(native["input_origin"], "user-diagnostic")
            self.assertEqual(len(keys), 2)

    def test_probe_and_sink_errors_do_not_change_input_or_enable_retries(self):
        for broken_probe, broken_sink in ((True, False), (False, True)):
            with self.subTest(probe=broken_probe, sink=broken_sink):
                with self.ordinary(
                    broken_probe=broken_probe, broken_sink=broken_sink
                ) as (_, harness, events, keys, _):
                    harness.fire_advance()
                    frame = harness.reader.latest_frame
                    harness.push(frame["character"], frame["text"], background="later")
                    self.assertEqual(len(keys), 2)
                    self.assertEqual(
                        harness.reader.last_auto_advance_dispatched_generation, 1
                    )
                    if broken_probe:
                        native = next(e[2] for e in events if e[0] == "key-input")
                        self.assertEqual(
                            native["before_focus_error_type"], "RuntimeError"
                        )
                        self.assertEqual(
                            native["after_focus_error_type"], "RuntimeError"
                        )

    def test_diagnostic_request_preserves_reading_and_mode_barriers(self):
        for barrier in ("pause", "stop", "disabled", "audio-manual"):
            with self.subTest(barrier=barrier):
                with self.ordinary() as (controller, harness, _, keys, _):
                    reader = harness.reader
                    if barrier == "pause":
                        reader.paused = True
                    elif barrier == "stop":
                        reader.stop_event.set()
                    else:
                        controller.settings = controller.settings.updated(
                            auto_advance_enabled=barrier != "disabled",
                            live_sequence_mode="audio-manual"
                            if barrier == "audio-manual"
                            else "off",
                        )
                        reader.set_auto_advance(
                            controller._live_auto_advance_callback()
                        )
                    reader._run_auto_advance(
                        reader.active_generation, input_origin="user-diagnostic"
                    )
                    self.assertIsNone(reader._advance_requested_generation)
                    self.assertEqual(keys, [])

    def test_sequence_sender_and_authorization_share_id(self):
        fixture = sequence_fixtures.ControllerTransitionObservationTest()
        controller, lines = fixture.controller()
        controller.settings = controller.settings.updated(
            auto_advance_enabled=True, capture_mode="window"
        )
        chunk = fixture.accept(controller, lines[0])
        lease = controller._begin_sequence_playback(chunk)
        controller._finish_sequence_playback(
            lease, PlaybackOutcome(PlaybackStatus.COMPLETED, 1.0, audio_source="live")
        )
        controller._is_game_focused = lambda: True
        events = []
        controller.pipeline_event_handler = lambda *args, **details: events.append(
            (args, details)
        )

        def construct(_key, **options):
            def advance():
                options["diagnostic_handler"](
                    {"completed_monotonic": 1.0, "success": True}
                )
                return True

            return SimpleNamespace(advance=advance)

        with patch("vntts.controller.DialogueAdvancer", side_effect=construct):
            outcome = controller._sequence_auto_advance_dialog()
        self.assertTrue(outcome)
        matching = [
            e
            for e in events
            if e[0][0] in {"key-input", "sequence-key-dispatch-authorized"}
        ]
        self.assertEqual(len(matching), 2)
        self.assertEqual({e[1]["dispatch_id"] for e in matching}, {outcome.dispatch_id})


if __name__ == "__main__":
    unittest.main()
