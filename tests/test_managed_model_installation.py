import builtins
import hashlib
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.symlink_support import symlink_or_skip
from vntts.authoring import managed_model_installation as managed


class ManagedModelAdmissionTest(unittest.TestCase):
    def _inputs(self, root):
        source = root / "source"
        source.mkdir()
        payload = b"pinned model"
        (source / "model.bin").write_bytes(payload)
        model = managed.ManagedModelFiles(
            "test",
            "repo",
            "rev",
            ("model.bin",),
            file_sha256s={"model.bin": hashlib.sha256(payload).hexdigest()},
        )
        return source, model, {"model": "test"}, "Model license\nSecond line\n"

    def _install(self, installation, source, model, metadata, notice):
        return managed.install_managed_model(
            installation,
            model,
            metadata=metadata,
            notice=notice,
            source=source,
            fetch_file=lambda name: source / name,
        )

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
            if Path(candidate) == path and kwargs.get("opener") is None:
                self.fail("input acquisition must admit the opened regular file")
            return plain_open(candidate, *args, **kwargs)

        def guard_path_open(candidate, *args, **kwargs):
            if Path(candidate) == path:
                self.fail("input acquisition must not use blocking Path.open")
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
    def test_metadata_acquisition_rejects_fifo_swap_and_status_recovers(self):
        for filename in ("managed-model.json", "THIRD_PARTY_NOTICES.txt"):
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source, model, metadata, notice = self._inputs(root)
                installation = root / "managed" / "rev"
                self._install(installation, source, model, metadata, notice)
                path = installation / filename
                payload = path.read_bytes()
                with self._swapped_file(path):
                    result = managed.managed_model_status(
                        installation, model, metadata=metadata, notice=notice
                    )
                    self.assertEqual(result["status"], "invalid")
                    self.assertIn("regular file", result["reason"])
                self.assertEqual(path.read_bytes(), payload)
                result = managed.managed_model_status(
                    installation, model, metadata=metadata, notice=notice
                )
                self.assertEqual(result["status"], "installed")

    def test_metadata_text_keeps_utf8_and_universal_newline_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, model, metadata, notice = self._inputs(root)
            installation = root / "managed" / "rev"
            self._install(installation, source, model, metadata, notice)
            metadata_path = installation / "managed-model.json"
            notice_path = installation / "THIRD_PARTY_NOTICES.txt"
            original = metadata_path.read_bytes()
            notice_path.write_bytes(notice.replace("\n", "\r\n").encode("utf-8"))
            self.assertEqual(
                managed.managed_model_status(
                    installation, model, metadata=metadata, notice=notice
                )["status"],
                "installed",
            )
            for malformed in (b"\xff", b"\xef\xbb\xbf" + original):
                with self.subTest(malformed=malformed):
                    metadata_path.write_bytes(malformed)
                    result = managed.managed_model_status(
                        installation, model, metadata=metadata, notice=notice
                    )
                    self.assertEqual(result["status"], "invalid")
                    self.assertIn("metadata is unavailable", result["reason"])
                    self.assertEqual(metadata_path.read_bytes(), malformed)
            metadata_path.write_bytes(original)
            self.assertEqual(
                managed.managed_model_status(
                    installation, model, metadata=metadata, notice=notice
                )["status"],
                "installed",
            )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_source_copy_rejects_fifo_swap_without_publication_and_recovers(self):
        for fetched in (False, True):
            with self.subTest(fetched=fetched), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                source, model, metadata, notice = self._inputs(root)
                installation = root / "managed" / "rev"
                with self._swapped_file(source / "model.bin"):
                    with self.assertRaisesRegex(OSError, "regular file"):
                        managed.install_managed_model(
                            installation,
                            model,
                            metadata=metadata,
                            notice=notice,
                            source=None if fetched else source,
                            fetch_file=lambda name: source / name,
                        )
                self.assertFalse(installation.exists())
                self.assertEqual(list(installation.parent.iterdir()), [])
                self.assertEqual(
                    self._install(installation, source, model, metadata, notice)[
                        "status"
                    ],
                    "installed",
                )
                self.assertEqual(
                    (installation / "model" / "model.bin").read_bytes(), b"pinned model"
                )

    def test_source_symlinks_to_regular_files_remain_supported(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, model, metadata, notice = self._inputs(root)
            payload = root / "weights"
            (source / "model.bin").rename(payload)
            symlink_or_skip(source / "model.bin", payload)
            installation = root / "managed" / "rev"
            result = self._install(installation, source, model, metadata, notice)
            self.assertEqual(result["status"], "installed")
            self.assertEqual(
                (installation / "model" / "model.bin").read_bytes(), b"pinned model"
            )
