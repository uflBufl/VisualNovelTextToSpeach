import unittest
from unittest.mock import patch

import numpy as np

from vntts.playback import ReplayAudioCapture, replay_audio_capture_context


class ReplayAudioCaptureTests(unittest.TestCase):
    def capture(self):
        token = replay_audio_capture_context.set(True)
        self.addCleanup(replay_audio_capture_context.reset, token)
        return ReplayAudioCapture(24_000)

    def test_capture_is_disabled_without_a_history_listener(self):
        capture = ReplayAudioCapture(24_000)
        capture.append(np.ones((3, 1), dtype=np.float32))
        self.assertIsNone(capture.finish(True))

    def test_completed_capture_reuses_the_existing_render_result(self):
        capture = self.capture()
        pcm = np.arange(6, dtype=np.float32).reshape(-1, 1)
        capture.append(pcm[:3])
        capture.append(pcm[3:])

        retained = capture.finish(True, pcm)

        self.assertIs(retained.pcm, pcm)
        self.assertTrue(retained.complete)
        self.assertEqual(retained.sample_rate, 24_000)

    def test_interrupted_capture_keeps_only_submitted_chunks(self):
        capture = self.capture()
        pcm = np.arange(6, dtype=np.float32).reshape(-1, 1)
        capture.append(pcm[:3])

        retained = capture.finish(False, pcm)

        np.testing.assert_array_equal(retained.pcm, pcm[:3])
        self.assertFalse(retained.complete)

    def test_oversized_clip_is_discarded_without_retaining_a_truncated_prefix(self):
        capture = self.capture()
        pcm = np.ones((3, 1), dtype=np.float32)
        with patch("vntts.playback.MAX_REPLAY_AUDIO_BYTES", pcm.nbytes):
            capture.append(pcm)
            capture.append(pcm)
            capture.append(pcm)
        self.assertIsNone(capture.finish(True))
        self.assertIsNone(capture.chunks)
