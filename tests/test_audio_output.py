import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from sounddevice import PortAudioError

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.audio_output import (
    PcmAdmission,
    PcmAdmissionRejected,
    match_output_sample_rate,
    pcm_admission_context,
    resolve_audio_output,
    sanitize_playback_audio,
    write_pcm_chunks,
)
from vntts.support import configure_audio_lifecycle_log


class PlaybackSanitizationTest(unittest.TestCase):
    def test_shared_policy_preserves_backend_shapes_and_input_buffers(self):
        from vntts.speech_backend import (
            MossTTSVoiceRouterBackend,
            PocketTTSVoiceRouterBackend,
        )
        from vntts.speech_worker import IsolatedSpeechBackend

        values = np.array([np.nan, np.inf, -np.inf, -2, -0.4, 0, 0.3, 2])
        for backend_type in (
            PocketTTSVoiceRouterBackend,
            MossTTSVoiceRouterBackend,
            IsolatedSpeechBackend,
        ):
            backend = object.__new__(backend_type)
            for volume in (0.0, 0.25, 1.0):
                backend.volume = volume
                for source in (
                    values,
                    values.reshape(4, 2),
                    np.asfortranarray(values.reshape(4, 2)),
                    values[::-2],
                    np.empty((0, 2)),
                    np.array(np.nan),
                ):
                    with self.subTest(
                        backend=backend_type.__name__, volume=volume, shape=source.shape
                    ):
                        source.setflags(write=False)
                        before = source.copy()
                        expected = np.asarray(source, dtype=np.float32).copy()
                        expected[np.isnan(expected)] = 0
                        expected[np.isposinf(expected)] = np.finfo(np.float32).max
                        expected[np.isneginf(expected)] = np.finfo(np.float32).min
                        expected = np.clip(expected * volume, -0.95, 0.95)
                        if backend_type is PocketTTSVoiceRouterBackend:
                            expected = expected.reshape(-1)
                        actual = backend._prepare_audio(source)
                        np.testing.assert_array_equal(actual, expected)
                        np.testing.assert_array_equal(source, before)
                        self.assertFalse(np.shares_memory(actual, source))
                        self.assertTrue(actual.flags.writeable)
                        self.assertEqual(actual.dtype, np.float32)
                        self.assertEqual(actual.shape, expected.shape)

    def test_sanitization_updates_the_owned_buffer_in_place(self):
        samples = np.array([np.nan, -2, -0.5, 0, 0.5, 2], dtype=np.float32)
        pointer = samples.__array_interface__["data"][0]
        self.assertIsNone(sanitize_playback_audio(samples, 0.5))
        self.assertEqual(samples.__array_interface__["data"][0], pointer)
        np.testing.assert_array_equal(
            samples, np.array([0, -0.95, -0.25, 0, 0.25, 0.95], dtype=np.float32)
        )


class FakeStream:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def write(self, _audio):
        return False

    def abort(self):
        return None

    def stop(self):
        return None

    def close(self):
        return None


class FakeSoundDevice:
    __name__ = "sounddevice"

    def query_devices(self, *, kind):
        self.kind = kind
        return {"name": "Speakers", "hostapi": 2, "default_samplerate": 48000}

    def query_hostapis(self, index):
        self.hostapi_index = index
        return {"name": "WASAPI"}

    def OutputStream(self, **_options):
        return FakeStream()

    def play(self, _audio, _sample_rate, *, latency):
        del latency

    def wait(self):
        return None

    def stop(self):
        return None


class IncompleteSoundDevice:
    __name__ = "sounddevice"

    def query_devices(self, *, kind):
        del kind
        return {}

    def OutputStream(self, **_options):
        return FakeStream()


class DelegatingStream(FakeStream):
    def __init__(self):
        self.entered = FakeStream()
        self.exited = False

    def __enter__(self):
        return self.entered

    def __exit__(self, *_args):
        self.exited = True
        return False


class DelegatingSoundDevice(FakeSoundDevice):
    def OutputStream(self, **_options):
        self.context = DelegatingStream()
        return self.context


class FailingSoundDevice(FakeSoundDevice):
    def play(self, _audio, _sample_rate, *, latency):
        self.latency = latency

    def wait(self):
        raise RuntimeError("device wait failed")

    def stop(self):
        raise RuntimeError("device stop failed")


class ConvenienceSoundDevice(FakeSoundDevice):
    def play(self, _audio, _sample_rate, *, latency):
        del latency

    def wait(self):
        return None


class PcmAdmissionTest(unittest.TestCase):
    def test_rejected_stream_never_writes_and_retains_its_authority(self):
        claim = Mock(return_value=False)
        native = FakeStream()
        native.write = Mock(return_value=False)
        output = FakeSoundDevice()
        output.OutputStream = Mock(return_value=native)
        wrapped = resolve_audio_output(output)
        token = pcm_admission_context.set(PcmAdmission(claim))
        try:
            stream = wrapped.OutputStream(
                samplerate=24_000, channels=1, dtype="float32", latency="low"
            )
        finally:
            pcm_admission_context.reset(token)

        with stream:
            stream.write(np.empty((0, 1), dtype=np.float32))
            claim.assert_not_called()
            native.write.reset_mock()
            for _ in range(2):
                with self.assertRaises(PcmAdmissionRejected):
                    stream.write(np.zeros((8, 1), dtype=np.float32))
        claim.assert_called_once_with()
        native.write.assert_not_called()

    def test_announcement_dialogue_and_convenience_share_one_claim_before_pcm(self):
        from vntts.rust_output import RustOutputStream

        calls = []

        def claim():
            calls.append("claim")
            return True

        output = FakeSoundDevice()
        native = FakeStream()
        native.write = Mock(side_effect=lambda _audio: calls.append("write"))
        output.OutputStream = Mock(return_value=native)
        output.play = Mock(side_effect=lambda *_args, **_kwargs: calls.append("play"))
        token = pcm_admission_context.set(PcmAdmission(claim))
        try:
            wrapped = resolve_audio_output(output)
            for _ in range(2):
                with wrapped.OutputStream(
                    samplerate=24_000, channels=1, dtype="float32", latency="low"
                ) as stream:
                    stream.write(np.zeros((8, 1), dtype=np.float32))
            prepared = object.__new__(RustOutputStream)

            def play_buffer(*_args):
                calls.append("buffer")
                return True, False

            prepared.play_buffer = Mock(side_effect=play_buffer)
            self.assertEqual(
                write_pcm_chunks(
                    prepared, np.zeros(8, dtype=np.float32), 24_000, lambda: False
                ),
                (True, False),
            )
            wrapped.play(np.zeros(8, dtype=np.float32), 24_000, latency="low")
        finally:
            pcm_admission_context.reset(token)

        self.assertEqual(calls, ["claim", "write", "write", "buffer", "play"])

    def test_rejected_convenience_play_never_reaches_output(self):
        output = FakeSoundDevice()
        output.play = Mock()
        token = pcm_admission_context.set(PcmAdmission(lambda: False))
        try:
            with self.assertRaises(PcmAdmissionRejected):
                resolve_audio_output(output).play(
                    np.zeros(8, dtype=np.float32), 24_000, latency="low"
                )
        finally:
            pcm_admission_context.reset(token)
        output.play.assert_not_called()

    def test_injected_streaming_output_is_wrapped_only_under_admission(self):
        output = IncompleteSoundDevice()
        self.assertIs(resolve_audio_output(output), output)
        token = pcm_admission_context.set(PcmAdmission(lambda: False))
        try:
            wrapped = resolve_audio_output(output)
            with wrapped.OutputStream(
                samplerate=24_000, channels=1, dtype="float32", latency="low"
            ) as stream:
                with self.assertRaises(PcmAdmissionRejected):
                    stream.write(np.zeros((8, 1), dtype=np.float32))
        finally:
            pcm_admission_context.reset(token)

    def test_rejected_prepared_buffers_never_reach_generic_or_native_output(self):
        from vntts.rust_output import RustOutputStream

        for native in (False, True):
            with self.subTest(native=native):
                stream = object.__new__(RustOutputStream) if native else FakeStream()
                stream.write = Mock(return_value=False)
                stream.play_buffer = Mock(return_value=(True, False))
                first_write = Mock()
                token = pcm_admission_context.set(PcmAdmission(lambda: False))
                try:
                    result = write_pcm_chunks(
                        stream,
                        np.zeros(8, dtype=np.float32),
                        24_000,
                        lambda: False,
                        first_write=first_write,
                    )
                finally:
                    pcm_admission_context.reset(token)
                self.assertEqual(result, (False, False))
                stream.write.assert_not_called()
                stream.play_buffer.assert_not_called()
                first_write.assert_not_called()

    def test_cancelled_native_buffer_does_not_claim_pcm(self):
        from vntts.rust_output import RustOutputStream

        stream = object.__new__(RustOutputStream)
        stream.play_buffer = Mock()
        claim = Mock(return_value=True)
        token = pcm_admission_context.set(PcmAdmission(claim))
        try:
            result = write_pcm_chunks(
                stream, np.zeros(8, dtype=np.float32), 24_000, lambda: True
            )
        finally:
            pcm_admission_context.reset(token)
        self.assertEqual(result, (False, False))
        claim.assert_not_called()
        stream.play_buffer.assert_not_called()

    def test_admitted_buffer_still_checks_cancellation_before_each_write(self):
        stream = FakeStream()
        stream.write = Mock(return_value=False)
        claim = Mock(return_value=True)
        token = pcm_admission_context.set(PcmAdmission(claim))
        try:
            result = write_pcm_chunks(
                stream,
                np.zeros(120, dtype=np.float32),
                1000,
                lambda: stream.write.called,
            )
        finally:
            pcm_admission_context.reset(token)
        self.assertEqual(result, (False, False))
        claim.assert_called_once_with()
        stream.write.assert_called_once()

    def test_concurrent_streams_claim_once(self):
        claim = Mock(return_value=True)
        admission = PcmAdmission(claim)
        start = Barrier(2)

        def admit():
            start.wait(timeout=2)
            admission.admit()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(admit) for _ in range(2)]
            for future in futures:
                future.result(timeout=2)
        claim.assert_called_once_with()


class AudioOutputLifecycleTest(unittest.TestCase):
    def test_default_and_explicit_sounddevice_use_the_shared_system_output(self):
        shared = FakeSoundDevice()
        with patch(
            "vntts.system_audio.get_system_audio_output", return_value=shared
        ) as resolve:
            for module in (None, ModuleType("sounddevice")):
                output = resolve_audio_output(module)
                self.assertIs(output.output, shared)
            self.assertEqual(resolve.call_count, 2)

    def test_incomplete_sounddevice_shape_is_not_promoted_to_audio_output(self):
        incomplete = IncompleteSoundDevice()

        self.assertIs(resolve_audio_output(incomplete), incomplete)

    def test_stream_exit_uses_the_context_manager_that_was_entered(self):
        output = DelegatingSoundDevice()
        wrapped = resolve_audio_output(output)

        with patch("vntts.audio_output.record_audio_lifecycle"):
            with wrapped.OutputStream(
                samplerate=24_000, channels=1, dtype="float32", latency="low"
            ) as stream:
                self.assertIsNot(stream, output.context.entered)

        self.assertTrue(output.context.exited)

    def test_nonfinite_device_rate_falls_back_to_source_rate(self):
        output = Mock()
        output.query_devices.return_value = {"default_samplerate": float("inf")}
        audio = np.zeros(8, dtype=np.float32)

        matched, sample_rate = match_output_sample_rate(output, audio, 24_000)

        self.assertIs(matched, audio)
        self.assertEqual(sample_rate, 24_000)

    def test_device_query_failure_falls_back_to_source_rate(self):
        output = Mock()
        output.query_devices.side_effect = PortAudioError("device unavailable")
        audio = np.zeros(8, dtype=np.float32)

        matched, sample_rate = match_output_sample_rate(output, audio, 24_000)

        self.assertIs(matched, audio)
        self.assertEqual(sample_rate, 24_000)

    def test_failed_convenience_finish_is_not_logged_as_complete(self):
        for method, operation in (("wait", "close"), ("stop", "abort")):
            with self.subTest(method=method):
                log = configure_audio_lifecycle_log()
                try:
                    output = resolve_audio_output(FailingSoundDevice())
                    output.play(np.zeros(8, dtype=np.float32), 24000, latency="low")
                    with self.assertRaisesRegex(
                        RuntimeError, f"device {method} failed"
                    ):
                        getattr(output, method)()
                    events = log.report()["events"]
                finally:
                    configure_audio_lifecycle_log()
                self.assertEqual(events[-1]["operation"], operation)
                self.assertEqual(events[-1]["outcome"], "failed")
                self.assertEqual(len({event["stream_id"] for event in events}), 1)

    def test_replacing_convenience_playback_closes_previous_log_identity(self):
        log = configure_audio_lifecycle_log()
        try:
            output = resolve_audio_output(ConvenienceSoundDevice())
            audio = np.zeros(8, dtype=np.float32)
            output.play(audio, 24_000, latency="low")
            output.play(audio, 24_000, latency="low")
            output.wait()
            events = log.report()["events"]
        finally:
            configure_audio_lifecycle_log()

        self.assertEqual(
            [event["operation"] for event in events],
            ["open", "abort", "open", "close"],
        )
        self.assertEqual(events[1]["reason"], "convenience-replaced")
        self.assertEqual(events[0]["stream_id"], events[1]["stream_id"])
        self.assertEqual(events[2]["stream_id"], events[3]["stream_id"])
        self.assertNotEqual(events[0]["stream_id"], events[2]["stream_id"])

    def test_stream_lifecycle_keeps_device_owner_and_playback_identity(self):
        log = configure_audio_lifecycle_log()
        token = audio_lifecycle_context.set(
            {"session_id": "a" * 32, "generation": 7, "chunk_id": "chunk-1"}
        )
        try:
            output = resolve_audio_output(FakeSoundDevice())
            with output.OutputStream(
                samplerate=24000,
                channels=1,
                dtype="float32",
                latency="low",
            ) as stream:
                stream.write(np.zeros((8, 1), dtype=np.float32))
            events = log.report()["events"]
        finally:
            audio_lifecycle_context.reset(token)
            configure_audio_lifecycle_log()

        self.assertEqual(
            [event["operation"] for event in events],
            ["open", "start", "stop", "stop", "close"],
        )
        self.assertEqual(len({event["stream_id"] for event in events}), 1)
        for event in events:
            self.assertEqual(event["session_id"], "a" * 32)
            self.assertEqual(event["generation"], 7)
            self.assertEqual(event["chunk_id"], "chunk-1")
            self.assertEqual(event["device_name"], "Speakers")
            self.assertEqual(event["host_api"], "WASAPI")
            self.assertTrue(event["owner"])


if __name__ == "__main__":
    unittest.main()


class PcmWriteDiagnosticsTest(unittest.TestCase):
    def test_summary_is_emitted_once_after_unchanged_pcm_writes(self):
        samples = np.arange(120, dtype=np.float32)
        writes = []
        now = [0.0]

        def guard():
            now[0] += 0.002
            return False

        def write(block):
            writes.append(block.copy())
            now[0] += 0.05
            return len(writes) == 2

        with (
            patch("vntts.audio_output.monotonic", side_effect=lambda: now[0]),
            patch("vntts.audio_output.record_audio_lifecycle") as log,
        ):
            completed, underflowed = write_pcm_chunks(
                SimpleNamespace(write=write), samples, 1000, guard
            )
            log.assert_called_once()
            summary = log.call_args.kwargs
        self.assertTrue(completed)
        self.assertTrue(underflowed)
        np.testing.assert_array_equal(np.concatenate(writes).reshape(-1), samples)
        self.assertEqual(summary["written_frames"], 120)
        self.assertEqual(summary["write_count"], 3)
        self.assertEqual(summary["underflow_count"], 1)
        self.assertEqual(summary["first_underflow_frame"], 50)
        self.assertEqual(summary["max_guard_ms"], 2)
        self.assertEqual(summary["max_write_ms"], 50)
        self.assertEqual(summary["max_write_gap_ms"], 2)
        self.assertEqual(summary["first_underflow_write_index"], 2)
        self.assertEqual(summary["first_underflow_input_frame_offset"], 50)
        self.assertEqual(summary["first_underflow_input_frames"], 50)
        self.assertEqual(summary["first_underflow_guard_ms"], 2)
        self.assertEqual(summary["first_underflow_write_gap_ms"], 2)
        self.assertAlmostEqual(summary["first_underflow_started_monotonic"], 0.054)
        self.assertAlmostEqual(summary["first_underflow_ended_monotonic"], 0.104)

    def test_cancellation_and_failure_retain_partial_write_diagnostics(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                writes = []

                def write(block):
                    if writes and failed:
                        raise OSError("device failed")
                    writes.append(block.copy())
                    return False

                with patch("vntts.audio_output.record_audio_lifecycle") as log:
                    if failed:
                        with self.assertRaises(OSError):
                            write_pcm_chunks(
                                SimpleNamespace(write=write),
                                np.zeros(120),
                                1000,
                                lambda: False,
                            )
                    else:
                        result = write_pcm_chunks(
                            SimpleNamespace(write=write),
                            np.zeros(120),
                            1000,
                            lambda: bool(writes),
                        )
                        self.assertEqual(result, (False, False))
                log.assert_called_once()
                self.assertEqual(log.call_args.kwargs["written_frames"], 50)
                self.assertEqual(
                    log.call_args.kwargs["outcome"],
                    "failed" if failed else "interrupted",
                )
