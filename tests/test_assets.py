import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from vntts.assets import (
    ModelAsset,
    ModelAssetManager,
    ModelDownloadCancelled,
    ModelIntegrityError,
    VoicePackManager,
)
from vntts.voices import CharacterVoiceRegistry


class MemoryResponse:
    def __init__(self, data, *, status=200, headers=None):
        self.data = data
        self.position = 0
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self, size):
        chunk = self.data[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


class MemoryOpener:
    def __init__(self, files):
        self.files = files
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        data = self.files[request.full_url]
        if request.get_method() == "HEAD":
            return MemoryResponse(b"", headers={"Content-Length": str(len(data))})
        range_header = request.get_header("Range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            return MemoryResponse(
                data[start:],
                status=206,
                headers={"Content-Length": str(len(data) - start)},
            )
        return MemoryResponse(
            data,
            headers={"Content-Length": str(len(data))},
        )


class ModelAssetManagerTest(unittest.TestCase):
    def create_asset(self):
        return ModelAsset(
            name="tts_models/test/dataset/model",
            urls=(
                "https://models.invalid/model.pth",
                "https://models.invalid/hash.md5",
            ),
            expected_hash="publisher-hash",
        )

    def test_download_reports_progress_and_verifies_checksums(self):
        asset = self.create_asset()
        opener = MemoryOpener(
            {
                asset.urls[0]: b"model-weights",
                asset.urls[1]: b"publisher-hash\n",
            }
        )
        progress = []
        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory, opener=opener)

            model_path = manager.download(
                asset.name,
                asset=asset,
                progress=lambda percent, message: progress.append((percent, message)),
            )

            self.assertEqual(manager.validate(asset.name, asset=asset), model_path)
            manifest = json.loads(
                (model_path / "vntts-asset.json").read_text(encoding="utf-8")
            )

        self.assertIn("model.pth", manifest["files"])
        self.assertEqual(progress[-1][0], 100)
        self.assertIn("checksums passed", progress[-1][1])

    def test_cancelled_download_keeps_partial_file_and_retry_resumes(self):
        asset = self.create_asset()
        model_data = b"x" * (2 * 1024 * 1024 + 17)
        opener = MemoryOpener(
            {
                asset.urls[0]: model_data,
                asset.urls[1]: b"publisher-hash\n",
            }
        )
        cancel_event = Event()
        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory, opener=opener)

            with self.assertRaises(ModelDownloadCancelled):
                manager.download(
                    asset.name,
                    asset=asset,
                    cancel_event=cancel_event,
                    progress=lambda _percent, _message: cancel_event.set(),
                )

            partial = manager.model_path(asset.name) / "model.pth.part"
            self.assertTrue(partial.is_file())
            partial_size = partial.stat().st_size
            self.assertGreater(partial_size, 0)

            manager.download(asset.name, asset=asset)

            self.assertTrue(manager.is_ready_with_asset(asset.name, asset))
            get_requests = [
                request for request in opener.requests if request.get_method() == "GET"
            ]

        self.assertTrue(
            any(request.get_header("Range") for request in get_requests),
            "Retry should continue from the partial file",
        )

    def test_validation_detects_modified_model_file(self):
        asset = self.create_asset()
        opener = MemoryOpener(
            {
                asset.urls[0]: b"model-weights",
                asset.urls[1]: b"publisher-hash\n",
            }
        )
        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory, opener=opener)
            model_path = manager.download(asset.name, asset=asset)
            (model_path / "model.pth").write_bytes(b"tampered")

            with self.assertRaisesRegex(ModelIntegrityError, "size changed"):
                manager.validate(asset.name, asset=asset)


class VoicePackManagerTest(unittest.TestCase):
    def test_import_voice_copies_local_references_and_builds_manifest(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "marcus.wav"
            source.write_bytes(b"local voice data")
            manager = VoicePackManager(root / "managed")

            manifest = manager.import_voice(
                "Marcus",
                [source],
                aliases=["Ms. Hoffman"],
            )
            registry = CharacterVoiceRegistry.from_file(manifest)
            voice = registry.resolve("Ms. Hoffman")

            self.assertEqual(voice.character, "Marcus")
            self.assertTrue(voice.references[0].is_file())
            self.assertNotEqual(voice.references[0], source)
            self.assertEqual(manager.validate(manifest), manifest)

    def test_import_manifest_copies_pack_without_modifying_source(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_pack = root / "source"
            source_pack.mkdir()
            reference = source_pack / "x.ogg"
            reference.write_bytes(b"voice")
            source_manifest = source_pack / "manifest.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "X",
                                "speaker": "x-v2",
                                "reference": reference.name,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manager = VoicePackManager(root / "managed")

            imported = manager.import_pack(source_manifest)
            imported_voice = CharacterVoiceRegistry.from_file(imported).resolve("X")

            self.assertTrue(reference.is_file())
            self.assertTrue(imported_voice.reference.is_file())
            self.assertNotEqual(imported_voice.reference, reference)

    def test_validation_detects_modified_voice_manifest(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "marcus.wav"
            source.write_bytes(b"local voice data")
            manager = VoicePackManager(root / "managed")
            manifest_path = manager.import_voice("Marcus", [source])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["voices"][0]["character"] = "Tampered"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                ModelIntegrityError,
                "Voice manifest checksum failed",
            ):
                manager.validate(manifest_path)


if __name__ == "__main__":
    unittest.main()
