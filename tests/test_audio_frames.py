import unittest

import numpy as np

from vntts.authoring.audio_frames import frame_rms


class AudioFramesTest(unittest.TestCase):
    def test_partial_frame_uses_only_recorded_samples(self):
        for dtype in (np.float32, np.float64):
            with self.subTest(dtype=dtype):
                samples = np.array([3, 4, 0, 0, 6], dtype=dtype)
                samples.flags.writeable = False
                actual = frame_rms(samples, 2)
                np.testing.assert_array_equal(
                    actual, np.sqrt(np.array([12.5, 0, 36], dtype=dtype))
                )
                self.assertEqual(actual.dtype, samples.dtype)
                np.testing.assert_array_equal(samples, [3, 4, 0, 0, 6])

    def test_frame_reductions_preserve_precision_at_the_silence_threshold(self):
        rng = np.random.default_rng(918)
        for dtype in (np.float32, np.float64):
            threshold = dtype(10 ** (-45 / 20))
            levels = [
                np.nextafter(threshold, dtype(0)),
                threshold,
                np.nextafter(threshold, dtype(1)),
            ]
            for size in (1, 79, 80, 81, 160, 167):
                with self.subTest(dtype=dtype, size=size):
                    samples = rng.choice(levels, size * 2).astype(dtype)[::2]
                    samples.flags.writeable = False
                    before = samples.tobytes()
                    expected = np.asarray(
                        [
                            np.sqrt(np.mean(samples[start : start + 80] ** 2))
                            for start in range(0, size, 80)
                        ]
                    )
                    actual = frame_rms(samples, 80)
                    np.testing.assert_array_equal(actual, expected)
                    np.testing.assert_array_equal(
                        actual <= threshold, expected <= threshold
                    )
                    self.assertEqual(actual.dtype, samples.dtype)
                    self.assertEqual(samples.tobytes(), before)
