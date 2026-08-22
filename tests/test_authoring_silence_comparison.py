import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np
from vntts_artifacts.audio import read_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.cli import main as authoring_main
from vntts.authoring.listening import load_listening_session
from vntts.authoring.silence_comparison import (
    SILENCE_COMPARISON_INPUT_SCHEMA,
    SILENCE_COMPARISON_INPUT_VERSION,
    SilenceComparisonError,
    SilenceComparisonSample,
    create_silence_comparison_session,
    load_silence_comparison,
    load_silence_comparison_input_plan,
    publish_silence_comparison,
)


class AuthoringSilenceComparisonTest(unittest.TestCase):
    def _fixture(self, root):
        root = Path(root)
        speech = np.full(800, 0.2, dtype=np.float32)
        raw = root / "raw.wav"
        segmented = root / "segmented.wav"
        write_pcm16_wav(
            raw,
            np.concatenate((speech, np.zeros(1_600, dtype=np.float32), speech)),
            1_000,
        )
        write_pcm16_wav(
            segmented,
            np.concatenate((speech, np.zeros(180, dtype=np.float32), speech)),
            1_000,
        )
        sample = SilenceComparisonSample(
            "queue:one",
            "line:one",
            "The gate is already open. We should leave before dawn.",
            raw,
            segmented,
        )
        return sample

    def _write_input_plan(self, root, sample):
        root = Path(root)
        plan = root / "comparison-input.json"
        text_sha256 = hashlib.sha256(sample.text.encode("utf-8")).hexdigest()
        plan.write_text(
            json.dumps(
                {
                    "schema": SILENCE_COMPARISON_INPUT_SCHEMA,
                    "schema_version": SILENCE_COMPARISON_INPUT_VERSION,
                    "samples": [
                        {
                            "queue_id": sample.queue_id,
                            "line_id": sample.line_id,
                            "text": sample.text,
                            "text_sha256": text_sha256,
                            "raw_audio": sample.raw_audio.name,
                            "raw_audio_sha256": sha256_file(sample.raw_audio),
                            "segmented_audio": sample.segmented_audio.name,
                            "segmented_audio_sha256": sha256_file(
                                sample.segmented_audio
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return plan

    def test_publishes_checksum_bound_reports_and_blind_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)

            result = publish_silence_comparison((sample,), root / "comparison")
            document = load_silence_comparison(result.directory)
            session = create_silence_comparison_session(
                result.directory, root / "listening", seed=7
            )

            self.assertEqual(result.sample_count, 1)
            self.assertFalse(document["policy"]["production_enabled"])
            self.assertTrue(document["policy"]["requires_blind_review"])
            self.assertEqual(
                document["samples"][0]["transform"]["removed_samples"], 1_000
            )
            compressed = result.directory / document["samples"][0]["compressed_audio"]
            samples, info = read_pcm16_mono_wav(compressed)
            self.assertEqual(len(samples), 2_200)
            self.assertEqual(info.duration_seconds, 2.2)
            loaded_session = load_listening_session(session)
            self.assertEqual(loaded_session["trial_count"], 1)
            self.assertEqual(set(loaded_session["trials"][0]["audio"]), {"a", "b"})

    def test_loader_rejects_tampered_or_escaping_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = publish_silence_comparison(
                (self._fixture(root),), root / "comparison"
            )
            document = json.loads(
                (result.directory / "comparison.json").read_text(encoding="utf-8")
            )
            original_document = json.loads(json.dumps(document))
            document["samples"][0]["compressed_audio_sha256"] = "0" * 64
            (result.directory / "comparison.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(SilenceComparisonError, "not bound"):
                load_silence_comparison(result.directory)

            (result.directory / "comparison.json").write_text(
                json.dumps(original_document), encoding="utf-8"
            )
            artifact = result.directory / document["artifacts"][0]["path"]
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(SilenceComparisonError, "checksum changed"):
                load_silence_comparison(result.directory)

            document["artifacts"][0]["path"] = "../outside.wav"
            (result.directory / "comparison.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(SilenceComparisonError, "leaves"):
                load_silence_comparison(result.directory)

    def test_publication_rechecks_source_bytes_and_never_replaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)
            output = root / "comparison"
            real_write = Path.write_bytes
            mutated = False

            def write_and_mutate(path, payload):
                nonlocal mutated
                result = real_write(path, payload)
                if not mutated and Path(path).name.endswith("-raw.wav"):
                    mutated = True
                    write_pcm16_wav(
                        sample.raw_audio,
                        np.full(3_200, 0.1, dtype=np.float32),
                        1_000,
                    )
                return result

            with mock.patch.object(Path, "write_bytes", write_and_mutate):
                with self.assertRaisesRegex(SilenceComparisonError, "changed during"):
                    publish_silence_comparison((sample,), output)
            self.assertFalse(output.exists())

            sample = self._fixture(root)
            publish_silence_comparison((sample,), output)
            digest = sha256_file(output / "comparison.json")
            with self.assertRaisesRegex(SilenceComparisonError, "already exists"):
                publish_silence_comparison((sample,), output)
            self.assertEqual(sha256_file(output / "comparison.json"), digest)

    def test_input_plan_and_cli_publish_check_and_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)
            plan_path = self._write_input_plan(root, sample)

            plan = load_silence_comparison_input_plan(plan_path)
            self.assertEqual(plan.samples[0].queue_id, sample.queue_id)
            self.assertEqual(plan.samples[0].line_id, sample.line_id)
            self.assertEqual(plan.samples[0].text, sample.text)
            self.assertEqual(plan.samples[0].raw_audio, sample.raw_audio.resolve())
            self.assertEqual(
                plan.samples[0].segmented_audio, sample.segmented_audio.resolve()
            )
            self.assertEqual(plan.sha256, sha256_file(plan_path))

            publication_output = io.StringIO()
            comparison = root / "comparison"
            with redirect_stdout(publication_output):
                self.assertEqual(
                    authoring_main(
                        [
                            "silence-comparison-publish",
                            str(plan_path),
                            "--output",
                            str(comparison),
                        ]
                    ),
                    0,
                )
            publication = json.loads(publication_output.getvalue())
            self.assertEqual(publication["sample_count"], 1)
            self.assertEqual(publication["input_plan_sha256"], plan.sha256)

            check_output = io.StringIO()
            with redirect_stdout(check_output):
                self.assertEqual(
                    authoring_main(["silence-comparison-check", str(comparison)]),
                    0,
                )
            inspection = json.loads(check_output.getvalue())
            self.assertFalse(inspection["production_enabled"])
            self.assertTrue(inspection["requires_blind_review"])
            self.assertEqual(inspection["sample_count"], 1)
            self.assertEqual(inspection["input_plan_sha256"], plan.sha256)

            session_output = io.StringIO()
            listening = root / "listening"
            with redirect_stdout(session_output):
                self.assertEqual(
                    authoring_main(
                        [
                            "silence-comparison-session",
                            str(comparison),
                            "--output",
                            str(listening),
                            "--seed",
                            "17",
                        ]
                    ),
                    0,
                )
            session_path = Path(json.loads(session_output.getvalue())["session"])
            self.assertEqual(load_listening_session(session_path)["trial_count"], 1)

    def test_input_plan_rejects_changed_audio_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)
            plan_path = self._write_input_plan(root, sample)
            sample.raw_audio.write_bytes(sample.segmented_audio.read_bytes())
            with self.assertRaisesRegex(SilenceComparisonError, "checksum changed"):
                load_silence_comparison_input_plan(plan_path)

            sample = self._fixture(root)
            plan_path = self._write_input_plan(root, sample)
            alias = root / "raw-alias.wav"
            alias.symlink_to(sample.raw_audio)
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["samples"][0]["raw_audio"] = alias.name
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SilenceComparisonError, "symlink"):
                load_silence_comparison_input_plan(plan_path)

    def test_loaded_input_plan_remains_bound_during_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)
            plan = load_silence_comparison_input_plan(
                self._write_input_plan(root, sample)
            )
            write_pcm16_wav(
                sample.raw_audio,
                np.concatenate(
                    (
                        np.full(800, 0.1, dtype=np.float32),
                        np.zeros(1_600, dtype=np.float32),
                        np.full(800, 0.1, dtype=np.float32),
                    )
                ),
                1_000,
            )

            output = root / "comparison"
            with self.assertRaisesRegex(SilenceComparisonError, "Planned raw"):
                publish_silence_comparison(plan.samples, output)
            self.assertFalse(output.exists())

    def test_loader_rejects_policy_report_and_inventory_forgery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = publish_silence_comparison(
                (self._fixture(root),), root / "comparison"
            )
            comparison_path = result.directory / "comparison.json"
            original = json.loads(comparison_path.read_text(encoding="utf-8"))

            forged = json.loads(json.dumps(original))
            forged["policy"]["production_enabled"] = True
            comparison_path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(SilenceComparisonError, "policy"):
                load_silence_comparison(result.directory)

            forged = json.loads(json.dumps(original))
            forged["samples"][0]["transform"]["removed_samples"] += 1
            comparison_path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(SilenceComparisonError, "transform ledger"):
                load_silence_comparison(result.directory)

            comparison_path.write_text(json.dumps(original), encoding="utf-8")
            report_path = result.directory / original["reports"][0]
            original_report_payload = report_path.read_bytes()
            report = json.loads(original_report_payload)
            report["samples"][0]["text"] = "Different words. Still different words."
            report_path.write_text(json.dumps(report), encoding="utf-8")
            forged = json.loads(json.dumps(original))
            artifact = next(
                value
                for value in forged["artifacts"]
                if value["path"] == original["reports"][0]
            )
            artifact["sha256"] = sha256_file(report_path)
            comparison_path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(SilenceComparisonError, "diverges"):
                load_silence_comparison(result.directory)

            report_path.write_bytes(original_report_payload)
            comparison_path.write_text(json.dumps(original), encoding="utf-8")
            (result.directory / "unlisted.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(SilenceComparisonError, "not exact"):
                load_silence_comparison(result.directory)


if __name__ == "__main__":
    unittest.main()
