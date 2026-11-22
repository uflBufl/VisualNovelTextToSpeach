import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

import numpy as np

from vntts.audio_output import AudioDeviceChangedError
from vntts.history_playback import HistoryAudioPlayer
from vntts.playback import ReplayAudio
from vntts.playback_observer import observe_playback_start


class _Output:
    def __init__(self):
        self.streams = []
        self.on_write = lambda _stream: None

    def query_devices(self, *, kind):
        return {"default_samplerate": 100}

    def OutputStream(self, **options):
        stream = _Stream(self, options)
        self.streams.append(stream)
        return stream


class _Stream:
    def __init__(self, output, options):
        self.output = output
        self.options = options
        self.writes = []
        self.aborted = self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def write(self, samples):
        self.output.on_write(self)
        self.writes.append(samples.copy())
        return False

    def abort(self):
        self.aborted = True


def _clip(value=0.4, *, sample_rate=100, channels=1):
    shape = (10,) if channels == 1 else (10, channels)
    return ReplayAudio(np.full(shape, value, dtype=np.float32), sample_rate)


class HistoryAudioPlayerTest(unittest.TestCase):
    def play(self, player, clips, *, cancelled=None, volume=1.0):
        return player.play(
            clips,
            volume=volume,
            playback_latency="low",
            cancelled=cancelled or Event(),
        )

    def test_sequence_uses_current_settings_without_changing_retained_pcm(self):
        output = _Output()
        player = HistoryAudioPlayer(output)
        clips = [_clip(), _clip(0.8, sample_rate=50, channels=2)]
        before = [clip.pcm.copy() for clip in clips]
        self.assertTrue(self.play(player, clips, volume=0.5))
        self.assertEqual(len(output.streams), 2)
        for stream, channels in zip(output.streams, (1, 2), strict=True):
            self.assertEqual(
                stream.options,
                {
                    "samplerate": 100,
                    "channels": channels,
                    "dtype": "float32",
                    "latency": "low",
                },
            )
            self.assertTrue(stream.closed)
        np.testing.assert_allclose(np.concatenate(output.streams[0].writes), 0.2)
        self.assertEqual(np.concatenate(output.streams[1].writes).shape, (20, 2))
        for clip, original in zip(clips, before, strict=True):
            np.testing.assert_array_equal(clip.pcm, original)
        self.assertTrue(self.play(player, clips[:1], volume=0.25))
        np.testing.assert_allclose(np.concatenate(output.streams[-1].writes), 0.1)

    def test_shared_output_notifies_only_after_successful_first_pcm(self):
        output = _Output()
        started = Mock()

        def before_write(_stream):
            if not any(stream.writes for stream in output.streams):
                started.assert_not_called()

        output.on_write = before_write
        with (
            observe_playback_start(started),
            patch(
                "vntts.system_audio.get_system_audio_output", return_value=output
            ) as shared_output,
        ):
            self.assertTrue(self.play(HistoryAudioPlayer(), [_clip(), _clip()]))
        shared_output.assert_called_once_with()
        started.assert_called_once_with()

    def test_cancel_before_start_never_resolves_output_or_notifies(self):
        cancelled = Event()
        cancelled.set()
        started = Mock()
        with (
            observe_playback_start(started),
            patch("vntts.system_audio.get_system_audio_output") as shared_output,
        ):
            self.assertFalse(
                self.play(HistoryAudioPlayer(), [_clip()], cancelled=cancelled)
            )
        shared_output.assert_not_called()
        started.assert_not_called()

    def test_stop_during_pcm_aborts_entry_before_next_part(self):
        output = _Output()
        cancelled = Event()
        output.on_write = lambda _stream: cancelled.set()
        self.assertFalse(
            self.play(
                HistoryAudioPlayer(output), [_clip(), _clip()], cancelled=cancelled
            )
        )
        self.assertEqual(len(output.streams), 1)
        self.assertTrue(output.streams[0].aborted)
        self.assertTrue(output.streams[0].closed)
        self.assertEqual(len(output.streams[0].writes), 1)

    def test_failure_before_pcm_does_not_report_started_and_allows_retry(self):
        output = _Output()
        error = AudioDeviceChangedError("Output changed")

        def fail(_stream):
            raise error

        output.on_write = fail
        player = HistoryAudioPlayer(output)
        started = Mock()
        with observe_playback_start(started):
            with self.assertRaises(AudioDeviceChangedError) as raised:
                self.play(player, [_clip()])
        self.assertIs(raised.exception, error)
        self.assertTrue(output.streams[0].closed)
        started.assert_not_called()
        output.on_write = lambda _stream: None
        self.assertTrue(self.play(player, [_clip()]))

    def test_cancelled_failure_returns_false(self):
        output = _Output()
        cancelled = Event()

        def fail_after_stop(_stream):
            cancelled.set()
            raise RuntimeError("Stopped output")

        output.on_write = fail_after_stop
        self.assertFalse(
            self.play(HistoryAudioPlayer(output), [_clip()], cancelled=cancelled)
        )
        self.assertTrue(output.streams[0].closed)

    def test_concurrent_retry_keeps_its_own_cancellation(self):
        output = _Output()
        player = HistoryAudioPlayer(output)
        first_write = Event()
        release_write = Event()
        cancelled = Event()

        def wait_on_first(stream):
            if stream is output.streams[0] and not stream.writes:
                first_write.set()
                self.assertTrue(release_write.wait(5))

        output.on_write = wait_on_first
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.play, player, [_clip()], cancelled=cancelled)
            self.assertTrue(first_write.wait(5))
            retry = executor.submit(self.play, player, [_clip(0.6)])
            cancelled.set()
            release_write.set()
            self.assertFalse(first.result(timeout=5))
            self.assertTrue(retry.result(timeout=5))
        self.assertTrue(output.streams[0].aborted)
        self.assertFalse(output.streams[1].aborted)
        np.testing.assert_allclose(np.concatenate(output.streams[1].writes), 0.6)

    def test_empty_entry_has_no_playback(self):
        output = _Output()
        player = HistoryAudioPlayer(output)
        self.assertFalse(self.play(player, []))
        self.assertEqual(output.streams, [])

    def test_retained_interrupted_prefix_can_be_replayed(self):
        output = _Output()
        prefix = ReplayAudio(_clip().pcm, 100, complete=False)
        self.assertTrue(self.play(HistoryAudioPlayer(output), [prefix]))
        self.assertTrue(output.streams[0].writes)


if __name__ == "__main__":
    unittest.main()
