"""Replay timing is admitted before snapshots, audio and worker acquisition."""

import unittest
from tempfile import TemporaryDirectory
from threading import TIMEOUT_MAX
from unittest.mock import Mock, patch

import tests.test_live_replay as replay_tests
from vntts.live_replay import LiveReplayRunner, load_live_replay_corpus, main


class FloatOnce:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __float__(self):
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("numeric callback called twice")
        return self.value


class ReplayConfigurationTest(unittest.TestCase):
    def test_invalid_timing_is_refused_before_replay_resources(self):
        corpus = Mock(dialogue=(object(),))
        with (
            patch(
                "vntts.live_replay._ReplaySnapshots",
                side_effect=AssertionError("snapshot reached"),
            ) as snapshots,
            patch.object(
                LiveReplayRunner,
                "_create_audio_stack",
                side_effect=AssertionError("audio reached"),
            ) as audio,
            patch(
                "vntts.live_replay.ThreadPoolExecutor",
                side_effect=AssertionError("executor reached"),
            ) as executors,
        ):
            for field, name, values in (
                (
                    "interval_seconds",
                    "interval",
                    (
                        0,
                        -1,
                        1.0,
                        10.0,
                        1e307,
                        float("nan"),
                        float("inf"),
                        10**400,
                        None,
                    ),
                ),
                (
                    "timeout_seconds",
                    "timeout",
                    (0, -1, TIMEOUT_MAX + 1, float("nan"), float("inf"), 10**400, None),
                ),
            ):
                for value in values:
                    with self.subTest(field=field, value_type=type(value).__name__):
                        with self.assertRaisesRegex(ValueError, "Live replay " + name):
                            LiveReplayRunner(corpus, **{field: value}).run()
            snapshots.assert_not_called()
            audio.assert_not_called()
            executors.assert_not_called()

    def test_timeout_cli_refuses_invalid_values_before_loading_corpus(self):
        with (
            patch(
                "vntts.live_replay.load_live_replay_corpus",
                side_effect=AssertionError("corpus reached"),
            ) as load,
            patch("vntts.live_replay.cli_error", return_value=1) as report,
        ):
            for value in (0, -1, TIMEOUT_MAX + 1, float("nan"), float("inf")):
                with self.subTest(value=value):
                    self.assertEqual(
                        main(["unused-corpus.json", "--timeout", str(value)]), 1
                    )
                    self.assertIsInstance(report.call_args.args[0], ValueError)
                    self.assertTrue(
                        str(report.call_args.args[0]).startswith("Live replay timeout")
                    )
            load.assert_not_called()

    def test_valid_coercions_and_default_timing_remain_compatible(self):
        corpus = Mock(dialogue=(object(),))
        defaults = LiveReplayRunner(corpus)
        self.assertEqual(
            (defaults.interval_seconds, defaults.timeout_seconds), (0.01, 30.0)
        )
        for interval, timeout in (
            ("0.02", "2"),
            (b"0.02", b"2"),
            (0.9, True),
            (0.001, TIMEOUT_MAX),
        ):
            with self.subTest(
                interval_type=type(interval).__name__,
                timeout_type=type(timeout).__name__,
            ):
                runner = LiveReplayRunner(
                    corpus, interval_seconds=interval, timeout_seconds=timeout
                )
                self.assertEqual(runner.interval_seconds, float(interval))
                self.assertEqual(runner.timeout_seconds, float(timeout))
        interval, timeout = FloatOnce(0.02), FloatOnce(2.0)
        runner = LiveReplayRunner(
            corpus, interval_seconds=interval, timeout_seconds=timeout
        )
        self.assertEqual((runner.interval_seconds, runner.timeout_seconds), (0.02, 2.0))
        self.assertEqual((interval.calls, timeout.calls), (1, 1))

    def test_runtime_updates_are_refused_before_replay_acquisition(self):
        for field, value, name in (
            ("interval_seconds", 1.0, "interval"),
            ("timeout_seconds", TIMEOUT_MAX + 1, "timeout"),
        ):
            with self.subTest(field=field):
                runner = LiveReplayRunner(Mock(dialogue=(object(),)))
                setattr(runner, field, value)
                with patch(
                    "vntts.live_replay._ReplaySnapshots",
                    side_effect=AssertionError("snapshot reached"),
                ) as snapshots:
                    with self.assertRaisesRegex(ValueError, "Live replay " + name):
                        runner.run()
                    snapshots.assert_not_called()

    def test_valid_interval_updates_reach_actual_reader_options(self):
        class ReaderReached(RuntimeError):
            pass

        with TemporaryDirectory() as directory:
            path = replay_tests.LiveReplayTest().create_corpus(directory)
            runner = LiveReplayRunner(load_live_replay_corpus(path))
            for interval in (0.9, 0.02):
                with self.subTest(interval=interval):
                    runner.interval_seconds = interval
                    with (
                        patch("vntts.live_replay.ThreadPoolExecutor"),
                        patch(
                            "vntts.live_replay.LiveDialogReader",
                            side_effect=ReaderReached,
                        ) as reader,
                        self.assertRaises(ReaderReached),
                    ):
                        runner.run()
                    options = reader.call_args.kwargs
                    self.assertEqual(options["interval_seconds"], interval)
                    self.assertEqual(
                        options["auto_advance_confirmation_timeout_seconds"],
                        9.0 if interval == 0.9 else 5.0,
                    )
                    self.assertEqual(
                        options["auto_advance_terminal_timeout_seconds"], 10.0
                    )

    def test_empty_corpus_remains_the_first_failure(self):
        with self.assertRaisesRegex(ValueError, "no dialogue frames"):
            LiveReplayRunner(
                Mock(dialogue=()),
                interval_seconds=10**400,
                timeout_seconds=float("inf"),
            )
