import io
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.bulk_generation_fixtures import queue_item, write_queue
from tests.generation_state_fixtures import replace_generation_state
from vntts.authoring.failed_audio import retain_failed_audio
from vntts.document_identity import sha256_file
from vntts.pregeneration_failures import (
    inspect_preparation_failures,
    load_preparation_failure_audio,
)
from vntts.pregeneration_queue import PregenerationInput


def _inputs(root):
    directory = root / "input"
    directory.mkdir()
    item = queue_item(text="First sentence. Another sentence.")
    queue = write_queue(directory / "queue.jsonl", [item])
    inputs = PregenerationInput(
        identity="a" * 64,
        directory=directory,
        story_index=directory / "story-index.jsonl",
        voice_manifest=directory / "voice-manifest.json",
        queue=queue,
        queue_sha256=sha256_file(queue),
        queue_items=1,
        ready_items=1,
        narrator_fallback_roles=(),
        story_index_sha256="b" * 64,
        voice_manifest_sha256="c" * 64,
        source_audio_semantic_evidence_sha256=None,
    )
    return inputs, item


def _save(inputs, item, result, **metadata):
    replace_generation_state(
        inputs.output_directory / "generation-state.sqlite3",
        {
            "schema": "vntts.authoring-generation-state",
            "schema_version": 1,
            "queue_sha256": inputs.queue_sha256,
            "items": {item["queue_id"]: result},
            **metadata,
        },
    )


def _failure(kind="backend_error", **values):
    return {
        "status": "failed",
        "last_error": "Original provider error",
        "failure": {"schema_version": 1, "kind": kind},
        **values,
    }


class PregenerationFailuresTest(unittest.TestCase):
    def test_missing_state_is_empty(self):
        with TemporaryDirectory() as directory:
            inputs, _item = _inputs(Path(directory))
            self.assertEqual(inspect_preparation_failures(inputs), ())

    def test_measured_silence_uses_rejection_limits_and_exact_queue_text(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            result = _failure("speech_silence")
            result["failure"]["speech_quality"] = {
                "leading_silence_seconds": 0.96,
                "trailing_silence_seconds": 1.04,
                "longest_internal_silence_seconds": 1.68,
                "silence_ratio": 0.64,
            }
            result["failure"]["pause_diagnosis"] = {
                "classification": "sentence_boundary_pause_candidate",
                "threshold_seconds": 0.5,
            }
            _save(inputs, item, result)
            failure = inspect_preparation_failures(inputs)[0]
            self.assertEqual(failure.text, item["text"])
            self.assertEqual(failure.line_id, item["line_id"])
            self.assertEqual(failure.speaker, "Hero")
            self.assertEqual(
                failure.reason,
                "Leading silence: 0.96 s (limit 0.8 s); "
                "Trailing silence: 1.04 s (limit 0.8 s); "
                "Internal pause: 1.68 s (limit 1.2 s); "
                "Silence: 64% (limit 50%)",
            )
            self.assertNotIn("sentence", failure.reason)
            self.assertEqual(failure.details, "Original provider error")

    def test_small_threshold_excess_is_not_rounded_away(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            result = _failure("speech_silence")
            result["failure"]["speech_quality"] = {
                "leading_silence_seconds": 0.801,
                "silence_ratio": 0.5001,
            }
            _save(inputs, item, result)
            self.assertEqual(
                inspect_preparation_failures(inputs)[0].reason,
                "Leading silence: 0.801 s (limit 0.8 s); Silence: 50.01% (limit 50%)",
            )

    def test_other_reasons_preserve_details_and_limits(self):
        for kind, phrase in (
            ("missed_eos_audio_limit", "reached a limit"),
            ("reference_unavailable", "reference was missing"),
            ("interrupted", "was interrupted"),
            ("backend_error", "engine could not generate"),
            ("speech_silence", "pause check"),
        ):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                inputs, item = _inputs(Path(directory))
                result = _failure(kind)
                result["failure"]["render"] = {
                    "max_audio_seconds": 30,
                    "max_tokens": 200,
                }
                result["last_error"] = "Provider failed at /private/provider/output.wav"
                _save(inputs, item, result)
                failure = inspect_preparation_failures(inputs)[0]
                self.assertIn(phrase, failure.reason)
                self.assertNotIn("/private", failure.reason)
                self.assertIn(result["last_error"], failure.details)
                self.assertIn('"max_tokens": 200', failure.details)

    def test_legacy_and_explicit_no_audio_are_distinct(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            _save(inputs, item, {"status": "failed", "last_error": "Missing reference"})
            failure = inspect_preparation_failures(inputs)[0]
            self.assertIn("reference", failure.reason)
            self.assertEqual(failure.audio_status, "unavailable")
            _save(inputs, item, _failure(failure_audio={"created": False}))
            self.assertEqual(
                inspect_preparation_failures(inputs)[0].audio_status, "not_created"
            )

    def test_fallback_preserves_failure_but_resolved_outputs_disappear(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            _save(
                inputs,
                item,
                _failure(status="live_fallback", review_status="live_fallback"),
            )
            self.assertEqual(len(inspect_preparation_failures(inputs)), 1)
            for status, review in (
                ("generated", "pending_review"),
                ("approved", "approved"),
            ):
                _save(inputs, item, _failure(status=status, review_status=review))
                self.assertEqual(inspect_preparation_failures(inputs), ())
            _save(
                inputs,
                item,
                {"status": "live_fallback", "review_status": "live_fallback"},
            )
            self.assertEqual(inspect_preparation_failures(inputs), ())

    def test_cancellation_only_displays_preserved_defect(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            result = _failure("cancelled")
            _save(inputs, item, result)
            self.assertEqual(inspect_preparation_failures(inputs), ())
            result["failure"]["previous_failure"] = _failure("interrupted")
            _save(inputs, item, result)
            self.assertIn("interrupted", inspect_preparation_failures(inputs)[0].reason)

    def test_queue_state_digest_and_presented_identities_must_match(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            _save(inputs, item, _failure())
            with self.assertRaises(ValueError):
                inspect_preparation_failures(replace(inputs, queue_sha256="f" * 64))
            _save(inputs, item, _failure(), queue_sha256="f" * 64)
            with self.assertRaises(ValueError):
                inspect_preparation_failures(inputs)
            for name in ("queue_id", "line_id", "text_sha256", "speaker", "text"):
                _save(inputs, item, _failure(**{name: "wrong"}))
                with self.subTest(field=name), self.assertRaises(ValueError):
                    inspect_preparation_failures(inputs)
            _save(inputs, {**item, "queue_id": "unknown"}, _failure())
            with self.assertRaises(ValueError):
                inspect_preparation_failures(inputs)

    def test_audio_is_read_only_on_play_and_tampering_is_rejected(self):
        with TemporaryDirectory() as directory:
            inputs, item = _inputs(Path(directory))
            inputs.output_directory.mkdir()
            source = inputs.output_directory / "partial.wav"
            payload = io.BytesIO()
            with wave.open(payload, "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(b"\x00\x01" * 160)
            source.write_bytes(payload.getvalue())
            metadata = retain_failed_audio(
                inputs.output_directory, item["queue_id"], source
            )
            _save(inputs, item, _failure(failure_audio=metadata))
            with patch(
                "vntts.pregeneration_failures.read_failed_audio",
                side_effect=AssertionError,
            ):
                failure = inspect_preparation_failures(inputs)[0]
            self.assertEqual(failure.audio_status, "available")
            self.assertEqual(
                load_preparation_failure_audio(inputs, failure), payload.getvalue()
            )
            retained = inputs.output_directory / metadata["path"]
            retained.write_bytes(payload.getvalue() + b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_preparation_failure_audio(inputs, failure)
            retained.unlink()
            self.assertEqual(
                inspect_preparation_failures(inputs)[0].audio_status, "unavailable"
            )
            with self.assertRaises(ValueError):
                load_preparation_failure_audio(inputs, failure)
            _save(inputs, item, {"status": "approved", "review_status": "approved"})
            with self.assertRaises(ValueError):
                load_preparation_failure_audio(inputs, failure)


if __name__ == "__main__":
    unittest.main()
