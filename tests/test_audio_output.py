import unittest
from unittest.mock import Mock, patch

import numpy as np
from sounddevice import PortAudioError

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.audio_output import match_output_sample_rate, resolve_audio_output
from vntts.support import configure_audio_lifecycle_log


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

    def get_stream(self):
        return Mock(status=Mock(output_underflow=False))

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


class AudioOutputLifecycleTest(unittest.TestCase):
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
