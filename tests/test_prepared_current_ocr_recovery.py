import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts_artifacts.generated_audio import text_sha256

from tests import test_controller_transition_observation as transitions
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.ocr import OCRResult
from vntts.playback import PlaybackOutcome, PlaybackStatus, PreparedPlayback


class PreparedCurrentOCRRecoveryTest(unittest.TestCase):
    text = "The supplies are stored beside the locked cellar."

    def preloader(self, *extra):
        rows = tuple(
            ChapterDialogue(
                f"line-{index}", "chapter", index, speaker, text, text_sha256(text)
            )
            for index, (speaker, text) in enumerate((("Ada", self.text), *extra), 1)
        )
        preloader = ChapterVoicePreloader(rows)
        preloader.select_line_id(rows[0].line_id)
        return preloader, rows

    def test_fully_seen_current_line_recovers_one_missing_edge_letter(self):
        for observed in (self.text[1:], self.text[:-2] + "."):
            with self.subTest(observed=observed):
                preloader, rows = self.preloader()
                line, _ = preloader.resolve_complete_among(
                    "Ada",
                    observed,
                    (rows[0].line_id,),
                    fully_observed_line_id=rows[0].line_id,
                )
                self.assertEqual(line, rows[0])

    def test_boundary_recovery_requires_fully_seen_current_owner(self):
        for current, seen in ((True, None), (False, "line-1")):
            with self.subTest(current=current, seen=seen):
                preloader, rows = self.preloader(("Ada", "Birds fill the garden."))
                if not current:
                    preloader.select_line_id(rows[1].line_id)
                line, _ = preloader.resolve_complete_among(
                    "Ada",
                    self.text[:-2] + ".",
                    tuple(row.line_id for row in rows),
                    fully_observed_line_id=seen,
                )
                self.assertIsNone(line)

    def test_fully_seen_owner_does_not_complete_an_unseen_target(self):
        target = "Birds gather beside the fountain."
        preloader, rows = self.preloader(("Ada", target))
        line, _ = preloader.resolve_complete_among(
            "Ada",
            target[:-2] + ".",
            tuple(row.line_id for row in rows),
            fully_observed_line_id=rows[0].line_id,
        )
        self.assertIsNone(line)

    def test_boundary_recovery_preserves_speaker_protection_and_prefix_guards(self):
        for speaker, observed in (
            ("Bob", self.text[:-2] + "."),
            ("", self.text[:-2] + "."),
            ("Ada", "The supplies are stored beside the locked cell."),
            ("Ada", "The supplies are stored beside the locked garden."),
            ("Ada", "The supplies are stored beside the locked"),
        ):
            with self.subTest(speaker=speaker, observed=observed):
                preloader, rows = self.preloader()
                line, _ = preloader.resolve_complete_among(
                    speaker,
                    observed,
                    (rows[0].line_id,),
                    fully_observed_line_id=rows[0].line_id,
                )
                self.assertIsNone(line)
        for text, observed in (
            ("There is no one.", "There is no on."),
            ("There are 12.", "There are 1."),
        ):
            with self.subTest(text=text):
                preloader = ChapterVoicePreloader(
                    (
                        ChapterDialogue(
                            "line-1", "chapter", 1, "Ada", text, text_sha256(text)
                        ),
                    )
                )
                preloader.select_line_id("line-1")
                line, _ = preloader.resolve_complete_among(
                    "Ada", observed, ("line-1",), fully_observed_line_id="line-1"
                )
                self.assertIsNone(line)

    def test_current_owner_cannot_win_against_competing_target(self):
        preloader, rows = self.preloader(("Ada", self.text.replace("cellar", "cellad")))
        line, _ = preloader.resolve_complete_among(
            "Ada",
            self.text[:-2] + ".",
            tuple(row.line_id for row in rows),
            fully_observed_line_id=rows[0].line_id,
        )
        self.assertIsNone(line)

    def test_spoken_current_line_rebinds_after_boundary_ocr_loss_without_replay(self):
        fixture = transitions.ControllerTransitionObservationTest()
        controller, lines = fixture.controller((self.text, "Birds fill the garden."))
        harness = fixture.reader_harness(controller)
        reader = harness.reader
        controller.speech_backend = SimpleNamespace(
            name="fixture",
            prepare_playback=lambda _speaker, text: PreparedPlayback(
                text, None, None, None, "generated:fixture"
            ),
            play_prepared=lambda _prepared, **_options: PlaybackOutcome(
                PlaybackStatus.COMPLETED, 1.0, audio_source="generated"
            ),
        )

        def recognize(frame, *_args, result_resolver, **_kwargs):
            result = result_resolver(
                OCRResult(frame["character"], frame["text"], 95, "fixture", 1)
            )
            return result.character, result.text

        with (
            patch("vntts.controller.recognize_live_frame", side_effect=recognize),
            patch("vntts.live.Timer", side_effect=harness.timer_scheduler.create),
        ):
            harness.start()
            try:
                harness.push(
                    "Ada", self.text, background="complete", fingerprint="full"
                )
                self.assertEqual(controller._fully_observed_line_id(), lines[0].line_id)
                harness.finish_playback()
                accepted = tuple(reader.accepted_chunks)
                for background in ("ocr-a", "ocr-b"):
                    harness.push(
                        "Ada",
                        self.text[:-2] + ".",
                        background=background,
                        fingerprint="missing-last-letter",
                        expected=("Ada", self.text),
                    )
                self.assertEqual(tuple(reader.accepted_chunks), accepted)
                self.assertFalse(reader.observation_uncertain)
                self.assertEqual(reader.routed_frame_fingerprint, "missing-last-letter")
                self.assertEqual(
                    reader.dialog_ready_generation, reader.active_generation
                )
                self.assertTrue(harness.timer_scheduler.pending("_run_auto_advance"))
                self.assertEqual(harness.errors, [])
            finally:
                harness.stop()

    def test_low_confidence_boundary_loss_is_not_source_confirmation(self):
        fixture = transitions.ControllerTransitionObservationTest()
        controller, lines = fixture.controller((self.text, "Birds fill the garden."))
        fixture.accept(controller, lines[0])
        controller._fully_observed_line_id = Mock(return_value=lines[0].line_id)
        raw = OCRResult("Ada", self.text[:-2] + ".", 41, "fixture", 1)
        resolution = {}
        self.assertEqual(
            controller._resolve_story_ocr_result(raw, resolution=resolution), raw
        )
        self.assertEqual(resolution["match_result"], "ocr-uncertain")
        self.assertIsNone(resolution["line"])

    def test_owned_prefix_does_not_supply_fully_observed_evidence(self):
        fixture = transitions.ControllerTransitionObservationTest()
        controller, lines = fixture.controller((self.text, "Birds fill the garden."))
        controller._expected_sequence_prefix_line_locked = Mock(return_value=lines[0])
        raw = OCRResult("Ada", "The supplies are stored", 95, "fixture", 1)
        resolution = {}
        self.assertEqual(
            controller._resolve_story_ocr_result(raw, resolution=resolution), raw
        )
        self.assertEqual(resolution, {"match_result": "owned-prefix"})
        self.assertIsNone(controller._fully_observed_line_id())


if __name__ == "__main__":
    unittest.main()
