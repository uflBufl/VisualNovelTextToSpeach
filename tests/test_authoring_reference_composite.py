import hashlib
import json
import math
import struct
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.reference_composite import (
    COMPOSITE_SCHEMA,
    ReferenceCompositeError,
    publish_exact_bank_reference_composite,
)


class AuthoringReferenceCompositeTest(unittest.TestCase):
    def write_wav(self, path, *, frequency, leading=0, trailing=0):
        sample_rate = 8_000
        tone = [
            int(6_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for index in range(4_000)
        ]
        samples = [0] * leading + tone + [0] * trailing
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    def make_report(self, root, *, scope="complete_exact_bank"):
        root = Path(root)
        candidates = []
        for index, media_id in enumerate((20, 10), start=1):
            reference = root / "references" / f"{media_id}.wav"
            self.write_wav(
                reference,
                frequency=200 + index * 50,
                leading=800 if media_id == 10 else 0,
                trailing=800 if media_id == 20 else 0,
            )
            candidates.append(
                {
                    "character": "Hotelier",
                    "portrait": "505401.png",
                    "source_bank": "hotelier.bnk",
                    "source_bank_sha256": "a" * 64,
                    "media_id": media_id,
                    "source_sha256": hashlib.sha256(
                        f"encoded-{media_id}".encode()
                    ).hexdigest(),
                    "candidate_origin": "exact_bank_unrouted_media",
                    "source_event_ids": [1_000 + media_id],
                    "reference": f"references/{media_id}.wav",
                    "reference_sha256": hashlib.sha256(
                        reference.read_bytes()
                    ).hexdigest(),
                    "source_lines": [],
                }
            )
        report = root / "report.json"
        report.write_text(
            json.dumps(
                {
                    "schema": "r1999.story-voice-reference-candidates",
                    "schema_version": 2,
                    "bank_inventory_scope": scope,
                    "groups": [
                        {
                            "character": "Hotelier",
                            "portrait": "505401.png",
                            "source_bank": "hotelier.bnk",
                            "candidate_count": 2,
                        }
                    ],
                    "candidates": candidates,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return report

    def test_publishes_all_exact_clips_and_checksum_ledger(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root)

            result = publish_exact_bank_reference_composite(
                report,
                "Hotelier",
                "505401.png",
                "hotelier.bnk",
                root / "composite",
            )
            ledger = json.loads(
                (result.directory / "composite.json").read_text(encoding="utf-8")
            )

            self.assertEqual(ledger["schema"], COMPOSITE_SCHEMA)
            self.assertEqual(result.clips, 2)
            self.assertEqual([clip["media_id"] for clip in ledger["clips"]], [10, 20])
            self.assertTrue(ledger["clips"][0]["trimmed_leading_frames"] > 0)
            self.assertTrue(ledger["clips"][1]["trimmed_trailing_frames"] > 0)
            self.assertEqual(
                hashlib.sha256(
                    (result.directory / "composite.wav").read_bytes()
                ).hexdigest(),
                result.sha256,
            )
            self.assertEqual(
                ledger["composite"]["objective_preflight"]["objective_preflight"],
                "pass",
            )
            self.assertEqual(
                ledger["composite"]["objective_preflight"]["path"],
                "composite.wav",
            )

    def test_rejects_story_routed_only_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root, scope="story_routed_only")

            with self.assertRaisesRegex(ReferenceCompositeError, "complete exact-bank"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )

    def test_rejects_changed_reference_and_existing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = self.make_report(root)
            (root / "references" / "10.wav").write_bytes(b"changed")

            with self.assertRaisesRegex(ReferenceCompositeError, "checksum"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    root / "composite",
                )
            output = root / "exists"
            output.mkdir()
            with self.assertRaisesRegex(ReferenceCompositeError, "output exists"):
                publish_exact_bank_reference_composite(
                    report,
                    "Hotelier",
                    "505401.png",
                    "hotelier.bnk",
                    output,
                )


if __name__ == "__main__":
    unittest.main()
