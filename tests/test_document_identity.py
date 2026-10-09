import hashlib
import os
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.pregeneration_fixtures import clean_wav_bytes
from vntts import assets, document_identity, speech_backend_runtime
from vntts.authoring import managed_model_installation as managed


class ChecksumAdmissionTest(unittest.TestCase):
    def test_regular_file_hash_and_missing_file_error_contract(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            for payload in (b"", b"verified" * 100000):
                path.write_bytes(payload)
                expected = hashlib.sha256(payload).hexdigest()
                self.assertEqual(document_identity.file_sha256(path), expected)
                self.assertEqual(document_identity.file_sha256(str(path)), expected)
                self.assertEqual(path.read_bytes(), payload)
            path.unlink()
            for error_type in (ValueError, RuntimeError, OSError):
                with self.subTest(error_type=error_type):
                    with self.assertRaises(error_type) as raised:
                        document_identity.file_sha256(path, error_type=error_type)
                    self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def _assert_fifo_admission(self, path, invoke, error_type):
        payload = path.read_bytes()
        native_open, native_path_open = os.open, Path.open
        original_admit = document_identity.open_regular_binary
        descriptors = []
        acquiring_target = False

        def admit(candidate):
            nonlocal acquiring_target
            if Path(candidate) == path:
                acquiring_target = True
            return original_admit(candidate)

        def swap_and_open(candidate, flags, mode=0o777, *, dir_fd=None):
            if acquiring_target and Path(candidate) == path:
                self.assertTrue(flags & os.O_NONBLOCK)
                path.unlink()
                os.mkfifo(path)
            descriptor = native_open(candidate, flags, mode, dir_fd=dir_fd)
            if acquiring_target and Path(candidate) == path:
                descriptors.append(descriptor)
            return descriptor

        def reject_plain_open(candidate, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(candidate) == path and "b" in mode:
                self.fail("verified hashing must not use blocking Path.open")
            return native_path_open(candidate, *args, **kwargs)

        try:
            with (
                patch.object(
                    document_identity, "open_regular_binary", side_effect=admit
                ),
                patch("vntts.path_safety.os.open", side_effect=swap_and_open),
                patch.object(Path, "open", reject_plain_open),
            ):
                if error_type is None:
                    self.assertEqual(invoke(), str(path))
                else:
                    with self.assertRaisesRegex(error_type, "regular file"):
                        invoke()
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
        finally:
            path.unlink(missing_ok=True)
            path.write_bytes(payload)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_shared_and_binary_integrity_hashes_reject_swaps_and_recover(self):
        for owner in (
            "shared",
            "model asset",
            "managed file",
            "managed tree",
            "source identity",
        ):
            with self.subTest(owner=owner), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                path = root / "model.bin"
                payload = b"verified binary"
                path.write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
                model = managed.ManagedModelFiles(
                    "test",
                    "repo",
                    "rev",
                    (path.name,),
                    file_sha256s={path.name: digest},
                )
                if owner == "shared":
                    invoke = partial(document_identity.file_sha256, path)
                    error_type = ValueError
                elif owner == "model asset":
                    manager = assets.ModelAssetManager(root / "models")
                    invoke = partial(
                        manager._validate_model_file,
                        path,
                        path.name,
                        {"size": len(payload), "sha256": digest},
                    )
                    error_type = assets.ModelIntegrityError
                elif owner == "managed file":
                    invoke = partial(managed._verify, root, model)
                    error_type = OSError
                elif owner == "managed tree":
                    invoke = partial(managed._tree_sha256, root)
                    error_type = OSError
                else:
                    speech_backend_runtime._file_content_identity.cache_clear()
                    self.addCleanup(
                        speech_backend_runtime._file_content_identity.cache_clear
                    )
                    invoke = partial(speech_backend_runtime._source_identity, path)
                    error_type = None
                self._assert_fifo_admission(path, invoke, error_type)
                result = invoke()
                if owner == "shared":
                    self.assertEqual(result, digest)
                elif owner == "source identity":
                    self.assertEqual(result, f"sha256:{digest}:{len(payload)}")
                elif owner == "managed file":
                    self.assertEqual(result, (None, None, {path.name: digest}))
                elif owner == "managed tree":
                    name = path.name.encode()
                    expected = hashlib.sha256(
                        len(name).to_bytes(8, "big") + name + bytes.fromhex(digest)
                    ).hexdigest()
                    self.assertEqual(result, expected)
                else:
                    self.assertIsNone(result)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_voice_pack_manifest_and_reference_hashes_reject_swaps_and_recover(self):
        for target in ("manifest", "reference"):
            with self.subTest(target=target), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source = root / "source.wav"
                source.write_bytes(clean_wav_bytes())
                manager = assets.VoicePackManager(root / "packs")
                manifest = manager.import_voice("Ada", [source])
                reference = next((manifest.parent / "references").iterdir())
                path = manifest if target == "manifest" else reference
                invoke = partial(manager.validate, manifest)
                self._assert_fifo_admission(path, invoke, assets.ModelIntegrityError)
                self.assertEqual(invoke(), manifest)
