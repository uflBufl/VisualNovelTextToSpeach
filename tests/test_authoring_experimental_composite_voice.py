import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import load_voice_manifest

from vntts.authoring.experimental_composite_voice import (
    EXPERIMENTAL_COMPOSITE_VOICE_FIELD,
    ExperimentalCompositeVoiceError,
    publish_experimental_composite_voice_input,
)
from vntts.authoring.source_reference_bindings import (
    queue_voice_overrides_from_manifest,
)


class AuthoringExperimentalCompositeVoiceTest(unittest.TestCase):
    def create_fixture(self, root):
        source = root / "source"
        source.mkdir()
        (source / "centurion.wav").write_bytes(b"centurion")
        manifest = source / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 2,
                    "game": "fixture",
                    "language": "en",
                    "voices": [
                        {
                            "character": "Centurion",
                            "speaker": "centurion",
                            "references": ["centurion.wav"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        composite = root / "composite"
        (composite / "clips").mkdir(parents=True)
        (composite / "composite.wav").write_bytes(b"composite")
        clips = []
        for index, payload in enumerate((b"one", b"two"), start=1):
            path = composite / "clips" / f"{index}.wav"
            path.write_bytes(payload)
            clips.append(
                {
                    "media_id": index,
                    "reference": f"clips/{index}.wav",
                    "reference_sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        reference_sha256 = sha256_file(composite / "composite.wav")
        ledger = {
            "schema": "vntts.authoring-exact-bank-reference-composite",
            "schema_version": 1,
            "character": "Hotelier",
            "portrait": "505401.png",
            "source_bank": "hotel.bnk",
            "clips": clips,
            "composite": {
                "path": "composite.wav",
                "sha256": reference_sha256,
            },
        }
        (composite / "composite.json").write_text(
            json.dumps(ledger, sort_keys=True), encoding="utf-8"
        )
        ledger_sha256 = sha256_file(composite / "composite.json")
        evaluation = {
            "schema": "vntts.authoring-exact-bank-composite-evaluation",
            "schema_version": 1,
            "source_composite_sha256": ledger_sha256,
        }
        (composite / "evaluation.json").write_text(
            json.dumps(evaluation, sort_keys=True), encoding="utf-8"
        )
        quality = root / "quality"
        quality.mkdir()
        review_path = quality / "review.json"
        review_path.write_text("{}", encoding="utf-8")
        review = {
            "source_reference_plan_sha256": ledger_sha256,
            "source_reference_evaluation_sha256": sha256_file(
                composite / "evaluation.json"
            ),
            "variants": [
                {
                    "variant_id": f"exact-bank-composite:{reference_sha256}",
                    "reference_kind": "exact_bank_composite",
                    "character": "Hotelier",
                    "portrait": "505401.png",
                    "source_bank": "hotel.bnk",
                    "reference": {"audio_sha256": reference_sha256},
                    "decision": {"decision": "needs_sample"},
                }
            ],
        }
        return manifest, composite, review_path, review

    def publish(self, root, review, *, output=None):
        manifest, composite, review_path, _ = self.create_fixture(root)
        with patch(
            "vntts.authoring.experimental_composite_voice."
            "load_source_reference_quality_review",
            return_value=review,
        ):
            result = publish_experimental_composite_voice_input(
                manifest,
                composite,
                review_path,
                "Experimental Hotelier exact-bank composite",
                output or root / "output",
            )
        return result, manifest, composite, review_path

    def test_publishes_idempotent_comparison_only_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, review = self.create_fixture(root)
            output = root / "output"
            with patch(
                "vntts.authoring.experimental_composite_voice."
                "load_source_reference_quality_review",
                return_value=review,
            ):
                first = publish_experimental_composite_voice_input(
                    manifest,
                    composite,
                    review_path,
                    "Experimental Hotelier exact-bank composite",
                    output,
                )
                second = publish_experimental_composite_voice_input(
                    manifest,
                    composite,
                    review_path,
                    "Experimental Hotelier exact-bank composite",
                    output,
                )

            document = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )
            _metadata, voices = load_voice_manifest(
                output / "manifest.json", allow_legacy=False
            )
            overrides = queue_voice_overrides_from_manifest(document, voices=voices)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.bundle_id, second.bundle_id)
        self.assertEqual(overrides, {})
        self.assertEqual(len(voices), 2)
        self.assertEqual(
            document[EXPERIMENTAL_COMPOSITE_VOICE_FIELD]["authority"],
            "experimental_only_no_queue_override_or_production_binding",
        )
        self.assertEqual(
            document[EXPERIMENTAL_COMPOSITE_VOICE_FIELD]["voices"][0][
                "quality_decision"
            ],
            "needs_sample",
        )

    def test_rejects_non_needs_sample_card_and_composite_tampering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, review = self.create_fixture(root)
            rejected = json.loads(json.dumps(review))
            rejected["variants"][0]["decision"]["decision"] = "reject"
            with patch(
                "vntts.authoring.experimental_composite_voice."
                "load_source_reference_quality_review",
                return_value=rejected,
            ):
                with self.assertRaisesRegex(
                    ExperimentalCompositeVoiceError, "needs_sample"
                ):
                    publish_experimental_composite_voice_input(
                        manifest,
                        composite,
                        review_path,
                        "Experimental Hotelier exact-bank composite",
                        root / "rejected",
                    )

            (composite / "composite.wav").write_bytes(b"changed")
            with patch(
                "vntts.authoring.experimental_composite_voice."
                "load_source_reference_quality_review",
                return_value=review,
            ):
                with self.assertRaisesRegex(
                    ExperimentalCompositeVoiceError, "Composite WAV changed"
                ):
                    publish_experimental_composite_voice_input(
                        manifest,
                        composite,
                        review_path,
                        "Experimental Hotelier exact-bank composite",
                        root / "tampered",
                    )

    def test_existing_output_tampering_and_different_source_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, review = self.create_fixture(root)
            output = root / "output"
            with patch(
                "vntts.authoring.experimental_composite_voice."
                "load_source_reference_quality_review",
                return_value=review,
            ):
                publish_experimental_composite_voice_input(
                    manifest,
                    composite,
                    review_path,
                    "Experimental Hotelier exact-bank composite",
                    output,
                )
                reference = next((output / "experimental-composites").rglob("*.wav"))
                reference.write_bytes(b"forged")
                with self.assertRaisesRegex(
                    ExperimentalCompositeVoiceError, "artifact changed"
                ):
                    publish_experimental_composite_voice_input(
                        manifest,
                        composite,
                        review_path,
                        "Experimental Hotelier exact-bank composite",
                        output,
                    )


if __name__ == "__main__":
    unittest.main()
