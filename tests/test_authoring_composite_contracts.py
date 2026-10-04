import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.file_integrity import sha256_file

import tests.test_authoring_experimental_composite_voice as experimental_fixture
import tests.test_authoring_reference_composite as reference_fixture
import vntts.authoring.experimental_composite_voice as experimental
from vntts.authoring.authority import canonical_document_sha256


class CompositeContractsTest(unittest.TestCase):
    def fixture(self, root):
        return experimental_fixture.AuthoringExperimentalCompositeVoiceTest().create_fixture(
            root
        )[:3]

    def publish(self, root, inputs):
        return experimental.publish_experimental_composite_voice_input(
            *inputs, "Experimental Hotelier", root / "output"
        )

    def test_non_object_composite_controls_raise_domain_errors(self):
        for name in ("composite.json", "evaluation.json"):
            for value in ([], None, "wrong"):
                with (
                    self.subTest(name=name, value=value),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    inputs = self.fixture(root)
                    (inputs[1] / name).write_text(json.dumps(value))
                    with self.assertRaises(
                        experimental.ExperimentalCompositeVoiceError
                    ):
                        self.publish(root, inputs)
                    self.assertFalse((root / "output").exists())

    def test_composite_versions_require_exact_integers(self):
        for name in ("composite.json", "evaluation.json"):
            for value in (True, 1.0):
                with (
                    self.subTest(name=name, value=value),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    inputs = self.fixture(root)
                    path = inputs[1] / name
                    doc = json.loads(path.read_text())
                    doc["schema_version"] = value
                    path.write_text(json.dumps(doc))
                    if name == "composite.json":
                        evaluation_path = inputs[1] / "evaluation.json"
                        evaluation = json.loads(evaluation_path.read_text())
                        evaluation["source_composite_sha256"] = sha256_file(path)
                        evaluation_path.write_text(json.dumps(evaluation))
                    review = json.loads(inputs[2].read_text())
                    review["source_reference_plan_sha256"] = sha256_file(
                        inputs[1] / "composite.json"
                    )
                    review["source_reference_evaluation_sha256"] = sha256_file(
                        inputs[1] / "evaluation.json"
                    )
                    inputs[2].write_text(json.dumps(review))
                    with self.assertRaises(
                        experimental.ExperimentalCompositeVoiceError
                    ):
                        self.publish(root, inputs)

    def test_existing_bundle_requires_object_and_exact_version(self):
        for value in ([], None, True, 1.0):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                root = Path(directory)
                inputs = self.fixture(root)
                self.publish(root, inputs)
                path = root / "output/bundle.json"
                doc = json.loads(path.read_text())
                if isinstance(value, (bool, float)):
                    doc["schema_version"] = value
                    doc["bundle_id"] = canonical_document_sha256(
                        {k: v for k, v in doc.items() if k != "bundle_id"}
                    )
                else:
                    doc = value
                path.write_text(json.dumps(doc))
                with self.assertRaises(experimental.ExperimentalCompositeVoiceError):
                    self.publish(root, inputs)

    def test_candidate_report_version_requires_exact_integer(self):
        from vntts.authoring.reference_composite import (
            ReferenceCompositeError,
            publish_exact_bank_reference_composite,
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = reference_fixture.AuthoringReferenceCompositeTest().make_report(
                root
            )
            doc = json.loads(report.read_text())
            doc["schema_version"] = 2.0
            report.write_text(json.dumps(doc))
            with self.assertRaises(ReferenceCompositeError):
                publish_exact_bank_reference_composite(
                    report, "Hotelier", "505401.png", "hotelier.bnk", root / "composite"
                )

    def test_manifest_change_after_parsing_blocks_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self.fixture(root)
            original = experimental._load_composite_authority

            def change(*args):
                result = original(*args)
                doc = json.loads(inputs[0].read_text())
                doc["context"] = "changed after capture"
                inputs[0].write_text(json.dumps(doc))
                return result

            with patch.object(
                experimental, "_load_composite_authority", side_effect=change
            ):
                with self.assertRaises(experimental.ExperimentalCompositeVoiceError):
                    self.publish(root, inputs)
            self.assertFalse((root / "output").exists())

    def test_review_change_after_validation_blocks_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self.fixture(root)
            original = experimental.load_source_reference_quality_review

            def change(*args):
                result = original(*args)
                doc = json.loads(inputs[2].read_text())
                doc["variants"][0]["decision"]["decision"] = "reject"
                inputs[2].write_text(json.dumps(doc))
                return result

            with patch.object(
                experimental, "load_source_reference_quality_review", side_effect=change
            ):
                with self.assertRaises(experimental.ExperimentalCompositeVoiceError):
                    self.publish(root, inputs)
            self.assertFalse((root / "output").exists())

    def test_same_bytes_symlink_substitution_blocks_publication(self):
        import vntts.authoring.reference_composite as reference

        for source_name in ("report.json", "references/10.wav"):
            with (
                self.subTest(source_name=source_name),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                report = (
                    reference_fixture.AuthoringReferenceCompositeTest().make_report(
                        root
                    )
                )
                source = root / source_name
                target = root / "same-bytes"
                target.write_bytes(source.read_bytes())
                probe = root / "symlink-probe"
                try:
                    probe.symlink_to(target)
                except OSError as error:
                    self.skipTest(f"Symlink creation is unavailable: {error}")
                probe.unlink()
                original = reference._write_composite_artifacts

                def change(*args, **kwargs):
                    result = original(*args, **kwargs)
                    source.unlink()
                    source.symlink_to(target)
                    return result

                with patch.object(
                    reference, "_write_composite_artifacts", side_effect=change
                ):
                    with self.assertRaises(reference.ReferenceCompositeError):
                        reference.publish_exact_bank_reference_composite(
                            report,
                            "Hotelier",
                            "505401.png",
                            "hotelier.bnk",
                            root / "composite",
                        )
                self.assertFalse((root / "composite").exists())

    def test_quality_state_change_after_validation_blocks_publication(self):
        import vntts.authoring.generation_state as generation_state
        import vntts.authoring.reference_composite as reference
        from vntts.authoring.bulk_generation import run_bulk_generation

        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = reference_fixture.AuthoringReferenceCompositeTest().make_report(
                root
            )
            composite = reference.publish_exact_bank_reference_composite(
                report, "Hotelier", "505401.png", "hotelier.bnk", root / "composite"
            )
            generation = run_bulk_generation(
                composite.directory / "queue.jsonl",
                root / "generation",
                reference_fixture._Renderer(),
                provider="synthetic",
                model="synthetic-v1",
                generation_profile="stable",
            )
            original = generation_state._validate_state_document

            def change(*args, **kwargs):
                result = original(*args, **kwargs)
                doc = json.loads(generation.state.read_text())
                doc["context"] = "changed after validation"
                generation.state.write_text(json.dumps(doc))
                return result

            with patch.object(
                generation_state, "_validate_state_document", side_effect=change
            ):
                with self.assertRaises(reference.ReferenceCompositeError):
                    reference.publish_composite_quality_review(
                        composite.directory, generation.state, root / "quality"
                    )
            self.assertFalse((root / "quality").exists())

    def test_nested_bundle_named_artifact_is_preserved(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self.fixture(root)
            (inputs[2].parent / "bundle.json").write_text('{"context":"retained"}')
            first = self.publish(root, inputs)
            second = self.publish(root, inputs)
            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertEqual(
                (root / "output/authority/quality-review/bundle.json").read_text(),
                '{"context":"retained"}',
            )
