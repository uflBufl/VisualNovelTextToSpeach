import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import vntts.authoring.listening as listening
from tests.authoring_fixtures import (
    write_silence_comparison_input_plan,
    write_silence_comparison_sample,
)
from tests.symlink_support import symlink_or_skip
from vntts.authoring.silence_comparison import (
    SilenceComparisonError,
    create_silence_comparison_session,
    load_silence_comparison,
    load_silence_comparison_input_plan,
    publish_silence_comparison,
)
from vntts.path_safety import open_regular_binary


class SilenceComparisonContractsTest(unittest.TestCase):
    def fixture(self, root):
        root = root.resolve()
        sample = write_silence_comparison_sample(root)
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
            plan = write_silence_comparison_input_plan(root.parent, sample)
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

    def test_audio_decoding_cannot_use_bytes_outside_validated_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            _sample, root, _path, document = self.fixture(Path(directory))
            audio = root / document["samples"][0]["segmented_copy"]
            alternate = (root / document["samples"][0]["raw_copy"]).read_bytes()
            original_open = open_regular_binary

            def substituted(path, *args, **kwargs):
                if Path(path) == audio:
                    return io.BytesIO(alternate)
                return original_open(path, *args, **kwargs)

            with patch(
                "vntts.authoring.silence_comparison.open_regular_binary",
                side_effect=substituted,
            ):
                with self.assertRaises(SilenceComparisonError):
                    load_silence_comparison(root)

    def test_session_rejects_report_changed_after_comparison_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            _sample, root, _path, document = self.fixture(Path(directory))
            report = root / document["reports"][0]
            digest = listening._source_digest
            output = root.parent / "listening"

            def changed(*args, **kwargs):
                value = json.loads(report.read_bytes())
                value["model"] = "Never validated by the comparison"
                report.write_text(json.dumps(value))
                return digest(*args, **kwargs)

            with patch.object(listening, "_source_digest", changed):
                with self.assertRaises(SilenceComparisonError):
                    create_silence_comparison_session(root, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".listening-*")), [])

    def test_session_rejects_comparison_changed_during_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            _sample, root, path, document = self.fixture(Path(directory))
            digest = listening._source_digest
            output = root.parent / "listening"

            def changed(*args, **kwargs):
                document["policy"]["production_enabled"] = True
                path.write_text(json.dumps(document))
                return digest(*args, **kwargs)

            with patch.object(listening, "_source_digest", changed):
                with self.assertRaises(SilenceComparisonError):
                    create_silence_comparison_session(root, output)
            self.assertFalse(output.exists())

    def test_generic_listening_rejects_same_byte_report_symlink_before_publication(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            _sample, root, _path, document = self.fixture(Path(directory))
            report = root / document["reports"][0]
            alternate = root.parent / "alternate-report.json"
            alternate.write_bytes(report.read_bytes())
            link = listening._link_blind_audio
            substituted = False
            output = root.parent / "listening"

            def substitute(source, destination):
                nonlocal substituted
                link(source, destination)
                if not substituted:
                    substituted = True
                    report.unlink()
                    symlink_or_skip(report, alternate)

            with patch.object(listening, "_link_blind_audio", substitute):
                with self.assertRaises(listening.ModelListeningError):
                    listening.create_listening_session_from_reports(
                        (root / value for value in document["reports"]), output
                    )
            self.assertFalse(output.exists())

    def test_generic_listening_parses_only_the_report_bytes_it_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            _sample, root, _path, document = self.fixture(Path(directory))
            report = root / document["reports"][0]
            read = Path.read_text

            def substituted(path, *args, **kwargs):
                value = read(path, *args, **kwargs)
                if path == report:
                    altered = json.loads(value)
                    altered["model"] = "Reopened unaudited model"
                    return json.dumps(altered)
                return value

            with patch.object(Path, "read_text", substituted):
                session = listening.create_listening_session_from_reports(
                    (root / value for value in document["reports"]),
                    root.parent / "listening",
                )
            key = json.loads((session.parent / ".blind-key.json").read_bytes())
            self.assertNotIn(
                "Reopened unaudited model", {model["model"] for model in key["models"]}
            )
