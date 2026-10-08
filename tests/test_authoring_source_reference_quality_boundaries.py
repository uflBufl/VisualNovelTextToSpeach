import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.source_reference_fixtures import publish_source_reference_quality_fixture
from vntts.authoring import source_reference_quality
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


if __name__ == "__main__":
    unittest.main()
