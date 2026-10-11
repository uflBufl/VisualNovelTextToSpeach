import json
import os
import shutil
import stat
import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.assets as assets_module
from tests.authoring_fixtures import tree_hashes
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.symlink_support import symlink_or_skip
from vntts.assets import VoicePackManager
from vntts.voices import CharacterVoiceRegistry, VoiceManifestError


class AssetInputAdmissionTest(unittest.TestCase):
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
                gate: RegularFileGate = {"path": path, "armed": True}
                with swap_at_acquisition(self, gate):
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

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_backup_acquisitions_refuse_fifo_keep_prior_pack_and_recover(self):
        for import_mode in ("voice", "pack"):
            for name in ("manifest.json", "vntts-asset.json"):
                with (
                    self.subTest(import_mode=import_mode, backup=name),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory).resolve()
                    old, new = root / "old.wav", root / "new.wav"
                    old.write_bytes(b"old voice")
                    new.write_bytes(b"new voice")
                    manager = VoicePackManager(root / "managed")
                    manifest = manager.import_voice("Old", [old])
                    source_manifest = VoicePackManager(root / "source").import_voice(
                        "New", [new]
                    )
                    if import_mode == "voice":
                        invoke = partial(manager.import_voice, "New", [new])
                    else:
                        invoke = partial(
                            manager.import_pack, source_manifest, pack_name="custom"
                        )
                    selected = manifest.with_name(name)
                    before = tree_hashes(root)
                    original_copy = assets_module.copy_regular_file
                    refused = []

                    def copy(original, target, **kwargs):
                        if Path(original) != selected:
                            return original_copy(original, target, **kwargs)
                        gate: RegularFileGate = {"path": selected, "armed": True}
                        with (
                            swap_at_acquisition(self, gate),
                            self.assertRaisesRegex(OSError, "regular file") as caught,
                        ):
                            original_copy(original, target, **kwargs)
                        self.assertFalse(Path(target).exists())
                        refused.append(caught.exception)
                        raise caught.exception

                    with (
                        patch.object(assets_module, "copy_regular_file", copy),
                        self.assertRaisesRegex(OSError, "regular file") as caught,
                    ):
                        invoke()
                    self.assertEqual(len(refused), 1)
                    self.assertIs(caught.exception, refused[0])
                    self.assertEqual(tree_hashes(root), before)
                    self.assertEqual(
                        list(manifest.parent.glob(".voice-pack-backup-*")), []
                    )
                    self.assertEqual(manager.validate(manifest), manifest)
                    self.assertEqual(invoke(), manifest)
                    self.assertIsNotNone(
                        CharacterVoiceRegistry.from_file(manifest).resolve("New")
                    )
                    self.assertEqual(manager.validate(manifest), manifest)
                    self.assertEqual(
                        list(manifest.parent.glob(".voice-pack-backup-*")), []
                    )
                    self.assertEqual(old.read_bytes(), b"old voice")
                    self.assertEqual(new.read_bytes(), b"new voice")

    def test_partial_backup_stream_failure_cleans_owned_copies_and_recovers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old, new = root / "old.wav", root / "new.wav"
            old.write_bytes(b"old voice")
            new.write_bytes(b"new voice")
            manager = VoicePackManager(root / "managed")
            manifest = manager.import_voice("Old", [old])
            checksum = manifest.with_name("vntts-asset.json")
            before = tree_hashes(root)
            original_copy = assets_module.copy_regular_file
            primary = PermissionError("backup stream failed")
            streams = []

            def fail_stream(original, target):
                streams.extend((original, target))
                target.write(original.read(1))
                raise primary

            def copy(original, target, **kwargs):
                if Path(original) != checksum:
                    return original_copy(original, target, **kwargs)
                with patch("vntts.path_safety.shutil.copyfileobj", fail_stream):
                    try:
                        original_copy(original, target, **kwargs)
                    finally:
                        self.assertEqual(
                            Path(target).read_bytes(), checksum.read_bytes()[:1]
                        )

            with (
                patch.object(assets_module, "copy_regular_file", copy),
                self.assertRaises(PermissionError) as caught,
            ):
                manager.import_voice("New", [new])
            self.assertIs(caught.exception, primary)
            self.assertEqual(len(streams), 2)
            self.assertTrue(all(stream.closed for stream in streams))
            self.assertEqual(tree_hashes(root), before)
            self.assertEqual(list(manifest.parent.glob(".voice-pack-backup-*")), [])
            self.assertEqual(manager.validate(manifest), manifest)
            self.assertEqual(manager.import_voice("New", [new]), manifest)
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
