"""Pure catalog admission gates before Qt construction or output publication."""

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.ui_catalog import _review_packet, _write_catalog, load_catalog

ROOT = Path(__file__).resolve().parents[1]


class UiCatalogSchemaTest(unittest.TestCase):
    def test_invalid_catalog_fails_at_admission(self) -> None:
        original = json.loads((ROOT / "ui-catalog.json").read_text())
        invalid: list[object] = [None, [], True]
        for field, values in {
            "schema_version": (True, 1.0, 2),
            "title": (None, "", [], 3),
            "contracts": (None, [], {"rule": False}),
            "surfaces": (None, {}, [False]),
        }.items():
            for value in values:
                invalid.append({**original, field: value})
        for field, values in {
            "related": (None, "dashboard", [{}], ["missing"]),
            "contracts": (None, "app-shell", [[]], ["missing"]),
            "stories": (None, {}, [False], [{"id": "x"}]),
            "canonical_owner": (None, "missing"),
            "id": (None, "../outside", "..\\outside", "bad\0name"),
        }.items():
            for value in values:
                document = copy.deepcopy(original)
                document["surfaces"][0][field] = value
                invalid.append(document)
        for value in ("../outside", "..\\outside", "bad\0name"):
            document = copy.deepcopy(original)
            document["surfaces"][0]["stories"][0]["id"] = value
            invalid.append(document)
        duplicate_surface = copy.deepcopy(original)
        duplicate_surface["surfaces"].append(
            {**duplicate_surface["surfaces"][0], "stories": []}
        )
        invalid.append(duplicate_surface)
        duplicate_story = copy.deepcopy(original)
        duplicate_story["surfaces"][1]["stories"].append(
            duplicate_story["surfaces"][0]["stories"][0]
        )
        invalid.append(duplicate_story)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            for document in invalid:
                with self.subTest(document=document):
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_catalog(path)

    def test_optional_arrays_and_metadata_preserve_public_output(self) -> None:
        original = json.loads((ROOT / "ui-catalog.json").read_text())
        original["private_metadata"] = {"source_path": "private"}
        original["surfaces"][0]["private_metadata"] = "private"
        original["surfaces"][0]["stories"][0]["private_metadata"] = "private"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            catalog = load_catalog(path)
            packet = _review_packet(catalog, catalog["surfaces"][0], {})
            self.assertNotIn("private_metadata", json.dumps(packet))
            self.assertIsNone(packet["target"]["stories"][0]["screenshot"])
            self.assertTrue(any(not item["stories"] for item in catalog["surfaces"]))
            for surface in original["surfaces"]:
                for field in ("stories", "related", "contracts"):
                    if surface.get(field) == []:
                        surface.pop(field)
            path.write_text(json.dumps(original), encoding="utf-8")
            self.assertEqual(load_catalog(path), catalog)
            _write_catalog(catalog, Path(directory), {})
            self.assertTrue((Path(directory) / "index.html").is_file())
            self.assertEqual(
                json.loads(
                    (Path(directory) / "review-packets/dashboard.json").read_text()
                ),
                packet,
            )

    def test_cli_rejects_bad_catalog_before_creating_output(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text('{"schema_version": true, "contracts": {}, "surfaces": []}')
            output = Path(directory) / "output"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/render_ui_catalog.py",
                    "--catalog",
                    str(path),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must use schema_version 1", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
