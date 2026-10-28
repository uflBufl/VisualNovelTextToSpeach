import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from tests.test_live import AutoAdvanceFakeFrameHarness
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController, SequenceEventLease
from vntts.dialog_capture import CapturedDialogFrame
from vntts.generated_audio import AudioRouteTrace, SourceAudioRoute
from vntts.live import LiveDialogReader, RecognizedDialog, SpeechChunk
from vntts.live_sequence import (
    LiveSequenceChapter,
    LiveSequenceEvent,
    LiveSequencePlan,
    StoryCursorState,
)
from vntts.ocr import OCRResult
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback
from vntts.settings import AppSettings
from vntts.voices import CharacterVoiceRegistry


class ControllerTransitionObservationTest(unittest.TestCase):
    def controller(self, texts=None):
        texts = texts or (
            "The train arrives before sunrise.",
            "Birds gather around the fountain.",
            "Our supplies are hidden in the cellar.",
            "Nobody has opened this gate for years.",
            "A stranger finally answers the telephone.",
        )
        lines = tuple(
            ChapterDialogue(
                f"line-{index}", "chapter", index, "Ada", text, text_sha256(text)
            )
            for index, text in enumerate(texts, 1)
        )
        events = {
            f"event-{index}": LiveSequenceEvent(
                f"event-{index}",
                "chapter",
                index,
                "speech",
                "automatic" if index < len(lines) else "terminal",
                (f"event-{index + 1}",) if index < len(lines) else (),
                line.line_id,
            )
            for index, line in enumerate(lines, 1)
        }
        plan = LiveSequencePlan(
            Path("plan.json"),
            "synthetic",
            "test",
            "en",
            Path("story.jsonl"),
            "1" * 64,
            "2" * 64,
            (LiveSequenceChapter("chapter", ("event-1",), tuple(events)),),
            events,
            {line.line_id: f"event-{index}" for index, line in enumerate(lines, 1)},
        )
        controller = AppController(
            AppSettings(
                story_index="story.jsonl",
                live_sequence_plan="plan.json",
                live_sequence_mode="audio-auto",
            ),
            chapter_voice_preloader=ChapterVoicePreloader(lines),
            live_sequence_plan_factory=Mock(return_value=plan),
        )
        controller._prime_observed_voice = Mock()
        controller._prime_likely_chapter_voice = Mock()
        controller._offer_unknown_speaker_mapping = Mock(return_value=False)
        return controller, lines

    def accept(self, controller, line, generation=1):
        self.assertEqual(
            controller._dialog_observed(line.speaker, line.text),
            (line.speaker, line.text),
        )
        chunk = controller._accept_live_chunk(
            SpeechChunk(generation, line.speaker, line.text, line_id=line.line_id)
        )
        self.assertIsNotNone(chunk)
        return chunk

    def test_visible_later_line_reanchors_while_older_accepted_audio_plays(self):
        controller, lines = self.controller()
        first = self.accept(controller, lines[0])
        first_lease = controller._begin_sequence_playback(first)

        last = self.accept(controller, lines[-1], 2)
        visible = controller.story_cursor.snapshot()
        self.assertEqual(visible.current_event_id, "event-5")
        self.assertEqual(visible.reason, "observation-chapter-exact")
        self.assertFalse(
            controller._finish_sequence_playback(
                first_lease,
                PlaybackOutcome(PlaybackStatus.COMPLETED, 1.0, audio_source="live"),
            )
        )
        self.assertEqual(controller.story_cursor.snapshot(), visible)
        self.assertEqual(controller.sequence_event_terminal_routes[first_lease], "live")
        self.assertFalse(controller.story_cursor.can_auto_advance)
        self.assertIsNotNone(controller._begin_sequence_playback(last))

    def test_queued_accepted_line_can_start_after_visible_cursor_moves(self):
        controller, lines = self.controller()
        queued = self.accept(controller, lines[0])
        self.accept(controller, lines[-1], 2)
        visible = controller.story_cursor.snapshot()

        self.assertEqual(
            controller._begin_sequence_playback(queued),
            SequenceEventLease(queued.sequence_event_id, queued.sequence_occurrence_id),
        )
        self.assertEqual(controller.story_cursor.snapshot(), visible)

    def test_repeated_occurrence_is_admitted_once_and_pause_resume_keeps_lease(self):
        controller, lines = self.controller()
        chunk = self.accept(controller, lines[0])
        self.assertIsNone(
            controller._accept_live_chunk(
                SpeechChunk(2, chunk.character, chunk.text, line_id=chunk.line_id)
            )
        )
        lease = controller._begin_sequence_playback(chunk)
        self.accept(controller, lines[-1], 3)
        visible = controller.story_cursor.snapshot()
        self.assertFalse(
            controller._finish_sequence_playback(
                lease,
                PlaybackOutcome(PlaybackStatus.INTERRUPTED, 1.0, first_audio_ms=0),
                interrupted_for_pause=True,
            )
        )
        self.assertNotIn(lease, controller.sequence_event_terminal_routes)
        self.assertEqual(controller._begin_sequence_playback(chunk), lease)
        self.assertEqual(controller.story_cursor.snapshot(), visible)

    def test_clear_during_admission_releases_rejected_occurrence_for_explicit_retry(
        self,
    ):
        controller, lines = self.controller()
        line = lines[0]
        controller._dialog_observed(line.speaker, line.text)
        reader = LiveDialogReader(
            capture_executor=Mock(),
            ocr_executor=Mock(),
            speech_executor=Mock(),
            playback_executor=Mock(),
            capture_frame=Mock(),
            recognize_frame=Mock(),
            prepare_chunk=Mock(),
            play_prepared=Mock(),
            report_error=Mock(),
            accept_chunk=controller._accept_live_chunk,
            reject_chunk=controller._reject_live_chunk,
        )
        controller.live_reader = reader

        def clear_during_accept(chunk):
            accepted = controller._accept_live_chunk(chunk)
            reader.clear_queue()
            return accepted

        reader.accept_chunk = clear_during_accept
        self.assertIsNone(
            reader._admit_chunk(
                SpeechChunk(0, line.speaker, line.text, line_id=line.line_id)
            )
        )
        self.assertEqual(controller.sequence_admitted_chunks, {})
        self.assertEqual(reader.accepted_chunks, {})

        reader.accept_chunk = controller._accept_live_chunk
        self.assertTrue(reader.enqueue(line.speaker, line.text, line_id=line.line_id))

        self.assertEqual(len(controller.sequence_admitted_chunks), 1)
        self.assertEqual(len(reader.accepted_chunks), 1)
        reader.report_error.assert_not_called()

    def test_chapter_fallback_rejects_prefix_fuzzy_and_ambiguous_text(self):
        for text in (
            "A stranger finally answers",
            "A stranger finally answers the teleph0ne.",
        ):
            with self.subTest(text=text):
                controller, lines = self.controller()
                self.accept(controller, lines[0])
                self.assertFalse(controller._dialog_observed("Ada", text))
                self.assertEqual(controller.story_cursor.current_event_id, "event-1")
        controller, lines = self.controller(
            (
                "Starting line.",
                "Same words.",
                "Another line.",
                "More lines.",
                "Same words.",
            )
        )
        self.accept(controller, lines[0])
        line, _match = controller._resolve_chapter_exact_sequence_line_locked(
            "Ada", "Same words."
        )
        self.assertIsNone(line)

    def test_exact_visible_target_after_manual_choice_reanchors_without_dispatch(self):
        controller, lines = self.controller()
        plan = controller.live_sequence_plan
        plan.events["event-1"] = replace(
            plan.events["event-1"], kind="choice", control="manual", line_id=None
        )
        controller.story_cursor.anchor_event("event-1")
        self.assertEqual(controller.story_cursor.state, StoryCursorState.MANUAL)

        self.accept(controller, lines[-1])

        self.assertEqual(controller.story_cursor.current_event_id, "event-5")
        self.assertFalse(controller.story_cursor.can_auto_advance)
        self.assertEqual(controller.sequence_advance_leases, set())

    def test_exact_fallback_does_not_search_another_chapter(self):
        controller, lines = self.controller()
        controller.live_sequence_plan.events["event-5"] = replace(
            controller.live_sequence_plan.events["event-5"], chapter="other"
        )
        self.accept(controller, lines[0])

        self.assertFalse(controller._dialog_observed(lines[-1].speaker, lines[-1].text))

        self.assertEqual(controller.story_cursor.current_event_id, "event-1")

    def test_original_game_audio_wait_expires_when_visible_event_changes(self):
        controller, lines = self.controller()
        chunk = self.accept(controller, lines[0])
        route = SourceAudioRoute(
            Mock(), AudioRouteTrace(None, "game", "exact", None, None, "line-1", "test")
        )
        reader = Mock(active_generation=1)
        reader.wait_until_playable.return_value = True

        def play(_route, *, playback_guard):
            self.assertTrue(playback_guard())
            self.accept(controller, lines[-1], 2)
            self.assertFalse(playback_guard())
            return PlaybackOutcome(PlaybackStatus.INTERRUPTED, 1.0, audio_source="game")

        controller.speech_backend = SimpleNamespace(play_route=play)
        self.assertEqual(
            controller._dispatch_live_playback(reader, chunk, route).status,
            PlaybackStatus.INTERRUPTED,
        )

    def test_live_backend_recognition_carries_canonical_identity_without_reservation(
        self,
    ):
        controller, lines = self.controller()
        controller.voice_router = SimpleNamespace(registry=CharacterVoiceRegistry())
        controller.speech_backend = Mock()
        controller.uncertain_frame_recorder = None
        controller._publish_diagnostic = Mock()
        line = lines[0]
        frame = CapturedDialogFrame(Image.new("RGB", (300, 100)), 0)
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result",
            return_value=OCRResult("Ada", line.text, 95, "balanced", 1),
        ):
            recognized = controller._recognize_live_frame(frame)

        self.assertIsInstance(recognized, RecognizedDialog)
        self.assertEqual(recognized.line_id, line.line_id)
        self.assertTrue(recognized.exact_match)
        self.assertFalse(recognized.allow_unheard_replacement)

        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result",
            return_value=OCRResult(
                "Ada", line.text.replace("arrives", "arrivesx"), 95, "balanced", 1
            ),
        ):
            fuzzy = controller._recognize_live_frame(frame)

        self.assertIsInstance(fuzzy, RecognizedDialog)
        self.assertEqual(fuzzy.line_id, line.line_id)
        self.assertFalse(fuzzy.exact_match)
        self.assertFalse(fuzzy.allow_unheard_replacement)

    def reader_harness(self, controller):
        harness = AutoAdvanceFakeFrameHarness(
            stable_frame_route=controller._stable_live_frame_route,
            dialog_observed=controller._dialog_observed,
            ocr_purpose=controller._live_ocr_purpose,
        )
        reader = harness.reader
        controller.live_reader = reader
        controller.voice_router = SimpleNamespace(registry=CharacterVoiceRegistry())
        controller.uncertain_frame_recorder = None
        controller._publish_diagnostic = Mock()
        controller._prepare_speaker_announcement = Mock(return_value=(None, None))
        controller.error_handler = harness.errors.append
        controller.status_handler = Mock()
        reader.stable_frame_owner = controller._stable_live_frame_owner
        reader.line_id_resolver = controller._live_sequence_line_id
        reader.accept_chunk = controller._accept_live_chunk
        reader.reject_chunk = controller._reject_live_chunk
        reader.prepare_chunk = controller._prepare_live_chunk
        reader.play_prepared = controller._play_live_chunk
        reader.recognize_frame = controller._recognize_live_frame
        reader.auto_advance = controller._sequence_auto_advance_dialog
        reader.require_visible_auto_advance = True
        controller._auto_advance_dialog = Mock(return_value=True)
        return harness

    def test_real_reader_recognizes_new_lines_during_playback_and_never_guesses_skipped_speech(
        self,
    ):
        for playing, next_indices in ((True, (1, 4)), (False, (4,))):
            with self.subTest(playing=playing):
                controller, lines = self.controller()
                harness = self.reader_harness(controller)
                started, release = Event(), Event()
                played = []

                def play(prepared, *, playback_guard):
                    self.assertTrue(playback_guard())
                    played.append(prepared.payload)
                    if playing and prepared.payload == lines[0].text:
                        started.set()
                        self.assertTrue(release.wait(timeout=2))
                    self.assertTrue(playback_guard())
                    return PlaybackOutcome(
                        PlaybackStatus.COMPLETED,
                        1.0,
                        first_audio_ms=0,
                        audio_source="live",
                    )

                controller.speech_backend = SimpleNamespace(
                    name="test",
                    prepare_playback=lambda _speaker, text: PreparedPlayback(
                        text, None, None, None, "live:test"
                    ),
                    play_prepared=play,
                )

                def recognize(frame, *_args, result_resolver, **_kwargs):
                    harness.recognized_frames.append(frame)
                    result = result_resolver(
                        OCRResult(frame["character"], frame["text"], 95, "test", 1)
                    )
                    return result.character, result.text

                with patch(
                    "vntts.controller.recognize_live_frame", side_effect=recognize
                ):
                    harness.start()
                    try:
                        harness.push(
                            "Ada", lines[0].text, background="a", fingerprint="a"
                        )
                        harness.speech_executor.run_next()
                        if playing:
                            worker = Thread(target=harness.playback_executor.run_next)
                            worker.start()
                            self.assertTrue(started.wait(timeout=1))
                        else:
                            harness.playback_executor.run_next()
                        for index in next_indices:
                            harness.push(
                                "Ada",
                                lines[index].text,
                                background=f"frame-{index}",
                                fingerprint=f"frame-{index}",
                            )
                        visible = controller.story_cursor.snapshot()
                        self.assertEqual(visible.current_event_id, "event-5")
                        self.assertEqual(
                            len(harness.recognized_frames), 1 + len(next_indices)
                        )
                        accepted = tuple(controller.sequence_admitted_chunks)
                        self.assertEqual(
                            tuple(lease.event_id for lease in accepted),
                            (
                                "event-1",
                                *(f"event-{index + 1}" for index in next_indices),
                            ),
                        )
                        harness.push(
                            "Ada",
                            lines[0].text,
                            background="old-a",
                            fingerprint="old-a",
                        )
                        self.assertEqual(controller.story_cursor.snapshot(), visible)
                        self.assertEqual(
                            tuple(controller.sequence_admitted_chunks), accepted
                        )
                        if playing:
                            release.set()
                            worker.join(timeout=1)
                            self.assertFalse(worker.is_alive())
                            self.assertEqual(
                                controller.story_cursor.snapshot(), visible
                            )
                        controller._auto_advance_dialog.assert_not_called()
                        self.assertEqual(played, [lines[0].text])
                        self.assertEqual(harness.errors, [])
                    finally:
                        release.set()
                        harness.stop()

    def test_old_accepted_frame_flicker_cannot_reopen_occurrence_but_explicit_resync_can(
        self,
    ):
        controller, lines = self.controller()
        self.accept(controller, lines[0])
        self.accept(controller, lines[-1], 2)
        visible = controller.story_cursor.snapshot()

        self.assertFalse(controller._dialog_observed(lines[0].speaker, lines[0].text))
        self.assertEqual(controller.story_cursor.snapshot(), visible)

        self.assertTrue(controller.resync_live_sequence("event-1"))
        self.assertIsNotNone(
            controller._accept_live_chunk(
                SpeechChunk(
                    3, lines[0].speaker, lines[0].text, line_id=lines[0].line_id
                )
            )
        )
        self.accept(controller, lines[-1], 4)

    def ordinary_harness(self):
        controller, lines = self.controller()
        controller.settings = controller.settings.updated(
            live_sequence_mode="off", capture_mode="window", auto_advance_enabled=True
        )
        harness = self.reader_harness(controller)
        reader = harness.reader
        controller._auto_advance_dialog = AppController._auto_advance_dialog.__get__(
            controller, AppController
        )
        controller._is_game_focused = lambda: True
        reader.auto_advance = controller._live_auto_advance_callback()
        reader.prepare_chunk = lambda chunk: chunk.text
        reader.play_prepared = harness._play
        return controller, lines, harness

    def test_ordinary_ready_a_cannot_advance_pending_approximate_c(self):
        _controller, lines, harness = self.ordinary_harness()
        reader = harness.reader
        reader.recognize_frame = lambda frame: RecognizedDialog(
            "Ada",
            frame["text"],
            lines[0].line_id if frame["text"] == lines[0].text else lines[-1].line_id,
            exact_match=frame["text"] == lines[0].text,
            allow_unheard_replacement=False,
        )
        with (
            patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create),
            patch("vntts.controller.DialogueAdvancer") as advancer,
        ):
            harness.start()
            try:
                harness.push("Ada", lines[0].text, background="a", fingerprint="a")
                harness.push("Ada", lines[-1].text, background="c", fingerprint="c")
                self.assertEqual(reader.active_generation, 1)
                self.assertEqual(reader.routed_frame_fingerprint, "a")
                harness.speech_executor.run_next()
                harness.playback_executor.run_next()
                harness.timer_scheduler.fire_next("_run_auto_advance")
                advancer.return_value.advance.assert_not_called()

                harness.push("Ada", lines[-1].text, background="c2", fingerprint="c2")
                self.assertEqual(reader.active_generation, 2)
                harness.speech_executor.run_next()
                harness.playback_executor.run_next()
                harness.timer_scheduler.fire_next("_run_auto_advance")
                advancer.return_value.advance.assert_called_once_with()
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_raw_candidate_withdraws_old_readiness_before_tracking(self):
        _controller, lines, harness = self.ordinary_harness()
        reader = harness.reader
        raw_text = "An unindexed message about a harbor and a lost captain."
        reader.recognize_frame = lambda frame: (
            RecognizedDialog(
                "Ada", lines[0].text, lines[0].line_id, allow_unheard_replacement=False
            )
            if frame["text"] == lines[0].text
            else ("Ada", frame["text"])
        )
        original = reader._track_routed_dialog

        def intercept(tracker, character, text, *args):
            if text == raw_text:
                self.assertIsNone(reader.dialog_ready_generation)
                reader._run_auto_advance(reader.active_generation)
            return original(tracker, character, text, *args)

        reader._track_routed_dialog = intercept
        with (
            patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create),
            patch("vntts.controller.DialogueAdvancer") as advancer,
        ):
            harness.start()
            try:
                harness.push("Ada", lines[0].text, background="a", fingerprint="a")
                harness.speech_executor.run_next()
                harness.playback_executor.run_next()
                timer = harness.timer_scheduler.pending("_run_auto_advance")[0]
                harness.push("Ada", lines[0].text, background="same-a", fingerprint="a")
                self.assertIs(
                    harness.timer_scheduler.pending("_run_auto_advance")[0], timer
                )
                harness.push("Ada", raw_text, background="raw-c", fingerprint="raw-c")
                self.assertIsNone(reader.dialog_ready_generation)
                self.assertEqual(reader.active_generation, 1)
                advancer.return_value.advance.assert_not_called()
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_stale_typed_route_keeps_captured_anchor_and_cannot_restore_old_readiness(
        self,
    ):
        _controller, lines, harness = self.ordinary_harness()
        reader = harness.reader
        reader.frame_equivalent = lambda old, new: old == new
        reader.recognize_frame = lambda frame: (
            RecognizedDialog(
                "Ada",
                frame["text"],
                lines[0].line_id
                if frame["text"] == lines[0].text
                else lines[-1].line_id,
                allow_unheard_replacement=False,
            )
            if frame["text"] in {lines[0].text, lines[-1].text}
            else ("Ada", frame["text"])
        )
        captured = []

        def capture_during_publish(frame, fingerprint, *_args):
            if fingerprint == "c":
                captured.append(frame)
                with reader.state_lock:
                    reader.latest_frame = {
                        "character": "Ada",
                        "text": "Unrecognized D.",
                        "background": "d",
                        "visible": True,
                    }
                    reader.latest_frame_fingerprint = "d"
                    reader.frame_version += 1

        reader.frame_routed = capture_during_publish
        original = reader._track_routed_dialog

        def intercept(tracker, character, text, *args):
            chunks = original(tracker, character, text, *args)
            if text == lines[-1].text:
                self.assertIsNone(chunks)
                self.assertIsNone(reader.dialog_ready_generation)
                self.assertIs(reader.routed_frame, captured[0])
                self.assertIsNot(reader.routed_frame, reader.latest_frame)
                reader._run_auto_advance(reader.active_generation)
            return chunks

        reader._track_routed_dialog = intercept
        with (
            patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create),
            patch("vntts.controller.DialogueAdvancer") as advancer,
        ):
            harness.start()
            try:
                harness.push("Ada", lines[0].text, background="a", fingerprint="a")
                harness.speech_executor.run_next()
                harness.playback_executor.run_next()
                harness.push("Ada", lines[-1].text, background="c", fingerprint="c")
                advancer.return_value.advance.assert_not_called()
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_old_typed_identity_cannot_make_current_dialogue_ready_on_old_pixels(self):
        _controller, lines, harness = self.ordinary_harness()
        reader = harness.reader
        reader.recognize_frame = lambda frame: RecognizedDialog(
            "Ada",
            frame["text"],
            lines[0].line_id if frame["text"] == lines[0].text else lines[-1].line_id,
            allow_unheard_replacement=False,
        )
        with (
            patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create),
            patch("vntts.controller.DialogueAdvancer") as advancer,
        ):
            harness.start()
            try:
                for fingerprint, line in (("a", lines[0]), ("c", lines[-1])):
                    harness.push(
                        "Ada",
                        line.text,
                        background=fingerprint,
                        fingerprint=fingerprint,
                    )
                    harness.speech_executor.run_next()
                    harness.playback_executor.run_next()
                harness.push(
                    "Ada", lines[0].text, background="old-a", fingerprint="old-a"
                )
                self.assertIsNone(reader.dialog_ready_generation)
                self.assertEqual(reader.active_generation, 2)
                reader._run_auto_advance(reader.active_generation)
                advancer.return_value.advance.assert_not_called()
                harness.push(
                    "Ada", lines[-1].text, background="new-c", fingerprint="new-c"
                )
                harness.timer_scheduler.fire_next("_run_auto_advance")
                advancer.return_value.advance.assert_called_once_with()
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_unchanged_cached_unnamed_narration_keeps_its_auto_timer(self):
        _controller, _lines, harness = self.ordinary_harness()
        reader = harness.reader
        text = "An unnamed unindexed sentence about a harbor and a lost captain."
        reader.recognize_frame = lambda frame: (None, frame["text"])
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                harness.push(
                    None, text, background="narration", fingerprint="narration"
                )
                harness.push(
                    None, text, background="narration", fingerprint="narration"
                )
                harness.clock.advance(0.2)
                harness.push(
                    None, text, background="narration", fingerprint="narration"
                )
                harness.speech_executor.run_next()
                harness.playback_executor.run_next()
                timer = harness.timer_scheduler.pending("_run_auto_advance")[0]

                harness.push(None, text, background="same", fingerprint="narration")

                self.assertIs(
                    harness.timer_scheduler.pending("_run_auto_advance")[0], timer
                )
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_cached_related_raw_variant_held_by_canonical_tracker_keeps_auto_timer(
        self,
    ):
        _controller, lines, harness = self.ordinary_harness()
        reader = harness.reader
        variant = lines[0].text.replace("arrives", "arrivesx")
        reader.recognize_frame = lambda frame: (
            RecognizedDialog(
                "Ada", lines[0].text, lines[0].line_id, allow_unheard_replacement=False
            )
            if frame["text"] == lines[0].text
            else ("Ada", frame["text"])
        )
        with patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create):
            harness.start()
            try:
                harness.push("Ada", lines[0].text, background="a", fingerprint="a")
                harness.speech_executor.run_next()
                harness.playback_executor.run_next()
                harness.push(
                    "Ada", variant, background="variant", fingerprint="variant"
                )
                timer = harness.timer_scheduler.pending("_run_auto_advance")[0]

                harness.push(
                    "Ada", variant, background="same-variant", fingerprint="variant"
                )

                self.assertIs(
                    harness.timer_scheduler.pending("_run_auto_advance")[0], timer
                )
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()


if __name__ == "__main__":
    unittest.main()
