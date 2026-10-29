import unittest

from vntts.playback import (
    PlaybackOutcome,
    PlaybackStatus,
    SilentPlayback,
    prepare_silent_playback,
)


class PlaybackOutcomeTest(unittest.TestCase):
    def test_only_recognized_symbol_text_prepares_silence(self):
        for text in ("?", "?!", "—", "_", "♪", " ？ ！ "):
            with self.subTest(text=text):
                prepared = prepare_silent_playback(text)
                self.assertIsInstance(prepared.payload, SilentPlayback)
                self.assertTrue(prepared.generation_completed)
                self.assertIsNone(prepared.first_audio_ms)
        for text in (
            "",
            " ",
            "\ufffd",
            "? \ufffc",
            "\u0301",
            "I",
            "1",
            "No!",
            "Hello ♪",
        ):
            with self.subTest(text=text):
                self.assertIsNone(prepare_silent_playback(text))

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
