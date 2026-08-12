import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.reverse1999_catalog import (
    Reverse1999CatalogError,
    Reverse1999NpcCatalog,
)


def catalog_document(**npc_overrides):
    npc = {
        "id": "520301",
        "display_name": "Kamuta",
        "aliases": ["Village Chief"],
        "language": "en",
        "game_versions": ["3.6.5"],
        "banks": ["kamuta.bnk"],
        "approved_references": [
            {
                "bank": "kamuta.bnk",
                "media_id": 123,
                "source_sha256": "a" * 64,
                "reference": "references/kamuta.wav",
                "reference_sha256": "b" * 64,
            }
        ],
    }
    npc.update(npc_overrides)
    return {"version": 1, "game": "Reverse: 1999", "npcs": [npc]}


class Reverse1999NpcCatalogTest(unittest.TestCase):
    def test_resolves_display_name_alias_and_internal_id(self):
        catalog = Reverse1999NpcCatalog.from_dict(catalog_document())

        self.assertEqual(catalog.resolve(" kamuta ").npc_id, "520301")
        self.assertEqual(catalog.resolve("Village-Chief").npc_id, "520301")
        self.assertEqual(catalog.get(520301).display_name, "Kamuta")
        self.assertEqual(catalog.get("520301").banks, ("kamuta.bnk",))

    def test_rejects_invalid_reference_metadata(self):
        invalid = catalog_document(
            approved_references=[
                {
                    "bank": "other.bnk",
                    "media_id": 0,
                    "source_sha256": "bad",
                    "reference": "",
                    "reference_sha256": "bad",
                }
            ]
        )

        with self.assertRaisesRegex(Reverse1999CatalogError, "reference bank"):
            Reverse1999NpcCatalog.from_dict(invalid)

    def test_loads_and_validates_reference_checksum(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            references = root / "references"
            references.mkdir()
            reference = references / "kamuta.wav"
            reference.write_bytes(b"voice")
            document = catalog_document()
            document["npcs"][0]["approved_references"][0]["reference_sha256"] = (
                hashlib.sha256(b"voice").hexdigest()
            )
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(document), encoding="utf-8")

            catalog = Reverse1999NpcCatalog.load(catalog_path)

            self.assertTrue(catalog.validate_reference_files(root))

    def test_shipped_catalog_matches_approved_local_references(self):
        catalog = Reverse1999NpcCatalog.load()

        self.assertEqual(catalog.version, 1)
        self.assertEqual(catalog.resolve("Kamuta").npc_id, "520301")
        self.assertEqual(catalog.resolve("Selone").npc_id, "521001")
        self.assertEqual(
            catalog.resolve("Selone").banks,
            ("activityvoc_story_npc521001_diqiu.bnk",),
        )
        self.assertTrue(catalog.validate_reference_files())


if __name__ == "__main__":
    unittest.main()
