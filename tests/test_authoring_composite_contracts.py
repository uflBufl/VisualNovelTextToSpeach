import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
