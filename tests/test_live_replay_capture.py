import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

import vntts.live_replay_capture as live_replay_capture
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
            self.assertIsNone(document["dialogue"][0]["expected_source"])
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

    def test_capture_retries_after_frames_directory_creation_failure(self):
        with TemporaryDirectory() as directory:
            requested = Path(directory) / "capture"
            root = requested.parent.resolve() / requested.name
            original_mkdir = Path.mkdir

            def fail_frames_directory(path, *arguments, **keywords):
                if path == root / "frames":
                    raise OSError("injected frames directory failure")
                return original_mkdir(path, *arguments, **keywords)

            with patch.object(Path, "mkdir", fail_frames_directory):
                with self.assertRaisesRegex(
                    LiveReplayCaptureError, "frames directory failure"
                ):
                    LiveReplayCaptureSession(requested)

            self.assertFalse(root.exists())
            LiveReplayCaptureSession(requested)

    def test_capture_retries_after_partial_frame_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            original_named_temporary_file = tempfile.NamedTemporaryFile

            class PartialWriter:
                def __init__(self, stream):
                    self.stream = stream
                    self.name = stream.name

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *arguments):
                    return self.stream.__exit__(*arguments)

                def write(self, payload):
                    self.stream.write(payload[:1])
                    raise OSError("injected partial write")

                def __getattr__(self, name):
                    return getattr(self.stream, name)

            with patch(
                "vntts.live_replay_capture.tempfile.NamedTemporaryFile",
                side_effect=lambda **kwargs: PartialWriter(
                    original_named_temporary_file(**kwargs)
                ),
            ):
                with self.assertRaisesRegex(LiveReplayCaptureError, "partial write"):
                    session.note_uncertain_observation(frame("red"))

            self.assertEqual(session.frame_count, 0)
            self.assertEqual(session.uncertain_observations, 0)
            self.assertEqual(list((root / "frames").iterdir()), [])
            session.note_uncertain_observation(frame("red"))
            self.assertEqual(session.frame_count, 1)
            self.assertEqual(session.uncertain_observations, 1)
            self.assertEqual(
                [path.name for path in (root / "frames").iterdir()],
                ["frame-000001.png"],
            )

    def test_capture_keeps_frame_when_temp_cleanup_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            with patch(
                "vntts.live_replay_capture.Path.unlink",
                side_effect=OSError("injected cleanup failure"),
            ):
                session.observe(frame("red"), "Narrator", "A line.")

            self.assertEqual(session.frame_count, 1)
            self.assertTrue((root / "frames" / "frame-000001.png").is_file())

    def test_capture_retries_after_partial_result_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            session.observe(frame("red"), "Narrator", "A line.")
            original_write = live_replay_capture._write_payload_no_replace
            calls = 0

            def fail_after_ledger(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise LiveReplayCaptureError("injected result write failure")
                return original_write(path, payload)

            with patch(
                "vntts.live_replay_capture._write_payload_no_replace",
                side_effect=fail_after_ledger,
            ):
                with self.assertRaisesRegex(
                    LiveReplayCaptureError, "injected result write failure"
                ):
                    session.finish()

            self.assertFalse(session.finished)
            for name in (
                "observation-ledger.json",
                "capture-report.json",
                "corpus.json",
            ):
                self.assertFalse((root / name).exists())
            result = session.finish()
            self.assertTrue(result.observation_ledger.is_file())
            self.assertTrue(result.report.is_file())
            self.assertTrue(result.corpus.is_file())

    def test_capture_rollback_keeps_replaced_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            session.observe(frame("red"), "Narrator", "A line.")
            replacement = root / "replacement.json"
            replacement.write_bytes(b"other writer")
            original_write = live_replay_capture._write_payload_no_replace
            calls = 0

            def replace_then_fail(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    replacement.replace(root / "observation-ledger.json")
                    raise LiveReplayCaptureError("injected later failure")
                return original_write(path, payload)

            with patch(
                "vntts.live_replay_capture._write_payload_no_replace",
                side_effect=replace_then_fail,
            ):
                with self.assertRaisesRegex(LiveReplayCaptureError, "later failure"):
                    session.finish()

            self.assertEqual(
                (root / "observation-ledger.json").read_bytes(), b"other writer"
            )

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

    def test_capture_rejects_mutation_after_finish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            session = LiveReplayCaptureSession(root)
            session.observe(frame("red"), "Narrator", "A line.")
            result = session.finish()
            frames = sorted((root / "frames").iterdir())

            with self.assertRaisesRegex(LiveReplayCaptureError, "already finished"):
                session.note_uncertain_observation(frame("blue"))

            self.assertEqual(sorted((root / "frames").iterdir()), frames)
            self.assertEqual(
                json.loads(result.observation_ledger.read_text(encoding="utf-8"))[
                    "observation_count"
                ],
                1,
            )


if __name__ == "__main__":
    unittest.main()
