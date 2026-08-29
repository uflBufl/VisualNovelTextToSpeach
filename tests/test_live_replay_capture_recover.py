import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.live_sequence import write_live_sequence_plan

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialog_capture import (
    CapturedDialogFrame,
    detect_standalone_ellipsis_frame,
)
from vntts.live_replay_capture import (
    LiveReplayCaptureSession,
    ellipsis_speaker_hint,
)
from vntts.live_replay_capture_recover import recover_live_replay_capture
from vntts.live_replay_sequence_seal import (
    SequenceReplaySealError,
    seal_sequence_replay,
)


class LiveReplayCaptureRecoverTest(unittest.TestCase):
    @staticmethod
    def write_story(root, lines):
        story = root / "story.jsonl"
        records = [
            {
                "record_type": "metadata",
                "schema": "vntts.story-index",
                "schema_version": 1,
                "line_count": len(lines),
            },
            *({"record_type": "line", "kind": "dialogue", **line} for line in lines),
        ]
        story.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return story

    @staticmethod
    def write_plan(root, story, events):
        plan = root / "live-sequence.json"
        write_live_sequence_plan(
            plan,
            {
                "game_id": "capture-recovery-test",
                "producer": {"name": "tests", "version": "1"},
                "source_extract_sha256": hashlib.sha256(b"fixture").hexdigest(),
                "chapters": [
                    {
                        "chapter": "1",
                        "entry_event_ids": [events[0]["event_id"]],
                        "events": events,
                    }
                ],
            },
            story,
        )
        return plan

    @staticmethod
    def frame(marker, *, ellipsis=False):
        image = Image.new("RGB", (80, 40), "black")
        image.putpixel((0, 0), marker)
        draw = ImageDraw.Draw(image)
        if ellipsis:
            for left in (2, 8, 14):
                draw.rectangle((left, 24, left + 1, 25), fill="white")
        else:
            draw.rectangle((12, 22, 48, 28), fill="white")
        return CapturedDialogFrame(image, 0.0)

    def capture(self, root, story, observations):
        resolver = ChapterVoicePreloader.load_optional(story)
        session = LiveReplayCaptureSession(
            root / "raw",
            story_resolver=resolver,
            story_index_path=story,
            story_index_sha256=sha256_file(story),
        )
        recognition = {}
        for value in observations:
            marker, character, text, *options = value
            frame = self.frame(marker, ellipsis=bool(options and options[0]))
            session.observe(frame, character, text)
            recognition[marker] = (character, text)
        result = session.finish()

        def recognize(frame):
            character, text = recognition[frame.image.getpixel((0, 0))]
            if detect_standalone_ellipsis_frame(frame.image):
                return ellipsis_speaker_hint(character, text, resolver), "..."
            return character, text

        return result, recognize

    def test_recovers_explicit_speech_silent_run_without_rewriting_raw(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                {
                    "line_id": "story:1",
                    "chapter": "1",
                    "sequence": 1,
                    "speaker": "Ada",
                    "text": "First line.",
                },
                {
                    "line_id": "story:2",
                    "chapter": "1",
                    "sequence": 3,
                    "speaker": "Bea",
                    "text": "Second line.",
                },
            ]
            story = self.write_story(root, lines)
            plan = self.write_plan(
                root,
                story,
                [
                    {
                        "event_id": "event-1",
                        "sequence": 1,
                        "kind": "speech",
                        "control": "automatic",
                        "successors": ["event-silent"],
                        "line_id": "story:1",
                    },
                    {
                        "event_id": "event-silent",
                        "sequence": 2,
                        "kind": "silent",
                        "control": "automatic",
                        "successors": ["event-silent-2"],
                    },
                    {
                        "event_id": "event-silent-2",
                        "sequence": 3,
                        "kind": "silent",
                        "control": "automatic",
                        "successors": ["event-2"],
                    },
                    {
                        "event_id": "event-2",
                        "sequence": 4,
                        "kind": "speech",
                        "control": "terminal",
                        "successors": [],
                        "line_id": "story:2",
                    },
                ],
            )
            captured, recognize = self.capture(
                root,
                story,
                (
                    ((255, 0, 0), "Ada", "First line."),
                    ((0, 255, 0), "Narrator", "Ada nameplate noise", True),
                    ((0, 0, 255), "Narrator", "Bea nameplate noise", True),
                    ((255, 0, 255), "Narrator", "Second"),
                    ((255, 255, 0), "Bea", "Second line."),
                ),
            )
            raw_sha256 = sha256_file(captured.corpus)
            with self.assertRaisesRegex(
                SequenceReplaySealError, "recover one explicit sequence"
            ):
                seal_sequence_replay(
                    captured.corpus,
                    root / "unsafe-seal",
                    story_index=story,
                    sequence_plan=plan,
                    recognizer=recognize,
                )

            recovered = recover_live_replay_capture(
                captured.corpus,
                root / "recovered",
                story_index=story,
                sequence_plan=plan,
                minimum_events=4,
            )
            document = json.loads(recovered.corpus.read_text(encoding="utf-8"))
            analysis = json.loads(recovered.report.read_text(encoding="utf-8"))
            sealed = seal_sequence_replay(
                recovered.corpus,
                root / "sealed",
                story_index=story,
                sequence_plan=plan,
                recognizer=recognize,
                interval_seconds=0.002,
                timeout_seconds=5,
            )

            self.assertTrue(recovered.sufficient)
            self.assertTrue(recovered.contains_silent)
            self.assertEqual(recovered.event_count, 4)
            self.assertEqual(
                [item["line_id"] for item in document["dialogue"]],
                ["story:1", "capture:2", "capture:3", "story:2"],
            )
            self.assertEqual(document["capture"]["unresolved_observation_count"], 0)
            self.assertEqual(analysis["raw_observation_count"], 5)
            self.assertEqual(
                [
                    item["speaker_hint"]
                    for item in analysis["visually_classified_ellipsis_observations"]
                ],
                ["Ada", "Bea"],
            )
            self.assertEqual(
                [
                    item["observation_index"]
                    for item in analysis["absorbed_transient_observations"]
                ],
                [4],
            )
            self.assertEqual(sha256_file(captured.corpus), raw_sha256)
            self.assertTrue(sealed.corpus.is_file())

    def test_insufficient_branch_capture_publishes_only_analysis(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                {
                    "line_id": f"story:{index}",
                    "chapter": "1",
                    "sequence": index,
                    "speaker": speaker,
                    "text": text,
                }
                for index, speaker, text in (
                    (1, "Ada", "First line."),
                    (2, "Bea", "Missing middle line."),
                    (3, "Cora", "Third line."),
                )
            ]
            story = self.write_story(root, lines)
            plan = self.write_plan(
                root,
                story,
                [
                    {
                        "event_id": "event-1",
                        "sequence": 1,
                        "kind": "speech",
                        "control": "manual",
                        "successors": ["event-2", "event-3"],
                        "line_id": "story:1",
                    },
                    {
                        "event_id": "event-2",
                        "sequence": 2,
                        "kind": "speech",
                        "control": "terminal",
                        "successors": [],
                        "line_id": "story:2",
                    },
                    {
                        "event_id": "event-3",
                        "sequence": 3,
                        "kind": "speech",
                        "control": "terminal",
                        "successors": [],
                        "line_id": "story:3",
                    },
                ],
            )
            captured, _recognize = self.capture(
                root,
                story,
                (
                    ((255, 0, 0), "Ada", "First line."),
                    ((0, 0, 255), "Cora", "Third line."),
                ),
            )

            result = recover_live_replay_capture(
                captured.corpus,
                root / "insufficient",
                story_index=story,
                sequence_plan=plan,
                minimum_events=2,
                require_silent=False,
            )
            report = json.loads(result.report.read_text(encoding="utf-8"))

            self.assertFalse(result.sufficient)
            self.assertEqual(result.event_count, 1)
            self.assertIsNone(result.corpus)
            self.assertFalse((result.directory / "corpus.json").exists())
            self.assertFalse(report["sufficient"])
            self.assertEqual(report["recommended_follow_up_capture"]["events"], [])

            raw = json.loads(captured.corpus.read_text(encoding="utf-8"))
            frame = captured.directory / raw["dialogue"][0]["frames"][0]["path"]
            frame.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "frame checksum changed"):
                recover_live_replay_capture(
                    captured.corpus,
                    root / "changed",
                    story_index=story,
                    sequence_plan=plan,
                    minimum_events=1,
                    require_silent=False,
                )


if __name__ == "__main__":
    unittest.main()
