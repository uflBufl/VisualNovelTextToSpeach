import hashlib
import importlib.util
import io
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from scripts.study_reference_preview_signals import (
    diagnostic_direction,
    measure_sample,
    summarize,
    terminal_pitch_change,
)
from tests.pregeneration_fixtures import clean_wav_bytes


class PreviewSignalStudyTest(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("parselmouth"), "Optional Praat runtime"
    )
    def test_terminal_contour_preserves_rise_and_fall(self):
        for start, end, sign in ((120, 240, 1), (240, 120, -1)):
            rate = 16000
            frequencies = np.linspace(start, end, rate * 2)
            signal = 0.4 * np.sin(2 * np.pi * np.cumsum(frequencies) / rate)
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes((signal * 32767).astype("<i2").tobytes())
            change = terminal_pitch_change(output.getvalue())
            self.assertIsNotNone(change)
            self.assertGreater(sign * change, 0.5)

    def test_mixed_seeds_and_ties_do_not_disappear_in_an_average(self):
        self.assertEqual(diagnostic_direction([3, -2], 1), "mixed")
        self.assertEqual(diagnostic_direction([1, -1, 0], 1), "tie")
        self.assertEqual(diagnostic_direction([3, 0], 1), "typical_lower")

    def test_checksum_mismatch_rejects_before_acoustic_analysis(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "preview.wav").write_bytes(clean_wav_bytes())
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                measure_sample({"wav": "preview.wav", "wav_sha256": "0" * 64}, root)

    def test_qa_and_asr_are_separate_from_pitch_and_missing_measurements(self):
        samples = []
        for method in ("typical", "steady"):
            for seed in (0, 1):
                samples.append(
                    {
                        "character": "Example",
                        "method": method,
                        "text_index": 0,
                        "text": "A statement.",
                        "seed": seed,
                        "qa_failures": [],
                        "asr_exact": method == "typical" or seed == 1,
                        "delivery": {"pitch_hz": 150, "pitch_span_st": 6},
                        "duration_seconds": 2,
                        "words_per_second": 1,
                    }
                )
        report = summarize(samples)["Example"]
        self.assertEqual(report["matched_qa_failure_direction"], "tie")
        self.assertEqual(report["matched_asr_error_direction"], "typical_lower")
        self.assertEqual(report["matched_pitch_span_direction"], "tie")
        samples[-1]["asr_exact"] = None
        self.assertEqual(
            summarize(samples)["Example"]["matched_asr_error_direction"], "unmeasured"
        )
        with self.assertRaisesRegex(ValueError, "both preview methods"):
            summarize(samples[:-1])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            summarize(samples + [samples[0]])
        samples[-1]["text"] = "Different text."
        with self.assertRaisesRegex(ValueError, "identical text"):
            summarize(samples)

    def test_measurement_preserves_independent_asr_and_checks_pcm(self):
        payload = clean_wav_bytes()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "preview.wav").write_bytes(payload)
            with (
                patch(
                    "scripts.study_reference_preview_signals.delivery_features",
                    return_value=None,
                ),
                patch(
                    "scripts.study_reference_preview_signals.terminal_pitch_change",
                    return_value=None,
                ),
            ):
                row = measure_sample(
                    {
                        "wav": "preview.wav",
                        "wav_sha256": hashlib.sha256(payload).hexdigest(),
                        "text": "A statement.",
                        "transcript": "A statement!",
                    },
                    root,
                )
        self.assertTrue(row["asr_exact"])
        self.assertGreater(row["duration_seconds"], 0)
        self.assertEqual(row["qa_failures"], [])


if __name__ == "__main__":
    unittest.main()
