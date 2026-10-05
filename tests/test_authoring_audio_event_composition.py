import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.audio_event_composition as composition_module
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

    def test_composition_and_decision_versions_require_integers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            publish_audio_event_composition(review.directory, output)
            path = output / "composition.json"
            payload = path.read_bytes()
            for version in (True, 1.0):
                with self.subTest(composition_version=version):
                    document = json.loads(payload)
                    document["schema_version"] = version
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
            path.write_bytes(payload)
            record_audio_event_composition_decision(output, "approved")
            terminal = output / "composition-decision.json"
            decision = json.loads(terminal.read_text())
            for version in (True, 1.0):
                with self.subTest(decision_version=version):
                    decision["schema_version"] = version
                    terminal.write_text(json.dumps(decision, sort_keys=True))
                    with self.assertRaisesRegex(
                        AudioEventCompositionError, "decision changed"
                    ):
                        load_audio_event_composition(output)

    def test_composition_metadata_and_recipe_keep_their_field_types(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            publish_audio_event_composition(review.directory, output)
            path = output / "composition.json"
            payload = path.read_bytes()
            cases = (
                ("sample_rate", 24_000.0),
                ("sample_count", 1_200.0),
                ("sample_offset", False),
                ("fade_in_samples", False),
                ("fade_out_samples", False),
                ("gain", True),
                ("speaker_identity_claim", 0),
            )
            for field, value in cases:
                with self.subTest(field=field):
                    document = json.loads(payload)
                    if field in document:
                        document[field] = value
                    else:
                        document["composition"][field] = value
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
                        AudioEventCompositionError, "metadata|ledger"
                    ):
                        load_audio_event_composition(output)

    def test_loader_and_publisher_races_raise_composition_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            publish_audio_event_composition(review.directory, output)
            original = composition_module._validate_composition_document

            def validate_then_mutate(*args):
                original(*args)
                path = output / "composition.json"
                path.write_bytes(path.read_bytes() + b" ")

            with patch.object(
                composition_module,
                "_validate_composition_document",
                validate_then_mutate,
            ):
                with self.assertRaisesRegex(AudioEventCompositionError, "changed"):
                    load_audio_event_composition(output)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            original = load_audio_event_composition

            def load_then_mutate(path):
                loaded = original(path)
                queue = review.directory / "queue.jsonl"
                queue.write_bytes(queue.read_bytes() + b"\n")
                return loaded

            with patch.object(
                composition_module, "load_audio_event_composition", load_then_mutate
            ):
                with self.assertRaisesRegex(AudioEventCompositionError, "changed"):
                    publish_audio_event_composition(
                        review.directory, root / "composition"
                    )
            self.assertFalse((root / "composition").exists())
            self.assertFalse(list(root.glob(".composition.staging-*")))

    def test_persisted_decision_rejects_non_strings_with_composition_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            review, _queue, _audio = publish(root)
            record_audio_event_review_decision(review.directory, "accept")
            output = root / "composition"
            publish_audio_event_composition(review.directory, output)
            record_audio_event_composition_decision(output, "approved")
            path = output / "composition-decision.json"
            document = json.loads(path.read_text())
            for decision in ([], {}, None, True, 1):
                with self.subTest(decision=decision):
                    document["decision"] = decision
                    path.write_text(json.dumps(document, sort_keys=True))
                    with self.assertRaisesRegex(
                        AudioEventCompositionError, "decision changed"
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
