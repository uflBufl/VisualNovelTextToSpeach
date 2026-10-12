"""Live cadence and queue settings stay valid before worker or timer acquisition."""

import sys
import unittest
from concurrent.futures import Future, TimeoutError
from threading import TIMEOUT_MAX
from unittest.mock import Mock

from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.live import (
    AdaptiveCapturePolicy,
    AdaptiveSpeechBackpressure,
    LiveDialogReader,
)


def make_reader(**overrides):
    options = dict(
        capture_executor=Mock(),
        ocr_executor=Mock(),
        speech_executor=Mock(),
        playback_executor=Mock(),
        capture_frame=Mock(),
        recognize_frame=Mock(),
        prepare_chunk=Mock(),
        play_prepared=Mock(),
        report_error=Mock(),
    )
    options.update(overrides)
    return LiveDialogReader(**options)


class LiveConfigurationTest(unittest.TestCase):
    def test_nonfinite_timings_are_rejected_by_each_owner(self):
        owners = (
            (AdaptiveSpeechBackpressure, ("cooldown_seconds",)),
            (
                AdaptiveCapturePolicy,
                (
                    "base_interval",
                    "fast_interval",
                    "idle_interval",
                    "unfocused_interval",
                ),
            ),
            (
                make_reader,
                (
                    "frame_recheck_interval_seconds",
                    "render_quiet_minimum_seconds",
                    "stable_frame_minimum_seconds",
                    "interval_seconds",
                    "auto_advance_delay_seconds",
                    "auto_advance_confirmation_timeout_seconds",
                    "auto_advance_terminal_timeout_seconds",
                ),
            ),
        )
        for factory, names in owners:
            for name in names:
                for value in (float("nan"), float("inf"), -float("inf"), 10**400):
                    with self.subTest(
                        owner=factory.__name__,
                        field=name,
                        value_type=type(value).__name__,
                    ):
                        with self.assertRaisesRegex(
                            ValueError, name + " must be finite"
                        ):
                            factory(**{name: value})

    def test_capture_intervals_require_positive_explicit_values(self):
        for name in (
            "base_interval",
            "fast_interval",
            "idle_interval",
            "unfocused_interval",
        ):
            for value in (0, -1, TIMEOUT_MAX + 1):
                with self.subTest(field=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        AdaptiveCapturePolicy(**{name: value})

    def test_counts_must_be_positive_integral_numbers(self):
        for factory, name in (
            (AdaptiveSpeechBackpressure, "normal_jobs"),
            (AdaptiveCapturePolicy, "unchanged_frames"),
            (make_reader, "max_speech_jobs"),
        ):
            for value in (0, -1, 1.5, float("nan"), float("inf")):
                with self.subTest(owner=factory.__name__, field=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        factory(**{name: value})

    def test_counts_keep_exact_large_ints_and_integral_compatibility(self):
        for factory, name in (
            (AdaptiveSpeechBackpressure, "normal_jobs"),
            (AdaptiveCapturePolicy, "unchanged_frames"),
            (make_reader, "max_speech_jobs"),
        ):
            for value, expected in ((True, 1), (2.0, 2), (10**400, 10**400)):
                result = factory(**{name: value})
                with self.subTest(
                    owner=factory.__name__, field=name, value_type=type(value).__name__
                ):
                    self.assertEqual(getattr(result, name), expected)
                    self.assertIs(type(getattr(result, name)), int)

    def test_runtime_updates_reject_invalid_values_without_replacing_state(self):
        reader = make_reader()
        for name, values in (
            ("interval_seconds", (float("nan"), float("inf"), 0, TIMEOUT_MAX + 1)),
            (
                "auto_advance_delay_seconds",
                (float("nan"), float("inf"), -1, TIMEOUT_MAX + 1),
            ),
            ("max_speech_jobs", (float("nan"), 1.5, 0)),
        ):
            previous = getattr(reader, name)
            for value in values:
                with self.subTest(field=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        setattr(reader, name, value)
                    self.assertEqual(getattr(reader, name), previous)
        reader.interval_seconds = 0.001
        reader.auto_advance_delay_seconds = 0
        reader.max_speech_jobs = 3
        self.assertEqual(
            (
                reader.interval_seconds,
                reader.auto_advance_delay_seconds,
                reader.max_speech_jobs,
            ),
            (0.001, 0, 3),
        )

    def test_timer_bounds_follow_actual_confirmation_and_terminal_delays(self):
        for options in (
            {"auto_advance_delay_seconds": TIMEOUT_MAX + 1},
            {"auto_advance_confirmation_timeout_seconds": TIMEOUT_MAX + 1},
            {"auto_advance_terminal_timeout_seconds": TIMEOUT_MAX + 3},
        ):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "thread timeout range"),
            ):
                make_reader(**options)
        reader = make_reader(
            auto_advance_delay_seconds=0,
            auto_advance_terminal_timeout_seconds=TIMEOUT_MAX + 2,
        )
        self.assertEqual(
            reader.auto_advance_terminal_timeout_seconds
            - reader.auto_advance_confirmation_timeout_seconds,
            TIMEOUT_MAX,
        )
        make_reader(
            auto_advance_confirmation_timeout_seconds=TIMEOUT_MAX,
            auto_advance_terminal_timeout_seconds=2 * TIMEOUT_MAX,
        )

    def test_wait_rejects_nonfinite_and_unrepresentable_timeouts(self):
        reader = make_reader()
        for value in (
            float("nan"),
            float("inf"),
            -float("inf"),
            10**400,
            TIMEOUT_MAX + 1,
        ):
            with (
                self.subTest(value_type=type(value).__name__),
                self.assertRaisesRegex(ValueError, "timeout_seconds"),
            ):
                reader.wait(timeout_seconds=value)

    def test_wait_preserves_unbounded_string_and_negative_timeout_contracts(self):
        reader = make_reader()
        completed = Future()
        completed.set_result(None)
        reader.capture_future = completed
        reader.wait()
        reader.wait(timeout_seconds="1")
        reader.wait(timeout_seconds=TIMEOUT_MAX)
        with self.assertRaises(TimeoutError):
            reader.wait(timeout_seconds=-1)

    def test_comparison_durations_do_not_receive_a_thread_limit(self):
        maximum = sys.float_info.max
        policy = AdaptiveSpeechBackpressure(cooldown_seconds=maximum)
        reader = make_reader(
            frame_recheck_interval_seconds=maximum,
            render_quiet_minimum_seconds=maximum,
            stable_frame_minimum_seconds=maximum,
            auto_advance_delay_seconds=0,
        )
        self.assertEqual(policy.cooldown_seconds, maximum)
        self.assertEqual(reader.frame_recheck_interval_seconds, maximum)
        self.assertEqual(reader.render_quiet_minimum_seconds, maximum)
        self.assertEqual(reader.stable_frame_minimum_seconds, maximum)
        self.assertEqual(
            make_reader(stable_frame_minimum_seconds=0).stable_frame_minimum_seconds, 0
        )

    def test_bool_timings_keep_existing_numeric_and_zero_contracts(self):
        self.assertEqual(
            AdaptiveSpeechBackpressure(cooldown_seconds=True).cooldown_seconds, 1
        )
        self.assertEqual(AdaptiveCapturePolicy(base_interval=True).base_interval, 1)
        reader = make_reader(
            interval_seconds=True,
            frame_recheck_interval_seconds=True,
            render_quiet_minimum_seconds=True,
            stable_frame_minimum_seconds=False,
            auto_advance_delay_seconds=False,
        )
        self.assertEqual(reader.interval_seconds, 1)
        self.assertEqual(reader.stable_frame_minimum_seconds, 0)
        self.assertEqual(reader.auto_advance_delay_seconds, 0)
        for factory, name in (
            (AdaptiveSpeechBackpressure, "cooldown_seconds"),
            (AdaptiveCapturePolicy, "base_interval"),
            (make_reader, "interval_seconds"),
        ):
            with (
                self.subTest(field=name),
                self.assertRaisesRegex(ValueError, "must be positive"),
            ):
                factory(**{name: False})

    def test_default_and_equal_capture_cadences_keep_existing_policy(self):
        policy = AdaptiveCapturePolicy(base_interval=0.001)
        self.assertEqual(
            (
                policy.base_interval,
                policy.fast_interval,
                policy.idle_interval,
                policy.unfocused_interval,
            ),
            (0.001, 0.05, 0.003, 0.0025),
        )
        policy = AdaptiveCapturePolicy(
            base_interval=0.1,
            fast_interval=0.1,
            idle_interval=0.1,
            unfocused_interval=0.1,
        )
        self.assertEqual(policy.observe("Alice", "hello"), 0.1)
        self.assertEqual(policy.observe(None, None, focused=False), 0.1)

    def test_reader_does_not_construct_or_constrain_custom_policy_options(self):
        tracker, policy = Mock(), Mock()
        tracker_options = {"custom": "tracker"}
        policy_options = {"custom": "policy"}
        reader = make_reader(
            tracker_factory=tracker,
            adaptive_policy_factory=policy,
            tracker_options=tracker_options,
            adaptive_options=policy_options,
        )
        tracker.assert_not_called()
        policy.assert_not_called()
        self.assertIs(reader.tracker_options, tracker_options)
        self.assertIs(reader.adaptive_options, policy_options)

    def test_unrepresentable_recheck_telemetry_does_not_prevent_ocr(self):
        harness = AutoAdvanceFakeFrameHarness(
            frame_recheck_required=lambda: True,
            frame_recheck_interval_seconds=sys.float_info.max,
        )
        harness.start()
        self.addCleanup(harness.stop)
        harness.push("Alice", "hello", background="first")
        self.assertEqual(len(harness.recognized_frames), 1)
        self.assertEqual(harness.errors, [])
        details = next(
            details
            for stage, _generation, _at, details in harness.pipeline_events
            if stage == "canonical-prefix-visual-recheck"
        )
        self.assertIsNone(details["recheck_interval_ms"])
