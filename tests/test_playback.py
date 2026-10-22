import unittest

from vntts.playback import PlaybackOutcome, PlaybackStatus


class PlaybackOutcomeTest(unittest.TestCase):
    def test_onset_observation_preserves_measured_estimated_and_unknown_timing(self):
        for absolute, relative, expected in (
            (12.0, 500.0, (12.0, "measured")),
            (None, 500.0, (10.5, "estimated")),
            (None, 0.0, (10.0, "estimated")),
            (None, None, None),
            (None, True, None),
        ):
            with self.subTest(absolute=absolute, relative=relative):
                outcome = PlaybackOutcome(
                    PlaybackStatus.COMPLETED,
                    playback_ms=None,
                    first_audio_at=absolute,
                    first_audio_ms=relative,
                )
                self.assertEqual(outcome.onset_observation(10.0), expected)
