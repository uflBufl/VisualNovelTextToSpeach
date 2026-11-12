import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch

from vntts_artifacts.generated_audio import text_sha256

from tests.story_fixtures import write_synthetic_game_pack
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.current_voice import CurrentVoiceSnapshot
from vntts.generated_audio import GeneratedAudioRoute, LiveTTSRoute
from vntts.live import SpeechChunk
from vntts.ocr import OCRResult
from vntts.playback import PreparedPlayback
from vntts.prepared_audio_discovery import (
    PreparedAudioSnapshot,
    discover_prepared_audio,
)
from vntts.settings import AppSettings
from vntts.speech_backend_contract import SpeechBackendCapabilities
from vntts.voice_library import VoiceLibrary


class AutomaticPreparedReadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        pack, self.line_id, self.text, self.text_hash, _wav = write_synthetic_game_pack(
            self.root
        )
        self.snapshot = discover_prepared_audio(game_pack=pack)
        self.voices = Mock(spec=CurrentVoiceSnapshot)
        self.voices.matches.return_value = True
        self.capture = patch(
            "vntts.controller.CurrentVoiceSnapshot.capture", return_value=self.voices
        ).start()
        self.addCleanup(patch.stopall)
        self.discovery = Mock(side_effect=lambda *args, **kwargs: self.snapshot)
        self.controller = AppController(
            AppSettings(
                capture_mode="window",
                game_window_title="Synthetic Game",
                speech_backend="pocket-tts",
                speaker_announcement_mode="off",
            ),
            voice_library=VoiceLibrary(self.root / "voice-library"),
            prepared_audio_discovery_factory=self.discovery,
        )
        self.voices.settings = self.controller.settings
        self.live = Mock()
        self.live.name = "pocket-tts"
        self.live.capabilities = SpeechBackendCapabilities(False, False, True)
        self.live.prepare_playback.return_value = PreparedPlayback(
            object(), 0.0, 0.0, None, "live"
        )
        self.backend = self.controller._create_generated_audio_backend(self.live)
        self.controller.speech_backend = self.backend
        self.backend.set_live_mode_active(True)

    def resolve(self, text):
        resolution = {}
        result = self.controller._resolve_story_ocr_result(
            OCRResult("Ada", text, 95, "fixture", 1), resolution=resolution
        )
        return result, resolution.get("line")

    def test_named_complete_line_keeps_prepared_route_despite_other_speaker_continuation(
        self,
    ):
        source = self.snapshot.sources[0]
        continuation = self.text[:-1] + " And we should leave now."
        other = ChapterDialogue(
            "other-speaker",
            "other-chapter",
            1,
            "Bea",
            continuation,
            text_sha256(continuation),
        )
        self.snapshot = PreparedAudioSnapshot(
            (
                replace(
                    source,
                    preloader=ChapterVoicePreloader(
                        (*source.preloader.dialogue, other)
                    ),
                ),
            )
        )
        self.controller._refresh_prepared_audio(force=True)
        for previous_line in (None, other.line_id):
            with self.subTest(previous_line=previous_line):
                preloader = self.controller.chapter_voice_preloader
                preloader.current_match = None
                if previous_line:
                    preloader.select_line_id(previous_line)
                admitted, line = self.resolve(self.text)
                self.assertEqual(line.line_id, self.line_id)
                route = self.controller._prepare_live_chunk(
                    SpeechChunk(
                        1,
                        admitted.character,
                        admitted.text,
                        line_id=line.line_id,
                    )
                )
                self.assertIsInstance(route, GeneratedAudioRoute)
                self.assertEqual(route.trace.line_id, self.line_id)
                self.live.prepare_playback.assert_not_called()

    def test_ambiguous_phrase_stays_live_after_next_phrase_identifies_story(self):
        source = self.snapshot.sources[0]
        duplicate = "We should leave this place."
        rows = (
            *source.preloader.dialogue,
            ChapterDialogue(
                "ambiguous-a", "chapter-1", 1, "Ada", duplicate, text_sha256(duplicate)
            ),
            ChapterDialogue(
                "ambiguous-b", "chapter-2", 1, "Ada", duplicate, text_sha256(duplicate)
            ),
        )
        self.snapshot = PreparedAudioSnapshot(
            (replace(source, preloader=ChapterVoicePreloader(rows)),)
        )
        self.controller._refresh_prepared_audio(force=True)
        admitted, line = self.resolve(duplicate)
        self.assertIsNone(line)
        queued = SpeechChunk(1, admitted.character, admitted.text)
        _, identified = self.resolve(self.text)
        self.assertEqual(identified.line_id, self.line_id)
        first = self.controller._prepare_live_chunk(queued)
        self.assertIsInstance(first, LiveTTSRoute)
        self.assertEqual(first.trace.match_result, "unresolved-at-admission")
        following = self.controller._prepare_live_chunk(
            SpeechChunk(
                2, identified.speaker, identified.text, line_id=identified.line_id
            )
        )
        self.assertIsInstance(following, GeneratedAudioRoute)
        self.live.prepare_playback.assert_called_once_with("Ada", duplicate)

    def test_metadata_refresh_keeps_capture_backend_and_accepted_identity(self):
        _, line = self.resolve(self.text)
        reader = self.controller.live_reader
        self.controller._refresh_prepared_audio(force=True)
        self.assertIs(self.controller.speech_backend, self.backend)
        self.assertIs(self.controller.live_reader, reader)
        self.assertEqual(
            self.controller.chapter_voice_preloader.current_match.chapter, line.chapter
        )
        self.assertIsInstance(
            self.controller._prepare_live_chunk(
                SpeechChunk(1, line.speaker, line.text, line_id=line.line_id)
            ),
            GeneratedAudioRoute,
        )

    def test_changed_current_voice_excludes_old_audio_without_archive_rescan(self):
        _, line = self.resolve(self.text)
        changed = Mock(spec=CurrentVoiceSnapshot)
        changed.library = Mock()
        changed.settings = self.controller.settings
        changed.matches.return_value = False
        self.capture.return_value = changed
        self.controller.voice_assignments = Mock()
        self.controller._refresh_prepared_audio()
        self.discovery.assert_called_once()
        self.controller.voice_assignments.refresh_current_voices.assert_called_once_with(
            changed.library,
            projection=self.controller.voice_assignments.project_current_voices.return_value,
        )
        route = self.controller._prepare_live_chunk(
            SpeechChunk(1, line.speaker, line.text, line_id=line.line_id)
        )
        self.assertIsInstance(route, LiveTTSRoute)
        self.assertIsNone(
            self.backend.generated_reservations.get((self.line_id, self.text_hash))
        )

    def test_stop_or_profile_switch_rejects_late_discovery(self):
        before = self.controller.prepared_audio_snapshot
        for action in (
            lambda: self.controller.shutdown_requested.set(),
            lambda: setattr(
                self.controller,
                "settings",
                self.controller.settings.updated(game_window_title="Other Game"),
            ),
        ):
            with self.subTest(action=action):
                self.controller.shutdown_requested.clear()
                self.discovery.side_effect = lambda *args, **kwargs: (
                    action() or self.snapshot
                )
                self.controller._refresh_prepared_audio(force=True)
                self.assertIs(self.controller.prepared_audio_snapshot, before)

    def test_stop_does_not_wait_for_archive_route_construction(self):
        entered, release = Event(), Event()
        before = self.controller.prepared_audio_snapshot
        original = PreparedAudioSnapshot.library_for

        def build(snapshot, voices):
            entered.set()
            release.wait(2)
            return original(snapshot, voices)

        with patch.object(PreparedAudioSnapshot, "library_for", build):
            worker = Thread(
                target=lambda: self.controller._refresh_prepared_audio(force=True)
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.controller.story_cursor_lock.acquire(timeout=0.2))
                try:
                    self.controller.shutdown_requested.set()
                finally:
                    self.controller.story_cursor_lock.release()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIs(self.controller.prepared_audio_snapshot, before)

    def test_game_change_clears_previous_chapter_and_unbinding_clears_routes(self):
        self.resolve(self.text)
        self.assertIsNotNone(self.controller.chapter_voice_preloader.current_match)
        self.snapshot = PreparedAudioSnapshot(
            (
                replace(
                    self.snapshot.sources[0], game_id="other", game_name="Other Game"
                ),
            )
        )
        self.controller.settings = self.controller.settings.updated(
            game_window_title="Other Game"
        )
        self.controller._refresh_prepared_audio(force=True)
        self.assertIsNone(self.controller.chapter_voice_preloader.current_match)
        self.controller.settings = self.controller.settings.updated(
            game_window_title=None
        )
        self.controller._refresh_prepared_audio(force=True)
        self.assertIsNone(self.controller.prepared_audio_snapshot)
        self.assertIsNone(self.backend.library)
        self.assertFalse(self.controller.chapter_voice_preloader.dialogue)

    def test_waiting_for_audio_preflight_leaves_stop_lock_available(self):
        entered, release = Event(), Event()
        before = self.controller.prepared_audio_snapshot

        class HeldPreflight:
            def __enter__(self):
                entered.set()
                release.wait(2)

            def __exit__(self, *args):
                return False

        with patch.object(self.backend, "generated_preflight_lock", HeldPreflight()):
            worker = Thread(
                target=lambda: self.controller._refresh_prepared_audio(force=True)
            )
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.controller.story_cursor_lock.acquire(timeout=0.2))
                try:
                    self.controller.shutdown_requested.set()
                finally:
                    self.controller.story_cursor_lock.release()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertIs(self.controller.prepared_audio_snapshot, before)

    def test_incomplete_text_probe_uses_refreshed_catalog(self):
        probe = self.controller._get_live_configuration()["tracker_options"][
            "incomplete_dialogue_probe"
        ]
        old_text = "We should leave this place before the storm returns."
        short_text = "We should leave this place."
        old = ChapterDialogue(
            "old", "old-chapter", 1, "Ada", old_text, text_sha256(old_text)
        )
        new = replace(
            old,
            line_id="new",
            chapter="new-chapter",
            text=short_text,
            text_sha256=text_sha256(short_text),
        )
        self.controller.chapter_voice_preloader = ChapterVoicePreloader((old,))
        self.assertTrue(probe("Ada", short_text))
        self.snapshot = PreparedAudioSnapshot(
            (
                replace(
                    self.snapshot.sources[0], preloader=ChapterVoicePreloader((new,))
                ),
            )
        )
        self.controller._refresh_prepared_audio(force=True)
        self.assertFalse(probe("Ada", short_text))

    def test_missing_archive_uses_live_and_later_preparation_can_join_same_backend(
        self,
    ):
        self.snapshot = PreparedAudioSnapshot()
        self.controller._refresh_prepared_audio(force=True)
        self.assertIsInstance(
            self.controller._prepare_live_chunk(SpeechChunk(1, "Ada", self.text)),
            LiveTTSRoute,
        )
        pack = self.root / "game-pack.json"
        self.snapshot = discover_prepared_audio(game_pack=pack)
        self.controller._refresh_prepared_audio(force=True)
        self.assertIs(self.controller.speech_backend, self.backend)
        _, line = self.resolve(self.text)
        self.assertIsNotNone(line)


if __name__ == "__main__":
    unittest.main()
