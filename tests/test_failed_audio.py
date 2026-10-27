import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
from vntts_artifacts.audio import write_pcm16_wav

from tests.bulk_generation_fixtures import audio_samples
from tests.symlink_support import symlink_or_skip
from vntts.authoring.failed_audio import (
    failed_audio_path,
    read_failed_audio,
    remove_failed_audio,
    retain_failed_audio,
)


class FailedAudioTest(unittest.TestCase):
    def test_latest_wav_replaces_previous_and_old_metadata_cannot_play_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.wav"
            write_pcm16_wav(source, audio_samples(), 16_000)
            first = retain_failed_audio(root, "queue-one", source)
            self.assertEqual(
                read_failed_audio(root, "queue-one", first), source.read_bytes()
            )
            write_pcm16_wav(source, audio_samples() / 2, 16_000)
            latest = retain_failed_audio(root, "queue-one", source)
            self.assertEqual(first["path"], latest["path"])
            self.assertNotEqual(first["sha256"], latest["sha256"])
            self.assertEqual(len(list((root / ".failed-audio").iterdir())), 1)
            self.assertEqual(
                read_failed_audio(root, "queue-one", latest), source.read_bytes()
            )
            with self.assertRaisesRegex(ValueError, "checksum"):
                read_failed_audio(root, "queue-one", first)
            remove_failed_audio(root, "queue-one")
            with self.assertRaises((ValueError, OSError)):
                read_failed_audio(root, "queue-one", latest)

    def test_silent_diagnostic_is_playable_but_malformed_or_missing_audio_is_not(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.wav"
            self.assertEqual(
                retain_failed_audio(root, "one", source), {"created": False}
            )
            source.write_bytes(b"not a WAV")
            self.assertEqual(
                retain_failed_audio(root, "one", source), {"created": True}
            )
            write_pcm16_wav(source, np.zeros(100, dtype=np.float32), 16_000)
            metadata = retain_failed_audio(root, "one", source)
            destination = failed_audio_path(root, "one", metadata)
            self.assertEqual(
                read_failed_audio(root, "one", metadata), source.read_bytes()
            )
            payload = source.read_bytes()[:-2]
            destination.write_bytes(payload)
            metadata["sha256"] = hashlib.sha256(payload).hexdigest()
            with self.assertRaises(ValueError):
                read_failed_audio(root, "one", metadata)

    def test_metadata_cannot_select_another_queue_or_escape_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.wav"
            write_pcm16_wav(source, audio_samples(), 16_000)
            metadata = retain_failed_audio(root, "one", source)
            for invalid in (
                None,
                {"created": False},
                {"created": True},
                dict(metadata, created=1),
                dict(metadata, path="../partial.wav"),
                dict(metadata, path=str(source)),
                dict(metadata, sha256="forged"),
            ):
                with self.subTest(metadata=invalid), self.assertRaises(ValueError):
                    read_failed_audio(root, "one", invalid)
            with self.assertRaises(ValueError):
                read_failed_audio(root, "other", metadata)
            destination = failed_audio_path(root, "one", metadata)
            destination.write_bytes(
                destination.read_bytes().replace(b"RIFF", b"RIFX", 1)
            )
            with self.assertRaises(ValueError):
                read_failed_audio(root, "one", metadata)

    def test_source_and_retained_symlinks_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.wav"
            original = root / "original.wav"
            write_pcm16_wav(original, audio_samples(), 16_000)
            symlink_or_skip(source, original)
            self.assertEqual(
                retain_failed_audio(root, "one", source), {"created": True}
            )
            source.unlink()
            source.write_bytes(original.read_bytes())
            metadata = retain_failed_audio(root, "one", source)
            destination = failed_audio_path(root, "one", metadata)
            destination.unlink()
            symlink_or_skip(destination, original)
            with self.assertRaises((ValueError, OSError)):
                read_failed_audio(root, "one", metadata)
            destination.unlink()
            destination.parent.rmdir()
            symlink_or_skip(destination.parent, root)
            self.assertEqual(
                retain_failed_audio(root, "one", source), {"created": True}
            )
            with self.assertRaises((ValueError, OSError)):
                read_failed_audio(root, "one", metadata)

    def test_file_replacement_during_read_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.wav"
            write_pcm16_wav(source, audio_samples(), 16_000)
            metadata = retain_failed_audio(root, "one", source)
            original = source.read_bytes()

            def replace_during_read(path):
                replacement = root / "replacement.wav"
                replacement.write_bytes(original)
                replacement.replace(path)
                return original

            with (
                patch(
                    "vntts.authoring.failed_audio.read_regular_bytes",
                    side_effect=replace_during_read,
                ),
                self.assertRaisesRegex(ValueError, "changed while"),
            ):
                read_failed_audio(root, "one", metadata)

    def test_retention_write_error_only_marks_diagnostic_unavailable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "partial.wav"
            write_pcm16_wav(source, audio_samples(), 16_000)
            with patch(
                "vntts.authoring.failed_audio.atomic_write_bytes",
                side_effect=RuntimeError("diagnostic storage failed"),
            ):
                self.assertEqual(
                    retain_failed_audio(root, "one", source), {"created": True}
                )


if __name__ == "__main__":
    unittest.main()
