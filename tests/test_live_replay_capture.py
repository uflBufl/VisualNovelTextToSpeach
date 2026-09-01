import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from tests.symlink_support import symlink_or_skip
from vntts.dialog_capture import (
    CapturedDialogFrame,
    detect_standalone_ellipsis_frame,
    recognize_live_frame,
)
from vntts.live_replay import load_live_replay_corpus
from vntts.live_replay_capture import (
    LiveReplayCaptureError,
    LiveReplayCaptureSession,
    capture_replay_session,
)
from vntts.ocr import OCRResult


class FakeStoryResolver:
    def __init__(self):
        self.calls = []
        self.known = {
            ("Rhiannon", "Hello there."): self._line(
                "story:1", "1", "Rhiannon", "Hello there."
            ),
            ("Hotelier", "A room?"): self._line("story:2", "1", "Hotelier", "A room?"),
            ("Narrator", "He"): self._line("other:1", "2", "Narrator", "He"),
        }
        self.by_chapter = {
            chapter: [line for line in self.known.values() if line.chapter == chapter]
            for chapter in {line.chapter for line in self.known.values()}
        }
        self.speaker_names = {
            "rhiannon": "Rhiannon",
            "hotelier": "Hotelier",
            "narrator": "Narrator",
        }

    @staticmethod
    def _line(line_id, chapter, speaker, text):
        return SimpleNamespace(
            speaker=speaker,
            text=text,
            line_id=line_id,
            chapter=chapter,
            source_audio_status="available",
            source_audio_id=f"event:{line_id[-1]}",
            source_audio_duration_seconds=1.25,
        )

    def resolve_exact_with_result(self, character, text):
        self.calls.append((character, text))
        line = self.known.get((character, text))
        if line is None:
            return None, "no-match"
        return line, "exact"

    def resolve_exact_among(self, character, text, line_ids):
        line, result = self.resolve_exact_with_result(character, text)
        if line is None or line.line_id not in set(line_ids):
            return None, "expected-no-match"
        return line, result


def frame(color):
    return CapturedDialogFrame(Image.new("RGB", (20, 10), color), 0.0)


class LiveReplayCaptureTest(unittest.TestCase):
    def test_capture_groups_prefixes_and_publishes_loadable_exact_frames(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            resolver = FakeStoryResolver()
            session = LiveReplayCaptureSession(root, story_resolver=resolver)

            session.observe(frame("red"), "Rhiannon", "Hello")
            session.observe(frame("green"), "Rhiannon", "Hello there.")
            session.observe(frame("yellow"), "Narrator", "???")
            session.observe(frame("purple"), "Narrator", "...")
            session.observe(frame("blue"), "Hotelier", "A room?")
            result = session.finish()

            document = json.loads(result.corpus.read_text(encoding="utf-8"))
            report = json.loads(result.report.read_text(encoding="utf-8"))
            loaded = load_live_replay_corpus(result.corpus)

            self.assertEqual(result.dialogue_count, 3)
            self.assertEqual(result.frame_count, 5)
            self.assertEqual(result.boundary_review_count, 0)
            self.assertEqual(document["fixture_kind"], "saved-frame-ocr-replay-capture")
            self.assertEqual(document["dialogue"][0]["line_id"], "story:1")
            self.assertEqual(document["dialogue"][0]["expected_source"], "game")
            self.assertEqual(document["dialogue"][1]["line_id"], "capture:2")
            self.assertEqual(document["dialogue"][1]["text"], "...")
            self.assertEqual(document["dialogue"][2]["line_id"], "story:2")
            self.assertFalse(report["boundary_review_required"])
            self.assertEqual(report["unresolved_observation_count"], 2)
            ledger = json.loads(result.observation_ledger.read_text(encoding="utf-8"))
            self.assertEqual(ledger["observation_count"], 5)
            self.assertEqual(
                [entry["status"] for entry in ledger["observations"]],
                [
                    "unresolved",
                    "canonical",
                    "unresolved",
                    "punctuation-only",
                    "canonical",
                ],
            )
            self.assertEqual(loaded.dialogue[0].character, "Rhiannon")
            self.assertEqual(loaded.dialogue[0].text, "Hello there.")
            self.assertEqual(loaded.dialogue[0].frame_recognition_sources, ("ocr",))
            for dialogue in document["dialogue"]:
                for specification in dialogue["frames"]:
                    payload = root.joinpath(
                        *Path(specification["path"]).parts
                    ).read_bytes()
                    self.assertEqual(
                        hashlib.sha256(payload).hexdigest(), specification["sha256"]
                    )

    def test_capture_locks_exact_resolution_to_first_canonical_chapter(self):
        with TemporaryDirectory() as directory:
            session = LiveReplayCaptureSession(
                Path(directory) / "capture", story_resolver=FakeStoryResolver()
            )
            session.observe(frame("green"), "Rhiannon", "Hello there.")
            session.observe(frame("red"), "Narrator", "He")
            result = session.finish()
            ledger = json.loads(result.observation_ledger.read_text(encoding="utf-8"))

            self.assertEqual(result.dialogue_count, 1)
            self.assertEqual(
                [entry["status"] for entry in ledger["observations"]],
                ["canonical", "unresolved"],
            )

    def test_visual_ellipsis_detector_rejects_other_glyph_counts(self):
        image = Image.new("RGB", (100, 60), "black")
        draw = ImageDraw.Draw(image)
        for left in (2, 8, 14):
            draw.rectangle((left, 34, left + 1, 35), fill="white")

        self.assertTrue(detect_standalone_ellipsis_frame(image))
        self.assertFalse(detect_standalone_ellipsis_frame(frame("black").image))
        draw.rectangle((20, 34, 21, 35), fill="white")
        self.assertFalse(detect_standalone_ellipsis_frame(image))

    def test_visual_ellipsis_preserves_checksum_bound_nameplate_speaker(self):
        image = Image.new("RGB", (100, 60), "black")
        draw = ImageDraw.Draw(image)
        for left in (2, 8, 14):
            draw.rectangle((left, 34, left + 1, 35), fill="white")
        frame_value = CapturedDialogFrame(image, 0.0)
        with patch(
            "vntts.dialog_capture.recognize_screenshot_result",
            return_value=OCRResult(
                "Narrator",
                "Rhiannon nameplate noise",
                90.0,
                "balanced",
                1,
            ),
        ):
            character, text = recognize_live_frame(
                frame_value,
                minimum_confidence=60,
                ellipsis_speaker_resolver=FakeStoryResolver(),
            )

        self.assertEqual((character, text), ("Rhiannon", "..."))

    def test_capture_loop_counts_duplicates_and_uncertain_observations(self):
        with TemporaryDirectory() as directory:
            session = LiveReplayCaptureSession(Path(directory) / "capture")
            frames = [
                frame("red"),
                frame("red"),
                frame("green"),
                frame("black"),
                frame("blue"),
            ]
            observations = iter(
                [
                    ("Rhiannon", "First line."),
                    None,
                    ("Narrator", ""),
                    ("Hotelier", "Second line."),
                ]
            )

            result = capture_replay_session(
                session,
                capture_frame=lambda: frames.pop(0),
                recognize_frame=lambda _frame: next(observations),
                fingerprint_frame=lambda value: value.image.getpixel((0, 0)),
                interval_seconds=0,
                maximum_frames=2,
                sleep=lambda _seconds: None,
            )
            report = json.loads(result.report.read_text(encoding="utf-8"))

            self.assertEqual(result.frame_count, 4)
            self.assertEqual(result.dialogue_count, 2)
            self.assertEqual(report["duplicate_fingerprints_skipped"], 1)
            self.assertEqual(report["uncertain_observations_skipped"], 2)
            ledger = json.loads(result.observation_ledger.read_text(encoding="utf-8"))
            self.assertEqual(ledger["observation_count"], 4)

    def test_capture_rejects_existing_output_and_changed_frame(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            root.mkdir()
            with self.assertRaisesRegex(LiveReplayCaptureError, "already exists"):
                LiveReplayCaptureSession(root)

        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            session.observe(frame("red"), "Narrator", "A line.")
            captured = next((root / "frames").iterdir())
            captured.write_bytes(b"changed")
            with self.assertRaisesRegex(LiveReplayCaptureError, "frame changed"):
                session.finish()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            session.observe(frame("red"), "Narrator", "A line.")
            (root / "corpus.json").write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(LiveReplayCaptureError, "already exists"):
                session.finish()

    def test_capture_rejects_a_story_index_symlink(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            story = root / "story.jsonl"
            story.write_text("{}", encoding="utf-8")
            alias = root / "story-alias.jsonl"
            symlink_or_skip(alias, story)
            with self.assertRaisesRegex(LiveReplayCaptureError, "cannot be a symlink"):
                LiveReplayCaptureSession(
                    root / "capture",
                    story_index_path=alias,
                    story_index_sha256=hashlib.sha256(story.read_bytes()).hexdigest(),
                )

    def test_capture_requires_accepted_dialogue(self):
        with TemporaryDirectory() as directory:
            session = LiveReplayCaptureSession(Path(directory) / "capture")
            session.observe(frame("black"), "Narrator", "")
            with self.assertRaisesRegex(LiveReplayCaptureError, "no accepted"):
                session.finish()


if __name__ == "__main__":
    unittest.main()
