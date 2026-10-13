import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
from vntts_artifacts.audio import read_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from tests.authoring_fixtures import (
    write_silence_comparison_input_plan,
    write_silence_comparison_sample,
)
from tests.symlink_support import symlink_or_skip
from vntts.authoring import silence_comparison as comparison_module
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.listening import load_listening_session
from vntts.authoring.silence_comparison import (
    SilenceComparisonError,
    create_silence_comparison_session,
    load_silence_comparison,
    load_silence_comparison_input_plan,
    publish_silence_comparison,
)


class AuthoringSilenceComparisonTest(unittest.TestCase):
    def test_truncated_odd_byte_wav_is_a_domain_error_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            payload = sample.raw_audio.read_bytes()[:-1]
            sample.raw_audio.write_bytes(payload)
            destination = root / "comparison"

            with self.assertRaisesRegex(
                SilenceComparisonError,
                "Unable to read raw comparison audio.*incomplete",
            ):
                publish_silence_comparison((sample,), destination)

            self.assertFalse(destination.exists())
            self.assertEqual(sample.raw_audio.read_bytes(), payload)

    def test_captured_wav_decode_preserves_exact_pcm_and_empty_stream(self):
        import wave

        for samples in ([-32768, -1, 0, 1, 32767], []):
            with (
                self.subTest(samples=samples),
                tempfile.TemporaryDirectory() as temporary,
            ):
                path = Path(temporary) / "source.wav"
                with wave.open(str(path), "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(22_050)
                    writer.writeframes(np.asarray(samples, dtype="<i2").tobytes())
                payload = path.read_bytes()
                expected_hash = sha256_file(path)
                result = comparison_module._read_source_wav(path, "comparison audio")
                self.assertEqual(result[:3], (path.resolve(), payload, expected_hash))
                np.testing.assert_array_equal(
                    result[3], np.asarray(samples, dtype=np.float32) / 32768.0
                )
                self.assertEqual(result[3].dtype, np.dtype(np.float32))
                self.assertEqual(result[4], 22_050)

    def test_publishes_checksum_bound_reports_and_blind_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)

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

    def test_reports_preserve_sample_order_and_share_the_comparison_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = write_silence_comparison_sample(root)
            second = replace(first, queue_id="second-sample", line_id="second-line")
            result = publish_silence_comparison((second, first), root / "comparison")
            document = load_silence_comparison(result.directory)
            self.assertEqual(
                [sample["queue_id"] for sample in document["samples"]],
                [second.queue_id, first.queue_id],
            )
            for path, audio_field, digest_field in (
                (result.report_paths[0], "segmented_copy", "segmented_source_sha256"),
                (result.report_paths[1], "compressed_audio", "compressed_audio_sha256"),
            ):
                with self.subTest(report=path.name):
                    report = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        report["samples"],
                        [
                            {
                                "id": sample["queue_id"],
                                "line_id": sample["line_id"],
                                "text": sample["text"],
                                "text_sha256": sample["text_sha256"],
                                "audio": f"../{sample[audio_field]}",
                                "audio_sha256": sample[digest_field],
                            }
                            for sample in document["samples"]
                        ],
                    )
            session = create_silence_comparison_session(
                result.directory, root / "listening", seed=7
            )
            self.assertEqual(load_listening_session(session)["trial_count"], 2)

    def test_invalid_target_is_rejected_before_sources_or_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            output = root / "new-parent" / "comparison"
            for value in (
                None,
                "0.6",
                [],
                {},
                True,
                False,
                10**400,
                -(10**400),
                float("nan"),
                float("inf"),
                float("-inf"),
                0,
                -1,
            ):
                with (
                    self.subTest(target=value),
                    mock.patch.object(comparison_module, "_read_source_wav") as read,
                ):
                    with self.assertRaisesRegex(SilenceComparisonError, "policy"):
                        publish_silence_comparison(
                            (sample,), output, target_seconds=value
                        )
                    read.assert_not_called()
                    self.assertFalse(output.parent.exists())

    def test_racing_publication_preserves_competitor_and_uses_domain_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            output = root / "comparison"
            rename = comparison_module.rename_directory_no_replace

            def race(staging, destination):
                destination.mkdir()
                (destination / "competitor").write_bytes(b"unchanged")
                return rename(staging, destination)

            with mock.patch.object(
                comparison_module, "rename_directory_no_replace", side_effect=race
            ):
                with self.assertRaises(SilenceComparisonError):
                    publish_silence_comparison((sample,), output)
            self.assertEqual((output / "competitor").read_bytes(), b"unchanged")
            self.assertEqual(list(output.iterdir()), [output / "competitor"])
            self.assertEqual(list(root.glob(".comparison.staging-*")), [])

    def test_loader_rejects_tampered_or_escaping_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = publish_silence_comparison(
                (write_silence_comparison_sample(root),), root / "comparison"
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
            sample = write_silence_comparison_sample(root)
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

            sample = write_silence_comparison_sample(root)
            publish_silence_comparison((sample,), output)
            digest = sha256_file(output / "comparison.json")
            with self.assertRaisesRegex(SilenceComparisonError, "already exists"):
                publish_silence_comparison((sample,), output)
            self.assertEqual(sha256_file(output / "comparison.json"), digest)

    def test_publication_cleans_staging_after_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            output = root / "comparison"
            real_write = Path.write_bytes

            def write_and_interrupt(path, payload):
                result = real_write(path, payload)
                if Path(path).name.endswith("-raw.wav"):
                    raise KeyboardInterrupt
                return result

            with mock.patch.object(Path, "write_bytes", write_and_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    publish_silence_comparison((sample,), output)

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".comparison.staging-*")), [])

    def test_input_plan_and_cli_publish_check_and_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            plan_path = write_silence_comparison_input_plan(root, sample)

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
            sample = write_silence_comparison_sample(root)
            plan_path = write_silence_comparison_input_plan(root, sample)
            sample.raw_audio.write_bytes(sample.segmented_audio.read_bytes())
            with self.assertRaisesRegex(SilenceComparisonError, "checksum changed"):
                load_silence_comparison_input_plan(plan_path)

            sample = write_silence_comparison_sample(root)
            plan_path = write_silence_comparison_input_plan(root, sample)
            alias = root / "raw-alias.wav"
            symlink_or_skip(alias, sample.raw_audio)
            document = json.loads(plan_path.read_text(encoding="utf-8"))
            document["samples"][0]["raw_audio"] = alias.name
            plan_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(SilenceComparisonError, "symlink"):
                load_silence_comparison_input_plan(plan_path)

    def test_loaded_input_plan_remains_bound_during_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            plan = load_silence_comparison_input_plan(
                write_silence_comparison_input_plan(root, sample)
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
                (write_silence_comparison_sample(root),), root / "comparison"
            )
            comparison_path = result.directory / "comparison.json"
            original = json.loads(comparison_path.read_text(encoding="utf-8"))

            for key, value in (
                ("production_enabled", True),
                *(
                    ("target_seconds", target)
                    for target in (
                        10**400,
                        -(10**400),
                        True,
                        0,
                        -1,
                        "0.2",
                        float("inf"),
                        float("nan"),
                    )
                ),
            ):
                with self.subTest(policy=key, value=value):
                    forged = json.loads(json.dumps(original))
                    forged["policy"][key] = value
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

    def test_artifact_writer_excludes_only_root_comparison_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "comparison.json").write_text("{}", encoding="utf-8")
            nested = root / "audio/comparison.json"
            nested.parent.mkdir()
            nested.write_text("{}", encoding="utf-8")

            self.assertEqual(
                comparison_module._silence_comparison_artifacts(root),
                [{"path": "audio/comparison.json", "sha256": sha256_file(nested)}],
            )

    def test_loader_rejects_unlisted_nested_comparison_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = publish_silence_comparison(
                (write_silence_comparison_sample(root),), root / "comparison"
            )
            (result.directory / "audio/comparison.json").write_text(
                "{}", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                SilenceComparisonError, "inventory is not exact"
            ):
                load_silence_comparison(result.directory)

    def test_parsed_schema_versions_require_json_integers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = write_silence_comparison_sample(root)
            plan_path = write_silence_comparison_input_plan(root, sample)
            result = publish_silence_comparison((sample,), root / "comparison")
            comparison_path = result.directory / "comparison.json"
            report_path = result.report_paths[0]
            originals = {
                path: path.read_bytes()
                for path in (plan_path, comparison_path, report_path)
            }
            input_document = json.loads(originals[plan_path])
            comparison_document = json.loads(originals[comparison_path])
            report_document = json.loads(originals[report_path])
            for source, document, read, message in (
                (
                    plan_path,
                    input_document,
                    lambda: load_silence_comparison_input_plan(plan_path),
                    "Unsupported silence comparison input schema",
                ),
                (
                    comparison_path,
                    comparison_document,
                    lambda: load_silence_comparison(result.directory),
                    "Unsupported silence comparison schema",
                ),
                (
                    report_path,
                    report_document,
                    lambda: load_silence_comparison(result.directory),
                    "report is malformed",
                ),
            ):
                for version in (True, 1.0):
                    with self.subTest(source=source.name, version=version):
                        source.write_text(
                            json.dumps({**document, "schema_version": version}),
                            encoding="utf-8",
                        )
                        if source == report_path:
                            changed = json.loads(json.dumps(comparison_document))
                            for artifact in changed["artifacts"]:
                                if (
                                    artifact["path"]
                                    == "reports/sentence-segmentation.json"
                                ):
                                    artifact["sha256"] = sha256_file(report_path)
                            comparison_path.write_text(
                                json.dumps(changed), encoding="utf-8"
                            )
                        with self.assertRaisesRegex(SilenceComparisonError, message):
                            read()
                        source.write_bytes(originals[source])
                        comparison_path.write_bytes(originals[comparison_path])
                read()


if __name__ == "__main__":
    unittest.main()
