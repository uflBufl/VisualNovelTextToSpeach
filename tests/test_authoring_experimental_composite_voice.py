import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_manifest import load_voice_manifest

from tests.source_reference_fixtures import (
    write_experimental_composite_voice_fixture,
    write_experimental_composite_wav,
)
from vntts.authoring.experimental_composite_voice import (
    EXPERIMENTAL_COMPOSITE_VOICE_FIELD,
    ExperimentalCompositeVoiceError,
    publish_experimental_composite_voice_input,
)
from vntts.authoring.source_reference_bindings import (
    queue_voice_overrides_from_manifest,
)


class AuthoringExperimentalCompositeVoiceTest(unittest.TestCase):
    def test_publishes_idempotent_comparison_only_manifest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, composite, review_path, _review = (
                write_experimental_composite_voice_fixture(root)
            )
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
            manifest, composite, review_path, review = (
                write_experimental_composite_voice_fixture(root)
            )
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

            write_experimental_composite_wav(composite / "composite.wav", (5000, -5000))
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
            manifest, composite, review_path, _review = (
                write_experimental_composite_voice_fixture(root)
            )
            write_experimental_composite_wav(composite / "clips/1.wav", (5000, -5000))
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
            manifest, composite, review_path, _review = (
                write_experimental_composite_voice_fixture(root)
            )
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
