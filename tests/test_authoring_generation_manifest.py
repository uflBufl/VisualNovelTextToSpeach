import hashlib
import io
import os
import unittest
import wave
from array import array
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.authoring import generation_manifest as manifest
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.path_safety import open_regular_binary


def pcm_wav(value=8192, sample_count=2400):
    with io.BytesIO() as output:
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(24000)
            writer.writeframes(array("h", [value] * sample_count).tobytes())
        return output.getvalue()


class GeneratedWavSnapshotTest(unittest.TestCase):
    def result(self, path, **options):
        return {
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "quality": asdict(manifest.inspect_generated_wav(path, **options)),
        }

    def test_verified_samples_come_from_hashed_bytes_after_path_replacement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            path.write_bytes(pcm_wav())
            result = self.result(path)
            original = manifest.inspect_generated_wav_with_samples

            def replace_before_decode(source, **options):
                path.write_bytes(pcm_wav(-8192))
                return original(source, **options)

            with patch.object(
                manifest,
                "inspect_generated_wav_with_samples",
                side_effect=replace_before_decode,
            ):
                quality, samples = manifest.validate_success_file_with_samples(
                    "q", result, path
                )
            self.assertEqual(asdict(quality), result["quality"])
            self.assertEqual(list(samples), [8192] * 2400)
            with self.assertRaisesRegex(BulkGenerationError, "checksum mismatch"):
                manifest.validate_success_file("q", result, path)

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_swapped_fifo_is_rejected_before_read_and_valid_file_recovers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            payload = pcm_wav()
            path.write_bytes(payload)
            result = self.result(path)
            original_open = os.open
            descriptors = []

            def swap_before_acquisition(candidate):
                path.unlink()
                os.mkfifo(path)
                return open_regular_binary(candidate)

            def nonblocking_open(candidate, flags, mode=0o777, *, dir_fd=None):
                self.assertTrue(flags & os.O_NONBLOCK)
                descriptor = original_open(candidate, flags, mode, dir_fd=dir_fd)
                descriptors.append(descriptor)
                return descriptor

            with (
                patch.object(
                    manifest, "open_regular_binary", side_effect=swap_before_acquisition
                ),
                patch("vntts.path_safety.os.open", side_effect=nonblocking_open),
            ):
                with self.assertRaisesRegex(BulkGenerationError, "not a regular file"):
                    manifest.validate_success_file_with_samples("q", result, path)
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
            path.unlink()
            path.write_bytes(payload)
            self.assertEqual(
                manifest.validate_success_file("q", result, path).sample_count, 2400
            )

    def test_quality_and_provider_contracts_remain_strict(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "event.wav"
            path.write_bytes(pcm_wav(sample_count=480))
            result = self.result(path, allow_short_audio_event=True)
            with self.assertRaisesRegex(BulkGenerationError, "duration is implausible"):
                manifest.validate_success_file("q", result, path)
            result["provider"] = "original-game-audio-event"
            self.assertEqual(
                manifest.validate_success_file("q", result, path).sample_count, 480
            )
            result["quality"]["channels"] = True
            with self.assertRaisesRegex(
                BulkGenerationError, "quality channels mismatch"
            ):
                manifest.validate_success_file("q", result, path)

    def test_missing_and_bad_pcm_keep_domain_errors(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.wav"
            with self.assertRaisesRegex(BulkGenerationError, "missing"):
                manifest.validate_success_file("q", {}, path)
            for payload in (b"not WAV", pcm_wav()[:-1], pcm_wav()[:-2]):
                with self.subTest(size=len(payload)):
                    path.write_bytes(payload)
                    result = {"file_sha256": hashlib.sha256(payload).hexdigest()}
                    with self.assertRaisesRegex(
                        BulkGenerationError, "not a readable PCM16 mono WAV"
                    ):
                        manifest.validate_success_file("q", result, path)
