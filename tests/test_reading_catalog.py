import unittest
from concurrent.futures import Future
from copy import copy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts_artifacts.story_index import write_story_index_document

from tests.audio_output_fixtures import FakeAudioOutput
from tests.story_fixtures import write_synthetic_game_pack
from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.controller import AppController
from vntts.current_voice import CurrentVoiceSnapshot
from vntts.game_content_importer import (
    GameContentImportCancelled,
    GameContentImportError,
)
from vntts.generated_audio import LiveTTSRoute, SourceAudioRoute
from vntts.live import SpeechChunk
from vntts.ocr import OCRResult
from vntts.prepared_audio_discovery import (
    PreparedAudioSnapshot,
    discover_prepared_audio,
)
from vntts.settings import AppSettings
from vntts.speech_backend_contract import SpeechBackendCapabilities
from vntts.voice_library import VoiceLibrary


class ReadingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog_root = self.root / "game-content"
        self.source = (
            self.catalog_root / "reverse1999" / "reverse1999" / "story-index.jsonl"
        )
        self.unvoiced = "She tries to summon her reason, looking for a way out, but only shattered half-thoughts come to her."
        self.original = (
            "Heinrich must be taken to the headquarters. He has vital information!"
        )
        self.write_catalog()
        self.snapshot = discover_prepared_audio(catalog_root=self.catalog_root)

    def write_catalog(self, original=None):
        rows = [
            {
                "record_type": "line",
                "line_id": f"reverse1999:100630:{sequence}",
                "chapter": "100630",
                "sequence": sequence,
                "speaker": "Narrator",
                "voice_character": "Narrator",
                "text": text,
                "kind": "narration",
                "collection_kind": "main_story",
                "source_audio_status": status,
                "source_audio_id": voice,
                "speakable": True,
            }
            for sequence, text, status, voice in (
                (6, self.unvoiced, "absent", None),
                (7, original or self.original, "available", "610016044"),
            )
        ]
        write_story_index_document(
            self.source, {"game": "Reverse: 1999", "language": "en"}, rows
        )

    def controller(self, *, policy="prefer-generated"):
        voices = Mock(spec=CurrentVoiceSnapshot)
        voices.matches.return_value = True
        capture = patch(
            "vntts.controller.CurrentVoiceSnapshot.capture", return_value=voices
        )
        capture.start()
        self.addCleanup(capture.stop)
        controller = AppController(
            AppSettings(
                game_window_title="Reverse: 1999",
                speaker_announcement_mode="off",
                audio_source_policy=policy,
            ),
            voice_library=VoiceLibrary(self.root / "voices"),
            prepared_audio_discovery_factory=lambda *args, **kwargs: self.snapshot,
        )
        voices.settings = controller.settings
        live = Mock()
        live.name = "live-test"
        live.capabilities = SpeechBackendCapabilities(False, False, True)
        backend = controller._create_generated_audio_backend(live)
        backend.audio_output = FakeAudioOutput()
        controller.speech_backend = backend
        backend.set_live_mode_active(True)
        return controller, backend, live

    def test_imported_catalog_is_discovered_without_generation_jobs(self):
        self.assertFalse(self.snapshot.sources)
        scoped = self.snapshot.for_settings(
            AppSettings(game_window_title="Reverse: 1999")
        )
        self.assertEqual(set(scoped.preloader.store.chapter_ids()), {"100630"})
        self.assertFalse(
            self.snapshot.for_settings(
                AppSettings(game_window_title="Other game")
            ).catalogs
        )
        explicit = self.snapshot.for_settings(AppSettings(story_index=str(self.source)))
        self.assertEqual(
            tuple(explicit.preloader.dialogue), tuple(scoped.preloader.dialogue)
        )

    def test_changed_catalog_is_reloaded_but_unchanged_catalog_is_reused(self):
        with patch(
            "vntts.prepared_audio_discovery.open_reading_catalog",
            side_effect=AssertionError("reloaded unchanged catalog"),
        ):
            unchanged = discover_prepared_audio(
                catalog_root=self.catalog_root, previous=self.snapshot
            )
        self.assertIs(unchanged.catalogs[0], self.snapshot.catalogs[0])
        self.write_catalog("An updated primary original.")
        self.assertTrue(self.snapshot.changed())
        updated = discover_prepared_audio(
            catalog_root=self.catalog_root, previous=self.snapshot
        )
        self.assertEqual(
            updated.preloader.line_for_id("reverse1999:100630:7").text,
            "An updated primary original.",
        )

    def test_repeated_refresh_preserves_loaded_recognition_and_chapter(self):
        controller, _backend, _live = self.controller()
        preloader = controller.chapter_voice_preloader
        preloader.select_line_id("reverse1999:100630:6")
        for _ in range(3):
            with patch(
                "vntts.prepared_audio_discovery.ChapterVoicePreloader",
                side_effect=AssertionError("recognition rebuilt"),
            ):
                controller._refresh_prepared_audio(force=True)
            self.assertIs(controller.chapter_voice_preloader, preloader)
            self.assertEqual(preloader.current_match.chapter, "100630")

    def test_new_ready_audio_preserves_the_existing_dialogue_index(self):
        pack, *_ = write_synthetic_game_pack(self.root / "pack")
        source = replace(
            discover_prepared_audio(game_pack=pack).sources[0],
            game_id="reverse1999",
            game_name="Reverse: 1999",
        )
        self.snapshot = PreparedAudioSnapshot(
            (source,), catalogs=self.snapshot.catalogs
        )
        controller, _backend, _live = self.controller()
        preloader = controller.chapter_voice_preloader
        self.snapshot = replace(
            self.snapshot, sources=(replace(source, library=copy(source.library)),)
        )
        with patch(
            "vntts.prepared_audio_discovery.ChapterVoicePreloader",
            side_effect=AssertionError("audio progress rebuilt recognition"),
        ):
            controller._refresh_prepared_audio(force=True)
        self.assertIs(controller.chapter_voice_preloader, preloader)

    def test_settings_reload_cannot_discard_unchanged_automatic_catalog(self):
        controller, _backend, _live = self.controller()
        preloader = controller.chapter_voice_preloader
        preloader.select_line_id("reverse1999:100630:6")
        controller.chapter_voice_preloader = ChapterVoicePreloader.load_optional(None)
        with patch(
            "vntts.prepared_audio_discovery.ChapterVoicePreloader",
            side_effect=AssertionError("recognition rebuilt"),
        ):
            controller._refresh_prepared_audio(force=True)
        self.assertIs(controller.chapter_voice_preloader, preloader)
        self.assertIsNone(preloader.current_match)
        self.assertTrue(
            preloader.line_for_id("reverse1999:100630:7").has_original_speech
        )

    def test_main_story_original_is_ineligible_for_synthesis_before_timing(self):
        line = self.snapshot.preloader.line_for_id("reverse1999:100630:7")
        self.assertTrue(line.has_original_speech)
        self.assertFalse(line.has_full_source_audio)
        duplicate = replace(
            line,
            line_id="unvoiced",
            chapter="elsewhere",
            source_audio_main_story=False,
            source_audio_status="absent",
        )
        preloader = ChapterVoicePreloader((line, duplicate))
        resolved, _match = preloader.resolve_complete_among(
            "Narrator",
            line.text,
            (row.line_id for row in preloader.dialogue),
            prefer_synthesis=True,
        )
        self.assertIs(resolved, duplicate)

    def test_identifying_unvoiced_line_requests_chapter_before_live_preparation(self):
        controller, backend, live = self.controller()
        harness = AutoAdvanceFakeFrameHarness()
        harness.reader.capture_future = Future()
        controller.live_reader = harness.reader
        with patch.object(controller.reading_content, "request_originals") as request:
            resolution = {}
            admitted = controller._resolve_story_ocr_result(
                OCRResult("Narrator", self.unvoiced, 95, "test", 1),
                resolution=resolution,
            )
            request.assert_called_once_with(
                self.snapshot.catalogs[0],
                "100630",
                harness.reader.stop_event,
                controller.status_handler,
            )
            live.prepare_playback.assert_not_called()
            route = backend.prepare_route(
                admitted.character, admitted.text, line_id=resolution["line"].line_id
            )
        self.assertIsInstance(route, LiveTTSRoute)

    def test_stop_cancels_content_preparation_before_a_reader_exists(self):
        controller, _backend, _live = self.controller()
        self.assertIsNone(controller.live_reader)
        with patch.object(controller.reading_content, "cancel_pending") as cancel:
            controller.emergency_stop()
        cancel.assert_called_once()

    def test_ordinary_read_falls_back_then_preserves_original_without_ready_pack(self):
        controller, backend, live = self.controller()
        catalog = self.snapshot.catalogs[0]
        original = catalog.by_line_id["reverse1999:100630:7"]
        measured = replace(
            original,
            source_audio_duration_seconds=2.0,
            source_audio_completeness="full",
            source_audio_authoritative=True,
        )
        with patch.object(
            controller.reading_content, "prepare_original", return_value=measured
        ) as timing:
            routes = []
            for ordinal, text in enumerate(
                (
                    self.unvoiced.replace("half-thoughts", "half- thoughts"),
                    self.original,
                ),
                1,
            ):
                resolution = {}
                admitted = controller._resolve_story_ocr_result(
                    OCRResult("Narrator", text, 95, "test", 1), resolution=resolution
                )
                line = resolution["line"]
                chunk = controller._accept_live_chunk(
                    SpeechChunk(
                        ordinal,
                        admitted.character,
                        admitted.text,
                        line_id=line.line_id,
                        first_observed_at=10.0,
                    )
                )
                routes.append(
                    backend.prepare_route(
                        chunk.character,
                        chunk.text,
                        line_id=chunk.line_id,
                        resolve_unidentified=False,
                        prepared_audio=chunk.prepared_audio,
                        first_observed_at=chunk.first_observed_at,
                    )
                )
        self.assertIsInstance(routes[0], LiveTTSRoute)
        self.assertIsInstance(routes[1], SourceAudioRoute)
        self.assertAlmostEqual(routes[1].prepared.completion_deadline, 12.35)
        timing.assert_called_once()
        live.prepare_playback.assert_called_once_with("Narrator", self.unvoiced)

    def test_failed_original_analysis_never_falls_through_to_live_synthesis(self):
        controller, backend, live = self.controller()
        original = controller.chapter_voice_preloader.line_for_id(
            "reverse1999:100630:7"
        )
        with patch.object(
            controller.reading_content,
            "prepare_original",
            side_effect=GameContentImportError("decoder unavailable"),
        ):
            with self.assertRaisesRegex(GameContentImportError, "decoder unavailable"):
                backend.prepare_route(
                    original.speaker, original.text, line_id=original.line_id
                )
        live.prepare_playback.assert_not_called()

    def test_live_only_policy_still_preserves_untimed_main_story_originals(self):
        controller, backend, live = self.controller(policy="live-tts-only")
        original = controller.chapter_voice_preloader.line_for_id(
            "reverse1999:100630:7"
        )
        measured = replace(
            original,
            source_audio_duration_seconds=2.0,
            source_audio_completeness="full",
            source_audio_authoritative=True,
        )
        with patch.object(
            controller.reading_content, "prepare_original", return_value=measured
        ):
            route = backend.prepare_route(
                original.speaker, original.text, line_id=original.line_id
            )
        self.assertIsInstance(route, SourceAudioRoute)
        self.assertIsNone(backend.library)
        live.prepare_playback.assert_not_called()

    def test_stopped_or_reconfigured_reading_cannot_use_completed_timing(self):
        controller, _backend, _live = self.controller()
        harness = AutoAdvanceFakeFrameHarness()
        controller.live_reader = harness.reader
        original = controller.chapter_voice_preloader.line_for_id(
            "reverse1999:100630:7"
        )
        for stop in (True, False):

            def complete(*args):
                if stop:
                    harness.reader.stop_event.set()
                else:
                    controller.settings = controller.settings.updated(
                        game_window_title="Other game"
                    )
                return original

            harness.reader.stop_event.clear()
            with (
                self.subTest(stop=stop),
                patch.object(
                    controller.reading_content, "prepare_original", side_effect=complete
                ),
            ):
                with self.assertRaises(GameContentImportCancelled):
                    controller._prepare_original_speech(original)
