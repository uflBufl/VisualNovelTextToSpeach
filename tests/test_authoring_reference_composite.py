import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts import VoiceGenerationQueue
from vntts_artifacts.voice_manifest import load_voice_manifest

from tests.source_reference_fixtures import (
    CompositeRenderer,
    write_exact_bank_composite_report,
    write_reference_composite_wav,
)
from vntts.authoring import source_reference_quality_records as quality_records
from vntts.authoring.bulk_generation import run_bulk_generation
from vntts.authoring.reference_composite import (
    COMPOSITE_SCHEMA,
    ReferenceCompositeError,
    publish_composite_quality_review,
    publish_exact_bank_reference_composite,
)
from vntts.authoring.source_reference_quality import (
    load_source_reference_quality_review,
)
from vntts.authoring.source_reference_quality_records import capture_quality_outcomes


class AuthoringReferenceCompositeTest(unittest.TestCase):
    def test_publishes_all_exact_clips_and_checksum_ledger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_exact_bank_composite_report(root)

            result = publish_exact_bank_reference_composite(
                report,
                "Hotelier",
                "505401.png",
                "hotelier.bnk",
                root / "composite",
            )
            ledger = json.loads(
                (result.directory / "composite.json").read_text(encoding="utf-8")
            )
            evaluation = json.loads(
                (result.directory / "evaluation.json").read_text(encoding="utf-8")
            )
            queue = VoiceGenerationQueue.load(result.directory / "queue.jsonl")
            _manifest, voices = load_voice_manifest(
                result.directory / "voice-manifest.json", allow_legacy=False
            )

            self.assertEqual(ledger["schema"], COMPOSITE_SCHEMA)
            self.assertEqual(result.clips, 2)
            self.assertEqual([clip["media_id"] for clip in ledger["clips"]], [10, 20])
            self.assertTrue(ledger["clips"][0]["trimmed_leading_frames"] > 0)
            self.assertTrue(ledger["clips"][1]["trimmed_trailing_frames"] > 0)
            self.assertEqual(
                hashlib.sha256(
                    (result.directory / "composite.wav").read_bytes()
                ).hexdigest(),
                result.sha256,
            )
            self.assertEqual(
                ledger["composite"]["objective_preflight"]["objective_preflight"],
                "pass",
            )
            self.assertEqual(
                ledger["composite"]["objective_preflight"]["path"],
                "composite.wav",
            )
            self.assertEqual(len(queue.items), 3)
            self.assertEqual(len(evaluation["fixed_queue_ids"]), 3)
            self.assertEqual(voices[0].references, ("composite.wav",))
            self.assertEqual(queue.items[0].voice_character, voices[0].character)

    def test_rejects_story_routed_only_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_exact_bank_composite_report(root, scope="story_routed_only")

            with self.assertRaisesRegex(ReferenceCompositeError, "complete exact-bank"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )

    def test_preserves_non_object_json_boundary_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_exact_bank_composite_report(root)
            report.write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ReferenceCompositeError, "complete exact-bank"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )
            report = write_exact_bank_composite_report(root)
            composite = publish_exact_bank_reference_composite(
                report,
                "Hotelier",
                "505401.png",
                "hotelier.bnk",
                root / "composite",
            )
            (composite.directory / "composite.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ReferenceCompositeError, "identity is invalid"):
                publish_composite_quality_review(
                    composite.directory, root / "missing-state.json", root / "quality"
                )

    def test_publishes_composite_quality_card_without_binding_authority(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_exact_bank_composite_report(root)
            composite = publish_exact_bank_reference_composite(
                report,
                "Hotelier",
                "505401.png",
                "hotelier.bnk",
                root / "composite",
            )
            generation = run_bulk_generation(
                composite.directory / "queue.jsonl",
                root / "generation",
                CompositeRenderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )

            quality = publish_composite_quality_review(
                composite.directory, generation.state, root / "quality"
            )
            session = load_source_reference_quality_review(quality.session)

            self.assertEqual(quality.generated_samples, 3)
            card = session["variants"][0]
            self.assertEqual(card["reference_kind"], "exact_bank_composite")
            self.assertEqual(card["media_ids"], [10, 20])
            self.assertEqual(len(card["generated_samples"]), 3)
            self.assertIn("not a source-reference plan", session["authority"])

            with self.assertRaisesRegex(ReferenceCompositeError, "output exists"):
                publish_composite_quality_review(
                    composite.directory, generation.state, root / "quality"
                )
            (composite.directory / "composite.wav").write_bytes(b"changed")
            with self.assertRaisesRegex(
                ReferenceCompositeError, "Composite WAV changed"
            ):
                publish_composite_quality_review(
                    composite.directory, generation.state, root / "changed-quality"
                )

    def test_quality_review_rejects_bound_non_object_source_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_exact_bank_composite_report(root)
            composite = publish_exact_bank_reference_composite(
                report, "Hotelier", "505401.png", "hotelier.bnk", root / "composite"
            )
            generation = run_bulk_generation(
                composite.directory / "queue.jsonl",
                root / "generation",
                CompositeRenderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )
            report.write_text("[]", encoding="utf-8")
            ledger_path = composite.directory / "composite.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["source_candidate_report_sha256"] = hashlib.sha256(b"[]").hexdigest()
            ledger_path.write_text(json.dumps(ledger, sort_keys=True), encoding="utf-8")
            evaluation_path = composite.directory / "evaluation.json"
            evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
            evaluation["source_composite_sha256"] = hashlib.sha256(
                ledger_path.read_bytes()
            ).hexdigest()
            evaluation_path.write_text(
                json.dumps(evaluation, sort_keys=True), encoding="utf-8"
            )

            with self.assertRaisesRegex(
                ReferenceCompositeError, "affected story-line count is invalid"
            ):
                publish_composite_quality_review(
                    composite.directory, generation.state, root / "quality"
                )

    def test_quality_review_keeps_composite_error_for_changed_generated_sample(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "generation" / "sample.wav"
            write_reference_composite_wav(source, frequency=250)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            source.write_bytes(b"changed")

            with self.assertRaisesRegex(
                ReferenceCompositeError, "Generated composite sample changed"
            ):
                capture_quality_outcomes(
                    [
                        (
                            {
                                "queue_id": "sample",
                                "evaluation_kind": "fixed",
                                "text": "Test",
                                "text_sha256": hashlib.sha256(b"Test").hexdigest(),
                            },
                            {
                                "status": "generated",
                                "path": "sample.wav",
                                "file_sha256": digest,
                            },
                            Path("audio/generated-01.wav"),
                        )
                    ],
                    source.parent,
                    root / "staging",
                    [],
                    error_type=ReferenceCompositeError,
                    generated_label="Generated composite sample",
                )

    def test_quality_audio_capture_preserves_the_composite_error_type(self):
        for failure in ("probe", "copy", "checksum"):
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "generation" / "sample.wav"
                write_reference_composite_wav(source, frequency=250)
                if failure == "probe":
                    source.write_bytes(b"invalid WAV")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                copy_audio = quality_records.shutil.copyfile

                def copy_with_failure(src, dest):
                    if failure == "copy":
                        raise PermissionError("Destination became unwritable")
                    result = copy_audio(src, dest)
                    if failure == "checksum":
                        Path(dest).write_bytes(b"changed after copy")
                    return result

                with patch.object(
                    quality_records.shutil, "copyfile", side_effect=copy_with_failure
                ):
                    with self.assertRaises(ReferenceCompositeError):
                        capture_quality_outcomes(
                            [
                                (
                                    {"queue_id": "sample"},
                                    {
                                        "status": "generated",
                                        "path": "sample.wav",
                                        "file_sha256": digest,
                                    },
                                    Path("audio/generated.wav"),
                                )
                            ],
                            source.parent,
                            root / "staging",
                            [],
                            error_type=ReferenceCompositeError,
                        )

    def test_rejects_changed_reference_and_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = write_exact_bank_composite_report(root)
            (root / "references" / "10.wav").write_bytes(b"changed")

            with self.assertRaisesRegex(ReferenceCompositeError, "checksum"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )
            output = root / "exists"
            output.mkdir()
            with self.assertRaisesRegex(ReferenceCompositeError, "output exists"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    output,
                )


if __name__ == "__main__":
    unittest.main()
