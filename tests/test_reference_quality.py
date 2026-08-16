import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from vntts.reference_quality import analyze_reference, analyze_reference_set, main


def write_wav(path, samples, sample_rate=1000):
    pcm = np.round(np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


class ReferenceQualityTest(unittest.TestCase):
    @staticmethod
    def tone(length, amplitude=0.2):
        values = np.full(length, amplitude)
        values[1::2] *= -1
        return values

    def test_clean_reference_passes_with_boundary_silence_metrics(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "clean.wav"
            signal = np.concatenate(
                (
                    np.zeros(100),
                    self.tone(1000),
                    np.zeros(100),
                )
            )
            write_wav(path, signal)

            result = analyze_reference(path)

        self.assertEqual(result["objective_preflight"], "pass")
        self.assertAlmostEqual(result["duration_seconds"], 1.2)
        self.assertAlmostEqual(result["leading_silence_seconds"], 0.1)
        self.assertAlmostEqual(result["trailing_silence_seconds"], 0.1)
        self.assertEqual(
            result["manual_review_required"],
            [
                "single-speaker-identity",
                "music-or-background-audio",
                "spoken-content-and-pronunciation",
            ],
        )

    def test_clipped_or_too_short_reference_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "bad.wav"
            write_wav(path, np.ones(500))

            result = analyze_reference(path)

        self.assertEqual(result["objective_preflight"], "reject")
        self.assertIn("duration-under-1-second", result["rejection_reasons"])
        self.assertIn("excessive-clipping", result["rejection_reasons"])

    def test_set_ranks_cleaner_boundary_silence_first_without_auto_selecting(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            slower = root / "slower.wav"
            cleaner = root / "cleaner.wav"
            write_wav(
                slower,
                np.concatenate((np.zeros(300), self.tone(1000))),
            )
            write_wav(
                cleaner,
                np.concatenate((np.zeros(100), self.tone(1000))),
            )

            report = analyze_reference_set([slower, cleaner])

        self.assertEqual(report["objective_ranking"], [2, 1])
        self.assertIn("blinded listening comparison", report["selection_policy"])

    def test_cli_returns_failure_for_an_invalid_wave(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "invalid.wav"
            source.write_text("not wave", encoding="utf-8")

            exit_code = main([str(source), "--output", str(root / "report.json")])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
