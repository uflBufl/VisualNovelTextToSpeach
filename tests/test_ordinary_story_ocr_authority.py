import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from tests import test_story_ocr_authority as authority
from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialog_capture import CapturedDialogFrame, OCRUncertainError
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioRoute,
    PreparedGeneratedAudio,
)
from vntts.live import RecognizedDialog


class OrdinaryStoryOCRAuthorityTest(unittest.TestCase):
    def ordinary(self, rows, *, owner=None, prepared=None):
        controller = authority.StoryOCRAuthorityTest().controller(rows)
        controller.settings = controller.settings.updated(live_sequence_mode="off")
        controller.automatic_prepared_reading = True
        harness = AutoAdvanceFakeFrameHarness(
            tracker_options={"complete_dialogue_only": True}
        )
        controller.live_reader = harness.reader
        entries = {
            (line.line_id, line.text_sha256): PreparedGeneratedAudio(
                line.line_id, line.text_sha256, np.zeros(1, dtype=np.float32), 24_000
            )
            for line in (rows if prepared is None else prepared)
        }
        library = Mock(runtime_progress=False)
        library.has_entry.side_effect = lambda line_id, digest: (
            (line_id, digest) in entries
        )
        library.find_audio_event_omission.return_value = None
        library.find_with_preflight.side_effect = lambda line_id, digest: (
            entries.get((line_id, digest)),
            "generated-audio-entry-ready",
        )
        controller.speech_backend = GeneratedAudioFallbackBackend(
            None, library, controller.chapter_voice_preloader, audio_output=Mock()
        )
        if owner is not None:
            self.assertTrue(
                harness.reader.enqueue(owner.speaker, owner.text, line_id=owner.line_id)
            )
        return controller, harness, library

    def raw(self, line):
        return replace(
            authority.observed(line.text, line.speaker, 52.5), speaker_confidence=95
        )

    def recognize(self, controller, raw):
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=raw
        ):
            return controller._recognize_live_frame(
                CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
            )

    def test_first_low_confidence_named_line_uses_metadata_before_preparation(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        controller, _, library = self.ordinary((previous, line), owner=previous)
        raw = replace(self.raw(line), text="Ｅｕｒｕｓ  ...\nEurus ...")
        resolved = controller._resolve_story_ocr_result(raw)
        self.assertEqual((resolved.character, resolved.text), (line.speaker, line.text))
        self.assertTrue(resolved.source_text_confirmed)
        self.assertEqual(resolved.confidence, 52.5)
        self.assertEqual(resolved.dialogue_confidence, 52.5)
        self.assertEqual(resolved.speaker_confidence, 95)
        self.assertFalse(resolved.is_confident())
        self.assertTrue(resolved.is_readable())
        library.has_entry.assert_any_call(line.line_id, line.text_sha256)
        library.find_with_preflight.assert_not_called()
        backend = controller.speech_backend
        self.assertIsNone(
            backend.generated_reservations.get((line.line_id, line.text_sha256))
        )
        actual = self.recognize(controller, raw)
        self.assertIsInstance(actual, RecognizedDialog)
        self.assertEqual(actual.line_id, line.line_id)
        route = backend.prepare_route(
            actual.character,
            actual.text,
            line_id=actual.line_id,
            resolve_unidentified=False,
        )
        self.assertIsInstance(route, GeneratedAudioRoute)
        self.assertEqual(route.prepared.line_id, line.line_id)
        library.find_with_preflight.assert_called_once_with(
            line.line_id, line.text_sha256
        )
        self.assertEqual(
            controller._publish_diagnostic.call_args.args[0].confidence, 52.5
        )
        controller._ocr_uncertain.assert_not_called()

    def test_ordinary_first_capture_reaches_prepared_playback_and_advances_once(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        skipped = replace(
            authority.story_line("A skipped line.", "Elma", "skipped"), sequence=2
        )
        line = replace(authority.story_line("Eurus ... Eurus ...", "Elma"), sequence=3)
        following = replace(
            authority.story_line("The next complete line.", "Elma", "next"), sequence=4
        )
        for app_advance in (True, False):
            with self.subTest(app_advance=app_advance):
                controller, harness, library = self.ordinary(
                    (previous, skipped, line, following)
                )
                if not app_advance:
                    harness.reader.auto_advance = None

                def recognize(frame):
                    raw = replace(
                        authority.observed(
                            frame["text"],
                            frame["character"],
                            94.9 if frame["text"] == previous.text else 52.5,
                        ),
                        speaker_confidence=95,
                    )
                    return self.recognize(controller, raw)

                harness.reader.recognize_frame = recognize
                harness.reader.prepare_chunk = lambda chunk: (
                    controller.speech_backend.prepare_route(
                        chunk.character,
                        chunk.text,
                        line_id=chunk.line_id,
                        resolve_unidentified=False,
                    )
                )
                with patch(
                    "vntts.live.Timer", side_effect=harness.timer_scheduler.create
                ):
                    harness.start()
                    try:
                        for index, current in enumerate((previous, line, following)):
                            for _ in range(3):
                                harness.clock.advance(0.2)
                                harness.push(
                                    current.speaker,
                                    current.text,
                                    background=current.line_id,
                                    complete=True,
                                )
                            self.assertEqual(len(harness.speech_executor.jobs), 1)
                            harness.finish_playback()
                            chunk, route = harness.played[-1]
                            self.assertEqual(chunk.line_id, current.line_id)
                            self.assertIsInstance(route, GeneratedAudioRoute)
                            self.assertEqual(route.prepared.line_id, current.line_id)
                            harness.push(
                                current.speaker,
                                current.text,
                                background="repeated-after-playback",
                                complete=True,
                            )
                            self.assertEqual(harness.speech_executor.jobs, [])
                            self.assertEqual(len(harness.played), index + 1)
                            if app_advance:
                                harness.fire_advance()
                                self.assertEqual(len(harness.advance_calls), index + 1)
                            else:
                                self.assertEqual(harness.advance_calls, [])
                        self.assertEqual(harness.errors, [])
                        self.assertEqual(
                            [
                                call.args[0]
                                for call in library.find_with_preflight.call_args_list
                            ],
                            [previous.line_id, line.line_id, following.line_id],
                        )
                    finally:
                        harness.stop()

    def test_new_line_requires_explicit_reliable_matching_name_and_complete_text(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        raw = self.raw(line)
        for rejected in (
            replace(raw, character="Hedone"),
            replace(raw, character="El ma"),
            replace(raw, speaker_confidence=26),
            replace(raw, speaker_confidence=None),
            replace(raw, character=""),
            replace(raw, character="Narrator"),
            replace(raw, speaker_note="Name OCR noise ignored"),
            replace(raw, text="Eurus ..."),
            replace(raw, text="Eurus ... Eurus"),
            replace(raw, text="Eurus, Eurus."),
            replace(raw, text="Evrus ... Eurus ..."),
            replace(raw, text=line.text + " dust"),
        ):
            with self.subTest(raw=rejected):
                controller, _, library = self.ordinary((previous, line), owner=previous)
                self.assertIs(controller._resolve_story_ocr_result(rejected), rejected)
                self.assertFalse(rejected.source_text_confirmed)
                with self.assertRaises(OCRUncertainError):
                    self.recognize(controller, rejected)
                library.find_with_preflight.assert_not_called()

    def test_overall_confidence_is_not_independent_speaker_evidence(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        controller, _, library = self.ordinary((previous, line), owner=previous)
        raw = replace(self.raw(line), confidence=95, speaker_confidence=None)
        resolution = {}
        self.assertIs(
            controller._resolve_story_ocr_result(raw, resolution=resolution), raw
        )
        self.assertFalse(raw.source_text_confirmed)
        self.assertIsNone(resolution.get("line"))
        library.has_entry.assert_not_called()
        library.find_with_preflight.assert_not_called()

    def test_reliable_name_cannot_confirm_another_speakers_current_text(self):
        previous = authority.story_line("Yes.", "Hedone", "previous")
        line = authority.story_line("Yes.", "Elma")
        controller, _, _ = self.ordinary((previous, line), owner=previous)
        actual = self.recognize(controller, self.raw(line))
        self.assertEqual(
            (actual.character, actual.line_id), (line.speaker, line.line_id)
        )
        for prepared, speaker in (
            ((previous,), line.speaker),
            ((previous, line), "Other"),
        ):
            with self.subTest(prepared=prepared, speaker=speaker):
                controller, _, library = self.ordinary(
                    (previous, line), owner=previous, prepared=prepared
                )
                raw = replace(self.raw(line), character=speaker)
                resolution = {}
                self.assertIs(
                    controller._resolve_story_ocr_result(raw, resolution=resolution),
                    raw,
                )
                self.assertFalse(raw.source_text_confirmed)
                self.assertIsNone(resolution.get("line"))
                self.assertNotEqual(
                    resolution.get("match_result"), "expected-text-only"
                )
                with self.assertRaises(OCRUncertainError):
                    self.recognize(controller, raw)
                library.find_with_preflight.assert_not_called()

    def test_chapter_ambiguity_is_not_hidden_by_prepared_membership(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        for competitor in (
            replace(line, line_id="duplicate"),
            authority.story_line(line.text + " More words.", "Elma", "continuation"),
        ):
            with self.subTest(competitor=competitor):
                controller, _, library = self.ordinary(
                    (previous, line, competitor), owner=previous, prepared=(line,)
                )
                raw = self.raw(line)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                library.find_with_preflight.assert_not_called()

    def test_owner_chapter_overrides_guessed_match_but_never_allows_cross_chapter(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        foreign = replace(line, line_id="foreign", chapter="another-chapter")
        controller, _, _ = self.ordinary((previous, line, foreign), owner=previous)
        controller.chapter_voice_preloader.select_line_id(foreign.line_id)
        actual = self.recognize(controller, self.raw(line))
        self.assertEqual(actual.line_id, line.line_id)
        controller, _, _ = self.ordinary((previous, foreign), owner=previous)
        controller.chapter_voice_preloader.select_line_id(foreign.line_id)
        raw = self.raw(foreign)
        self.assertIs(controller._resolve_story_ocr_result(raw), raw)

    def test_new_line_requires_current_owner_matching_unchanged_source(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        for state in (
            "no-owner",
            "old-run",
            "old-generation",
            "cancelled",
            "paused",
            "stopped",
            "changed-source-text",
            "changed-source-speaker",
            "missing-source",
        ):
            with self.subTest(state=state):
                controller, harness, library = self.ordinary(
                    (previous, line), owner=None if state == "no-owner" else previous
                )
                reader = harness.reader
                if state == "no-owner":
                    controller.chapter_voice_preloader.select_line_id(line.line_id)
                elif state == "old-run":
                    reader.run_id = "different-run"
                elif state == "old-generation":
                    reader._set_generation(reader.active_generation + 1)
                elif state == "cancelled":
                    reader.cancelled_chunk_ids.update(reader.accepted_chunks)
                elif state == "paused":
                    reader.toggle_pause()
                elif state == "stopped":
                    reader.stop_event.set()
                elif state.startswith("changed-source"):
                    replacement = (
                        replace(previous, text="Changed canonical text.")
                        if state == "changed-source-text"
                        else replace(previous, speaker="Another speaker")
                    )
                    controller.chapter_voice_preloader = ChapterVoicePreloader(
                        (replacement, line)
                    )
                    controller.speech_backend.line_resolver = (
                        controller.chapter_voice_preloader
                    )
                elif state == "missing-source":
                    controller.chapter_voice_preloader = ChapterVoicePreloader((line,))
                    controller.speech_backend.line_resolver = (
                        controller.chapter_voice_preloader
                    )
                raw = self.raw(line)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                library.find_with_preflight.assert_not_called()

    def test_new_line_requires_automatic_reading_and_prepared_backend_membership(self):
        previous = authority.story_line(
            "The previous complete line.", line_id="previous"
        )
        line = authority.story_line("Eurus ... Eurus ...", "Elma")
        for state in (
            "disabled",
            "unprepared",
            "missing-hash",
            "stale-hash",
            "no-backend",
            "live-only",
            "no-library",
            "altered-speed",
        ):
            with self.subTest(state=state):
                candidate = (
                    replace(line, text_sha256=None) if state == "missing-hash" else line
                )
                controller, _, library = self.ordinary(
                    (previous, candidate),
                    owner=previous,
                    prepared=() if state == "unprepared" else (line,),
                )
                if state == "disabled":
                    controller.automatic_prepared_reading = False
                elif state == "stale-hash":
                    library.has_entry.side_effect = lambda line_id, digest: (
                        (line_id, digest) == (line.line_id, "old-digest")
                    )
                elif state == "no-backend":
                    controller.speech_backend = None
                elif state == "live-only":
                    controller.speech_backend = Mock()
                elif state == "no-library":
                    controller.speech_backend.library = None
                elif state == "altered-speed":
                    controller.speech_backend.speed = 1.2
                raw = self.raw(line)
                self.assertIs(controller._resolve_story_ocr_result(raw), raw)
                library.find_with_preflight.assert_not_called()
