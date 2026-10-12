"""Millisecond settings stay usable when projected into runtime seconds."""

import io
import math
import sys
import unittest
from contextlib import redirect_stdout
from threading import TIMEOUT_MAX
from unittest.mock import patch

from vntts.live_tracking import IncrementalDialogTracker
from vntts.numeric_values import optional_milliseconds_seconds
from vntts.runtime_config import (
    get_auto_advance_delay_seconds,
    get_live_configuration,
)
from vntts.settings import AppSettings, settings_schema_version


class LiveRuntimeConfigurationTest(unittest.TestCase):
    def test_milliseconds_are_scaled_before_float_admission(self):
        self.assertEqual(optional_milliseconds_seconds(10**310), 1e307)
        self.assertEqual(optional_milliseconds_seconds(125.5), 0.1255)
        for value in (True, False, "200", None, float("nan"), float("inf"), 10**400):
            with self.subTest(value_type=type(value).__name__):
                self.assertIsNone(optional_milliseconds_seconds(value))

    def test_unrepresentable_saved_durations_recover_with_existing_warnings(self):
        defaults = AppSettings()
        for field in (
            "live_interval_ms",
            "live_idle_flush_ms",
            "auto_advance_delay_ms",
        ):
            with self.subTest(field=field):
                warnings = []
                settings = AppSettings.from_mapping(
                    {"schema_version": settings_schema_version, field: 10**400},
                    warn=warnings.append,
                )
                self.assertEqual(getattr(settings, field), getattr(defaults, field))
                self.assertEqual(
                    warnings, [f"Invalid {field!r} setting; using its default"]
                )
                self.assertEqual(
                    get_live_configuration(settings), get_live_configuration(defaults)
                )
                self.assertEqual(get_auto_advance_delay_seconds(settings), 0.35)

    def test_saved_wait_durations_use_the_actual_thread_limit(self):
        maximum_ms = int(TIMEOUT_MAX * 1000)
        for field in ("live_interval_ms", "auto_advance_delay_ms"):
            for milliseconds, accepted in (
                (maximum_ms, True),
                (maximum_ms + 1, False),
            ):
                with self.subTest(field=field, accepted=accepted):
                    warnings = []
                    settings = AppSettings.from_mapping(
                        {
                            "schema_version": settings_schema_version,
                            field: milliseconds,
                        },
                        warn=warnings.append,
                    )
                    expected = (
                        milliseconds if accepted else getattr(AppSettings(), field)
                    )
                    self.assertEqual(getattr(settings, field), expected)
                    self.assertEqual(bool(warnings), not accepted)
                    seconds = (
                        get_live_configuration(settings)["interval_seconds"]
                        if field == "live_interval_ms"
                        else get_auto_advance_delay_seconds(settings)
                    )
                    self.assertEqual(
                        seconds, TIMEOUT_MAX if accepted else expected / 1000
                    )

    def test_large_representable_idle_duration_is_not_a_thread_wait(self):
        settings = AppSettings.from_mapping(
            {
                "schema_version": settings_schema_version,
                "live_idle_flush_ms": 10**310,
            }
        )
        self.assertEqual(settings.live_idle_flush_ms, 10**310)
        seconds = get_live_configuration(settings)["tracker_options"][
            "idle_flush_seconds"
        ]
        self.assertEqual(seconds, 1e307)
        self.assertTrue(math.isfinite(seconds))
        with patch.dict(
            "os.environ", {"VNTTS_LIVE_IDLE_FLUSH_MS": str(10**310)}, clear=True
        ):
            self.assertEqual(
                get_live_configuration()["tracker_options"]["idle_flush_seconds"],
                seconds,
            )

    def test_invalid_environment_duration_recovers_before_runtime_projection(self):
        for variable in ("VNTTS_LIVE_INTERVAL_MS", "VNTTS_LIVE_IDLE_FLUSH_MS"):
            output = io.StringIO()
            with (
                self.subTest(variable=variable),
                patch.dict("os.environ", {variable: str(10**400)}, clear=True),
                redirect_stdout(output),
            ):
                self.assertEqual(
                    get_live_configuration(), get_live_configuration(AppSettings())
                )
                self.assertIn(f"Invalid {variable}", output.getvalue())
        saved = AppSettings(live_interval_ms=175, live_idle_flush_ms=625)
        warnings = []
        configured = saved.with_environment_overrides(
            {
                "VNTTS_LIVE_INTERVAL_MS": str(10**400),
                "VNTTS_LIVE_IDLE_FLUSH_MS": str(10**400),
            },
            warn=warnings.append,
        )
        self.assertEqual(configured.live_interval_ms, 175)
        self.assertEqual(configured.live_idle_flush_ms, 625)
        self.assertEqual(len(warnings), 2)

    def test_environment_interval_uses_the_actual_thread_limit(self):
        maximum_ms = int(TIMEOUT_MAX * 1000)
        for milliseconds, expected in (
            (maximum_ms, TIMEOUT_MAX),
            (maximum_ms + 1, 0.2),
        ):
            output = io.StringIO()
            with (
                self.subTest(milliseconds=milliseconds),
                patch.dict(
                    "os.environ",
                    {"VNTTS_LIVE_INTERVAL_MS": str(milliseconds)},
                    clear=True,
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(get_live_configuration()["interval_seconds"], expected)
                self.assertEqual(bool(output.getvalue()), milliseconds > maximum_ms)

    def test_manually_constructed_invalid_durations_use_defaults(self):
        for value in (True, "200", float("nan"), float("inf"), -1, 10**400):
            with self.subTest(value_type=type(value).__name__):
                settings = AppSettings(
                    live_interval_ms=value,
                    live_idle_flush_ms=value,
                    auto_advance_delay_ms=value,
                )
                self.assertEqual(
                    get_live_configuration(settings),
                    get_live_configuration(AppSettings()),
                )
                self.assertEqual(get_auto_advance_delay_seconds(settings), 0.35)
        maximum_ms = int(TIMEOUT_MAX * 1000)
        settings = AppSettings(
            live_interval_ms=maximum_ms + 1,
            auto_advance_delay_ms=maximum_ms + 1,
        )
        self.assertEqual(get_live_configuration(settings)["interval_seconds"], 0.2)
        self.assertEqual(get_auto_advance_delay_seconds(settings), 0.35)

    def test_stability_count_fits_native_deques_without_preallocating_history(self):
        for value in (2, sys.maxsize):
            with self.subTest(value=value):
                warnings = []
                settings = AppSettings.from_mapping(
                    {
                        "schema_version": settings_schema_version,
                        "live_stability_frames": value,
                    },
                    warn=warnings.append,
                )
                self.assertEqual(settings.live_stability_frames, value)
                self.assertEqual(warnings, [])
                tracker = IncrementalDialogTracker(
                    **get_live_configuration(settings)["tracker_options"]
                )
                self.assertEqual(tracker.history.maxlen, value)
                self.assertEqual(tracker.pending_history.maxlen, value)
                self.assertEqual(len(tracker.history), 0)
                self.assertEqual(len(tracker.pending_history), 0)
                with patch.dict(
                    "os.environ",
                    {"VNTTS_LIVE_STABILITY_FRAMES": str(value)},
                    clear=True,
                ):
                    options = get_live_configuration()["tracker_options"]
                self.assertEqual(options["stability_frames"], value)
                self.assertEqual(
                    IncrementalDialogTracker(**options).history.maxlen, value
                )

    def test_oversized_stability_recovers_before_native_tracker_construction(self):
        for value in (sys.maxsize + 1, 10**400):
            with self.subTest(value=value):
                warnings = []
                settings = AppSettings.from_mapping(
                    {
                        "schema_version": settings_schema_version,
                        "live_stability_frames": value,
                    },
                    warn=warnings.append,
                )
                self.assertEqual(settings.live_stability_frames, 2)
                self.assertEqual(
                    warnings,
                    ["Invalid 'live_stability_frames' setting; using its default"],
                )
                for configured in (settings, AppSettings(live_stability_frames=value)):
                    options = get_live_configuration(configured)["tracker_options"]
                    self.assertEqual(options["stability_frames"], 2)
                    self.assertEqual(
                        IncrementalDialogTracker(**options).history.maxlen, 2
                    )
                output = io.StringIO()
                with (
                    patch.dict(
                        "os.environ",
                        {"VNTTS_LIVE_STABILITY_FRAMES": str(value)},
                        clear=True,
                    ),
                    redirect_stdout(output),
                ):
                    options = get_live_configuration()["tracker_options"]
                self.assertIn("Invalid VNTTS_LIVE_STABILITY_FRAMES", output.getvalue())
                self.assertEqual(options["stability_frames"], 2)
                self.assertEqual(IncrementalDialogTracker(**options).history.maxlen, 2)
                warnings = []
                overridden = AppSettings(
                    live_stability_frames=4
                ).with_environment_overrides(
                    {"VNTTS_LIVE_STABILITY_FRAMES": str(value)}, warn=warnings.append
                )
                self.assertEqual(overridden.live_stability_frames, 4)
                self.assertEqual(len(warnings), 1)

    def test_manual_stability_requires_an_exact_native_integer(self):
        for value in (True, False, 1, 2.0, 2.5, "2", None):
            with self.subTest(value=value):
                options = get_live_configuration(
                    AppSettings(live_stability_frames=value)
                )["tracker_options"]
                self.assertIs(type(options["stability_frames"]), int)
                self.assertEqual(options["stability_frames"], 2)
                self.assertEqual(IncrementalDialogTracker(**options).history.maxlen, 2)

    def test_counts_remain_exact_integers(self):
        settings = AppSettings.from_mapping(
            {
                "schema_version": settings_schema_version,
                "live_min_chunk_characters": 10**400,
            }
        )
        for configured in (settings, None):
            with patch.dict(
                "os.environ",
                {
                    "VNTTS_LIVE_MIN_CHUNK_CHARACTERS": str(10**400),
                },
                clear=True,
            ):
                options = get_live_configuration(configured)["tracker_options"]
            self.assertEqual(options["min_chunk_characters"], 10**400)
            self.assertIs(type(options["min_chunk_characters"]), int)

    def test_defaults_and_ordinary_settings_keep_the_same_configuration(self):
        expected = {
            "interval_seconds": 0.2,
            "tracker_options": {
                "stability_frames": 2,
                "idle_flush_seconds": 0.4,
                "min_chunk_characters": 20,
            },
        }
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_live_configuration(), expected)
        self.assertEqual(get_live_configuration(AppSettings()), expected)
        settings = AppSettings(
            live_interval_ms=175,
            live_idle_flush_ms=625,
            live_stability_frames=4,
            live_min_chunk_characters=13,
            auto_advance_delay_ms=0,
        )
        self.assertEqual(
            get_live_configuration(settings),
            {
                "interval_seconds": 0.175,
                "tracker_options": {
                    "stability_frames": 4,
                    "idle_flush_seconds": 0.625,
                    "min_chunk_characters": 13,
                },
            },
        )
        self.assertEqual(get_auto_advance_delay_seconds(settings), 0)
