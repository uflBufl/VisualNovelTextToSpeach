import hashlib
import io
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.authoring import silence_evidence
from vntts.authoring.silence_evidence import (
    SilenceFailureEvidenceError,
    load_silence_failure_evidence,
    publish_silence_failure_evidence,
)
from vntts.document_identity import canonical_document_sha256


def wav_payload():
    content = io.BytesIO()
    with wave.open(content, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x01\x00\x02\x00")
    return content.getvalue()


def evidence_metadata():
    text = "Hello."
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    state_item = {
        "status": "failed",
        "text_sha256": text_sha256,
        "failure": {"kind": "speech_silence"},
    }
    return {
        "queue": "queue.jsonl",
        "queue_sha256": "a" * 64,
        "state": "generation-state.json",
        "state_sha256": "b" * 64,
        "queue_id": "queue-id",
        "line_id": "line-id",
        "text": text,
        "text_sha256": text_sha256,
        "state_item": state_item,
        "state_item_sha256": canonical_document_sha256(state_item),
        "synthesis_controls_sha256": "c" * 64,
    }


class SilenceFailureEvidenceTest(unittest.TestCase):
    def test_invalid_json_encoding_preserves_domain_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(
                SilenceFailureEvidenceError, "Unable to read silence-failure evidence"
            ):
                load_silence_failure_evidence(directory)

    def test_staged_wav_corruption_is_rejected_without_publication(self):
        payload = wav_payload()
        native_write = silence_evidence.atomic_write_json

        def corrupt_staged_wav(path, document, **options):
            native_write(path, document, **options)
            (path.parent / "rejected.wav").write_bytes(
                payload[:-1] + bytes((payload[-1] ^ 1,))
            )

        with TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence"
            with (
                patch.object(
                    silence_evidence,
                    "atomic_write_json",
                    side_effect=corrupt_staged_wav,
                ),
                self.assertRaisesRegex(SilenceFailureEvidenceError, "checksum changed"),
            ):
                publish_silence_failure_evidence(evidence, payload, evidence_metadata())
            self.assertFalse(evidence.exists())
            self.assertFalse(list(Path(directory).glob(".evidence.staging-*")))
