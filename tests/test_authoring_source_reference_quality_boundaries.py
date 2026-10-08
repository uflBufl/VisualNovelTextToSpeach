import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.source_reference_fixtures import publish_source_reference_quality_fixture
from vntts.authoring.source_reference_quality import (
    SourceReferenceQualityError,
    load_source_reference_quality_review,
    publish_source_reference_quality_review,
)


class SourceReferenceQualityBoundariesTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
