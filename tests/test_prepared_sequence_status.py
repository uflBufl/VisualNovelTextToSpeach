import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.prepared_sequence_status import (
    SequenceUnavailableReason,
    load_sequence_unavailable_reason,
    reason_from_extension,
    sequence_unavailable_notice,
)


class PreparedSequenceStatusTest(unittest.TestCase):
    def test_only_known_reason_codes_produce_trusted_notices(self):
        for reason in SequenceUnavailableReason:
            with self.subTest(reason=reason):
                restored = reason_from_extension(
                    {"sequence_unavailable_reason": reason.value}
                )
                self.assertIs(restored, reason)
                notice = sequence_unavailable_notice(restored)
                self.assertIn("from the screen", notice)
                self.assertIn("Prepared audio remains available", notice)
        for value in (
            None,
            {},
            {"sequence_unavailable_reason": "future-code"},
            {"sequence_unavailable_reason": "/private/user/file"},
            {"sequence_unavailable_reason": []},
        ):
            with self.subTest(value=value):
                self.assertIsNone(reason_from_extension(value))
        self.assertEqual(sequence_unavailable_notice(None), "")

    def test_display_metadata_does_not_preflight_audio_components(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "game-pack.json"
            path.write_text(
                json.dumps(
                    {
                        "components": {"generated_audio": {"path": "absent.wav"}},
                        "vntts.self-service": {
                            "sequence_unavailable_reason": "manual-boundary",
                        },
                    }
                )
            )
            self.assertIs(
                load_sequence_unavailable_reason(path),
                SequenceUnavailableReason.MANUAL_BOUNDARY,
            )
            for payload in ("not json", "[]", "{}"):
                path.write_text(payload)
                self.assertIsNone(load_sequence_unavailable_reason(path))
            path.unlink()
            self.assertIsNone(load_sequence_unavailable_reason(path))
