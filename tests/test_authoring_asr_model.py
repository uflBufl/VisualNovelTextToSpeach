import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.authoring.asr_model import (
    ManagedAsrModel,
    ManagedAsrModelError,
    install_managed_asr_model,
    managed_asr_status,
    resolve_managed_asr_model,
)
from vntts.authoring.bulk_generation import sha256_control_path
from vntts.authoring.cli import create_parser


def _model(source):
    return ManagedAsrModel(
        model_id="test-whisper",
        repository="example/test-whisper",
        revision="a" * 40,
        tree_sha256=sha256_control_path(source),
        files=("config.json", "model.safetensors"),
        snapshot_license="Apache-2.0",
        snapshot_license_url="https://example.invalid/model",
        upstream_license="MIT",
        upstream_license_url="https://example.invalid/license",
    )


class ManagedAsrModelTest(unittest.TestCase):
    def _source(self, root):
        source = root / "source"
        source.mkdir()
        (source / "config.json").write_text('{"model":"tiny"}\n', encoding="utf-8")
        (source / "model.safetensors").write_bytes(b"safe tensor bytes")
        return source

    def test_import_is_atomic_verified_and_idempotent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            model = _model(source)

            result = install_managed_asr_model(
                model,
                root=root / "managed",
                source=source,
            )
            repeated = install_managed_asr_model(
                model,
                root=root / "managed",
                source=root / "missing-source-is-not-read",
            )

            self.assertEqual(result, repeated)
            self.assertEqual(result["status"], "installed")
            self.assertEqual(
                resolve_managed_asr_model(model, root=root / "managed"),
                Path(result["model_directory"]),
            )
            metadata = json.loads(
                (Path(result["installation"]) / "managed-model.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["revision"], "a" * 40)
            self.assertEqual(
                [entry["spdx"] for entry in metadata["licenses"]],
                ["Apache-2.0", "MIT"],
            )

    def test_download_fetches_only_allowlisted_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            model = _model(source)
            fetched = []

            def fetch(requested_model, filename):
                self.assertEqual(requested_model, model)
                fetched.append(filename)
                return source / filename

            result = install_managed_asr_model(
                model,
                root=root / "managed",
                fetch_file=fetch,
            )

            self.assertEqual(tuple(fetched), model.files)
            self.assertEqual(result["actual_tree_sha256"], model.tree_sha256)

    def test_corruption_fails_closed_without_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._source(root)
            model = _model(source)
            result = install_managed_asr_model(
                model,
                root=root / "managed",
                source=source,
            )
            Path(result["model_directory"], "config.json").write_text(
                "changed", encoding="utf-8"
            )

            status = managed_asr_status(model, root=root / "managed")
            self.assertEqual(status["status"], "invalid")
            with self.assertRaisesRegex(ManagedAsrModelError, "Refusing to overwrite"):
                install_managed_asr_model(
                    model,
                    root=root / "managed",
                    source=source,
                )

    def test_missing_model_has_actionable_offline_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = _model(self._source(root))
            with self.assertRaisesRegex(ManagedAsrModelError, "asr-model-install"):
                resolve_managed_asr_model(model, root=root / "managed")

    def test_cli_model_path_is_optional_and_offline_is_explicit(self):
        parser = create_parser()
        managed = parser.parse_args(
            [
                "speech-robustness-asr",
                "corpus",
                "--output",
                "report.json",
                "--offline",
            ]
        )
        explicit = parser.parse_args(
            [
                "speech-robustness-asr",
                "corpus",
                "model",
                "--output",
                "report.json",
            ]
        )

        self.assertIsNone(managed.model)
        self.assertTrue(managed.offline)
        self.assertEqual(explicit.model, Path("model"))
        self.assertFalse(explicit.offline)


if __name__ == "__main__":
    unittest.main()
