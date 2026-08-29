import hashlib
import json
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import text_sha256, write_generated_audio_manifest
from vntts_artifacts.live_sequence import write_live_sequence_plan

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.dialog_capture import CapturedDialogFrame
from vntts.live_replay import LiveReplayRunner, load_live_replay_corpus
from vntts.live_replay_capture import LiveReplayCaptureSession
from vntts.live_replay_sequence_seal import (
    SequenceReplaySealError,
    seal_sequence_replay,
)


class LiveReplaySequenceSealTest(unittest.TestCase):
    @staticmethod
    def write_story(root, lines):
        story = root / "story.jsonl"
        records = [
            {
                "record_type": "metadata",
                "schema": "vntts.story-index",
                "schema_version": 1,
                "line_count": len(lines),
                "source_audio_completion": "duration-seconds",
            },
            *({"record_type": "line", "kind": "dialogue", **line} for line in lines),
        ]
        story.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )
        return story

    @staticmethod
    def write_plan(root, story, events, *, entries=None):
        plan = root / "live-sequence.json"
        write_live_sequence_plan(
            plan,
            {
                "game_id": "sequence-seal-test",
                "producer": {"name": "tests", "version": "1"},
                "source_extract_sha256": hashlib.sha256(b"fixture").hexdigest(),
                "chapters": [
                    {
                        "chapter": "1",
                        "entry_event_ids": entries or [events[0]["event_id"]],
                        "events": events,
                    }
                ],
            },
            story,
        )
        return plan

    @staticmethod
    def frame(marker):
        image = Image.new("RGB", (80, 40), "black")
        image.putpixel((0, 0), marker)
        ImageDraw.Draw(image).rectangle((12, 22, 48, 28), fill="white")
        return CapturedDialogFrame(image, 0.0)

    def capture(self, root, story, observations):
        resolver = ChapterVoicePreloader.load_optional(story)
        capture = root / "raw-capture"
        session = LiveReplayCaptureSession(
            capture,
            story_resolver=resolver,
            story_index_path=story,
            story_index_sha256=sha256_file(story),
        )
        markers = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))
        recognition = {}
        for marker, observation in zip(markers, observations):
            session.observe(self.frame(marker), *observation)
            recognition[marker] = observation
        result = session.finish()

        def recognize(frame):
            return recognition[frame.image.getpixel((0, 0))]

        return result, recognize

    @staticmethod
    def generated_manifest(root, line):
        audio = root / "generated.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24_000)
            output.writeframes(b"\0\0\1\0\0\0")
        manifest = root / "generated.json"
        write_generated_audio_manifest(
            manifest,
            {"fixture": "sequence-seal-test"},
            [
                {
                    "line_id": line["line_id"],
                    "text_sha256": text_sha256(line["text"]),
                    "audio": audio.name,
                    "audio_format": "wav-pcm16-mono",
                    "audio_sha256": sha256_file(audio),
                    "sample_rate": 24_000,
                    "sample_count": 3,
                }
            ],
        )
        return manifest

    def test_seals_silent_and_text_only_mapping_with_all_audio_routes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                {
                    "line_id": "story:1",
                    "chapter": "1",
                    "sequence": 1,
                    "speaker": "Ada",
                    "text": "The game already voices this.",
                    "source_audio_status": "available",
                    "source_audio_duration_seconds": 0.001,
                },
                {
                    "line_id": "story:2",
                    "chapter": "1",
                    "sequence": 3,
                    "speaker": "Bea",
                    "text": "The approved waveform is ready.",
                    "source_audio_status": "absent",
                },
                {
                    "line_id": "story:3",
                    "chapter": "1",
                    "sequence": 4,
                    "speaker": "Cora",
                    "text": "This line needs live speech.",
                    "source_audio_status": "absent",
                },
            ]
            story = self.write_story(root, lines)
            events = [
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
                    "successors": ["event-2"],
                },
                {
                    "event_id": "event-2",
                    "sequence": 3,
                    "kind": "speech",
                    "control": "automatic",
                    "successors": ["event-3"],
                    "line_id": "story:2",
                },
                {
                    "event_id": "event-3",
                    "sequence": 4,
                    "kind": "speech",
                    "control": "terminal",
                    "successors": [],
                    "line_id": "story:3",
                },
            ]
            plan = self.write_plan(root, story, events)
            captured, recognize = self.capture(
                root,
                story,
                (
                    ("Ada", "The game already voices this."),
                    ("Narrator", "..."),
                    ("Narrator", "The approved waveform is ready."),
                    ("Cora", "This line needs live speech."),
                ),
            )
            raw_sha256 = sha256_file(captured.corpus)
            manifest = self.generated_manifest(root, lines[1])

            result = seal_sequence_replay(
                captured.corpus,
                root / "sealed",
                story_index=story,
                sequence_plan=plan,
                generated_audio_manifest=manifest,
                recognizer=recognize,
                interval_seconds=0.002,
                timeout_seconds=5,
            )
            corpus = json.loads(result.corpus.read_text(encoding="utf-8"))
            review = json.loads(result.review.read_text(encoding="utf-8"))
            report = LiveReplayRunner(
                load_live_replay_corpus(result.corpus),
                recognizer=recognize,
                interval_seconds=0.002,
                timeout_seconds=5,
            ).run()
            raw_sha256_after = sha256_file(captured.corpus)
            generated_entries = json.loads(
                (result.directory / "generated" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )["entries"]

        self.assertEqual(raw_sha256_after, raw_sha256)
        self.assertTrue(result.operator_review_required)
        self.assertEqual(
            [item["event_id"] for item in corpus["dialogue"]],
            ["event-1", "event-silent", "event-2", "event-3"],
        )
        self.assertEqual(
            [item["line_id"] for item in corpus["dialogue"]],
            ["story:1", None, "story:2", "story:3"],
        )
        self.assertFalse(corpus["dialogue"][1]["expect_playback"])
        self.assertEqual(
            report["route_sources"], ["game", "generated", "live:replay-live-tts"]
        )
        self.assertTrue(report["successful"], report)
        self.assertEqual(
            review["measured_baseline"]["event_ids"],
            ["event-1", "event-silent", "event-2", "event-3"],
        )
        self.assertEqual(
            [item["mapping_method"] for item in review["mappings"]],
            [
                "exact-line-id",
                "unique-silent-frontier",
                "unique-text-frontier",
                "exact-line-id",
            ],
        )
        self.assertEqual([entry["line_id"] for entry in generated_entries], ["story:2"])

    def test_rejects_branch_ambiguous_text_and_changed_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repeated = "The same line."
            lines = [
                {
                    "line_id": f"story:{index}",
                    "chapter": "1",
                    "sequence": index,
                    "speaker": speaker,
                    "text": text,
                }
                for index, speaker, text in (
                    (1, "Ada", "Choose now."),
                    (2, "Bea", repeated),
                    (3, "Cora", repeated),
                )
            ]
            story = self.write_story(root, lines)
            events = [
                {
                    "event_id": "event-1",
                    "sequence": 1,
                    "kind": "speech",
                    "control": "automatic",
                    "successors": ["choice"],
                    "line_id": "story:1",
                },
                {
                    "event_id": "choice",
                    "sequence": 2,
                    "kind": "choice",
                    "control": "manual",
                    "successors": ["left", "right"],
                },
                {
                    "event_id": "left",
                    "sequence": 3,
                    "kind": "speech",
                    "control": "terminal",
                    "successors": [],
                    "line_id": "story:2",
                },
                {
                    "event_id": "right",
                    "sequence": 4,
                    "kind": "speech",
                    "control": "terminal",
                    "successors": [],
                    "line_id": "story:3",
                },
            ]
            plan = self.write_plan(root, story, events)
            captured, recognize = self.capture(
                root,
                story,
                (("Ada", "Choose now."), ("Narrator", repeated)),
            )
            with self.assertRaisesRegex(
                SequenceReplaySealError, "ambiguous or skipped"
            ):
                seal_sequence_replay(
                    captured.corpus,
                    root / "branch-sealed",
                    story_index=story,
                    sequence_plan=plan,
                    recognizer=recognize,
                )

            story.write_bytes(story.read_bytes() + b"\n")
            with self.assertRaisesRegex(SequenceReplaySealError, "not bound"):
                seal_sequence_replay(
                    captured.corpus,
                    root / "changed-sealed",
                    story_index=story,
                    sequence_plan=plan,
                    recognizer=recognize,
                )

    def test_shadow_seal_counts_line_less_silent_event_and_confirmed_keys(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                {
                    "line_id": "story:1",
                    "chapter": "1",
                    "sequence": 1,
                    "speaker": "Ada",
                    "text": "First line.",
                    "source_audio_status": "available",
                    "source_audio_duration_seconds": 0.001,
                },
                {
                    "line_id": "story:2",
                    "chapter": "1",
                    "sequence": 3,
                    "speaker": "Bea",
                    "text": "Second line.",
                    "source_audio_status": "absent",
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
                        "successors": ["event-2"],
                    },
                    {
                        "event_id": "event-2",
                        "sequence": 3,
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
                (("Ada", "First line."), ("Narrator", "..."), ("Bea", "Second line.")),
            )

            result = seal_sequence_replay(
                captured.corpus,
                root / "shadow-sealed",
                story_index=story,
                sequence_plan=plan,
                mode="shadow",
                recognizer=recognize,
                interval_seconds=0.002,
                timeout_seconds=5,
            )
            review = json.loads(result.review.read_text(encoding="utf-8"))

        self.assertEqual(
            review["measured_baseline"]["event_ids"],
            ["event-1", "event-silent", "event-2"],
        )
        self.assertEqual(
            review["measured_baseline"]["line_ids"], ["story:1", None, "story:2"]
        )
        self.assertEqual(review["measured_baseline"]["key_dispatch_attempts"], 2)
        self.assertEqual(review["measured_baseline"]["confirmed_key_dispatches"], 2)

    def test_rejects_changed_frame_and_symlinked_input(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lines = [
                {
                    "line_id": "story:1",
                    "chapter": "1",
                    "sequence": 1,
                    "speaker": "Ada",
                    "text": "One line.",
                }
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
                        "control": "terminal",
                        "successors": [],
                        "line_id": "story:1",
                    }
                ],
            )
            captured, recognize = self.capture(root, story, (("Ada", "One line."),))
            report = json.loads(captured.report.read_text(encoding="utf-8"))
            report["dialogue_count"] = 2
            captured.report.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(SequenceReplaySealError, "disagrees"):
                seal_sequence_replay(
                    captured.corpus,
                    root / "report-sealed",
                    story_index=story,
                    sequence_plan=plan,
                    recognizer=recognize,
                )
            report["dialogue_count"] = 1
            captured.report.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            report["dialogue"][0]["text"] = "Changed review text."
            captured.report.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(SequenceReplaySealError, "ledger disagrees"):
                seal_sequence_replay(
                    captured.corpus,
                    root / "ledger-sealed",
                    story_index=story,
                    sequence_plan=plan,
                    recognizer=recognize,
                )
            report["dialogue"][0]["text"] = "One line."
            captured.report.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
            raw = json.loads(captured.corpus.read_text(encoding="utf-8"))
            frame_path = captured.directory / raw["dialogue"][0]["frames"][0]["path"]
            frame_path.write_bytes(b"changed")
            with self.assertRaisesRegex(SequenceReplaySealError, "checksum changed"):
                seal_sequence_replay(
                    captured.corpus,
                    root / "frame-sealed",
                    story_index=story,
                    sequence_plan=plan,
                    recognizer=recognize,
                )

            alias = root / "story-alias.jsonl"
            alias.symlink_to(story)
            with self.assertRaisesRegex(
                SequenceReplaySealError, "must not be a symlink"
            ):
                seal_sequence_replay(
                    captured.corpus,
                    root / "alias-sealed",
                    story_index=alias,
                    sequence_plan=plan,
                    recognizer=recognize,
                )


if __name__ == "__main__":
    unittest.main()
