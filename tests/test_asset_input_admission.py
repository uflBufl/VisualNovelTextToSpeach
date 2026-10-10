import builtins
import json
import os
import shutil
import stat
import unittest
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts.assets import VoicePackManager
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError


class AssetInputAdmissionTest(unittest.TestCase):
    @contextmanager
    def _swapped_file(self, path):
        payload = path.read_bytes()
        native_open, plain_open, path_open = os.open, builtins.open, Path.open
        descriptors = []

        def swap_and_open(candidate, flags, mode=0o777, *, dir_fd=None):
            if Path(candidate) == path:
                self.assertTrue(flags & os.O_NONBLOCK)
                path.unlink()
                os.mkfifo(path)
            descriptor = native_open(candidate, flags, mode, dir_fd=dir_fd)
            if Path(candidate) == path:
                descriptors.append(descriptor)
            return descriptor

        def guard_open(candidate, *args, **kwargs):
            if isinstance(candidate, (str, os.PathLike)) and Path(candidate) == path:
                if kwargs.get("opener") is None:
                    self.fail("input must be admitted before opening")
            return plain_open(candidate, *args, **kwargs)

        def guard_path_open(candidate, *args, **kwargs):
            if Path(candidate) == path:
                self.fail("input must not use blocking Path.open")
            return path_open(candidate, *args, **kwargs)

        try:
            with (
                patch("vntts.path_safety.os.open", side_effect=swap_and_open),
                patch("builtins.open", side_effect=guard_open),
                patch.object(Path, "open", guard_path_open),
            ):
                yield
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
        finally:
            path.unlink(missing_ok=True)
            path.write_bytes(payload)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_import_inputs_refuse_fifo_swap_keep_prior_pack_and_recover(self):
        for phase in ("voice copy", "pack copy", "pack manifest"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                old = root / "old.wav"
                old.write_bytes(b"old")
                manager = VoicePackManager(root / "managed")
                manifest = manager.import_voice("Old", [old])
                checksum = manifest.parent / "vntts-asset.json"
                prior = (manifest.read_bytes(), checksum.read_bytes())
                references = set((manifest.parent / "references").iterdir())
                reference = root / "new.wav"
                reference.write_bytes(b"new")
                source = root / "source.json"
                source.write_text(
                    json.dumps(
                        {
                            "voices": [
                                {
                                    "character": "New",
                                    "speaker": "new",
                                    "reference": reference.name,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                if phase == "voice copy":
                    invoke = partial(manager.import_voice, "New", [reference])
                else:
                    invoke = partial(manager.import_pack, source, pack_name="custom")
                path = source if phase == "pack manifest" else reference
                error_type = VoiceManifestError if phase == "pack manifest" else OSError
                with self._swapped_file(path):
                    with self.assertRaisesRegex(error_type, "regular file"):
                        invoke()
                self.assertEqual((manifest.read_bytes(), checksum.read_bytes()), prior)
                self.assertEqual(
                    set((manifest.parent / "references").iterdir()), references
                )
                self.assertEqual(manager.validate(manifest), manifest)
                self.assertEqual(invoke(), manifest)
                self.assertIsNotNone(
                    CharacterVoiceRegistry.from_file(manifest).resolve("New")
                )
                self.assertEqual(manager.validate(manifest), manifest)

    def test_reference_copy_refuses_same_file_without_truncating(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "voice.wav"
            source.write_bytes(b"original voice")
            with self.assertRaises(shutil.SameFileError):
                VoicePackManager._copy_reference(source, source)
            self.assertEqual(source.read_bytes(), b"original voice")

    def test_reference_copy_preserves_binary_bytes_mode_and_mtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.wav", root / "target.wav"
            source.write_bytes(b"\x00\xff\r\nvoice")
            source.chmod(0o640)
            os.utime(source, ns=(1234567890000000000, 1234567890000000000))
            VoicePackManager._copy_reference(source, target)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(
                stat.S_IMODE(target.stat().st_mode), stat.S_IMODE(source.stat().st_mode)
            )
            self.assertEqual(target.stat().st_mtime_ns, source.stat().st_mtime_ns)

    def test_copy_stream_and_metadata_failures_preserve_prior_pack(self):
        for phase in ("stream", "metadata"):
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "voice.wav"
                source.write_bytes(b"old")
                manager = VoicePackManager(root / "managed")
                manifest = manager.import_voice("Ada", [source])
                checksum = manifest.parent / "vntts-asset.json"
                prior = (manifest.read_bytes(), checksum.read_bytes())
                references = set((manifest.parent / "references").iterdir())
                source.write_bytes(b"new")
                primary = PermissionError("copy phase failed")

                def fail_stream(original, target):
                    target.write(original.read(1))
                    raise primary

                name = "copyfileobj" if phase == "stream" else "copystat"
                failure = fail_stream if phase == "stream" else primary
                with patch(f"vntts.assets.shutil.{name}", side_effect=failure):
                    with self.assertRaises(OSError) as raised:
                        manager.import_voice("Ada", [source])
                self.assertIs(raised.exception, primary)
                self.assertEqual((manifest.read_bytes(), checksum.read_bytes()), prior)
                self.assertEqual(
                    set((manifest.parent / "references").iterdir()), references
                )
                self.assertEqual(manager.validate(manifest), manifest)

    def test_registry_keeps_text_decoding_errors_legacy_and_metadata_projection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "manifest.json"
            (root / "voice.wav").write_bytes(b"voice")
            document = {
                "voices": [
                    {
                        "character": "Ada",
                        "speaker": "ada",
                        "aliases": ["A"],
                        "reference": "voice.wav",
                        "vntts.source_character": "Source Ada",
                        "vntts.reference_transcript": "Words",
                    }
                ]
            }
            path.write_bytes(json.dumps(document).replace(",", ",\r\n").encode("utf-8"))
            registry = CharacterVoiceRegistry.from_file(path)
            self.assertEqual(
                registry.unique_voices(),
                CharacterVoiceRegistry.from_document(document, path).unique_voices(),
            )
            self.assertEqual(registry.resolve("A"), registry.resolve("Ada"))
            for payload, error_type in (
                (b"\xff", UnicodeDecodeError),
                (b"{", VoiceManifestError),
                (b"\xef\xbb\xbf{}", VoiceManifestError),
            ):
                with self.subTest(payload=payload):
                    path.write_bytes(payload)
                    with self.assertRaises(error_type) as raised:
                        CharacterVoiceRegistry.from_file(path)
                    if error_type is VoiceManifestError:
                        self.assertIsInstance(
                            raised.exception.__cause__, json.JSONDecodeError
                        )
            path.unlink()
            with self.assertRaises(VoiceManifestError) as raised:
                CharacterVoiceRegistry.from_file(path)
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_registry_keeps_requested_manifest_symlink_and_indexed_reference_errors(
        self,
    ):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "voice.wav").write_bytes(b"voice")
            path = root / "manifest.json"
            document = {
                "voices": [
                    {"character": "Ada", "speaker": "ada", "reference": "voice.wav"}
                ]
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            alias = root / "alias.json"
            symlink_or_skip(alias, path)
            self.assertEqual(
                CharacterVoiceRegistry.from_file(alias).unique_voices(),
                CharacterVoiceRegistry.from_file(path).unique_voices(),
            )
            symlink_or_skip(root / "linked.wav", root / "voice.wav")
            for reference, message in (
                ("linked.wav", "Voice entry 0 reference must not use symlinks"),
                (
                    "bad\x00.wav",
                    "Voice entry 0 reference must stay within the manifest directory",
                ),
            ):
                with self.subTest(reference=reference):
                    document["voices"][0]["reference"] = reference
                    path.write_text(json.dumps(document), encoding="utf-8")
                    with self.assertRaises(VoiceManifestError) as raised:
                        CharacterVoiceRegistry.from_file(path)
                    self.assertEqual(str(raised.exception), message)
