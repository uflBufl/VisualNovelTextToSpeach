import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

from vntts_artifacts.story_index import write_story_index_document

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.content_revision import ContentRevision, source_signature
from vntts.game_content_importer import GameContentImportCancelled
from vntts.game_reading_content import GameReadingContent
from vntts.reading_catalog import ReadingCatalog


class GameReadingContentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "story-index.jsonl"
        self.metadata = {"game": "Reverse: 1999", "language": "en"}
        self.records = [
            {
                "record_type": "line",
                "line_id": f"original:{chapter}",
                "chapter": chapter,
                "sequence": 7,
                "speaker": "Narrator",
                "voice_character": "Narrator",
                "text": "Heinrich must be taken to the headquarters. He has vital information!",
                "collection_kind": "main_story",
                "source_audio_status": "available",
                "source_audio_id": f"voice:{chapter}",
                "source_media_ids": [7],
                "available_media_ids": [7],
                "speakable": True,
            }
            for chapter in ("1", "2")
        ]
        self.catalog = self.write_catalog()
        self.timed = {}
        for row in self.records:
            chapter = row["chapter"]
            path = self.root / chapter / "timed.jsonl"
            write_story_index_document(
                path,
                {
                    **self.metadata,
                    "source_audio_completion": "verified-media-duration-seconds",
                },
                [
                    {
                        **row,
                        "source_audio_duration_seconds": 1.25,
                        "source_audio_duration_media_id": 7,
                        "source_audio_duration_media_sha256": "a" * 64,
                        "source_audio_duration_sample_rate": 24000,
                        "source_audio_duration_sample_count": 30000,
                        "source_audio_duration_decoder": "test-decoder",
                    }
                ],
            )
            self.timed[chapter] = path
        self.provider = Mock()
        self.provider.prepare_reading_source_audio.side_effect = (
            lambda revision, chapter, *args, **kwargs: self.timed[chapter]
        )
        self.factory = Mock(return_value=self.provider)
        self.content = GameReadingContent(self.factory)
        self.addCleanup(self.content.wait)
        self.addCleanup(self.content.close)

    def write_catalog(self):
        document = write_story_index_document(self.source, self.metadata, self.records)
        signature = source_signature(self.source)
        payload = self.source.read_bytes()
        revision = ContentRevision.capture(
            self.source, payload, document, signature=signature
        )
        return ReadingCatalog(
            "reverse1999",
            "Reverse: 1999",
            revision,
            ChapterVoicePreloader.dialogue_from_story_index(self.source, document),
        )

    def request(self, chapter="1", parent=None):
        return self.content.request_originals(
            self.catalog, chapter, parent or Event(), lambda _message: None
        )

    def test_prefetch_returns_before_timing_and_original_awaits_same_work(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def prepare(revision, chapter, cancellation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.timed[chapter]

        self.provider.prepare_reading_source_audio.side_effect = prepare
        future = self.request()
        self.assertTrue(entered.wait(2))
        self.assertFalse(future.done())
        self.assertIs(self.request(), future)
        release.set()
        future.result(timeout=2)
        line = self.catalog.by_line_id["original:1"]
        measured = self.content.prepare_original(
            self.catalog, line, Event(), lambda _message: None
        )
        self.assertTrue(measured.has_full_source_audio)
        self.provider.prepare_reading_source_audio.assert_called_once()

    def test_completed_timing_survives_stop_and_provider_is_reused_for_next_chapter(
        self,
    ):
        parent = Event()
        first = self.request(parent=parent)
        first.result(timeout=2)
        parent.set()
        self.content.cancel_pending()
        self.assertIs(self.request(), first)
        self.request("2").result(timeout=2)
        self.factory.assert_called_once_with(self.catalog)
        self.assertEqual(self.provider.prepare_reading_source_audio.call_count, 2)

    def test_cancelled_pending_work_can_be_retried_in_new_reading_session(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def prepare(revision, chapter, cancellation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            if cancellation.is_set():
                raise GameContentImportCancelled("cancelled")
            return self.timed[chapter]

        self.provider.prepare_reading_source_audio.side_effect = prepare
        first = self.request()
        self.assertTrue(entered.wait(2))
        self.content.cancel_pending()
        second = self.request()
        self.assertIsNot(first, second)
        release.set()
        with self.assertRaises(GameContentImportCancelled):
            first.result(timeout=2)
        self.assertTrue(second.result(timeout=2).dialogue[0].has_full_source_audio)

    def test_replaced_catalog_drops_completed_revision_and_pending_old_work(self):
        first = self.request()
        first.result(timeout=2)
        replacement = self.write_catalog()
        self.content.retain_catalogs((replacement,))
        second = self.content.request_originals(
            replacement, "1", Event(), lambda _message: None
        )
        self.assertIsNot(first, second)
        second.result(timeout=2)
        self.assertIs(
            self.provider.prepare_reading_source_audio.call_args.args[0],
            replacement.revision,
        )

    def test_failed_route_retries_provider_without_ocr_prefetch_retry_loop(self):
        self.provider.prepare_reading_source_audio.side_effect = [
            TimeoutError("decoder timeout"),
            self.timed["1"],
        ]
        line = self.catalog.by_line_id["original:1"]
        with self.assertRaisesRegex(TimeoutError, "decoder timeout"):
            self.content.prepare_original(
                self.catalog, line, Event(), lambda _message: None
            )
        failed = self.request()
        self.assertIs(self.request(), failed)
        self.assertEqual(self.provider.prepare_reading_source_audio.call_count, 1)
        measured = self.content.prepare_original(
            self.catalog, line, Event(), lambda _message: None
        )
        self.assertTrue(measured.has_full_source_audio)

    def test_completed_prefetch_failure_is_retried_by_actual_route_request(self):
        self.provider.prepare_reading_source_audio.side_effect = [
            RuntimeError("worker failed"),
            self.timed["1"],
        ]
        failed = self.request()
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            failed.result(timeout=2)
        self.assertIs(self.request(), failed)
        line = self.catalog.by_line_id["original:1"]
        measured = self.content.prepare_original(
            self.catalog, line, Event(), lambda _message: None
        )
        self.assertTrue(measured.has_full_source_audio)
        self.assertEqual(self.provider.prepare_reading_source_audio.call_count, 2)

    def test_same_controller_can_prepare_content_after_shutdown_and_restart(self):
        from vntts.controller import AppController

        controller = AppController(reading_content=self.content)
        controller.shutdown()
        self.assertTrue(controller.shutdown_complete.is_set())
        controller.prepare_startup()
        self.request().result(timeout=2)
        controller.shutdown()

    def test_shutdown_drains_old_preparation_before_reopening_controller(self):
        from vntts.controller import AppController

        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def prepare(revision, chapter, cancellation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            if cancellation.is_set():
                raise GameContentImportCancelled("cancelled")
            return self.timed[chapter]

        self.provider.prepare_reading_source_audio.side_effect = prepare
        self.request()
        self.assertTrue(entered.wait(2))
        controller = AppController(reading_content=self.content)
        controller.shutdown()
        self.assertFalse(controller.shutdown_complete.is_set())
        controller.prepare_startup()
        self.assertTrue(controller.shutdown_requested.is_set())
        release.set()
        self.assertTrue(controller.shutdown_complete.wait(2))
        controller.prepare_startup()
        self.request().result(timeout=2)
        controller.shutdown()

    def test_close_cancels_running_and_queued_work_and_rejects_new_requests(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def prepare(revision, chapter, cancellation, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            if cancellation.is_set():
                raise GameContentImportCancelled("cancelled")
            return self.timed[chapter]

        self.provider.prepare_reading_source_audio.side_effect = prepare
        first = self.request()
        self.assertTrue(entered.wait(2))
        queued = self.request("2")
        self.content.close()
        release.set()
        with self.assertRaises(GameContentImportCancelled):
            first.result(timeout=2)
        self.assertTrue(queued.cancelled())
        with self.assertRaises(GameContentImportCancelled):
            self.request()

    def test_cancelled_request_never_constructs_a_provider(self):
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(GameContentImportCancelled):
            self.request(parent=cancelled)
        self.factory.assert_not_called()
