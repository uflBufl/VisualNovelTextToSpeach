import builtins
import unittest
from threading import Event, Thread
from unittest.mock import Mock, patch

import numpy as np
import soxr
from sounddevice import PortAudioError

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
            self.assertEqual(record.call_count, 2)
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
            patch.dict("sys.modules", {"sounddevice": module}),
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
            patch.dict("sys.modules", {"sounddevice": DeviceModule()}),
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
