import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import patch

import vntts.assets as assets
from tests.symlink_support import symlink_or_skip
from vntts.assets import (
    ModelAsset,
    ModelAssetManager,
    ModelDownloadCancelled,
    ModelIntegrityError,
    VoicePackManager,
)
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError


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
                headers={
                    "Content-Length": str(len(data) - start),
                    "Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}",
                },
            )
        return MemoryResponse(
            data,
            headers={"Content-Length": str(len(data))},
        )


class ModelAssetManagerTest(unittest.TestCase):
    def test_configures_private_huggingface_model_cache(self):
        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(storage_root=temporary_directory)
            with patch.dict(os.environ, {}, clear=True):
                cache_root = manager.configure_huggingface_environment()

                self.assertEqual(
                    cache_root,
                    Path(temporary_directory).resolve() / "huggingface",
                )
                self.assertEqual(os.environ["HF_HOME"], str(cache_root))

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

    def test_truncated_download_is_not_marked_ready(self):
        asset = self.create_asset()
        files = {
            asset.urls[0]: b"complete-model-weights",
            asset.urls[1]: b"publisher-hash\n",
        }
        complete_opener = MemoryOpener(files)

        def opener(request, timeout):
            if request.get_method() == "GET" and request.full_url == asset.urls[0]:
                complete_opener.requests.append(request)
                return MemoryResponse(b"truncated")
            return complete_opener(request, timeout)

        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory, opener=opener)

            with self.assertRaisesRegex(ModelIntegrityError, "size"):
                manager.download(asset.name, asset=asset)

            self.assertFalse(
                (manager.model_path(asset.name) / "vntts-asset.json").exists()
            )

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

    def test_resume_rejects_a_response_for_the_wrong_range(self):
        asset = self.create_asset()
        files = {
            asset.urls[0]: b"complete-model-weights",
            asset.urls[1]: b"publisher-hash\n",
        }
        correct = MemoryOpener(files)

        def opener(request, timeout):
            if request.get_header("Range"):
                return MemoryResponse(
                    files[request.full_url],
                    status=206,
                    headers={"Content-Range": "bytes 0-21/22"},
                )
            return correct(request, timeout)

        with TemporaryDirectory() as directory:
            manager = ModelAssetManager(directory, opener=opener)
            partial = manager.model_path(asset.name) / "model.pth.part"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"old")

            with self.assertRaisesRegex(ModelIntegrityError, "invalid resume range"):
                manager.download(asset.name, asset=asset)

            self.assertEqual(partial.read_bytes(), b"old")
            self.assertFalse((partial.parent / "vntts-asset.json").exists())

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

    def test_malformed_checksum_metadata_is_repaired(self):
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
            manifest_path = model_path / "vntts-asset.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"]["model.pth"] = []
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            repaired = manager.download(asset.name, asset=asset)

            self.assertEqual(repaired, model_path)
            self.assertTrue(manager.is_ready_with_asset(asset.name, asset))

    def test_model_validation_rejects_malformed_and_future_checksum_documents(self):
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
            manifest_path = model_path / "vntts-asset.json"
            valid = json.loads(manifest_path.read_text(encoding="utf-8"))

            for payload in ([], {**valid, "version": 2}):
                with self.subTest(payload=payload):
                    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ModelIntegrityError, "malformed|version"
                    ):
                        manager.validate(asset.name, asset=asset)

    def test_model_validation_rejects_aliased_checksum_manifest(self):
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
            manifest_path = model_path / "vntts-asset.json"
            outside = Path(temporary_directory) / "outside-manifest.json"
            outside.write_bytes(manifest_path.read_bytes())
            manifest_path.unlink()
            symlink_or_skip(manifest_path, outside)

            with self.assertRaisesRegex(ModelIntegrityError, "manifest"):
                manager.validate(asset.name, asset=asset)

    def test_model_validation_rejects_oversized_checksum_manifest(self):
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
            manifest_path = model_path / "vntts-asset.json"
            manifest_path.write_text(
                " " * (64 * 1024) + manifest_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ModelIntegrityError, "manifest"):
                manager.validate(asset.name, asset=asset)

    def test_model_download_rejects_aliased_managed_directory(self):
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
            outside = Path(temporary_directory) / "outside-model"
            model_path.rename(outside)
            symlink_or_skip(model_path, outside, target_is_directory=True)

            with self.assertRaisesRegex(ModelIntegrityError, "directory"):
                manager.download(asset.name, asset=asset)

    def test_model_download_rejects_aliased_model_file(self):
        asset = self.create_asset()
        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory)
            model_path = manager.model_path(asset.name)
            model_path.mkdir(parents=True)
            outside = Path(temporary_directory) / "outside-model.pth"
            outside.write_bytes(b"model-weights")
            symlink_or_skip(model_path / "model.pth", outside)
            (model_path / "hash.md5").write_text(
                "publisher-hash\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ModelIntegrityError, "file must not be an alias"
            ):
                manager.download(asset.name, asset=asset)

    def test_model_download_rejects_aliased_partial_file(self):
        asset = self.create_asset()
        opener = MemoryOpener(
            {
                asset.urls[0]: b"model-weights",
                asset.urls[1]: b"publisher-hash\n",
            }
        )
        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory, opener=opener)
            model_path = manager.model_path(asset.name)
            model_path.mkdir(parents=True)
            outside = Path(temporary_directory) / "outside-partial"
            outside.write_bytes(b"external")
            symlink_or_skip(model_path / "model.pth.part", outside)

            with self.assertRaisesRegex(
                ModelIntegrityError, "file must not be an alias"
            ):
                manager.download(asset.name, asset=asset)

            self.assertEqual(outside.read_bytes(), b"external")

    def test_content_length_only_hides_expected_probe_failures(self):
        manager = ModelAssetManager(
            opener=lambda _request, timeout: MemoryResponse(
                b"", headers={"Content-Length": "invalid"}
            )
        )
        self.assertIsNone(manager._content_length("https://models.invalid/model"))

        def fail(_request, timeout):
            raise RuntimeError("programming error")

        manager = ModelAssetManager(opener=fail)
        with self.assertRaisesRegex(RuntimeError, "programming error"):
            manager._content_length("https://models.invalid/model")

    def test_concurrent_downloads_serialize_one_model_without_blocking_another(self):
        first_asset = ModelAsset(
            name="tts_models/test/dataset/first",
            urls=("https://models.invalid/first.pth",),
        )
        second_asset = ModelAsset(
            name="tts_models/test/dataset/second",
            urls=("https://models.invalid/second.pth",),
        )
        first_download_started = Event()
        release_first_download = Event()
        same_model_started = Event()
        same_model_downloaded = Event()
        different_model_done = Event()
        requests = []
        errors = []
        opener = MemoryOpener(
            {
                first_asset.urls[0]: b"first-model",
                second_asset.urls[0]: b"second-model",
            }
        )

        def blocking_opener(request, timeout):
            response = opener(request, timeout)
            if (
                request.get_method() == "GET"
                and request.full_url == first_asset.urls[0]
            ):
                requests.append(request)
                if len(requests) == 1:
                    first_download_started.set()
                    self.assertTrue(release_first_download.wait(2))
                else:
                    same_model_downloaded.set()
            return response

        with TemporaryDirectory() as temporary_directory:
            manager = ModelAssetManager(temporary_directory, opener=blocking_opener)

            def download(asset, done=None):
                try:
                    manager.download(asset.name, asset=asset)
                    if done:
                        done.set()
                except BaseException as error:
                    errors.append(error)

            first = Thread(target=download, args=(first_asset,))
            first.start()
            self.assertTrue(first_download_started.wait(2))

            def download_same_model():
                same_model_started.set()
                download(first_asset)

            same_model = Thread(target=download_same_model)
            same_model.start()
            self.assertTrue(same_model_started.wait(2))
            self.assertFalse(same_model_downloaded.wait(0.2))

            different_model = Thread(
                target=lambda: download(second_asset, different_model_done)
            )
            different_model.start()
            self.assertTrue(different_model_done.wait(2))

            release_first_download.set()
            for thread in (first, same_model, different_model):
                thread.join(2)
                self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(requests), 1)


class VoicePackManagerTest(unittest.TestCase):
    def assert_threads_finished(self, *threads):
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_import_voice_preserves_invalid_existing_manifest(self):
        for payload in (b"{", b'{"version":3,"voices":[]}'):
            with self.subTest(payload=payload), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "marcus.wav"
                source.write_bytes(b"local voice data")
                pack = root / "managed" / "custom"
                pack.mkdir(parents=True)
                manifest = pack / "manifest.json"
                manifest.write_bytes(payload)
                manager = VoicePackManager(root / "managed")

                with self.assertRaisesRegex(VoiceManifestError, "Existing"):
                    manager.import_voice("Marcus", [source])

                self.assertEqual(manifest.read_bytes(), payload)

    def test_import_voice_rejects_aliased_managed_pack(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "marcus.wav"
            source.write_bytes(b"local voice data")
            managed = root / "managed"
            managed.mkdir()
            outside = root / "outside-pack"
            outside.mkdir()
            symlink_or_skip(
                managed / "custom",
                outside,
                target_is_directory=True,
            )
            manager = VoicePackManager(managed)

            with self.assertRaisesRegex(VoiceManifestError, "alias"):
                manager.import_voice("Marcus", [source])

            self.assertEqual(list(outside.iterdir()), [])

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

    def test_import_voice_removes_replaced_managed_references(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.wav"
            second = root / "second.wav"
            first.write_bytes(b"first voice")
            second.write_bytes(b"second voice")
            manager = VoicePackManager(root / "managed")

            manager.import_voice("Ada", [first])
            manifest = manager.import_voice("Ada", [second])
            registry = CharacterVoiceRegistry.from_file(manifest)

            self.assertEqual(
                set((manifest.parent / "references").iterdir()),
                set(registry.resolve("Ada").references),
            )
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

    def test_import_manifest_removes_replaced_managed_references(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = VoicePackManager(root / "managed")

            def source_pack(name, payload):
                directory = root / name
                directory.mkdir()
                reference = directory / "voice.wav"
                reference.write_bytes(payload)
                manifest = directory / "manifest.json"
                manifest.write_text(
                    json.dumps(
                        {
                            "voices": [
                                {
                                    "character": "Ada",
                                    "speaker": "ada-v2",
                                    "reference": reference.name,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return manifest

            manager.import_pack(source_pack("first", b"first"), pack_name="custom")
            manifest = manager.import_pack(
                source_pack("second", b"second"), pack_name="custom"
            )
            registry = CharacterVoiceRegistry.from_file(manifest)

            self.assertEqual(
                set((manifest.parent / "references").iterdir()),
                set(registry.resolve("Ada").references),
            )
            self.assertEqual(manager.validate(manifest), manifest)

    def test_import_voice_waits_for_import_pack_and_keeps_both_voices(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_pack = root / "source"
            source_pack.mkdir()
            source_reference = source_pack / "source.wav"
            source_reference.write_bytes(b"source voice")
            source_manifest = source_pack / "manifest.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "voices": [
                            {
                                "character": "Source",
                                "speaker": "source-v2",
                                "reference": source_reference.name,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            local_reference = root / "marcus.wav"
            local_reference.write_bytes(b"local voice")
            manager = VoicePackManager(root / "managed")
            target_manifest = (root / "managed" / "custom" / "manifest.json").resolve()
            target_manifest.parent.mkdir(parents=True)
            target_manifest.write_text(
                json.dumps({"version": 2, "voices": []}), encoding="utf-8"
            )
            pack_copy_started = Event()
            release_pack_copy = Event()
            voice_import_started = Event()
            voice_manifest_read = Event()
            errors = []
            original_copy2 = shutil.copy2
            original_read_json = assets.read_json

            def blocking_copy2(source, destination, *args, **kwargs):
                if Path(source).resolve() == source_reference.resolve():
                    pack_copy_started.set()
                    self.assertTrue(release_pack_copy.wait(2))
                return original_copy2(source, destination, *args, **kwargs)

            def observing_read_json(path, default):
                if Path(path).resolve() == target_manifest:
                    voice_manifest_read.set()
                return original_read_json(path, default)

            def run(operation):
                try:
                    operation()
                except BaseException as error:
                    errors.append(error)

            with (
                patch("vntts.assets.shutil.copy2", side_effect=blocking_copy2),
                patch("vntts.assets.read_json", side_effect=observing_read_json),
            ):
                pack_import = Thread(
                    target=lambda: run(
                        lambda: manager.import_pack(source_manifest, pack_name="custom")
                    )
                )
                pack_import.start()
                self.assertTrue(pack_copy_started.wait(2))

                def import_voice():
                    voice_import_started.set()
                    manager.import_voice("Marcus", [local_reference])

                voice_import = Thread(target=lambda: run(import_voice))
                voice_import.start()
                self.assertTrue(voice_import_started.wait(2))
                self.assertFalse(voice_manifest_read.wait(0.2))

                release_pack_copy.set()
                self.assert_threads_finished(pack_import, voice_import)

            self.assertEqual(errors, [])
            registry = CharacterVoiceRegistry.from_file(target_manifest)
            self.assertEqual(
                {voice.character for voice in registry.voices.values()},
                {"Marcus", "Source"},
            )
            self.assertEqual(manager.validate(target_manifest), target_manifest)

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

    def test_validation_rejects_malformed_voice_checksum_inventory(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "marcus.wav"
            source.write_bytes(b"local voice data")
            manager = VoicePackManager(root / "managed")
            manifest_path = manager.import_voice("Marcus", [source])
            checksum_path = manifest_path.parent / "vntts-asset.json"
            checksum = json.loads(checksum_path.read_text(encoding="utf-8"))
            checksum["files"] = []
            checksum_path.write_text(json.dumps(checksum), encoding="utf-8")

            with self.assertRaisesRegex(ModelIntegrityError, "inventory"):
                manager.validate(manifest_path)

    def test_validation_binds_checksum_inventory_to_manifest_references(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "marcus.wav"
            source.write_bytes(b"local voice data")
            outside = root / "outside.wav"
            outside.write_bytes(b"unrelated data")
            manager = VoicePackManager(root / "managed")
            manifest_path = manager.import_voice("Marcus", [source])
            checksum_path = manifest_path.parent / "vntts-asset.json"
            checksum = json.loads(checksum_path.read_text(encoding="utf-8"))

            for files in ({}, {"../../outside.wav": "0" * 64}):
                with self.subTest(files=files):
                    checksum["files"] = files
                    checksum_path.write_text(json.dumps(checksum), encoding="utf-8")
                    with self.assertRaisesRegex(ModelIntegrityError, "wrong files"):
                        manager.validate(manifest_path)

    def test_validation_rejects_missing_or_future_voice_checksum_document(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "marcus.wav"
            source.write_bytes(b"local voice data")
            manager = VoicePackManager(root / "managed")
            manifest_path = manager.import_voice("Marcus", [source])
            checksum_path = manifest_path.parent / "vntts-asset.json"
            checksum = json.loads(checksum_path.read_text(encoding="utf-8"))

            checksum_path.unlink()
            with self.assertRaisesRegex(ModelIntegrityError, "missing"):
                manager.validate(manifest_path)
            self.assertFalse(checksum_path.exists())

            checksum["version"] = 2
            checksum_path.write_text(json.dumps(checksum), encoding="utf-8")
            with self.assertRaisesRegex(ModelIntegrityError, "version"):
                manager.validate(manifest_path)


if __name__ == "__main__":
    unittest.main()
