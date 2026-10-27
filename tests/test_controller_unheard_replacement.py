import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from vntts.audio_output import PcmAdmissionRejected
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.dialog_capture import CapturedDialogFrame
from vntts.generated_audio import (
    AudioRouteTrace,
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    LiveTTSRoute,
    PendingGeneratedAudioRoute,
    SourceAudioRoute,
)
from vntts.live import RecognizedDialog, SpeechChunk
from vntts.ocr import OCRResult
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.settings import AppSettings
from vntts.voices import CharacterVoiceRegistry


def trace(match="no-match", line_id=None):
    return AudioRouteTrace(None, "live:test", match, None, None, line_id, "test")


def prepared():
    return PreparedPlayback(object(), None, None, None, "live:test")


class ControllerUnheardReplacementTest(unittest.TestCase):
    def controller(self):
        controller = AppController(AppSettings(audio_source_policy="prefer-generated"))
        controller.error_handler = Mock()
        controller.status_handler = Mock()
        controller.pipeline_event_handler = Mock()
        controller.live_reader = Mock()
        return controller

    def test_only_ready_canonical_resolution_carries_identity(self):
        text = "The prepared story contains this complete sentence."
        line = ChapterDialogue(
            "line", "chapter", 1, "Narrator", text, text_sha256(text)
        )
        for ready, observed_text, owned_prefix, changed_speaker in (
            (True, text, False, False),
            (False, text, False, False),
            (True, "Unrelated text.", False, False),
            (True, text, True, False),
            (True, text, False, True),
        ):
            with self.subTest(ready=ready, observed=observed_text):
                controller = self.controller()
                controller.chapter_voice_preloader = ChapterVoicePreloader((line,))
                controller.voice_router = SimpleNamespace(
                    registry=CharacterVoiceRegistry()
                )
                controller.uncertain_frame_recorder = None
                controller._publish_diagnostic = Mock()
                controller._resolve_voice_label = Mock(return_value="Story voice")
                if owned_prefix:
                    controller._expected_sequence_prefix_line_locked = Mock(
                        return_value=line
                    )
                if changed_speaker:
                    controller._canonical_observed_character = Mock(
                        return_value="Different"
                    )
                backend = Mock(spec=GeneratedAudioFallbackBackend)
                backend.reserve_generated_line_for_early_playback.return_value = ready
                controller.speech_backend = backend
                raw = OCRResult("Narrator", observed_text, 95, "balanced", 1)
                frame = CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                with patch(
                    "vntts.dialog_capture.recognize_dialog_image_result",
                    return_value=raw,
                ):
                    actual = controller._recognize_live_frame(frame)
                qualified = (
                    observed_text == text and not owned_prefix and not changed_speaker
                )
                if ready and qualified:
                    self.assertEqual(actual, RecognizedDialog("Narrator", text, "line"))
                    backend.reserve_generated_line_for_early_playback.assert_called_once_with(
                        line
                    )
                else:
                    self.assertEqual(
                        actual,
                        ("Different" if changed_speaker else "Narrator", observed_text),
                    )
                if not qualified:
                    backend.reserve_generated_line_for_early_playback.assert_not_called()

    def test_only_unindexed_no_match_live_route_allows_replacement(self):
        chunk = SpeechChunk(1, "Narrator", "Current words.")
        for route, active_chunk, eligible in (
            (LiveTTSRoute(prepared(), trace(), None, None), chunk, True),
            (
                LiveTTSRoute(prepared(), trace("exact", "line"), None, None),
                chunk,
                False,
            ),
            (LiveTTSRoute(prepared(), trace("skipped"), None, None), chunk, False),
            (
                LiveTTSRoute(prepared(), trace("no-match", "line"), None, None),
                chunk,
                False,
            ),
            (
                LiveTTSRoute(prepared(), trace(), None, None),
                SpeechChunk(1, "Narrator", "Current words.", line_id="line"),
                False,
            ),
            (
                LiveTTSRoute(prepared(), trace(), None, None),
                SpeechChunk(1, "Narrator", "Current words.", explicit_replay=True),
                False,
            ),
            (GeneratedAudioRoute(Mock(), trace()), chunk, False),
            (
                PendingGeneratedAudioRoute("line", "hash", "text", "Narrator", trace()),
                chunk,
                False,
            ),
            (SourceAudioRoute(Mock(), trace()), chunk, False),
            (prepared(), chunk, False),
        ):
            with self.subTest(
                route=type(route).__name__, trace=getattr(route, "trace", None)
            ):
                controller = self.controller()
                controller.speech_backend = SimpleNamespace(
                    prepare_route=Mock(return_value=route)
                )
                controller._materialize_live_route = Mock(
                    side_effect=lambda value, _: value
                )
                controller._prepare_speaker_announcement = Mock(
                    return_value=(None, None)
                )
                controller._describe_audio_source = Mock(return_value="test")
                controller._build_audio_route_trace = Mock(return_value=trace())
                controller.route_trace_handler = Mock()
                controller._record_pipeline_route = Mock()
                controller._publish_live_sequence_status = Mock()
                controller._refresh_diagnostic_metrics = Mock()
                self.assertIs(controller._prepare_live_chunk(active_chunk), route)
                if eligible:
                    controller.live_reader.allow_unheard_replacement.assert_called_once_with(
                        active_chunk
                    )
                else:
                    controller.live_reader.allow_unheard_replacement.assert_not_called()

    def test_pcm_admission_rejection_is_interruption_for_dialogue_and_announcement(
        self,
    ):
        for direct in (True, False):
            for announcement in (True, False):
                with self.subTest(direct=direct, announcement=announcement):
                    controller = self.controller()
                    controller._observe_live_playback_backpressure = Mock()
                    play = Mock()
                    if direct:
                        play.side_effect = PcmAdmissionRejected("Replaced")
                    else:
                        play.return_value = PlaybackOutcome(
                            PlaybackStatus.FAILED,
                            None,
                            error="Replaced",
                            error_type=PcmAdmissionRejected,
                        )
                    controller.speech_backend = SimpleNamespace(play_route=play)
                    chunk = SpeechChunk(1, "Ada", "Current words.")
                    route = LiveTTSRoute(prepared(), trace(), None, None)
                    if announcement:
                        outcome = controller._play_speaker_announcement(
                            chunk, route, "Ada"
                        )
                        self.assertIsNone(controller.last_visible_speaker_key)
                    else:
                        outcome = controller._dispatch_live_playback(
                            controller.live_reader, chunk, route
                        )
                    self.assertEqual(outcome.status, PlaybackStatus.INTERRUPTED)
                    self.assertIsNone(outcome.error)
                    self.assertIsNone(outcome.error_type)
                    controller.error_handler.assert_not_called()
