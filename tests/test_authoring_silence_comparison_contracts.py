import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import tests.test_authoring_silence_comparison as fixtures
from vntts.authoring.silence_comparison import (
    SilenceComparisonError,
    load_silence_comparison,
    load_silence_comparison_input_plan,
    publish_silence_comparison,
)


class SilenceComparisonContractsTest(unittest.TestCase):
    def fixture(self, root):
        root = root.resolve()
        sample = fixtures.AuthoringSilenceComparisonTest()._fixture(root)
        result = publish_silence_comparison((sample,), root / "comparison")
        path = result.directory / "comparison.json"
        return sample, result.directory, path, json.loads(path.read_bytes())

    def replace_report(self, root, path, document, payload):
        report = root / document["reports"][0]
        report.write_bytes(payload)
        for artifact in document["artifacts"]:
            if artifact["path"] == document["reports"][0]:
                artifact["sha256"] = hashlib.sha256(payload).hexdigest()
        path.write_text(json.dumps(document))

    def test_unhashable_artifact_paths_and_report_ids_are_domain_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            _sample, root, path, original = self.fixture(Path(directory))
            report_path = root / original["reports"][0]
            original_report = report_path.read_bytes()
            for location in ("artifact", "sample", "report"):
                with self.subTest(location=location):
                    document = copy.deepcopy(original)
                    report_path.write_bytes(original_report)
                    if location == "artifact":
                        document["artifacts"][0]["path"] = []
                    elif location == "sample":
                        document["samples"][0]["raw_copy"] = []
                    else:
                        report = json.loads(original_report)
                        report["samples"][0]["id"] = []
                        self.replace_report(
                            root, path, document, json.dumps(report).encode()
                        )
                    path.write_text(json.dumps(document))
                    with self.assertRaises(SilenceComparisonError):
                        load_silence_comparison(root)

    def test_invalid_json_encoding_is_a_domain_error_at_each_input(self):
        with tempfile.TemporaryDirectory() as directory:
            sample, root, path, document = self.fixture(Path(directory))
            plan = fixtures.AuthoringSilenceComparisonTest()._write_input_plan(
                root.parent, sample
            )
            plan.write_bytes(b"\xff")
            with self.assertRaises(SilenceComparisonError):
                load_silence_comparison_input_plan(plan)
            original = path.read_bytes()
            path.write_bytes(b"\xff")
            with self.assertRaises(SilenceComparisonError):
                load_silence_comparison(root)
            path.write_bytes(original)
            self.replace_report(root, path, document, b"\xff")
            with self.assertRaises(SilenceComparisonError):
                load_silence_comparison(root)
