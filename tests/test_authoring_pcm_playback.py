import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from vntts.authoring.pcm_playback import PcmClip, PersistentPcmPlayer


class FakeStatus:
    def __init__(self, *, output_underflow=False):
        self.output_underflow = output_underflow


class FakeOutputStream:
    def __init__(self, module, **options):
        self.module = module
        self.options = options
        self.callback = options["callback"]
        self.samplerate = options["samplerate"]
        self.channels = options["channels"]
        self.time = 0.0
        self.started = False
        self.aborted = False
        self.closed = False

    def start(self):
        self.started = True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True

    def pump(self, frames, *, underflow=False):
        output = np.empty((frames, self.channels), dtype=np.float32)
        timing = SimpleNamespace(
            outputBufferDacTime=self.time + self.module.latency,
            currentTime=self.time,
        )
        self.callback(
            output,
            frames,
            timing,
            FakeStatus(output_underflow=underflow),
        )
        self.time += frames / self.samplerate
        return output


class FakeAudioModule:
    latency = 0.1

    def __init__(self, *, sample_rate=48_000, channels=2):
        self.device = {
            "default_samplerate": sample_rate,
            "max_output_channels": channels,
        }
        self.stream = None

    def query_devices(self, *, kind):
        if kind != "output":
            raise AssertionError(kind)
        return self.device

    def OutputStream(self, **options):
        self.stream = FakeOutputStream(self, **options)
        return self.stream


class PersistentPcmPlayerTest(unittest.TestCase):
    def create_player(self):
        audio = FakeAudioModule()
        player = PersistentPcmPlayer(audio)
        self.addCleanup(player.close)
        return audio, player

    def test_load_resamples_and_normalizes_channels_once(self):
        _audio, player = self.create_player()
        with TemporaryDirectory() as directory:
            mono = Path(directory) / "mono-24k.wav"
            stereo = Path(directory) / "stereo-48k.wav"
            sf.write(mono, np.linspace(-0.5, 0.5, 2_400), 24_000, subtype="PCM_16")
            sf.write(
                stereo,
                np.column_stack(
                    (np.linspace(-0.5, 0.5, 4_800), np.linspace(0.5, -0.5, 4_800))
                ),
                48_000,
                subtype="PCM_16",
            )

            prepared_mono = player.load(mono)
            prepared_stereo = player.load(stereo)

        self.assertEqual(prepared_mono.sample_rate, 48_000)
        self.assertEqual(prepared_mono.samples.shape, (4_800, 2))
        np.testing.assert_allclose(
            prepared_mono.samples[:, 0], prepared_mono.samples[:, 1]
        )
        self.assertEqual(prepared_stereo.samples.shape, (4_800, 2))

    def test_load_bytes_uses_the_same_preparation(self):
        _audio, player = self.create_player()
        payload = BytesIO()
        sf.write(payload, np.linspace(-0.5, 0.5, 2_400), 24_000, format="WAV")

        clip = player.load_bytes(payload.getvalue(), name="captured.wav")

        self.assertEqual(clip.sample_rate, 48_000)
        self.assertEqual(clip.samples.shape, (4_800, 2))

    def test_first_frames_and_reverse_switch_use_the_same_warm_stream(self):
        audio, player = self.create_player()
        first = PcmClip(
            np.array([[0.1, -0.1], [0.2, -0.2], [0.3, -0.3]], np.float32),
            player.sample_rate,
        )
        second = PcmClip(
            np.array([[0.7, 0.6], [0.5, 0.4], [0.3, 0.2]], np.float32),
            player.sample_rate,
        )

        player.play(first)
        np.testing.assert_array_equal(audio.stream.pump(2), first.samples[:2])
        player.play(second)
        np.testing.assert_array_equal(audio.stream.pump(2), second.samples[:2])

        self.assertTrue(audio.stream.started)

    def test_completion_waits_for_dac_and_stale_end_cannot_finish_new_clip(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        old_token = player.play(clip)
        audio.stream.pump(480)
        self.assertFalse(player.snapshot().finished)

        new_token = player.play(clip)
        audio.stream.time += audio.latency + 1
        snapshot = player.snapshot()

        self.assertNotEqual(old_token, new_token)
        self.assertEqual(snapshot.token, new_token)
        self.assertFalse(snapshot.finished)

    def test_pause_seek_resume_and_underflow_are_explicit(self):
        audio, player = self.create_player()
        samples = np.arange(20, dtype=np.float32).reshape(10, 2) / 20
        clip = PcmClip(samples, player.sample_rate)
        player.play(clip)
        np.testing.assert_array_equal(audio.stream.pump(3), samples[:3])

        player.pause()
        np.testing.assert_array_equal(audio.stream.pump(2), np.zeros((2, 2)))
        player.seek(6)
        player.resume()
        np.testing.assert_array_equal(audio.stream.pump(4), samples[6:])
        self.assertTrue(player.snapshot().started)

        player.play(clip)
        audio.stream.pump(2, underflow=True)
        self.assertTrue(player.snapshot().underflowed)

    def test_close_aborts_and_closes_stream(self):
        audio = FakeAudioModule()
        player = PersistentPcmPlayer(audio)

        player.close()

        self.assertTrue(audio.stream.aborted)
        self.assertTrue(audio.stream.closed)


if __name__ == "__main__":
    unittest.main()
