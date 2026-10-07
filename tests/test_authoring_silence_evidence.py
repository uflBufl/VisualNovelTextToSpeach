import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.silence_evidence import (
    SilenceFailureEvidenceError,
    load_silence_failure_evidence,
)


class SilenceFailureEvidenceTest(unittest.TestCase):
    def test_invalid_json_encoding_preserves_domain_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                SilenceFailureEvidenceError, "Unable to read silence-failure evidence"
            ):
                load_silence_failure_evidence(directory)
