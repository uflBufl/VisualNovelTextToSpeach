import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.test_authoring_audio_event_review import publish
from vntts.authoring.audio_event_composition import (
    AudioEventCompositionError,
    load_audio_event_composition,
    publish_audio_event_composition,
    record_audio_event_composition_decision,
)
from vntts.authoring.audio_event_review import record_audio_event_review_decision
from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.cli import main as authoring_main


class AudioEventCompositionTest(unittest.TestCase):
    def test_publishes_exact_speaker_neutral_event_and_is_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, queue, source_audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"

            created = publish_audio_event_composition(review.directory, output)
            repeated = publish_audio_event_composition(review.directory, output)
            queue.unlink()
            source_audio.unlink()
            loaded = load_audio_event_composition(output)
            document = json.loads((output / "composition.json").read_text())

            self.assertTrue(created.created)
            self.assertFalse(repeated.created)
            self.assertEqual(created.composition_id, loaded.composition_id)
            self.assertEqual(created.audio_sha256, review.audio_sha256)
            self.assertEqual(created.audio.read_bytes(), review.audio.read_bytes())
            self.assertEqual(document["composition"]["byte_transform"], "exact-copy")
            self.assertFalse(document["composition"]["speaker_identity_claim"])
            self.assertIsNone(document["composition"]["synthesis_provider"])
            self.assertIsNone(document["composition"]["synthesis_voice_character"])

    def test_requires_acceptance_and_rejects_audio_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _source_audio = publish(root)
            with self.assertRaisesRegex(
                AudioEventCompositionError, "requires an accepted"
            ):
                publish_audio_event_composition(review.directory, root / "unaccepted")
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            published = publish_audio_event_composition(review.directory, output)
            published.audio.write_bytes(b"changed")
            with self.assertRaisesRegex(
                AudioEventCompositionError, "authority changed|Invalid WAV|RIFF"
            ):
                load_audio_event_composition(output)

    def test_recomputed_composition_cannot_forge_source_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _source_audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            publish_audio_event_composition(review.directory, output)
            path = output / "composition.json"
            document = json.loads(path.read_text())
            document["source"]["source_speaker"] = "Forged"
            identity = {
                key: value
                for key, value in document.items()
                if key
                not in {
                    "composition_id",
                    "created_at",
                    "review",
                    "review_decision",
                    "queue",
                    "final_audio",
                    "sample_rate",
                    "sample_count",
                    "duration_seconds",
                    "peak",
                }
            }
            document["composition_id"] = canonical_document_sha256(identity)
            path.write_text(json.dumps(document, sort_keys=True))
            with self.assertRaisesRegex(
                AudioEventCompositionError, "authority changed"
            ):
                load_audio_event_composition(output)

    def test_final_decision_is_exact_idempotent_and_cli_visible(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _source_audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(
                    [
                        "audio-event-composition-publish",
                        str(review.directory),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["created"])

            first = record_audio_event_composition_decision(output, "approved")
            repeated = record_audio_event_composition_decision(output, "approved")
            self.assertEqual(first.decision, "approved")
            self.assertEqual(repeated.decision, "approved")
            with self.assertRaisesRegex(AudioEventCompositionError, "already decided"):
                record_audio_event_composition_decision(output, "rejected")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = authoring_main(["audio-event-composition-status", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["decision"], "approved")


if __name__ == "__main__":
    unittest.main()
