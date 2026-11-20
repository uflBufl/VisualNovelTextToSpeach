import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.dialog_capture import CapturedDialogFrame
from vntts.live import RecognizedDialog
from vntts.live_sequence import (
    LiveSequenceChapter,
    LiveSequenceEvent,
    LiveSequencePlan,
    StoryCursor,
)
from vntts.live_snapshot import read_live_snapshot
from vntts.ocr import OCRResult
from vntts.settings import AppSettings
from vntts.voices import CharacterVoiceRegistry


class StoryRecognitionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.previous = self.line(
            "reverse1999:100630:11",
            "Narrator",
            "Gun shot. Isolde, Kakania. Why are they here?",
            sequence=11,
        )
        self.target = replace(
            self.line(
                "reverse1999:100630:13",
                "Heinrich",
                "Ahh! Tosca ...! You're turning your dagger at me?!",
                sequence=13,
            ),
            source_audio_status="available",
            source_audio_id="originalHeinrich13",
            source_audio_duration_seconds=4.0,
            source_audio_completeness="full",
            source_audio_authoritative=True,
        )
        self.foreign = self.line(
            "other:1", "Other", "The other chapter begins here.", chapter="other"
        )

    def line(self, line_id, speaker, text, *, sequence=1, chapter="100630"):
        return ChapterDialogue(
            line_id, chapter, sequence, speaker, text, text_sha256(text)
        )

    def controller(self, *, sequence=False):
        controller = AppController(
            AppSettings(capture_mode="window", live_sequence_mode="off"),
            chapter_voice_preloader=ChapterVoicePreloader(
                (self.previous, self.target, self.foreign)
            ),
            status_handler=Mock(),
            dialog_handler=Mock(),
        )
        controller.automatic_prepared_reading = True
        controller.live_reader = AutoAdvanceFakeFrameHarness().reader
        controller._request_original_speech = Mock()
        controller._prime_observed_voice = Mock()
        controller._prime_likely_chapter_voice = Mock()
        controller._offer_unknown_speaker_mapping = Mock(return_value=False)
        controller.chapter_voice_preloader.select_line_id(self.previous.line_id)
        if sequence:
            controller.settings = controller.settings.updated(
                live_sequence_mode="audio-manual"
            )
            events = {
                "previous": LiveSequenceEvent(
                    "previous",
                    self.previous.chapter,
                    11,
                    "speech",
                    "automatic",
                    ("target",),
                    self.previous.line_id,
                ),
                "target": LiveSequenceEvent(
                    "target",
                    self.target.chapter,
                    13,
                    "speech",
                    "terminal",
                    (),
                    self.target.line_id,
                ),
            }
            controller.live_sequence_plan = LiveSequencePlan(
                path=Path("plan.json"),
                game_id="synthetic",
                producer_name="fixture",
                producer_version="1",
                story_index_path=Path("story-index.jsonl"),
                story_index_sha256="0" * 64,
                source_extract_sha256="0" * 64,
                chapters=(LiveSequenceChapter("100630", ("previous",), tuple(events)),),
                events=events,
                event_id_by_line_id={
                    event.line_id: event.event_id for event in events.values()
                },
            )
            controller.story_cursor = StoryCursor(controller.live_sequence_plan)
            controller.story_cursor.anchor_event("previous")
        return controller

    def raw(self, text=None):
        return OCRResult(
            self.target.speaker,
            self.target.text if text is None else text,
            95,
            "fixture",
            1,
            speaker_confidence=95,
            dialogue_confidence=95,
        )

    @contextmanager
    def held_matching(self, method, operation):
        entered, release, finished = Event(), Event(), Event()
        result, errors = [], []
        original = getattr(ChapterVoicePreloader, method)

        def match(preloader, *args, **kwargs):
            entered.set()
            release.wait()
            return original(preloader, *args, **kwargs)

        def run():
            try:
                result.append(operation())
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        with patch.object(ChapterVoicePreloader, method, match):
            worker = Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "Matching did not reach the gate")
                self.assertFalse(finished.is_set())
                yield result
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive(), "Matching did not finish after release")
        if errors:
            raise errors[0]

    def promptly(self, operation):
        finished, errors = Event(), []

        def run():
            try:
                operation()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        worker = Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(
            finished.wait(2), "UI control waited for the held matching calculation"
        )
        worker.join(2)
        if errors:
            raise errors[0]

    def test_heinrich_prefix_stays_in_known_chapter_until_complete_original(self):
        controller = self.controller()
        controller.live_reader._fully_observed_dialog = RecognizedDialog(
            self.previous.speaker,
            self.previous.text,
            self.previous.line_id,
            exact_match=True,
        )
        self.assertEqual(controller._fully_observed_line_id(), self.previous.line_id)
        preloader = controller.chapter_voice_preloader
        scopes = []
        original = ChapterVoicePreloader.resolve_complete_among

        def resolve(owner, speaker, text, line_ids, **kwargs):
            line_ids = tuple(line_ids)
            scopes.append(line_ids)
            return original(owner, speaker, text, line_ids, **kwargs)

        with patch.object(ChapterVoicePreloader, "resolve_complete_among", resolve):
            for text in ("Ahh! To", "Ahh! Tosca", "Ahh! Tosca ...! You're turning"):
                with self.subTest(text=text):
                    raw = self.raw(text)
                    resolution = controller._resolve_story_ocr_result(raw)
                    self.assertIs(resolution.ocr_result, raw)
                    self.assertEqual(resolution.match_result, "expected-incomplete")
                    self.assertIsNone(resolution.line)
                    self.assertEqual(preloader.current_match.sequence, 11)
            resolution = controller._resolve_story_ocr_result(self.raw())

        self.assertTrue(scopes)
        self.assertTrue(all(self.foreign.line_id not in scope for scope in scopes))
        self.assertIs(resolution.line, self.target)
        self.assertTrue(resolution.line.has_full_source_audio)
        self.assertEqual(preloader.current_match.sequence, self.target.sequence)
        controller._request_original_speech.assert_called_once_with(self.target)

    def test_genuine_chapter_miss_still_discovers_another_chapter(self):
        controller = self.controller()
        resolution = controller._resolve_story_ocr_result(
            OCRResult("Other", self.foreign.text, 95, "fixture", 1),
        )
        self.assertIs(resolution.line, self.foreign)
        self.assertEqual(
            controller.chapter_voice_preloader.current_match.chapter, "other"
        )

    def test_status_and_stop_finish_while_ordinary_or_sequence_matching_is_held(self):
        for path in ("ordinary", "sequence"):
            with self.subTest(path=path):
                controller = self.controller(sequence=path == "sequence")
                if path == "ordinary":
                    method = "resolve_complete_among"

                    def operation():
                        return controller._resolve_story_ocr_result(self.raw())
                else:
                    method = "resolve_observation_among"

                    def operation():
                        return controller._dialog_observed(
                            self.target.speaker, self.target.text
                        )

                with self.held_matching(method, operation) as results:
                    before = controller.chapter_voice_preloader.current_match
                    self.promptly(controller.get_live_sequence_status)
                    self.promptly(controller.stop_live)
                    self.assertEqual(
                        controller.chapter_voice_preloader.current_match, before
                    )
                self.assertEqual(
                    controller.chapter_voice_preloader.current_match, before
                )
                controller._request_original_speech.assert_not_called()
                if path == "ordinary":
                    self.assertEqual(results[0].match_result, "stale-observation")
                    self.assertIsNone(results[0].line)
                else:
                    self.assertFalse(results[0])
                    self.assertEqual(
                        controller.story_cursor.current_event_id, "previous"
                    )

    def test_changed_reading_owner_rejects_late_ocr_without_context_or_diagnostics(
        self,
    ):
        for change in ("restart", "preloader", "settings", "chapter", "resync"):
            with self.subTest(change=change):
                controller = self.controller(sequence=change == "resync")
                owner = controller.chapter_voice_preloader
                owner.last_resolution_diagnostics = {"owner": "before"}
                with self.held_matching(
                    "resolve_complete_among",
                    lambda: controller._resolve_story_ocr_result(self.raw()),
                ) as results:

                    def change_owner():
                        with controller.story_cursor_lock:
                            if change == "restart":
                                controller.live_reader.run_id = "restarted-reading"
                                controller.live_reader.stop_event = Event()
                            elif change == "preloader":
                                controller.chapter_voice_preloader = (
                                    ChapterVoicePreloader((self.foreign,))
                                )
                            elif change == "settings":
                                controller.settings = controller.settings.updated(
                                    ocr_language="ru"
                                )
                            elif change == "chapter":
                                owner.select_line_id(self.foreign.line_id)
                            else:
                                self.assertTrue(
                                    controller.resync_live_sequence("previous")
                                )

                    self.promptly(change_owner)
                    selected = controller.chapter_voice_preloader.current_match
                    occurrence = (
                        controller.story_cursor.occurrence_id
                        if controller.story_cursor is not None
                        else None
                    )
                    self.assertEqual(
                        owner.last_resolution_diagnostics, {"owner": "before"}
                    )
                self.assertEqual(results[0].match_result, "stale-observation")
                self.assertIsNone(results[0].line)
                self.assertEqual(
                    controller.chapter_voice_preloader.current_match, selected
                )
                self.assertEqual(owner.last_resolution_diagnostics, {"owner": "before"})
                if occurrence is not None:
                    self.assertEqual(controller.story_cursor.occurrence_id, occurrence)
                controller._request_original_speech.assert_not_called()

    def test_sequence_resync_rejects_late_observation_of_the_next_event(self):
        controller = self.controller(sequence=True)
        with self.held_matching(
            "resolve_observation_among",
            lambda: controller._dialog_observed(self.target.speaker, self.target.text),
        ) as results:
            self.promptly(
                lambda: self.assertTrue(controller.resync_live_sequence("previous"))
            )
            occurrence = controller.story_cursor.occurrence_id
        self.assertFalse(results[0])
        self.assertEqual(controller.story_cursor.current_event_id, "previous")
        self.assertEqual(controller.story_cursor.occurrence_id, occurrence)
        self.assertEqual(controller.chapter_voice_preloader.current_match.sequence, 11)

    def test_stop_or_resync_during_name_resolution_cannot_restart_sequence_matching(
        self,
    ):
        for change in ("stop", "resync"):
            with self.subTest(change=change):
                controller = self.controller(sequence=True)
                before = controller.chapter_voice_preloader.current_match
                with self.held_matching(
                    "resolve_unique_prefix_by_text",
                    lambda: controller._dialog_observed(
                        self.target.speaker, self.target.text
                    ),
                ) as results:
                    if change == "stop":
                        self.promptly(controller.stop_live)
                    else:
                        self.promptly(
                            lambda: self.assertTrue(
                                controller.resync_live_sequence("previous")
                            )
                        )
                    occurrence = controller.story_cursor.occurrence_id
                    self.assertEqual(
                        controller.chapter_voice_preloader.current_match, before
                    )
                self.assertFalse(results[0])
                self.assertEqual(controller.story_cursor.current_event_id, "previous")
                self.assertEqual(controller.story_cursor.occurrence_id, occurrence)
                self.assertEqual(
                    controller.chapter_voice_preloader.current_match, before
                )
                controller._request_original_speech.assert_not_called()

    def test_stale_frame_resolution_cannot_start_fresh_name_resolution(self):
        for change in ("stop", "resync"):
            with self.subTest(change=change):
                controller = self.controller(sequence=change == "resync")
                controller.voice_router = SimpleNamespace(
                    registry=CharacterVoiceRegistry()
                )
                controller._publish_diagnostic = Mock()
                controller._resolve_voice_label = Mock(return_value="Fixture voice")
                controller.uncertain_frame_recorder = None
                before = controller.chapter_voice_preloader.current_match
                frame = CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
                with patch(
                    "vntts.dialog_capture.recognize_dialog_image_result",
                    return_value=self.raw(),
                ):
                    with self.held_matching(
                        "resolve_complete_among",
                        lambda: controller._recognize_live_frame(frame),
                    ) as results:
                        if change == "stop":
                            self.promptly(controller.stop_live)
                        else:
                            self.promptly(
                                lambda: self.assertTrue(
                                    controller.resync_live_sequence("previous")
                                )
                            )
                        occurrence = (
                            controller.story_cursor.occurrence_id
                            if controller.story_cursor is not None
                            else None
                        )
                        self.assertEqual(
                            controller.chapter_voice_preloader.current_match, before
                        )
                self.assertNotIsInstance(results[0], RecognizedDialog)
                self.assertEqual(
                    controller.chapter_voice_preloader.current_match, before
                )
                if occurrence is not None:
                    self.assertEqual(
                        controller.story_cursor.current_event_id, "previous"
                    )
                    self.assertEqual(controller.story_cursor.occurrence_id, occurrence)
                controller._request_original_speech.assert_not_called()

    def test_scope_bootstrap_matching_releases_status_and_stop_lock(self):
        controller = self.controller()
        controller.voice_router = Mock()
        controller.capture_target = Mock(
            last_window=SimpleNamespace(handle=42, process_id=7)
        )
        controller.capture_target.is_focused.return_value = True
        with patch(
            "vntts.controller_components.read_live_snapshot",
            return_value=(self.target.speaker, self.target.text),
        ):
            with self.held_matching(
                "resolve_observation_among", controller.identify_live_scope
            ) as results:
                self.promptly(controller.get_live_sequence_status)
                self.promptly(controller.stop_live)
        self.assertFalse(results[0])
        self.assertEqual(controller.chapter_voice_preloader.current_match.sequence, 11)
        controller.dialog_handler.assert_not_called()

    def test_fresh_scope_bootstrap_can_match_after_reader_was_stopped(self):
        controller = self.controller()
        controller.live_reader.stop_event.set()
        controller.voice_router = Mock()
        controller.capture_target = Mock(
            last_window=SimpleNamespace(handle=42, process_id=7)
        )
        controller.capture_target.is_focused.return_value = True
        with patch(
            "vntts.controller_components.read_live_snapshot",
            return_value=(self.target.speaker, self.target.text),
        ):
            self.assertTrue(controller.identify_live_scope())
        controller.dialog_handler.assert_called_once_with(
            self.target.speaker, self.target.text
        )
        self.assertEqual(controller.chapter_voice_preloader.current_match.sequence, 13)

    def test_scope_bootstrap_cannot_rebind_stale_ocr_after_stop_or_resync(self):
        for change in ("stop", "resync"):
            with self.subTest(change=change):
                controller = self.controller(sequence=True)
                controller.voice_router = Mock()
                controller.capture_target = Mock(
                    last_window=SimpleNamespace(handle=42, process_id=7)
                )
                controller.capture_target.is_focused.return_value = True
                controller.uncertain_frame_recorder = None
                preloader = controller.chapter_voice_preloader
                preloader.last_resolution_diagnostics = {"owner": "before"}
                before = preloader.current_match

                def analyze(*_args, result_resolver, **_kwargs):
                    return Mock(), None, result_resolver(self.raw())

                with (
                    patch(
                        "vntts.controller_components.read_live_snapshot",
                        side_effect=read_live_snapshot,
                    ),
                    patch(
                        "vntts.live_snapshot.analyze_dialog_snapshot",
                        side_effect=analyze,
                    ),
                ):
                    with self.held_matching(
                        "resolve_complete_among", controller.identify_live_scope
                    ) as results:
                        if change == "stop":
                            self.promptly(controller.stop_live)
                        else:
                            self.promptly(
                                lambda: self.assertTrue(
                                    controller.resync_live_sequence("previous")
                                )
                            )
                        controller.dialog_handler.reset_mock()
                        occurrence = controller.story_cursor.occurrence_id
                        self.assertEqual(preloader.current_match, before)
                self.assertFalse(results[0])
                self.assertEqual(preloader.current_match, before)
                self.assertEqual(
                    preloader.last_resolution_diagnostics, {"owner": "before"}
                )
                self.assertEqual(controller.story_cursor.current_event_id, "previous")
                self.assertEqual(controller.story_cursor.occurrence_id, occurrence)
                controller.dialog_handler.assert_not_called()
                controller._request_original_speech.assert_not_called()
