import builtins
import unittest
from threading import Event, RLock, Thread
from unittest.mock import Mock, patch

import numpy as np
import soxr
from sounddevice import PortAudioError

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.audio_output import resolve_audio_output, write_pcm_chunks
from vntts.system_audio import (
    AudioDeviceChangedError,
    SystemAudioOutput,
    _DefaultOutputMonitor,
)


class DeviceModule:
    PortAudioError = PortAudioError

    def __init__(self):
        self.device = {
            "default_samplerate": 48_000,
            "max_output_channels": 2,
            "name": "First headphones",
        }
        self.events = []
        self.native_streams = []
        self.close_failure = None
        self.initialize_failure = None

    def query_devices(self, *, kind):
        if self.device is None:
            raise RuntimeError("No output device")
        return self.device

    def check_output_settings(self, *, samplerate, channels, dtype):
        if self.device is None:
            raise PortAudioError("No output device", -9996)
        if samplerate != self.device["default_samplerate"]:
            raise PortAudioError("Unsupported sample rate", -9997)
        if channels > self.device["max_output_channels"]:
            raise PortAudioError("Unsupported channels", -9998)

    def OutputStream(self, **options):
        native = Mock(time=0.0)
        native.options = options
        native.is_closed = False
        native.start.side_effect = lambda: self.events.append("start")
        native.abort.side_effect = lambda: self.events.append("abort")

        def close():
            self.events.append("close")
            if self.close_failure:
                raise self.close_failure
            native.is_closed = True

        native.close.side_effect = close
        native.write.return_value = False
        self.native_streams.append(native)
        self.events.append("open")
        return native

    def stop(self):
        self.events.append("stop-convenience")

    def _terminate(self):
        if not all(native.is_closed for native in self.native_streams):
            raise AssertionError("PortAudio terminated with an open stream")
        self.events.append("terminate")

    def _initialize(self):
        self.events.append("initialize")
        if self.initialize_failure:
            raise self.initialize_failure


class _DelayedLock:
    def __init__(self, clock, delay):
        self.lock = RLock()
        self.clock = clock
        self.delay = delay

    def acquire(self):
        self.lock.acquire()
        self.clock[0] += self.delay

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()


class SystemAudioTest(unittest.TestCase):
    def create_output(self):
        module = DeviceModule()
        return module, SystemAudioOutput(module)

    def create_monitor(self, output):
        probe = Mock(return_value=("headphones",))
        monitor = _DefaultOutputMonitor(output, lambda: probe)
        self.addCleanup(monitor.close)
        return probe, monitor

    def test_headless_probe_switches_the_existing_stream_through_shared_refresh(self):
        module, output = self.create_output()
        probe, monitor = self.create_monitor(output)
        stream = self.open_stream(output)
        stream.start()
        monitor.check()
        stream.write(np.zeros((480, 2), np.float32))
        opened = stream.native
        monitor.check()
        stream.write(np.zeros((480, 2), np.float32))
        self.assertIs(stream.native, opened)
        probe.return_value = ("replacement-headphones",)
        monitor.check()
        # The background probe requests refresh; only the playback owner closes.
        self.assertIs(stream.native, opened)
        stream.write(np.zeros((480, 2), np.float32))
        self.assertIsNot(stream.native, opened)
        self.assertEqual(module.events.count("terminate"), 2)

    def test_probe_failure_preserves_identity_and_recovers_without_logging_ids(self):
        _module, output = self.create_output()
        probe, monitor = self.create_monitor(output)
        monitor.check()
        output.pending.clear()
        probe.side_effect = OSError("private endpoint ID")
        with patch("vntts.system_audio.record_audio_lifecycle") as record:
            monitor.check()
            monitor.check()
            self.assertEqual(record.call_count, 1)
            self.assertEqual(record.call_args.kwargs["reason"], "OSError")
            self.assertFalse(output.pending.is_set())
            probe.side_effect = None
            probe.return_value = ("headphones",)
            monitor.check()
            self.assertEqual(record.call_count, 3)
            self.assertEqual(record.call_args.args, ("device-refresh-request",))
            self.assertEqual(
                record.call_args.kwargs["reason"], "native-probe-recovered"
            )
        self.assertTrue(output.pending.is_set())

    def test_refresh_requests_report_distinct_origins_without_device_identity(self):
        from vntts import system_audio

        module, output = self.create_output()
        probe, monitor = self.create_monitor(output)
        with (
            patch("vntts.system_audio.record_audio_lifecycle") as record,
            patch("vntts.rust_audio.notify_audio_device_changed") as notify,
            patch.object(system_audio, "_system_output", output),
        ):
            output.request_refresh()
            monitor.check()
            monitor.check()
            probe.side_effect = OSError("PRIVATE device name")
            monitor.check()
            probe.side_effect = None
            monitor.check()
            system_audio._request_system_refresh()
        refresh = [
            call
            for call in record.call_args_list
            if call.args == ("device-refresh-request",)
        ]
        self.assertEqual(
            [call.kwargs["reason"] for call in refresh],
            [
                "caller-request",
                "native-probe-identity-changed",
                "native-probe-recovered",
                "qt-audio-outputs-changed",
            ],
        )
        self.assertTrue(all(call.kwargs["outcome"] == "requested" for call in refresh))
        self.assertNotIn("PRIVATE", repr(record.call_args_list))
        self.assertEqual(notify.call_count, 4)
        self.assertTrue(output.pending.is_set())
        self.assertEqual(module.events, [])

    def test_failed_refresh_observation_does_not_prevent_native_invalidation(self):
        _module, output = self.create_output()
        with (
            patch(
                "vntts.system_audio.record_audio_lifecycle",
                side_effect=OSError("journal unavailable"),
            ),
            patch("vntts.rust_audio.notify_audio_device_changed") as notify,
        ):
            output.request_refresh()
        notify.assert_called_once_with()
        self.assertTrue(output.pending.is_set())

    def test_no_output_and_its_return_are_real_identity_changes(self):
        _module, output = self.create_output()
        probe, monitor = self.create_monitor(output)
        monitor.check()
        output.pending.clear()
        probe.return_value = None
        monitor.check()
        self.assertTrue(output.pending.is_set())
        output.pending.clear()
        monitor.check()
        self.assertFalse(output.pending.is_set())
        probe.return_value = ("headphones",)
        monitor.check()
        self.assertTrue(output.pending.is_set())

    def test_slow_native_probe_does_not_block_playback_or_cancellation(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        checking = Event()
        release = Event()

        def probe():
            checking.set()
            if not release.wait(2):
                raise AssertionError("Test did not release the probe")
            return ("headphones",)

        monitor = _DefaultOutputMonitor(output, lambda: probe)
        output.device_monitor = monitor
        monitor.ready.set()
        monitor.thread.start()
        try:
            self.assertTrue(checking.wait(1))
            stream.write(np.zeros((480, 2), np.float32))
            stream.abort()
            self.assertFalse(stream.started)
            self.assertEqual(module.events.count("terminate"), 0)
        finally:
            release.set()
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())

    def test_delayed_first_probe_finishes_before_opening_blocking_or_callback_stream(
        self,
    ):
        for callback in (False, True):
            with self.subTest(callback=callback):
                module, output = self.create_output()
                entered = Event()
                release = Event()
                opened = Event()
                errors = []
                streams = []

                def probe():
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("Test did not release the initial probe")
                    return ("headphones",)

                monitor = _DefaultOutputMonitor(output, lambda: probe)
                output.device_monitor = monitor
                output.request_refresh()

                def start():
                    try:
                        options = {"callback": Mock()} if callback else {}
                        stream = self.open_stream(output, **options)
                        streams.append(stream)
                        stream.start()
                        opened.set()
                    except Exception as error:
                        errors.append(error)

                player = Thread(target=start)
                monitor.thread.start()
                try:
                    self.assertTrue(entered.wait(1))
                    player.start()
                    self.assertFalse(opened.wait(0.05))
                    self.assertEqual(module.events, [])
                    release.set()
                    player.join(2)
                    self.assertFalse(player.is_alive())
                    self.assertEqual(errors, [])
                    self.assertTrue(opened.is_set())
                    self.assertEqual(module.events.count("terminate"), 1)
                    monitor.check()
                    streams[0].start()
                    self.assertEqual(module.events.count("terminate"), 1)
                finally:
                    release.set()
                    if player.ident is not None:
                        player.join(2)
                    monitor.close()

    def test_initial_probe_timeout_keeps_refresh_pending_and_retries(self):
        module, output = self.create_output()
        monitor = Mock()
        output.device_monitor = monitor
        output.request_refresh()
        monitor.ready.wait.return_value = False
        with self.assertRaisesRegex(RuntimeError, "detection timed out"):
            output.query_devices(kind="output")
        self.assertTrue(output.pending.is_set())
        self.assertEqual(module.events, [])
        monitor.ready.wait.return_value = True
        output.query_devices(kind="output")
        self.assertEqual(module.events.count("terminate"), 1)

    def test_probe_failure_after_shutdown_does_not_log_or_request_refresh(self):
        _module, output = self.create_output()
        entered = Event()
        release = Event()

        def probe():
            entered.set()
            release.wait(2)
            raise OSError("Probe ended after shutdown")

        monitor = _DefaultOutputMonitor(output, lambda: probe)
        with patch("vntts.system_audio.record_audio_lifecycle") as record:
            monitor.thread.start()
            try:
                self.assertTrue(entered.wait(1))
                monitor.stopping.set()
                release.set()
                monitor.close()
                self.assertFalse(monitor.thread.is_alive())
                record.assert_not_called()
                self.assertFalse(output.pending.is_set())
            finally:
                release.set()
                monitor.close()

    def test_blocking_device_change_between_open_and_start_reopens_new_format(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        old = stream.native
        module.device["default_samplerate"] = 24_000
        output.request_refresh()
        stream.start()
        self.assertIsNot(stream.native, old)
        self.assertTrue(old.is_closed)
        self.assertEqual(stream.native.options["samplerate"], 24_000)
        self.assertTrue(stream.started)

    def test_probe_factory_failure_retries_and_shutdown_prevents_late_refresh(self):
        _module, output = self.create_output()
        probe = Mock(return_value=("headphones",))
        factory = Mock(side_effect=[OSError("temporarily unavailable"), probe])
        monitor = _DefaultOutputMonitor(output, factory)
        monitor.check()
        self.assertTrue(monitor.failed)
        monitor.check()
        self.assertTrue(output.pending.is_set())
        output.pending.clear()
        monitor.close()
        monitor.check()
        self.assertEqual(factory.call_count, 2)
        probe.assert_called_once()
        self.assertFalse(output.pending.is_set())

    def test_cli_shared_output_starts_one_native_monitor_without_importing_qt(self):
        from vntts import system_audio

        module = DeviceModule()
        original_import = builtins.__import__

        def import_without_qt(name, *args, **kwargs):
            if name.startswith("PySide6"):
                raise AssertionError("CLI playback must not import Qt")
            return original_import(name, *args, **kwargs)

        with (
            patch("vntts.rust_output.RustAudioModule", return_value=module),
            patch.object(system_audio, "_system_output", None),
            patch("builtins.__import__", side_effect=import_without_qt),
            patch.object(system_audio, "Thread") as thread,
            patch.object(system_audio.atexit, "register") as register,
        ):
            output = system_audio.get_system_audio_output()
            self.assertIs(system_audio.get_system_audio_output(), output)
            self.assertIs(output.module, module)
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            register.assert_called_once_with(output.device_monitor.close)

    def test_failed_monitor_start_does_not_cache_an_unmonitored_output(self):
        from vntts import system_audio

        with (
            patch("vntts.rust_output.RustAudioModule", return_value=DeviceModule()),
            patch.object(system_audio, "_system_output", None),
            patch.object(system_audio, "Thread") as thread,
            patch.object(system_audio.atexit, "register"),
        ):
            thread.return_value.start.side_effect = [RuntimeError("No thread"), None]
            with self.assertRaisesRegex(RuntimeError, "No thread"):
                system_audio.get_system_audio_output()
            self.assertIsNone(system_audio._system_output)
            output = system_audio.get_system_audio_output()
            self.assertIsNotNone(output.device_monitor)
            self.assertEqual(thread.call_count, 2)

    def open_stream(self, output, **options):
        stream = output.OutputStream(
            samplerate=48_000, channels=2, dtype="float32", latency=0.1, **options
        )
        self.addCleanup(stream.close)
        return stream

    def test_write_phases_emit_one_summary_after_close_without_changing_pcm(self):
        clock = [0.0]

        module, output = self.create_output()
        token = audio_lifecycle_context.set({"generation": 7, "run_id": "owned-run"})
        try:
            stream = self.open_stream(output)
        finally:
            audio_lifecycle_context.reset(token)
        stream.start()
        output.lock = _DelayedLock(clock, 0.002)
        stream.write_lock = _DelayedLock(clock, 0.005)
        original_asarray = np.asarray
        received = []

        def advance(seconds):
            clock[0] += seconds

        def convert(*args, **kwargs):
            advance(0.004)
            return original_asarray(*args, **kwargs)

        def write(samples):
            advance(0.006)
            received.append(samples.copy())
            if len(received) == 2:
                raise OSError("output failed")
            return False

        samples = np.arange(8, dtype=np.float32).reshape(4, 2) / 10
        module.native_streams[0].write.side_effect = write
        with (
            patch("vntts.system_audio.monotonic", side_effect=lambda: clock[0]),
            patch.object(output, "_refresh", side_effect=lambda: advance(0.003)),
            patch("vntts.system_audio.np.asarray", side_effect=convert),
            patch("vntts.system_audio.record_audio_lifecycle") as record,
        ):
            self.assertFalse(stream.write(samples))
            with self.assertRaisesRegex(OSError, "output failed"):
                stream.write(samples)
            record.assert_not_called()
            stream.close()
            stream.close()
        record.assert_called_once()
        self.assertEqual(record.call_args.args, ("system-write-summary",))
        summary = record.call_args.kwargs
        self.assertEqual(summary["generation"], 7)
        self.assertEqual(summary["run_id"], "owned-run")
        self.assertEqual(summary["system_write_attempts"], 2)
        self.assertEqual(summary["system_write_failures"], 1)
        for field, expected in (
            ("max_output_lock_ms", 2),
            ("max_stream_lock_ms", 5),
            ("max_refresh_ms", 3),
            ("max_conversion_ms", 4),
            ("max_native_write_ms", 6),
        ):
            self.assertAlmostEqual(summary[field], expected)
        for actual in received:
            np.testing.assert_array_equal(actual, samples)

    def test_underflow_and_max_native_write_keep_distinct_pcm_block_associations(self):
        module, output = self.create_output()
        with patch("vntts.system_audio.get_system_audio_output", return_value=output):
            logged = resolve_audio_output(None)
        stream = logged.OutputStream(
            samplerate=48_000, channels=2, dtype="float32", latency=0.1
        )
        self.addCleanup(stream.close)
        stream.stream.start()
        clock, received, records = [0.0], [], []
        output.lock = _DelayedLock(clock, 0.002)
        stream.stream.write_lock = _DelayedLock(clock, 0.005)
        original_asarray = np.asarray

        def advance(seconds):
            clock[0] += seconds

        def convert(*args, **kwargs):
            advance(0.004)
            return original_asarray(*args, **kwargs)

        def write(samples):
            received.append(samples.copy())
            advance((0.01, 0.02, 0.07)[len(received) - 1])
            return len(received) == 2

        def guard():
            advance(0.001)
            return False

        def record(operation, **details):
            records.append((operation, details))

        samples = np.arange(12_000, dtype=np.float32).reshape(6000, 2) / 20_000
        module.native_streams[0].write.side_effect = write
        with (
            patch("vntts.audio_output.monotonic", side_effect=lambda: clock[0]),
            patch("vntts.system_audio.monotonic", side_effect=lambda: clock[0]),
            patch.object(output, "_refresh", side_effect=lambda: advance(0.003)),
            patch("vntts.system_audio.np.asarray", side_effect=convert),
            patch("vntts.audio_output.record_audio_lifecycle", side_effect=record),
            patch("vntts.system_audio.record_audio_lifecycle", side_effect=record),
        ):
            self.assertEqual(
                write_pcm_chunks(stream, samples, 48_000, guard), (True, True)
            )
            stream.close()
        summaries = {
            operation: details
            for operation, details in records
            if operation.endswith("write-summary")
        }
        pcm, system = summaries["pcm-write-summary"], summaries["system-write-summary"]
        self.assertEqual(pcm["stream_id"], system["stream_id"])
        for summary in (pcm, system):
            self.assertEqual(summary["first_underflow_write_index"], 2)
            self.assertEqual(summary["first_underflow_input_frame_offset"], 2400)
            self.assertEqual(summary["first_underflow_input_frames"], 2400)
        self.assertEqual(pcm["max_write_write_index"], 3)
        self.assertEqual(system["max_native_write_write_index"], 3)
        self.assertEqual(system["max_native_write_input_frame_offset"], 4800)
        self.assertEqual(system["max_native_write_native_frames"], 1200)
        self.assertEqual(system["max_native_write_native_sample_rate"], 48_000)
        for phase, duration in (
            ("output_lock", 2),
            ("refresh", 3),
            ("conversion", 4),
            ("stream_lock", 5),
            ("native_write", 20),
        ):
            self.assertAlmostEqual(system[f"first_underflow_{phase}_ms"], duration)
        self.assertAlmostEqual(system["max_native_write_native_write_ms"], 70)
        self.assertLess(
            system["first_underflow_ended_monotonic"],
            system["max_native_write_started_monotonic"],
        )
        self.assertEqual(pcm["first_underflow_guard_ms"], 1)
        np.testing.assert_array_equal(np.concatenate(received), samples)

    def test_zero_resample_block_keeps_input_offset_for_following_native_underflow(
        self,
    ):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        stream.resampler = Mock()
        stream.resampler.resample_chunk.side_effect = [
            np.empty((0, 2), np.float32),
            np.ones((3, 2), np.float32),
        ]
        module.native_streams[0].write.return_value = True
        samples = np.ones((4, 2), np.float32)
        with patch("vntts.system_audio.record_audio_lifecycle") as record:
            self.assertFalse(stream.write(samples))
            module.native_streams[0].write.assert_not_called()
            self.assertTrue(stream.write(samples))
            stream.close()
        summary = record.call_args.kwargs
        self.assertEqual(summary["first_underflow_write_index"], 2)
        self.assertEqual(summary["first_underflow_input_frame_offset"], 4)
        self.assertEqual(summary["first_underflow_input_frames"], 4)
        self.assertEqual(summary["first_underflow_native_frames"], 3)

    def test_reopened_retry_preserves_logical_write_and_input_identity(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        initial_native = stream.native
        original_open = module.OutputStream

        def reopen(**options):
            native = original_open(**options)
            native.write.return_value = True
            return native

        def lose_device(_samples):
            initial_native.close()
            stream.native = None
            raise OSError("device changed during write")

        module.OutputStream = reopen
        initial_native.write.side_effect = lose_device
        with patch("vntts.system_audio.record_audio_lifecycle") as record:
            self.assertTrue(stream.write(np.ones((4, 2), np.float32)))
            stream.close()
        summary = record.call_args.kwargs
        self.assertEqual(summary["system_write_attempts"], 2)
        self.assertEqual(summary["system_write_failures"], 1)
        self.assertEqual(summary["first_underflow_write_index"], 1)
        self.assertEqual(summary["first_underflow_input_frame_offset"], 0)
        self.assertEqual(summary["first_underflow_input_frames"], 4)
        self.assertEqual(summary["first_underflow_attempt"], 2)
        self.assertEqual(stream.input_frames, 4)

    def test_switch_closes_all_native_streams_before_refresh_and_reopens_on_write(self):
        module, output = self.create_output()
        first = self.open_stream(output)
        second = self.open_stream(output)
        first.start()
        second.start()
        module.device = {
            "default_samplerate": 24_000,
            "max_output_channels": 1,
            "name": "Replacement headphones",
        }

        output.request_refresh()
        first.write(np.ones((480, 2), np.float32) * 0.2)
        first.stop()

        self.assertEqual(module.events.count("terminate"), 1)
        self.assertEqual(module.events.count("close"), 2)
        self.assertLess(module.events.index("close"), module.events.index("terminate"))
        self.assertIsNone(second.native)
        replacement = module.native_streams[-1]
        self.assertEqual(replacement.options["samplerate"], 24_000)
        self.assertEqual(replacement.options["channels"], 1)
        written = np.concatenate(
            [call.args[0] for call in replacement.write.call_args_list]
        )
        self.assertEqual(written.shape, (240, 1))
        self.assertEqual(written.dtype, np.float32)
        expected = soxr.resample(np.full((480, 1), 0.2, np.float32), 48_000, 24_000)
        np.testing.assert_allclose(written, expected, atol=1e-6)
        second.write(np.zeros((480, 2), np.float32))
        self.assertEqual(module.events.count("terminate"), 1)

    def test_unchanged_device_keeps_warm_stream_and_samples(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        samples = np.zeros((480, 2), np.float32)
        stream.write(samples)
        stream.write(samples)
        self.assertEqual(module.events, ["open", "start"])
        self.assertIs(module.native_streams[0].write.call_args.args[0], samples)

    def test_host_supported_format_keeps_continuous_conversion_in_the_driver(self):
        module, output = self.create_output()
        module.device["default_samplerate"] = 24_000
        with patch.object(module, "check_output_settings"):
            stream = self.open_stream(output)
            stream.start()
            samples = np.ones((480, 2), np.float32) * 0.2
            stream.write(samples)
        self.assertEqual(module.native_streams[0].options["samplerate"], 48_000)
        self.assertIs(module.native_streams[0].write.call_args.args[0], samples)

    def test_fallback_conversion_preserves_signal_and_duration_across_small_chunks(
        self,
    ):
        module, output = self.create_output()
        module.device["default_samplerate"] = 44_100
        stream = output.OutputStream(
            samplerate=24_000, channels=1, dtype="float32", latency=0.1
        )
        self.addCleanup(stream.close)
        samples = np.full((11_300, 1), 0.2, np.float32)
        with stream:
            for offset in range(0, len(samples), 113):
                stream.write(samples[offset : offset + 113])
        written = np.concatenate(
            [call.args[0] for call in module.native_streams[0].write.call_args_list]
        )
        expected = soxr.resample(samples, 24_000, 44_100)
        self.assertEqual(len(written), len(expected))
        np.testing.assert_allclose(written, expected, atol=1e-6)

    def test_abort_discards_pending_conversion_instead_of_flushing_it(self):
        module, output = self.create_output()
        module.device["default_samplerate"] = 24_000
        stream = self.open_stream(output)
        stream.start()
        stream.write(np.full((100, 2), 0.2, np.float32))
        self.assertGreater(stream.resampler.delay(), 0)
        writes = module.native_streams[0].write.call_count
        stream.abort()
        stream.stop()
        self.assertEqual(module.native_streams[0].write.call_count, writes)
        stream.start()
        stream.write(np.full((480, 2), 0.2, np.float32))
        stream.stop()
        written = np.concatenate(
            [
                call.args[0]
                for call in module.native_streams[0].write.call_args_list[writes:]
            ]
        )
        expected = soxr.resample(np.full((480, 2), 0.2, np.float32), 48_000, 24_000)
        np.testing.assert_allclose(written, expected, atol=1e-6)

    def test_conversion_tail_underflow_cannot_report_successful_playback(self):
        module, output = self.create_output()
        module.device["default_samplerate"] = 24_000
        stream = self.open_stream(output)
        stream.start()
        stream.write(np.full((100, 2), 0.2, np.float32))
        module.native_streams[0].write.return_value = True
        with self.assertRaisesRegex(RuntimeError, "underflowed"):
            stream.stop()

    def test_callback_owner_must_reprepare_and_reopen_after_switch(self):
        module, output = self.create_output()
        stream = self.open_stream(output, callback=Mock())
        stream.start()
        output.request_refresh()
        self.assertTrue(stream.device_changed)
        with self.assertRaises(AudioDeviceChangedError):
            _ = stream.time
        with self.assertRaises(AudioDeviceChangedError):
            stream.start()
        self.assertEqual(len(module.native_streams), 1)

    def test_failed_close_blocks_termination_and_can_be_retried(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        module.close_failure = RuntimeError("Device close failed")
        output.request_refresh()
        with self.assertRaisesRegex(RuntimeError, "Device close failed"):
            output.query_devices(kind="output")
        self.assertNotIn("terminate", module.events)
        self.assertTrue(output.pending.is_set())
        self.assertIsNotNone(stream.native)

        module.close_failure = None
        output.query_devices(kind="output")
        self.assertEqual(module.events.count("terminate"), 1)

    def test_refresh_waits_for_inflight_write_but_qt_request_does_not(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        writing = Event()
        release = Event()
        refreshed = Event()

        def write(_samples):
            writing.set()
            if not release.wait(2):
                raise AssertionError("Test did not release the native write")

        module.native_streams[0].write.side_effect = write
        writer = Thread(target=stream.write, args=(np.zeros((480, 2), np.float32),))
        reader = Thread(
            target=lambda: (output.query_devices(kind="output"), refreshed.set())
        )
        writer.start()
        try:
            self.assertTrue(writing.wait(1))
            output.request_refresh()
            reader.start()
            self.assertFalse(refreshed.wait(0.05))
            self.assertNotIn("terminate", module.events)
        finally:
            release.set()
            writer.join(2)
            if reader.ident is not None:
                reader.join(2)
        self.assertFalse(writer.is_alive() or reader.is_alive())
        self.assertTrue(refreshed.is_set())
        self.assertEqual(module.events.count("terminate"), 1)

    def test_context_start_failure_closes_and_unregisters_native_stream(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        module.native_streams[0].start.side_effect = RuntimeError("Startup failed")
        with self.assertRaisesRegex(RuntimeError, "Startup failed"):
            with stream:
                self.fail("Failed stream must not be entered")
        self.assertTrue(module.native_streams[0].is_closed)
        self.assertEqual(output.streams, set())

    def test_cancellation_aborts_an_inflight_native_write(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        writing = Event()
        release = Event()

        def write(_samples):
            writing.set()
            if not release.wait(2):
                raise AssertionError("Cancellation could not reach native abort")

        module.native_streams[0].write.side_effect = write
        module.native_streams[0].abort.side_effect = release.set
        writer = Thread(target=stream.write, args=(np.zeros((480, 2), np.float32),))
        writer.start()
        try:
            self.assertTrue(writing.wait(1))
            stream.abort()
        finally:
            release.set()
            writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertFalse(stream.started)

    def test_switch_retries_a_write_aborted_during_refresh(self):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        writing = Event()
        release = Event()
        errors = []

        def write(_samples):
            writing.set()
            if not release.wait(2):
                raise AssertionError("Refresh did not abort the native write")
            raise RuntimeError("Old device stopped")

        def playback():
            try:
                stream.write(np.zeros((480, 2), np.float32))
            except Exception as error:
                errors.append(error)

        module.native_streams[0].write.side_effect = write
        module.native_streams[0].abort.side_effect = release.set
        writer = Thread(target=playback)
        writer.start()
        try:
            self.assertTrue(writing.wait(1))
            output.request_refresh()
            output.query_devices(kind="output")
        finally:
            release.set()
            writer.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(module.native_streams), 2)
        module.native_streams[1].write.assert_called_once()

    def test_initialize_failure_does_not_terminate_twice_on_retry(self):
        module, output = self.create_output()
        self.open_stream(output)
        module.initialize_failure = RuntimeError("Initialization failed")
        output.request_refresh()
        with self.assertRaisesRegex(RuntimeError, "Initialization failed"):
            output.query_devices(kind="output")
        module.initialize_failure = None
        output.query_devices(kind="output")
        self.assertEqual(module.events.count("terminate"), 1)
        self.assertEqual(module.events.count("initialize"), 2)

    def test_missing_device_recovers_on_next_write_without_reopening_closed_stream(
        self,
    ):
        module, output = self.create_output()
        stream = self.open_stream(output)
        stream.start()
        original_device = module.device
        module.device = None
        output.request_refresh()
        with self.assertRaisesRegex(PortAudioError, "No output device"):
            stream.write(np.zeros((480, 2), np.float32))
        module.device = original_device
        output.request_refresh()
        stream.write(np.zeros((480, 2), np.float32))
        stream.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            stream.write(np.zeros((480, 2), np.float32))

    def test_qt_notification_only_requests_refresh_and_has_one_owner(self):
        from vntts import system_audio

        module, output = self.create_output()
        media = Mock()
        with (
            patch.object(system_audio, "_system_output", output),
            patch.object(system_audio, "_media_devices", None),
            patch("PySide6.QtCore.QCoreApplication.instance", return_value=Mock()),
            patch("PySide6.QtMultimedia.QMediaDevices", return_value=media) as create,
        ):
            system_audio.initialize_audio_device_monitor()
            system_audio.initialize_audio_device_monitor()
            media.audioOutputsChanged.connect.call_args.args[0]()
            create.assert_called_once()
        self.assertTrue(output.pending.is_set())
        self.assertEqual(module.events, [])

    def test_qt_watcher_does_not_import_or_initialize_sounddevice(self):
        from vntts import system_audio

        media = Mock()
        with (
            patch.object(system_audio, "_system_output", None),
            patch.object(system_audio, "_media_devices", None),
            patch("PySide6.QtCore.QCoreApplication.instance", return_value=Mock()),
            patch("PySide6.QtMultimedia.QMediaDevices", return_value=media),
            patch.object(
                system_audio,
                "get_system_audio_output",
                side_effect=AssertionError("Audio must remain lazy"),
            ),
        ):
            system_audio.initialize_audio_device_monitor()
            media.audioOutputsChanged.connect.call_args.args[0]()


if __name__ == "__main__":
    unittest.main()
