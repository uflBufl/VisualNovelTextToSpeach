import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

from vntts.diagnostic_caf import diagnostic_audio_path, recover_caf_copy


class DiagnosticCafTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "capture.caf"
        self.copy = self.root / "capture.recovered.caf"

    def caf(self, *, padding=23, tail=b"", flags=3, bits=32, frames=160):
        samples = np.random.default_rng(19).normal(0, 0.1, (frames, 2)).astype("<f4")
        desc = struct.pack(">d4sIIIII", 16000, b"lpcm", flags, 8, 1, 2, bits)
        self.source.write_bytes(
            b"caff\x00\x01\x00\x00"
            + b"desc"
            + struct.pack(">q", 32)
            + desc
            + b"free"
            + struct.pack(">q", padding)
            + b"\0" * padding
            + b"data"
            + struct.pack(">q", -1)
            + b"\0" * 4
            + samples.tobytes()
            + tail
        )
        return samples

    def test_recovery_parses_variable_chunks_and_soundfile_decodes_copy(self):
        expected = self.caf()
        original = self.source.read_bytes()
        self.assertEqual(diagnostic_audio_path(self.source), self.copy)
        decoded, rate = sf.read(self.copy, dtype="float32")
        self.assertEqual(rate, 16000)
        np.testing.assert_array_equal(decoded, expected)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(recover_caf_copy(self.source, self.copy))
        self.assertFalse(recover_caf_copy(self.copy, self.root / "unneeded.caf"))
        self.assertFalse((self.root / "unneeded.caf").exists())

    def test_existing_recovery_must_match_every_source_byte(self):
        self.caf()
        self.copy.write_bytes(b"user file")
        with self.assertRaisesRegex(ValueError, "differs"):
            recover_caf_copy(self.source, self.copy)
        self.assertEqual(self.copy.read_bytes(), b"user file")

    def test_malformed_description_chunk_or_incomplete_frame_has_no_copy(self):
        for kwargs in (dict(tail=b"x"), dict(flags=7), dict(bits=31), dict(frames=0)):
            with self.subTest(kwargs=kwargs):
                self.caf(**kwargs)
                with self.assertRaises(ValueError):
                    recover_caf_copy(self.source, self.copy)
                self.assertFalse(self.copy.exists())
        self.caf()
        self.source.write_bytes(self.source.read_bytes()[:30])
        with self.assertRaises(ValueError):
            recover_caf_copy(self.source, self.copy)

    def test_symlinks_oversize_cancellation_and_space_are_rejected(self):
        self.caf()
        original = self.source.read_bytes()
        link = self.root / "link.caf"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            recover_caf_copy(link, self.copy)
        self.copy.symlink_to(self.source)
        with self.assertRaises(ValueError):
            recover_caf_copy(self.source, self.copy)
        self.copy.unlink()
        with patch("vntts.diagnostic_caf.MAX_CAF_BYTES", 20):
            with self.assertRaises(ValueError):
                recover_caf_copy(self.source, self.copy)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            recover_caf_copy(self.source, self.copy, cancelled=cancelled)
        with patch(
            "vntts.diagnostic_caf.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(1, 1, 0),
        ):
            with self.assertRaisesRegex(OSError, "free space"):
                recover_caf_copy(self.source, self.copy)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(self.copy.exists())

    def test_copy_cancellation_removes_only_new_incomplete_copy(self):
        self.caf()
        original = self.source.read_bytes()

        def cancel_when_copy_exists(*_args):
            if self.copy.exists():
                raise InterruptedError("cancelled during copy")

        with patch("vntts.diagnostic_caf._cancel", side_effect=cancel_when_copy_exists):
            with self.assertRaises(InterruptedError):
                recover_caf_copy(self.source, self.copy)
        self.assertFalse(self.copy.exists())
        self.assertEqual(self.source.read_bytes(), original)
        with self.assertRaises(TimeoutError):
            recover_caf_copy(self.source, self.copy, deadline=0)
        self.assertFalse(self.copy.exists())

    def test_caf_recovery_allows_final_tail_before_copying_any_hour_pcm(self):
        self.caf()
        info = self.source.stat()
        for seconds, error in ((3620, OSError), (3621, ValueError)):
            with self.subTest(seconds=seconds):
                metadata = SimpleNamespace(
                    st_mode=info.st_mode,
                    st_ino=info.st_ino,
                    st_size=info.st_size - 160 * 8 + seconds * 16000 * 8,
                    st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=info.st_ctime_ns,
                )
                with (
                    patch("vntts.diagnostic_caf.os.fstat", return_value=metadata),
                    patch(
                        "vntts.diagnostic_caf.shutil.disk_usage",
                        return_value=SimpleNamespace(free=0),
                    ),
                ):
                    # The admitted tail reaches the space gate; an overlong one fails parsing.
                    with self.assertRaisesRegex(
                        error, "free space" if error is OSError else "PCM frames"
                    ):
                        recover_caf_copy(self.source, self.copy)
                self.assertFalse(self.copy.exists())
