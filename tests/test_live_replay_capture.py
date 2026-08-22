import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from PIL import Image

from vntts.dialog_capture import CapturedDialogFrame
from vntts.live_replay import load_live_replay_corpus
from vntts.live_replay_capture import (
    LiveReplayCaptureError,
    LiveReplayCaptureSession,
    capture_replay_session,
)


class FakeStoryResolver:
    def __init__(self):
        self.calls = []

    def resolve_exact_with_result(self, character, text):
        self.calls.append((character, text))
        if (character, text) != ("Rhiannon", "Hello there."):
            return None, "no-match"
        return (
            SimpleNamespace(
                speaker="Rhiannon",
                text="Hello there.",
                line_id="story:1",
                source_audio_status="available",
                source_audio_id="event:1",
                source_audio_duration_seconds=1.25,
            ),
            "exact",
        )


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
            session.observe(frame("blue"), "Hotelier", "A room?")
            result = session.finish()

            document = json.loads(result.corpus.read_text(encoding="utf-8"))
            report = json.loads(result.report.read_text(encoding="utf-8"))
            loaded = load_live_replay_corpus(result.corpus)

            self.assertEqual(result.dialogue_count, 2)
            self.assertEqual(result.frame_count, 3)
            self.assertEqual(result.boundary_review_count, 1)
            self.assertEqual(document["fixture_kind"], "saved-frame-ocr-replay-capture")
            self.assertEqual(document["dialogue"][0]["line_id"], "story:1")
            self.assertEqual(document["dialogue"][0]["expected_source"], "game")
            self.assertEqual(document["dialogue"][1]["line_id"], "capture:2")
            self.assertTrue(report["boundary_review_required"])
            self.assertEqual(loaded.dialogue[0].character, "Rhiannon")
            self.assertEqual(loaded.dialogue[0].text, "Hello there.")
            self.assertEqual(
                loaded.dialogue[0].frame_recognition_sources, ("ocr", "ocr")
            )
            for dialogue in document["dialogue"]:
                for specification in dialogue["frames"]:
                    payload = root.joinpath(
                        *Path(specification["path"]).parts
                    ).read_bytes()
                    self.assertEqual(
                        hashlib.sha256(payload).hexdigest(), specification["sha256"]
                    )

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

            self.assertEqual(result.frame_count, 2)
            self.assertEqual(result.dialogue_count, 2)
            self.assertEqual(report["duplicate_fingerprints_skipped"], 1)
            self.assertEqual(report["uncertain_observations_skipped"], 1)

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
            alias.symlink_to(story)
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
