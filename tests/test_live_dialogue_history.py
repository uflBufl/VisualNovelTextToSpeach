import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from tests.pocket_model_fixtures import pocket_model_readiness
from tests.test_main import RecordingAnnouncementBackend
from vntts.controller import AppController, PreparedLiveChunkRoutes
from vntts.history import DialogueHistory
from vntts.live_tracking import SpeechChunk
from vntts.playback import PlaybackOutcome, PlaybackStatus, ReplayAudio
from vntts.playback_observer import playback_started_context
from vntts.settings import AppSettings
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class LiveDialogueHistoryTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(pocket_model_readiness(False))
        self.enterContext(
            patch(
                "vntts.controller_components.prepare_sparse_ocr_model",
                return_value=Path("models/short-text.onnx"),
            )
        )
        for name in ("warm_up_sparse_ocr", "configure_sparse_ocr"):
            self.enterContext(patch(f"vntts.controller_components.{name}"))
        self.controller = AppController(
            AppSettings(audio_source_policy="live-tts-only"),
            tts_factory=Mock(),
            status_handler=Mock(),
            dialog_handler=Mock(),
        )
        self.controller.live_reader = Mock(run_id="live-run", max_speech_jobs=2)
        self.controller.live_reader.wait_until_playable.return_value = True
        self.backend = RecordingAnnouncementBackend()
        self.controller.speech_backend = self.backend
        self.controller.voice_router = Mock(
            registry=CharacterVoiceRegistry([CharacterVoice("Ada", "ada")]),
            narrator_speaker="Narrator",
        )

    def play(self, chunk, *, audible=True, outcome=None):
        prepared = self.backend.prepare_playback(chunk.character, chunk.text)

        def playback(_prepared, *, playback_guard):
            if not playback_guard():
                return PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)
            if audible:
                notify = playback_started_context.get()
                self.assertIsNotNone(notify)
                notify()
                notify()
            return outcome or PlaybackOutcome(PlaybackStatus.COMPLETED, 1.0)

        self.backend.play_prepared = playback
        return self.controller._play_live_chunk(chunk, prepared)

    def texts(self):
        return [entry.text for entry in self.controller.history.snapshot()]

    def test_observations_and_preparation_leave_history_empty_until_audio_starts(self):
        chunk = SpeechChunk(
            1,
            "Narrator",
            "The gardener wields his loppers, reaching toward the higher branches.",
            ordinal=1,
        )
        for text in (
            "The gardener wields his loppers, reaching towarc",
            "The gardener wields his loppers, reaching toward the high«",
        ):
            self.controller._dialog_observed(chunk.character, text)
        self.controller._dialog_observed(chunk.character, chunk.text)
        self.controller._prepare_live_chunk(chunk)
        self.assertEqual(self.texts(), [])

        self.play(chunk, audible=False)
        self.assertEqual(self.texts(), [])
        self.play(chunk)
        self.assertEqual(self.texts(), [chunk.text])

    def test_only_the_recording_that_started_is_attached_to_its_history_entry(self):
        chunk = SpeechChunk(1, "Ada", "A line.", ordinal=1)
        clip = ReplayAudio(np.ones(4, np.float32), 24000, complete=False)
        outcome = PlaybackOutcome(PlaybackStatus.INTERRUPTED, 1.0, replay_audio=clip)
        self.play(chunk, audible=False, outcome=outcome)
        self.assertEqual(self.texts(), [])
        self.play(chunk, outcome=outcome)
        entry = self.controller.history.snapshot()[0]
        self.assertIs(self.controller.history.recording(entry.id)[0], clip)

    def test_history_is_visible_at_onset_before_interrupted_playback_returns(self):
        chunk = SpeechChunk(1, "Ada", "An interrupted line.", ordinal=1)
        prepared = self.backend.prepare_playback(chunk.character, chunk.text)

        def playback(_prepared, *, playback_guard):
            self.assertTrue(playback_guard())
            self.assertEqual(self.texts(), [])
            playback_started_context.get()()
            self.assertEqual(self.texts(), [chunk.text])
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, 1.0)

        self.backend.play_prepared = playback
        self.assertFalse(self.controller._play_live_chunk(chunk, prepared))
        self.assertEqual(self.texts(), [chunk.text])

    def test_chunks_share_one_entry_and_retries_do_not_duplicate_spoken_text(self):
        first = SpeechChunk(1, "Ada", "First sentence.", ordinal=1)
        second = replace(first, text="Second sentence.", ordinal=2)
        self.play(first)
        entry_id = self.controller.history.snapshot()[0].id
        self.controller._dialog_observed("Ada", "Unspoken OCR suffix")
        self.play(second)
        self.play(second)

        self.assertEqual(self.texts(), ["First sentence. Second sentence."])
        self.assertEqual(self.controller.history.snapshot()[0].id, entry_id)

    def test_identical_lines_in_new_generations_and_reader_runs_are_kept(self):
        chunk = SpeechChunk(1, "Ada", "Again.", ordinal=1)
        self.play(chunk)
        self.play(replace(chunk, generation=2))
        self.controller.live_reader.run_id = "next-live-run"
        self.play(chunk)

        self.assertEqual(self.texts(), ["Again.", "Again.", "Again."])

    def test_explicit_replay_does_not_add_another_history_entry(self):
        chunk = SpeechChunk(1, "Ada", "Repeat this line.", ordinal=1)
        self.play(chunk)
        self.play(replace(chunk, generation=2, explicit_replay=True))

        self.assertEqual(self.texts(), [chunk.text])

    def test_resuming_interrupted_suffix_records_it_once_before_or_after_onset(self):
        for audible_before_interruption in (False, True):
            with self.subTest(audible_before_interruption=audible_before_interruption):
                self.controller.history = DialogueHistory()
                first = SpeechChunk(1, "Ada", "First sentence.", ordinal=1)
                suffix = replace(first, text="Second sentence.", ordinal=2)
                self.play(first)
                self.assertFalse(
                    self.play(
                        suffix,
                        audible=audible_before_interruption,
                        outcome=PlaybackOutcome(PlaybackStatus.INTERRUPTED, None),
                    )
                )
                self.assertEqual(
                    self.texts(),
                    [
                        "First sentence. Second sentence."
                        if audible_before_interruption
                        else "First sentence."
                    ],
                )
                resumed = replace(
                    suffix,
                    generation=2,
                    ordinal=None,
                    explicit_replay=True,
                    resumed_part=(suffix.generation, suffix.ordinal),
                )
                self.play(resumed)
                self.assertEqual(self.texts(), ["First sentence. Second sentence."])

    def test_stale_playback_without_audio_does_not_enter_history(self):
        self.controller.live_reader.is_playback_current.return_value = False
        self.assertFalse(self.play(SpeechChunk(1, "Ada", "Stale line.", ordinal=1)))
        self.assertEqual(self.texts(), [])

    def test_speaker_announcement_without_dialogue_audio_does_not_enter_history(self):
        self.controller.settings = self.controller.settings.updated(
            speaker_announcement_mode="all-speakers"
        )
        chunk = SpeechChunk(1, "Ada", "A dialogue line.", ordinal=1)
        prepared = self.controller._prepare_live_chunk(chunk)
        self.assertIsInstance(prepared, PreparedLiveChunkRoutes)
        played = []

        def playback(prepared, *, playback_guard):
            self.assertTrue(playback_guard())
            played.append(prepared.payload.text)
            if prepared.payload.character == "Narrator":
                notify = playback_started_context.get()
                if notify is not None:
                    notify()
                return PlaybackOutcome(PlaybackStatus.COMPLETED, 1.0)
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, None)

        self.backend.play_prepared = playback
        self.assertFalse(self.controller._play_live_chunk(chunk, prepared))
        self.assertEqual(played, ["Ada.", chunk.text])
        self.assertEqual(self.texts(), [])


if __name__ == "__main__":
    unittest.main()
