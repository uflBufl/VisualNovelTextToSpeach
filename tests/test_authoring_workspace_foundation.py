import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.workspace_foundation import (
    contained_path,
    load_json_object,
    load_json_object_snapshot,
    read_regular_file,
    require_sha256,
    safe_relative_path,
)


class FoundationError(RuntimeError):
    pass


class AuthoringWorkspaceFoundationTest(unittest.TestCase):
    def test_path_primitives_preserve_containment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            relative = safe_relative_path(
                "nested/file.json", "Artifact", error_type=FoundationError
            )
            self.assertEqual(
                contained_path(root, relative, "Artifact", error_type=FoundationError),
                root / relative,
            )
            with self.assertRaisesRegex(FoundationError, "stay inside"):
                safe_relative_path("../escape", "Artifact", error_type=FoundationError)
            with self.assertRaisesRegex(FoundationError, "leaves its owning"):
                contained_path(
                    root,
                    Path("../escape"),
                    "Artifact",
                    error_type=FoundationError,
                )

    def test_file_and_json_snapshots_are_exact(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "document.json"
            document = {"value": 1}
            payload = json.dumps(document).encode("utf-8")
            path.write_bytes(payload)

            self.assertEqual(
                read_regular_file(path, "document", error_type=FoundationError),
                payload,
            )
            self.assertEqual(
                load_json_object(path, "document", error_type=FoundationError),
                document,
            )
            loaded, digest, captured = load_json_object_snapshot(
                path, "document", error_type=FoundationError
            )

        self.assertEqual(loaded, document)
        self.assertEqual(captured, payload)
        self.assertEqual(len(digest), 64)

    def test_sha256_validation_uses_the_supplied_error_type(self):
        self.assertEqual(
            require_sha256("a" * 64, "Digest", error_type=FoundationError),
            "a" * 64,
        )
        with self.assertRaisesRegex(FoundationError, "hexadecimal"):
            require_sha256("z" * 64, "Digest", error_type=FoundationError)


if __name__ == "__main__":
    unittest.main()
