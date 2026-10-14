import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

import vntts.authoring.audio_event_review as review_module
from tests.authoring_fixtures import (
    publish_audio_event_review_fixture as publish,
)
from tests.authoring_fixtures import (
    write_audio_event_queue as write_queue,
)
from tests.authoring_fixtures import (
    write_audio_event_source_story as write_source_story,
)
from tests.symlink_support import symlink_or_skip
from vntts.authoring.audio_event_review import (
    AudioEventReviewError,
    load_audio_event_review,
    publish_source_audio_event_review,
    record_audio_event_review_decision,
)
from vntts.authoring.cli import main as authoring_main


class AudioEventReviewTest(unittest.TestCase):
    def test_candidate_metadata_decodes_the_captured_audio(self):
        with TemporaryDirectory() as directory:
            result, _queue, _source = publish(Path(directory))
            payload = result.audio.read_bytes()
            probe = review_module.probe_pcm16_mono_wav

            def replace_before_decode(source):
                result.audio.write_bytes(b"RIFF")
                try:
                    return probe(source)
                finally:
                    result.audio.write_bytes(payload)

            with patch.object(
                review_module, "probe_pcm16_mono_wav", replace_before_decode
            ):
                loaded = load_audio_event_review(result.directory)
            self.assertEqual(loaded.audio_sha256, result.audio_sha256)
            self.assertEqual(loaded.review_id, result.review_id)

    def test_loader_and_publisher_races_raise_review_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root)
            original = review_module._validate_review_document

            def validate_then_mutate(*args):
                original(*args)
                path = result.directory / "review.json"
                path.write_bytes(path.read_bytes() + b" ")

            with patch.object(
                review_module, "_validate_review_document", validate_then_mutate
            ):
                with self.assertRaisesRegex(AudioEventReviewError, "changed"):
                    load_audio_event_review(result.directory)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = load_audio_event_review

            def load_then_mutate(path):
                loaded = original(path)
                queue = root / "queue.jsonl"
                queue.write_bytes(queue.read_bytes() + b"\n")
                return loaded

            with patch.object(
                review_module, "load_audio_event_review", load_then_mutate
            ):
                with self.assertRaisesRegex(AudioEventReviewError, "changed"):
                    publish(root)
            self.assertFalse((root / "review").exists())
            self.assertFalse(list(root.glob(".review.staging-*")))

    def test_persisted_decision_rejects_non_strings_with_review_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root)
            record_audio_event_review_decision(result.directory, "accept")
            path = result.directory / "decision.json"
            document = json.loads(path.read_text())
            for decision in ([], {}, None, True, 1):
                with self.subTest(decision=decision):
                    document["decision"] = decision
                    path.write_text(json.dumps(document, sort_keys=True))
                    with self.assertRaisesRegex(
                        AudioEventReviewError, "decision is invalid"
                    ):
                        load_audio_event_review(result.directory)

    def test_publication_rejects_symlinked_authority_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            queue_id = write_queue(queue)
            story = write_source_story(root / "story-index.jsonl")
            audio = root / "source.wav"
            samples = np.zeros(1_200, dtype=np.float32)
            samples[300:340] = 0.4
            write_pcm16_wav(audio, samples, 24_000)

            inputs = {
                "queue_path": queue,
                "source_story_index": story,
                "source_audio": audio,
            }
            for field, source in inputs.items():
                with self.subTest(input=field):
                    alias = root / f"{field}-alias"
                    symlink_or_skip(alias, source)
                    with self.assertRaisesRegex(AudioEventReviewError, "unavailable"):
                        publish_source_audio_event_review(
                            output=root / f"review-{field}",
                            queue_id=queue_id,
                            source_line_id="reverse1999:200308:6",
                            source_speaker="Kanjira",
                            source_event="play_activityvoc_hero3071_660",
                            source_bank="activityvoc_hero3071molu1_3_part02.bnk",
                            source_media_id=410389900,
                            source_audio_id="610008734",
                            **(inputs | {field: alias}),
                        )

    def test_publication_rejects_dangling_symlink_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "redirected"
            symlink_or_skip(root / "review", target, target_is_directory=True)
            with self.assertRaisesRegex(AudioEventReviewError, "output exists"):
                publish(root)
            self.assertFalse(target.exists())

    def test_public_loader_rejects_symlink_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root)
            link = root / "review-link"
            symlink_or_skip(link, result.directory, target_is_directory=True)
            with self.assertRaisesRegex(AudioEventReviewError, "unavailable"):
                load_audio_event_review(link)

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

    def test_public_reader_rejects_bool_and_float_schema_versions(self):
        for value in (True, 1.0):
            with (
                self.subTest(review_schema_version=value),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                result, _queue, _audio = publish(root)
                review_path = result.directory / "review.json"
                document = json.loads(review_path.read_text())
                document["schema_version"] = value
                review_path.write_text(json.dumps(document))
                with self.assertRaisesRegex(AudioEventReviewError, "schema"):
                    load_audio_event_review(result.directory)

            with (
                self.subTest(decision_schema_version=value),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                result, _queue, _audio = publish(root)
                record_audio_event_review_decision(result.directory, "accept")
                decision_path = result.directory / "decision.json"
                document = json.loads(decision_path.read_text())
                document["schema_version"] = value
                decision_path.write_text(json.dumps(document))
                with self.assertRaisesRegex(AudioEventReviewError, "schema"):
                    load_audio_event_review(result.directory)

    def test_public_reader_rejects_noncanonical_audio_metadata_numbers(self):
        for field, value in (("sample_rate", 24_000.0), ("sample_count", 1_200.0)):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                root = Path(directory)
                result, _queue, _audio = publish(root)
                review_path = result.directory / "review.json"
                document = json.loads(review_path.read_text())
                document["candidate"][field] = value
                review_path.write_text(json.dumps(document))
                with self.assertRaisesRegex(AudioEventReviewError, "metadata"):
                    load_audio_event_review(result.directory)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            result, _queue, _audio = publish(root, sample_count=24_000)
            review_path = result.directory / "review.json"
            document = json.loads(review_path.read_text())
            document["candidate"]["duration_seconds"] = True
            review_path.write_text(json.dumps(document))
            with self.assertRaisesRegex(AudioEventReviewError, "metadata"):
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

    def test_interrupted_publication_removes_staging_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.jsonl"
            queue_id = write_queue(queue)
            story = write_source_story(root / "story-index.jsonl")
            audio = root / "source.wav"
            samples = np.zeros(1_200, dtype=np.float32)
            samples[300:340] = 0.4
            write_pcm16_wav(audio, samples, 24_000)

            with (
                patch(
                    "vntts.authoring.audio_event_review.assert_authority_snapshot",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
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

            self.assertFalse((root / "review").exists())
            self.assertEqual(list(root.glob(".review.staging-*")), [])

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
