import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav
from vntts_artifacts.voice_generation_queue import write_voice_generation_queue

from vntts.authoring.audio_event_review import (
    AudioEventReviewError,
    load_audio_event_review,
    publish_source_audio_event_review,
    record_audio_event_review_decision,
)
from vntts.authoring.cli import main as authoring_main


def write_queue(path, text="Tsk!"):
    digest = hashlib.sha256(text.encode()).hexdigest()
    queue_id = f"line:tsk:{digest[:16]}"
    write_voice_generation_queue(
        path,
        {"game": "Synthetic", "language": "en"},
        [
            {
                "record_type": "generation_item",
                "queue_id": queue_id,
                "line_id": "line:tsk",
                "text_sha256": digest,
                "text": text,
                "speaker": "Poacher I",
                "voice_character": "Poacher I",
                "action": "manual_review",
                "state": "pending",
            }
        ],
    )
    return queue_id


def write_source_story(path):
    text = "Tsk!"
    path.write_text(
        json.dumps(
            {
                "record_type": "line",
                "line_id": "reverse1999:200308:6",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "speaker": "Kanjira",
                "source_audio_id": "610008734",
                "source_audio_status": "available",
                "source_event": "play_activityvoc_hero3071_660",
                "source_bank": "activityvoc_hero3071molu1_3_part02.bnk",
                "source_media_ids": [410389900],
            }
        )
        + "\n"
    )
    return path


def publish(root, *, text="Tsk!"):
    queue = root / "queue.jsonl"
    queue_id = write_queue(queue, text)
    audio = root / "source.wav"
    samples = np.zeros(1_200, dtype=np.float32)
    samples[300:340] = 0.4
    write_pcm16_wav(audio, samples, 24_000)
    output = root / "review"
    story = write_source_story(root / "story-index.jsonl")
    result = publish_source_audio_event_review(
        queue,
        queue_id,
        story,
        audio,
        output,
        source_line_id="reverse1999:200308:6",
        source_speaker="Kanjira",
        source_event="play_activityvoc_hero3071_660",
        source_bank="activityvoc_hero3071molu1_3_part02.bnk",
        source_media_id=410389900,
        source_audio_id="610008734",
    )
    return result, queue, audio


class AudioEventReviewTest(unittest.TestCase):
    def test_publishes_self_contained_speaker_neutral_tongue_click(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, queue, audio = publish(root)
            queue.unlink()
            audio.unlink()
            loaded = load_audio_event_review(result.directory)
            document = json.loads((result.directory / "review.json").read_text())

        self.assertEqual(loaded.queue_id, "line:tsk:27d02801f93c9036")
        self.assertIsNone(loaded.decision)
        self.assertEqual(document["candidate"]["sample_rate"], 24_000)
        self.assertEqual(document["candidate"]["sample_count"], 1_200)
        self.assertFalse(document["candidate"]["source"]["speaker_identity_claim"])
        self.assertIsNone(document["candidate"]["source"]["synthesis_voice_character"])
        self.assertEqual(
            document["audio_event_plan"]["events"][0]["kind"], "tongue-click"
        )

    def test_refuses_ordinary_speech_and_non_tsk_events(self):
        for text in ("Ordinary line.", "*gasp*"):
            with self.subTest(text=text), TemporaryDirectory() as directory:
                root = Path(directory)
                queue = root / "queue.jsonl"
                queue_id = write_queue(queue, text)
                audio = root / "source.wav"
                story = write_source_story(root / "story-index.jsonl")
                write_pcm16_wav(audio, np.zeros(200, dtype=np.float32), 24_000)
                with self.assertRaisesRegex(
                    AudioEventReviewError,
                    "requires one exact Tsk|does not require audio-event review",
                ):
                    publish_source_audio_event_review(
                        queue,
                        queue_id,
                        story,
                        audio,
                        root / "review",
                        source_line_id="source-line",
                        source_speaker="Source",
                        source_event="play_source",
                        source_bank="source.bnk",
                        source_media_id=1,
                    )

    def test_rejects_audio_and_review_tamper(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root)
            result.audio.write_bytes(result.audio.read_bytes() + b"tamper")
            with self.assertRaisesRegex(AudioEventReviewError, "audio changed"):
                load_audio_event_review(result.directory)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root)
            review_path = result.directory / "review.json"
            document = json.loads(review_path.read_text())
            document["candidate"]["source"]["source_speaker"] = "Poacher I"
            review_path.write_text(json.dumps(document))
            with self.assertRaisesRegex(AudioEventReviewError, "identity changed"):
                load_audio_event_review(result.directory)

    def test_rejects_unbound_source_story_claim_and_silent_audio(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            queue_id = write_queue(queue)
            story = write_source_story(root / "story-index.jsonl")
            record = json.loads(story.read_text())
            record["source_event"] = "different_event"
            story.write_text(json.dumps(record) + "\n")
            audio = root / "source.wav"
            samples = np.zeros(1_200, dtype=np.float32)
            samples[300:340] = 0.4
            write_pcm16_wav(audio, samples, 24_000)
            with self.assertRaisesRegex(AudioEventReviewError, "source_event"):
                publish_source_audio_event_review(
                    queue,
                    queue_id,
                    story,
                    audio,
                    root / "review",
                    source_line_id="reverse1999:200308:6",
                    source_speaker="Kanjira",
                    source_event="play_activityvoc_hero3071_660",
                    source_bank="activityvoc_hero3071molu1_3_part02.bnk",
                    source_media_id=410389900,
                    source_audio_id="610008734",
                )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            queue_id = write_queue(queue)
            story = write_source_story(root / "story-index.jsonl")
            audio = root / "source.wav"
            write_pcm16_wav(audio, np.zeros(1_200, dtype=np.float32), 24_000)
            with self.assertRaisesRegex(AudioEventReviewError, "silent"):
                publish_source_audio_event_review(
                    queue,
                    queue_id,
                    story,
                    audio,
                    root / "review",
                    source_line_id="reverse1999:200308:6",
                    source_speaker="Kanjira",
                    source_event="play_activityvoc_hero3071_660",
                    source_bank="activityvoc_hero3071molu1_3_part02.bnk",
                    source_media_id=410389900,
                    source_audio_id="610008734",
                )

    def test_terminal_decision_is_idempotent_and_no_replace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, queue, audio = publish(root)
            queue_before = queue.read_bytes()
            audio_before = audio.read_bytes()
            first = record_audio_event_review_decision(result.directory, "accept")
            decision_before = (result.directory / "decision.json").read_bytes()
            repeated = record_audio_event_review_decision(result.directory, "accept")

            self.assertEqual(first.decision, "accept")
            self.assertEqual(repeated.decision, "accept")
            self.assertEqual(
                (result.directory / "decision.json").read_bytes(), decision_before
            )
            self.assertEqual(queue.read_bytes(), queue_before)
            self.assertEqual(audio.read_bytes(), audio_before)
            with self.assertRaisesRegex(AudioEventReviewError, "already decided"):
                record_audio_event_review_decision(result.directory, "reject")

    def test_decision_rejects_mutated_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root)
            queue_path = result.directory / "queue.jsonl"
            queue_path.write_bytes(queue_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(AudioEventReviewError, "queue changed"):
                record_audio_event_review_decision(result.directory, "accept")

    def test_publication_is_no_replace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            publish(root)
            queue = root / "queue.jsonl"
            audio = root / "source.wav"
            queue_id = "line:tsk:27d02801f93c9036"
            story = root / "story-index.jsonl"
            with self.assertRaisesRegex(AudioEventReviewError, "output exists"):
                publish_source_audio_event_review(
                    queue,
                    queue_id,
                    story,
                    audio,
                    root / "review",
                    source_line_id="source-line",
                    source_speaker="Source",
                    source_event="play_source",
                    source_bank="source.bnk",
                    source_media_id=1,
                )

    def test_cli_publish_status_and_decide(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            queue_id = write_queue(queue)
            audio = root / "source.wav"
            samples = np.zeros(1_200, dtype=np.float32)
            samples[300:340] = 0.4
            write_pcm16_wav(audio, samples, 24_000)
            story = write_source_story(root / "story-index.jsonl")
            output = root / "review"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    [
                        "audio-event-review-publish",
                        str(queue),
                        queue_id,
                        str(story),
                        str(audio),
                        "--output",
                        str(output),
                        "--source-line-id",
                        "reverse1999:200308:6",
                        "--source-speaker",
                        "Kanjira",
                        "--source-event",
                        "play_activityvoc_hero3071_660",
                        "--source-bank",
                        "activityvoc_hero3071molu1_3_part02.bnk",
                        "--source-media-id",
                        "410389900",
                        "--source-audio-id",
                        "610008734",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["decision"], None)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(["audio-event-review-status", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["queue_id"], queue_id)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    ["audio-event-review-decide", str(output), "reject"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["decision"], "reject")


if __name__ == "__main__":
    unittest.main()
