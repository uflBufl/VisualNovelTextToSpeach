import json
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

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
    def write_wav(self, path, samples=(1000, -1000, 2000, -2000)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8_000)
            output.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    def create_fixture(self, root):
        source = root / "source"
        source.mkdir()
        self.write_wav(source / "centurion.wav")
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
        self.write_wav(composite / "composite.wav", (3000, -3000, 4000, -4000))
        clips = []
        for index, samples in enumerate(((1000, -1000), (2000, -2000)), start=1):
            path = composite / "clips" / f"{index}.wav"
            self.write_wav(path, samples)
            clips.append(
                {
                    "media_id": index,
                    "reference": f"clips/{index}.wav",
                    "reference_sha256": sha256_file(path),
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
        self.write_wav(quality / "reference.wav", (3000, -3000, 4000, -4000))
        review_path = quality / "review.json"
        review = {
            "schema": "vntts.authoring-source-reference-quality-review",
            "schema_version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "source_reference_plan_sha256": ledger_sha256,
            "source_reference_evaluation_sha256": sha256_file(
                composite / "evaluation.json"
            ),
            "generation_state_sha256": "c" * 64,
            "variant_count": 1,
            "completed_count": 1,
            "variants": [
                {
                    "cluster_id": f"exact-bank-composite:{reference_sha256}",
                    "variant_id": f"exact-bank-composite:{reference_sha256}",
                    "reference_kind": "exact_bank_composite",
                    "character": "Hotelier",
                    "portrait": "505401.png",
                    "source_bank": "hotel.bnk",
                    "media_ids": [1, 2],
                    "affected_queue_item_count": 1,
                    "reference": {
                        "audio": "reference.wav",
                        "audio_sha256": sha256_file(quality / "reference.wav"),
                        "sample_rate": 8_000,
                        "sample_count": 4,
                        "duration_seconds": 0.0005,
                    },
                    "generated_samples": [],
                    "excluded_results": [],
                    "decision": {
                        "decision": "needs_sample",
                        "reviewed_at": "2026-01-01T00:00:00+00:00",
                    },
                }
            ],
        }
        review_path.write_text(json.dumps(review, sort_keys=True), encoding="utf-8")
        return manifest, composite, review_path, review

    def test_publishes_idempotent_comparison_only_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, _review = self.create_fixture(root)
            output = root / "output"
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
            review_path.write_text(
                json.dumps(rejected, sort_keys=True), encoding="utf-8"
            )
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

            self.write_wav(composite / "composite.wav", (5000, -5000))
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

    def test_rejects_changed_composite_clip(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, _review = self.create_fixture(root)
            self.write_wav(composite / "clips/1.wav", (5000, -5000))
            with self.assertRaisesRegex(
                ExperimentalCompositeVoiceError, "Composite clip changed"
            ):
                publish_experimental_composite_voice_input(
                    manifest,
                    composite,
                    review_path,
                    "Experimental Hotelier exact-bank composite",
                    root / "output",
                )

    def test_existing_output_tampering_and_different_source_fail_closed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, _review = self.create_fixture(root)
            output = root / "output"
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
