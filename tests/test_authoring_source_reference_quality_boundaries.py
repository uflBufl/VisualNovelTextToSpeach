import hashlib
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts_artifacts.audio import write_pcm16_wav

from tests.source_reference_fixtures import publish_source_reference_quality_fixture
from vntts.authoring import source_reference_quality, source_reference_quality_records
from vntts.authoring.publication import AtomicPublicationError
from vntts.authoring.reference_composite import ReferenceCompositeError
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    load_source_reference_quality_review,
    publish_source_reference_quality_review,
)


class SourceReferenceQualityBoundariesTest(unittest.TestCase):
    def test_composite_quality_cli_reports_its_domain_error(self):
        errors = io.StringIO()
        with (
            patch(
                "vntts.authoring.reference_composite.publish_composite_quality_review",
                side_effect=ReferenceCompositeError("Composite publication failed"),
            ),
            redirect_stderr(errors),
        ):
            result = source_reference_quality.main(
                [
                    "create-composite",
                    "--composite",
                    "composite",
                    "--state",
                    "state.json",
                    "--output",
                    "output",
                ]
            )
        self.assertEqual(result, 1)
        self.assertEqual(errors.getvalue(), "Composite publication failed\n")

    def test_quality_publication_requires_exact_integer_media_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, evaluation, generation, quality = (
                publish_source_reference_quality_fixture(root)
            )
            session = load_source_reference_quality_review(quality.session)
            self.assertTrue(
                all(type(card["media_id"]) is int for card in session["variants"])
            )
            comparison_path = evaluation.directory / "comparison.json"
            original = json.loads(comparison_path.read_text(encoding="utf-8"))
            integer_one_index = next(
                index
                for index, variant in enumerate(original["variants"])
                if variant["media_id"] == 1
            )
            for index, media_id in (
                (0, float(original["variants"][0]["media_id"])),
                (integer_one_index, True),
            ):
                with self.subTest(media_id=media_id):
                    document = json.loads(json.dumps(original))
                    document["variants"][index]["media_id"] = media_id
                    comparison_path.write_text(json.dumps(document), encoding="utf-8")
                    output = root / "invalid-quality"
                    with self.assertRaisesRegex(
                        SourceReferenceQualityError, "changed media_id"
                    ):
                        publish_source_reference_quality_review(
                            plan.directory,
                            evaluation.directory,
                            generation.state,
                            output,
                        )
                    self.assertFalse(output.exists())
                    self.assertEqual(list(root.glob(".invalid-quality.staging-*")), [])

    def test_quality_publication_preserves_racing_destination_and_cleans_staging(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            plan, evaluation, generation, _quality = (
                publish_source_reference_quality_fixture(root)
            )
            original_paths = set(root.iterdir())
            output = root / "racing-quality"
            rename = source_reference_quality.rename_directory_no_replace

            def race(staging: Path, destination: Path) -> None:
                destination.mkdir()
                (destination / "sentinel").write_bytes(b"competitor output")
                rename(staging, destination)

            with (
                patch.object(
                    source_reference_quality,
                    "rename_directory_no_replace",
                    side_effect=race,
                ),
                self.assertRaisesRegex(
                    SourceReferenceQualityError,
                    "Publication destination already exists",
                ) as caught,
            ):
                publish_source_reference_quality_review(
                    plan.directory,
                    evaluation.directory,
                    generation.state,
                    output,
                )
            self.assertIsInstance(caught.exception.__cause__, AtomicPublicationError)
            self.assertEqual(set(output.iterdir()), {output / "sentinel"})
            self.assertEqual((output / "sentinel").read_bytes(), b"competitor output")
            self.assertEqual(set(root.iterdir()), original_paths | {output})

    def test_quality_copy_metadata_comes_from_checked_destination_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_pcm16_wav(root / "source.wav", [0.1, -0.1], 16_000)
            original = source.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            write_pcm16_wav(source, [0.2] * 4, 24_000)
            destination = root / "copied.wav"
            copyfile = source_reference_quality_records.shutil.copyfile

            def restore_before_copy(source_path, destination_path):
                source_path.write_bytes(original)
                return copyfile(source_path, destination_path)

            with patch.object(
                source_reference_quality_records.shutil,
                "copyfile",
                side_effect=restore_before_copy,
            ):
                audio = source_reference_quality_records._copy_audio(
                    source, digest, destination
                )
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(audio["audio_sha256"], digest)
            self.assertEqual(audio["sample_rate"], 16_000)
            self.assertEqual(audio["sample_count"], 2)

    def test_quality_copy_rejects_corrupted_destination(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = write_pcm16_wav(root / "source.wav", [0.1, -0.1], 16_000)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()

            def corrupt_copy(_source, destination):
                destination.write_bytes(b"corrupt copied audio")

            with (
                patch.object(
                    source_reference_quality_records.shutil,
                    "copyfile",
                    side_effect=corrupt_copy,
                ),
                self.assertRaisesRegex(
                    SourceReferenceQualityError, "changed while copied"
                ),
            ):
                source_reference_quality_records._copy_audio(
                    source, digest, root / "copied.wav"
                )

    def test_quality_audio_metadata_uses_checksum_bound_payload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = write_pcm16_wav(root / "audio.wav", [0.1, -0.1], 16_000)
            record = {
                "audio": audio.name,
                "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "sample_rate": 16_000,
                "sample_count": 2,
                "duration_seconds": round(2 / 16_000, 6),
            }
            probe = source_reference_quality_records.probe_pcm16_mono_wav

            def replace_before_decode(source):
                audio.write_bytes(b"replaced after checksum")
                return probe(source)

            with patch.object(
                source_reference_quality_records,
                "probe_pcm16_mono_wav",
                side_effect=replace_before_decode,
            ):
                validated = source_reference_quality_records._validate_audio_record(
                    root, record, "captured sample"
                )
            self.assertEqual(validated, audio.resolve())
            self.assertEqual(audio.read_bytes(), b"replaced after checksum")


if __name__ == "__main__":
    unittest.main()
