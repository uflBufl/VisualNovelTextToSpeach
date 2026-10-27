import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from vntts_artifacts.generated_audio import text_sha256

from vntts.capture_provenance import CaptureProvenance
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.controller import AppController
from vntts.dialog_capture import CapturedDialogFrame, recognize_screenshot_result
from vntts.ocr import OCRResult
from vntts.ocr_corrections import OCRCorrectionDictionary
from vntts.reading_diagnostics import retain_ocr_resolution
from vntts.settings import AppSettings
from vntts.support_timeline_acquisition import TimelineAcquisition
from vntts.voices import CharacterVoiceRegistry


def story_line(text="Hello from the prepared story.", line_id="line-1"):
    return ChapterDialogue(line_id, "chapter-1", 1, "Narrator", text, text_sha256(text))


class ReadingOCREvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "runtime.jsonl"
        self.runtime = TimelineAcquisition(self.path)
        self.addCleanup(self.runtime.stop)
        self.session = SimpleNamespace(
            recording=SimpleNamespace(target={"pid": 2, "window_id": 1}),
            phase="recording",
            stop_requested=Event(),
            started_monotonic=10,
            seconds=600,
            runtime=self.runtime,
        )
        self.provenance = CaptureProvenance(
            "capture-id",
            "run-id",
            11,
            12,
            "Game",
            1,
            "Game",
            2,
            None,
            None,
            (300, 100),
            True,
        )
        self.frame = CapturedDialogFrame(
            Image.new("RGB", (300, 100)), 1, provenance=self.provenance
        )
        self.frame.image.info["capture_provenance"] = self.provenance.as_dict()
        self.raw = OCRResult(
            "Wrong OCR speaker", "Helo from the prepared story.", 95, "balanced", 1
        )
        active = patch("vntts.reading_diagnostics._active", self.session)
        active.start()
        self.addCleanup(active.stop)
        clock = patch("vntts.reading_diagnostics.monotonic", return_value=13)
        clock.start()
        self.addCleanup(clock.stop)

    def retain(self, **kwargs):
        retain_ocr_resolution(
            kwargs.get("frame", self.frame),
            self.raw,
            {"match_result": "expected-exact", "line": story_line()},
            run_id=kwargs.get("run_id", "run-id"),
            capture_id=kwargs.get("capture_id", "capture-id"),
        )

    def events(self):
        self.runtime.stop()
        return [
            event
            for line in self.path.read_text().splitlines()
            if (event := json.loads(line))["kind"] == "event"
        ]

    def test_opt_out_no_media_and_closed_session_do_not_record_text(self):
        with patch("vntts.reading_diagnostics._active", None):
            self.retain()
        recording = self.session.recording
        self.session.recording = None
        self.retain()
        self.session.recording = recording
        self.session.phase = "finalizing"
        self.retain()
        self.session.phase = "recording"
        self.session.stop_requested.set()
        self.retain()
        self.assertEqual(self.events(), [])
        self.assertNotIn("Wrong OCR speaker", self.path.read_text())

    def test_capture_must_match_target_run_identity_and_recording_interval(self):
        for changes in (
            {"owner_pid": 3},
            {"window_id": 3},
            {"run_id": "other-run"},
            {"capture_id": "other-capture"},
            {"success": False},
            {"started_monotonic": 9},
            {"completed_monotonic": 14},
            {"started_monotonic": 12, "completed_monotonic": 11},
        ):
            with self.subTest(changes=changes):
                self.retain(
                    frame=replace(
                        self.frame, provenance=replace(self.provenance, **changes)
                    )
                )
        self.retain(frame=replace(self.frame, provenance=None))
        for fields in ({"window_id": None}, {"owner_pid": None}):
            with self.subTest(fields=fields):
                self.retain(
                    frame=replace(
                        self.frame, provenance=replace(self.provenance, **fields)
                    )
                )
        self.retain(run_id=None)
        self.retain(capture_id=None)
        self.retain(run_id="private run text")
        self.retain(capture_id="private capture text")
        with patch("vntts.reading_diagnostics.monotonic", return_value=611):
            self.retain()
        self.assertEqual(self.events(), [])

    def controller(self, rows):
        controller = AppController(
            AppSettings(audio_source_policy="prefer-generated"),
            chapter_voice_preloader=ChapterVoicePreloader(rows),
        )
        controller.voice_router = SimpleNamespace(registry=CharacterVoiceRegistry())
        controller.uncertain_frame_recorder = None
        controller._publish_diagnostic = Mock()
        controller._resolve_voice_label = Mock(return_value="Prepared story voice")
        return controller

    def test_native_capture_retains_actual_raw_ocr_and_canonical_line_identity(self):
        line = story_line()
        controller = self.controller((line,))
        controller.correction_dictionary = OCRCorrectionDictionary({"Helo": "Hello"})
        self.raw = replace(
            self.raw, confidence=26, speaker_confidence=26, dialogue_confidence=95
        )
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=self.raw
        ):
            actual = controller._recognize_live_frame(self.frame)
        self.assertEqual(actual, (line.speaker, line.text))
        events = self.events()
        evidence = next(
            event for event in events if event["operation"] == "story-ocr-evidence"
        )
        self.assertEqual(evidence["raw_character"], self.raw.character)
        self.assertEqual(evidence["raw_text"], self.raw.text)
        self.assertEqual(evidence["canonical_character"], line.speaker)
        self.assertEqual(evidence["canonical_text"], line.text)
        self.assertEqual(evidence["line_id"], line.line_id)
        self.assertEqual(evidence["ocr_confidence"], 26)
        self.assertEqual(evidence["ocr_speaker_confidence"], 26)
        self.assertEqual(evidence["ocr_dialogue_confidence"], 95)
        self.assertEqual(evidence["raw_character_length"], len(self.raw.character))
        self.assertEqual(evidence["raw_text_length"], len(self.raw.text))
        self.assertEqual(evidence["canonical_character_length"], len(line.speaker))
        self.assertEqual(evidence["canonical_text_length"], len(line.text))
        self.assertEqual(evidence["match_result"], "expected-text-only")
        self.assertEqual(
            (evidence["run_id"], evidence["capture_id"]), ("run-id", "capture-id")
        )
        ordinary = next(
            event for event in events if event["operation"] == "story-result-resolution"
        )
        self.assertNotIn(self.raw.text, json.dumps(ordinary))
        self.assertNotIn("raw_text", ordinary)

    def test_real_matcher_rejections_keep_reason_without_canonical_identity(self):
        cases = (
            (
                (story_line(),),
                replace(self.raw, text="Unrelated complete dialogue."),
                "expected-no-match",
            ),
            (
                (story_line(), story_line(line_id="line-2")),
                replace(self.raw, text=story_line().text),
                "expected-ambiguous",
            ),
            (
                (
                    story_line(),
                    story_line(story_line().text + " More words.", "line-2"),
                ),
                replace(self.raw, text=story_line().text),
                "expected-incomplete",
            ),
            ((story_line(),), replace(self.raw, confidence=20), "ocr-uncertain"),
            ((story_line(),), replace(self.raw, text=""), "empty"),
        )
        for rows, raw, reason in cases:
            with self.subTest(reason=reason):
                controller = self.controller(rows)
                resolution = {}
                self.assertIs(
                    controller._resolve_story_ocr_result(raw, resolution=resolution),
                    raw,
                )
                self.assertEqual(resolution["match_result"], reason)
                retain_ocr_resolution(
                    self.frame,
                    raw,
                    resolution,
                    run_id="run-id",
                    capture_id="capture-id",
                )
        events = self.events()
        self.assertEqual(
            [event["match_result"] for event in events], [case[2] for case in cases]
        )
        for event in events:
            self.assertNotIn("canonical_text", event)
            self.assertNotIn("line_id", event)

    def test_owned_prefix_gate_is_preserved_and_observability_failure_is_soft(self):
        controller = self.controller((story_line(),))
        controller._expected_sequence_prefix_line_locked = Mock(
            return_value=story_line()
        )
        resolution = {}
        self.assertIs(
            controller._resolve_story_ocr_result(self.raw, resolution=resolution),
            self.raw,
        )
        self.assertEqual(resolution, {"match_result": "owned-prefix"})
        with patch.object(
            self.runtime, "record_ocr_resolution", side_effect=OSError("failed")
        ):
            self.retain()
        self.assertEqual(self.events(), [])

    def test_failed_raw_observer_preserves_recognition_and_corrections(self):
        with patch(
            "vntts.dialog_capture.recognize_dialog_image_result", return_value=self.raw
        ):
            actual = recognize_screenshot_result(
                self.frame.image,
                correction_dictionary=OCRCorrectionDictionary({"Helo": "Hello"}),
                raw_result_observer=Mock(side_effect=OSError("failed")),
            )
        self.assertEqual(actual.text, story_line().text)

    def test_ordinary_record_stays_text_free_and_media_payload_is_bounded(self):
        self.runtime.record(
            {
                "operation": "ordinary",
                "raw_text": self.raw.text,
                "canonical_text": story_line().text,
            }
        )
        self.runtime.record_ocr_resolution(
            {"operation": "story-ocr-evidence"},
            raw_character="s" * 1000,
            raw_text="t" * 10000,
            canonical_character="c" * 1000,
            canonical_text="x" * 10000,
            arbitrary_text="private extra field",
        )
        ordinary, media = self.events()
        self.assertEqual(set(ordinary), {"kind", "acquisition_id", "operation"})
        self.assertEqual(len(media["raw_character"]), 256)
        self.assertEqual(len(media["raw_text"]), 4096)
        self.assertEqual(len(media["canonical_character"]), 256)
        self.assertEqual(len(media["canonical_text"]), 4096)
        self.assertEqual(media["raw_character_length"], 1000)
        self.assertEqual(media["raw_text_length"], 10000)
        self.assertEqual(media["canonical_character_length"], 1000)
        self.assertEqual(media["canonical_text_length"], 10000)
        self.assertNotIn("arbitrary_text", media)

    def test_media_evidence_uses_existing_acquisition_caps_and_stop(self):
        self.runtime.maximum_events = 1
        self.retain()
        self.retain()
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertTrue(self.runtime.status()["capped"])
        self.assertEqual(self.runtime.status()["dropped_events"], 1)
        self.retain()
        self.assertEqual(self.runtime.status()["late_rejected_events"], 1)


if __name__ == "__main__":
    unittest.main()
