import unittest

import numpy as np

from vntts.audio_lifecycle import audio_lifecycle_context
from vntts.audio_output import resolve_audio_output
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


class AudioOutputLifecycleTest(unittest.TestCase):
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
