import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from vntts_artifacts.audio import read_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.listening import load_listening_session
from vntts.authoring.silence_comparison import (
    SilenceComparisonError,
    SilenceComparisonSample,
    create_silence_comparison_session,
    load_silence_comparison,
    publish_silence_comparison,
)


class AuthoringSilenceComparisonTest(unittest.TestCase):
    def _fixture(self, root):
        root = Path(root)
        speech = np.full(800, 0.2, dtype=np.float32)
        raw = root / "raw.wav"
        segmented = root / "segmented.wav"
        write_pcm16_wav(
            raw,
            np.concatenate((speech, np.zeros(1_600, dtype=np.float32), speech)),
            1_000,
        )
        write_pcm16_wav(
            segmented,
            np.concatenate((speech, np.zeros(180, dtype=np.float32), speech)),
            1_000,
        )
        sample = SilenceComparisonSample(
            "queue:one",
            "line:one",
            "The gate is already open. We should leave before dawn.",
            raw,
            segmented,
        )
        return sample

    def test_publishes_checksum_bound_reports_and_blind_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)

            result = publish_silence_comparison((sample,), root / "comparison")
            document = load_silence_comparison(result.directory)
            session = create_silence_comparison_session(
                result.directory, root / "listening", seed=7
            )

            self.assertEqual(result.sample_count, 1)
            self.assertFalse(document["policy"]["production_enabled"])
            self.assertTrue(document["policy"]["requires_blind_review"])
            self.assertEqual(
                document["samples"][0]["transform"]["removed_samples"], 1_000
            )
            compressed = result.directory / document["samples"][0]["compressed_audio"]
            samples, info = read_pcm16_mono_wav(compressed)
            self.assertEqual(len(samples), 2_200)
            self.assertEqual(info.duration_seconds, 2.2)
            loaded_session = load_listening_session(session)
            self.assertEqual(loaded_session["trial_count"], 1)
            self.assertEqual(set(loaded_session["trials"][0]["audio"]), {"a", "b"})

    def test_loader_rejects_tampered_or_escaping_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = publish_silence_comparison(
                (self._fixture(root),), root / "comparison"
            )
            document = json.loads(
                (result.directory / "comparison.json").read_text(encoding="utf-8")
            )
            original_document = json.loads(json.dumps(document))
            document["samples"][0]["compressed_audio_sha256"] = "0" * 64
            (result.directory / "comparison.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(SilenceComparisonError, "not bound"):
                load_silence_comparison(result.directory)

            (result.directory / "comparison.json").write_text(
                json.dumps(original_document), encoding="utf-8"
            )
            artifact = result.directory / document["artifacts"][0]["path"]
            artifact.write_bytes(b"tampered")
            with self.assertRaisesRegex(SilenceComparisonError, "checksum changed"):
                load_silence_comparison(result.directory)

            document["artifacts"][0]["path"] = "../outside.wav"
            (result.directory / "comparison.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            with self.assertRaisesRegex(SilenceComparisonError, "leaves"):
                load_silence_comparison(result.directory)

    def test_publication_rechecks_source_bytes_and_never_replaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = self._fixture(root)
            output = root / "comparison"
            real_write = Path.write_bytes
            mutated = False

            def write_and_mutate(path, payload):
                nonlocal mutated
                result = real_write(path, payload)
                if not mutated and Path(path).name.endswith("-raw.wav"):
                    mutated = True
                    write_pcm16_wav(
                        sample.raw_audio,
                        np.full(3_200, 0.1, dtype=np.float32),
                        1_000,
                    )
                return result

            with mock.patch.object(Path, "write_bytes", write_and_mutate):
                with self.assertRaisesRegex(SilenceComparisonError, "changed during"):
                    publish_silence_comparison((sample,), output)
            self.assertFalse(output.exists())

            sample = self._fixture(root)
            publish_silence_comparison((sample,), output)
            digest = sha256_file(output / "comparison.json")
            with self.assertRaisesRegex(SilenceComparisonError, "already exists"):
                publish_silence_comparison((sample,), output)
            self.assertEqual(sha256_file(output / "comparison.json"), digest)


if __name__ == "__main__":
    unittest.main()
