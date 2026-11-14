import unittest
from unittest.mock import Mock

from vntts.live_speech import play_typed_text
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.services.tts_engine import (
    AudioPlaybackError,
    TTSConfigurationError,
    TTSSynthesisError,
)


class StubBackend:
    def __init__(self, outcome, *, prepared=None):
        self.outcome = outcome
        self.prepared = prepared or PreparedPlayback(object(), None, None, None, "test")

    def prepare_playback(self, character, text):
        del character, text
        return self.prepared

    def play_prepared(self, prepared, *, playback_guard=None):
        del prepared, playback_guard
        return self.outcome


class TypedLiveSpeechTests(unittest.TestCase):
    def test_symbol_only_text_skips_tts_and_preserves_playback_guard(self):
        for text in ("?", "?!", "—", "_", "♪", "？ ！"):
            with self.subTest(text=text):
                backend = Mock()
                self.assertTrue(play_typed_text(backend, "Ada", text))
                guard = Mock(return_value=False)
                self.assertFalse(play_typed_text(backend, "Ada", text, guard))
                guard.assert_called_once_with()
                backend.prepare_playback.assert_not_called()
                backend.play_prepared.assert_not_called()

    def test_words_digits_and_unknown_glyphs_keep_the_backend_path(self):
        for text in ("I", "1", "No!", "Hello ♪", "\ufffd", "? \ufffc"):
            with self.subTest(text=text):
                backend = Mock(
                    wraps=StubBackend(PlaybackOutcome(PlaybackStatus.COMPLETED, 1.0))
                )
                self.assertTrue(play_typed_text(backend, "Ada", text))
                backend.prepare_playback.assert_called_once_with("Ada", text)
                backend.play_prepared.assert_called_once()

    def test_completed_and_interrupted_outcomes_remain_distinct(self):
        self.assertTrue(
            play_typed_text(
                StubBackend(PlaybackOutcome(PlaybackStatus.COMPLETED, 10.0)),
                "Narrator",
                "Complete.",
            )
        )
        self.assertFalse(
            play_typed_text(
                StubBackend(PlaybackOutcome(PlaybackStatus.INTERRUPTED, 5.0)),
                "Narrator",
                "Interrupted.",
            )
        )

    def test_untyped_prepared_and_outcome_values_fail_closed(self):
        with self.assertRaisesRegex(TypeError, "untyped prepared"):
            play_typed_text(
                StubBackend(True, prepared="legacy-payload"),
                "Narrator",
                "Legacy.",
            )
        for invalid in (None, True, "completed", object()):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(TypeError, "untyped playback outcome"),
            ):
                play_typed_text(StubBackend(invalid), "Narrator", "Invalid.")

    def test_failed_outcome_preserves_approved_error_category(self):
        for error_type in (
            TTSConfigurationError,
            TTSSynthesisError,
            AudioPlaybackError,
        ):
            with (
                self.subTest(error_type=error_type),
                self.assertRaisesRegex(error_type, "specific failure"),
            ):
                play_typed_text(
                    StubBackend(
                        PlaybackOutcome(
                            PlaybackStatus.FAILED,
                            None,
                            error="specific failure",
                            error_type=error_type,
                        )
                    ),
                    "Narrator",
                    "Failure.",
                )

    def test_unknown_failure_category_maps_to_audio_playback_error(self):
        with self.assertRaisesRegex(AudioPlaybackError, "device failed"):
            play_typed_text(
                StubBackend(
                    PlaybackOutcome(
                        PlaybackStatus.FAILED,
                        None,
                        error="device failed",
                        error_type=OSError,
                    )
                ),
                "Narrator",
                "Failure.",
            )


if __name__ == "__main__":
    unittest.main()
