import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.voice_manifest import load_voice_manifest

from tests import test_workspace_input_admission as admission
from tests.authoring_fixtures import tree_hashes
from tests.source_reference_fixtures import (
    write_experimental_composite_voice_fixture,
    write_experimental_composite_wav,
)
from vntts.authoring import experimental_composite_voice as experimental
from vntts.authoring.experimental_composite_voice import (
    EXPERIMENTAL_COMPOSITE_VOICE_FIELD,
    ExperimentalCompositeVoiceError,
    publish_experimental_composite_voice_input,
)
from vntts.authoring.source_reference_bindings import (
    queue_voice_overrides_from_manifest,
)


class AuthoringExperimentalCompositeVoiceTest(unittest.TestCase):
    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_artifact_copies_refuse_acquisition_clean_up_and_recover(self):
        for role in ("manifest_reference", "composite", "quality_review"):
            with self.subTest(role=role), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                manifest, composite, review, _document = (
                    write_experimental_composite_voice_fixture(root)
                )
                selected = {
                    "manifest_reference": manifest.parent / "centurion.wav",
                    "composite": composite / "composite.wav",
                    "quality_review": review,
                }[role]
                output = root / "output"
                before = tree_hashes(root)
                original = experimental.copy_regular_file
                refused = []

                def copy(source, target, **kwargs):
                    if Path(source) != selected:
                        return original(source, target, **kwargs)
                    gate = {"path": selected, "armed": True}
                    with (
                        admission.WorkspaceInputAdmissionTest.swap_at_acquisition(
                            self, gate
                        ),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        original(source, target, **kwargs)
                    self.assertFalse(Path(target).exists())
                    refused.append(caught.exception)
                    raise caught.exception

                def publish():
                    return publish_experimental_composite_voice_input(
                        manifest,
                        composite,
                        review,
                        "Experimental Hotelier exact-bank composite",
                        output,
                    )

                with (
                    patch.object(experimental, "copy_regular_file", copy),
                    self.assertRaisesRegex(
                        ExperimentalCompositeVoiceError, "regular file"
                    ) as caught,
                ):
                    publish()
                self.assertEqual(len(refused), 1)
                self.assertIs(caught.exception.__cause__, refused[0])
                self.assertFalse(output.exists())
                self.assertEqual(tree_hashes(root), before)
                self.assertFalse(list(root.glob(".experimental-composite-*")))
                recovered = publish()
                repeated = publish()
                self.assertTrue(recovered.created)
                self.assertFalse(repeated.created)
                self.assertEqual(recovered.bundle_id, repeated.bundle_id)

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
