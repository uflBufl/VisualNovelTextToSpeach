import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.atomic_io import atomic_output_path, atomic_write_json, atomic_write_text


class AtomicIoTest(unittest.TestCase):
    def test_writes_text_and_json(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            text = root / "nested" / "value.txt"
            document = root / "document.json"
            atomic_write_text(text, "ready")
            atomic_write_json(document, {"speaker": "Зима"})
            self.assertEqual(text.read_text(encoding="utf-8"), "ready")
            self.assertEqual(
                json.loads(document.read_text(encoding="utf-8")), {"speaker": "Зима"}
            )
            self.assertTrue(document.read_text(encoding="utf-8").endswith("\n"))

    def test_failed_output_keeps_existing_destination(self):
        with TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "artifact.bin"
            destination.write_bytes(b"stable")
            with self.assertRaises(RuntimeError):
                with atomic_output_path(destination) as temporary:
                    temporary.write_bytes(b"partial")
                    raise RuntimeError("producer failed")
            self.assertEqual(destination.read_bytes(), b"stable")
            self.assertEqual(
                [path.name for path in destination.parent.iterdir()], [destination.name]
            )


if __name__ == "__main__":
    unittest.main()
