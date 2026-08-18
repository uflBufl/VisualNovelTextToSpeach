import unittest

import numpy as np

from vntts.authoring.failure_repair import (
    safe_sentence_segments,
    trim_excess_edge_silence,
)


class AuthoringFailureRepairTest(unittest.TestCase):
    def test_sentence_split_requires_complete_substantial_boundaries(self):
        self.assertEqual(
            safe_sentence_segments(
                "The gate is already open. We should leave before dawn."
            ),
            ("The gate is already open.", "We should leave before dawn."),
        )
        self.assertEqual(
            safe_sentence_segments("Mrs. Owen waits beside the gate."),
            ("Mrs. Owen waits beside the gate.",),
        )
        self.assertEqual(
            safe_sentence_segments("Wait here, and listen carefully."),
            ("Wait here, and listen carefully.",),
        )
        self.assertEqual(
            safe_sentence_segments("Stop! Go."),
            ("Stop! Go.",),
        )

    def test_edge_trim_removes_only_excess_boundary_silence(self):
        sample_rate = 1_000
        speech = np.full(500, 0.2, dtype=np.float32)
        internal = np.zeros(300, dtype=np.float32)
        samples = np.concatenate(
            (
                np.zeros(1_000, dtype=np.float32),
                speech,
                internal,
                speech,
                np.zeros(900, dtype=np.float32),
            )
        )

        result = trim_excess_edge_silence(samples, sample_rate)

        self.assertEqual(result.leading_trimmed_samples, 920)
        self.assertEqual(result.trailing_trimmed_samples, 820)
        self.assertEqual(len(result.pcm), len(samples) - 1_740)
        self.assertTrue(np.array_equal(result.pcm[580:880], internal))
        self.assertTrue(np.all(result.pcm[:80] == 0))
        self.assertTrue(np.all(result.pcm[-80:] == 0))

    def test_short_edges_and_all_silence_are_not_changed(self):
        short = np.concatenate(
            (
                np.zeros(100, dtype=np.float32),
                np.ones(200, dtype=np.float32) * 0.2,
                np.zeros(100, dtype=np.float32),
            )
        )
        untouched = trim_excess_edge_silence(short, 1_000)
        silent = np.zeros(2_000, dtype=np.float32)
        silent_result = trim_excess_edge_silence(silent, 1_000)

        self.assertEqual(untouched.leading_trimmed_samples, 0)
        self.assertEqual(untouched.trailing_trimmed_samples, 0)
        self.assertTrue(np.array_equal(untouched.pcm, short))
        self.assertTrue(np.array_equal(silent_result.pcm, silent))


if __name__ == "__main__":
    unittest.main()
