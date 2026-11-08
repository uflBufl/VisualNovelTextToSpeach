import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import Mock

import numpy as np

from vntts.audio_output import resolve_audio_output
from vntts.playback_observer import observe_playback_start, playback_started_context
from vntts.rust_output import RustOutputStream


class PlaybackObserverTest(unittest.TestCase):
    def test_copied_context_notifies_once_across_worker_and_caller(self):
        callback = Mock()
        with observe_playback_start(callback):
            notify = playback_started_context.get()
            self.assertIsNotNone(notify)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(copy_context().run, notify).result()
            notify()
        callback.assert_called_once_with()
        self.assertIsNone(playback_started_context.get())

    def test_injected_stream_notifies_only_after_successful_nonempty_write(self):
        callback = Mock()
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        output = Mock()
        output.OutputStream.return_value = stream
        with observe_playback_start(callback):
            wrapped = resolve_audio_output(output).OutputStream(
                samplerate=24000, channels=1, dtype="float32", latency="low"
            )
        with wrapped:
            callback.assert_not_called()
            wrapped.write(np.empty((0, 1)))
            callback.assert_not_called()
            stream.write.side_effect = RuntimeError("Output unavailable")
            with self.assertRaises(RuntimeError):
                wrapped.write(np.ones((2, 1)))
            callback.assert_not_called()
            stream.write.side_effect = None
            wrapped.write(np.ones((2, 1)))
            wrapped.write(np.ones((2, 1)))
        callback.assert_called_once_with()

    def test_native_stream_waits_for_nonempty_first_callback(self):
        callback = Mock()
        with observe_playback_start(callback):
            stream = RustOutputStream(
                Mock(), samplerate=24000, channels=1, dtype="float32", latency="low"
            )
        stream.worker = Mock()
        stream.clip_id = 1
        stream.sent_frames = 2
        stream.worker.receive.side_effect = [
            {"id": 1, "event": "appended", "frames": 2},
            {"id": 1, "event": "first_callback", "frames_written": 0},
            {"id": 1, "event": "first_callback", "frames_written": 1},
        ]
        stream._event(0.1)
        stream._event(0.1)
        callback.assert_not_called()
        stream._event(0.1)
        callback.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
