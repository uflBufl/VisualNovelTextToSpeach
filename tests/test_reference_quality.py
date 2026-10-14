import json
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from tests.symlink_support import symlink_or_skip
from vntts.reference_quality import (
    analyze_reference,
    analyze_reference_bytes,
    analyze_reference_set,
    main,
)


def write_wav(path, samples, sample_rate=1000):
    pcm = np.round(np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())


class ReferenceQualityTest(unittest.TestCase):
    def test_partial_windows_measure_only_recorded_samples(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "partial.wav"
            for silent_tail in (0, 1):
                with self.subTest(silent_tail=silent_tail):
                    signal = np.concatenate(
                        (self.tone(1001, 0.02), np.zeros(silent_tail))
                    )
                    write_wav(path, signal)
                    report = analyze_reference(path)
                    self.assertEqual(report["inactive_window_fraction"], 0.0)
                    self.assertEqual(report["trailing_silence_seconds"], 0.0)
            write_wav(path, np.concatenate((self.tone(1000), np.zeros(110))))
            self.assertEqual(analyze_reference(path)["trailing_silence_seconds"], 0.11)

    def test_oversized_window_uses_the_recording_without_padding(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "short.wav"
            write_wav(path, self.tone(1001, 0.02))
            payload = path.read_bytes()
            for window_ms in (10_000.0, 1e308):
                with self.subTest(window_ms=window_ms):
                    report = analyze_reference_bytes(
                        payload, path=path, window_ms=window_ms
                    )
                    self.assertEqual(report["inactive_window_fraction"], 0.0)
                    self.assertEqual(report["leading_silence_seconds"], 0.0)
                    self.assertEqual(report["trailing_silence_seconds"], 0.0)

    def test_invalid_analysis_parameters_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tone.wav"
            write_wav(path, self.tone(1000))
            for field, values in (
                ("window_ms", (0.0, -1.0, float("nan"), float("inf"), 10**400)),
                (
                    "silence_db",
                    (float("nan"), float("inf"), -float("inf"), 10**400, -(10**400)),
                ),
            ):
                for value in values:
                    with (
                        self.subTest(field=field, value=value),
                        self.assertRaisesRegex(ValueError, "must be finite"),
                    ):
                        analyze_reference_bytes(
                            path.read_bytes(), path=path, **{field: value}
                        )

    def test_truncated_pcm_is_not_misreported_as_a_short_reference(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            write_wav(path, self.tone(2000))
            for missing in (1, 3800, 4000):
                with (
                    self.subTest(missing=missing),
                    self.assertRaisesRegex(ValueError, "truncated PCM"),
                ):
                    analyze_reference_bytes(path.read_bytes()[:-missing], path=path)

    def test_numeric_bool_and_large_finite_parameters_keep_reports(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tone.wav"
            write_wav(path, self.tone(1000))
            payload = path.read_bytes()
            for options, equivalent in (
                (
                    {"window_ms": True, "silence_db": False},
                    {"window_ms": 1.0, "silence_db": 0.0},
                ),
                (
                    {"window_ms": np.int64(20), "silence_db": np.float32(-40)},
                    {"window_ms": 20.0, "silence_db": -40.0},
                ),
                (
                    {"window_ms": 10**300, "silence_db": -(10**300)},
                    {"window_ms": 1e300, "silence_db": -1e300},
                ),
            ):
                with self.subTest(options=options):
                    self.assertEqual(
                        analyze_reference_bytes(payload, path=path, **options),
                        analyze_reference_bytes(payload, path=path, **equivalent),
                    )

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

    def test_cli_report_cannot_replace_any_input(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            references = [root / "first.wav", root / "second.wav"]
            for path in references:
                write_wav(path, self.tone(2000))
            original = {path: path.read_bytes() for path in references}
            for output in references:
                with self.subTest(output=output), redirect_stderr(StringIO()) as errors:
                    result = main([*map(str, references), "--output", str(output)])
                    self.assertEqual(result, 1)
                    self.assertIn("must differ from input files", errors.getvalue())
                    self.assertEqual({p: p.read_bytes() for p in references}, original)
            report_path = root / "report.json"
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main([*map(str, references), "--output", str(report_path)]), 0
                )
            self.assertEqual(
                json.loads(report_path.read_text()), analyze_reference_set(references)
            )

    def test_cli_report_rejects_input_aliases_without_replacing_them(self):
        for alias in ("hardlink", "file-symlink", "directory-symlink"):
            with self.subTest(alias=alias), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "input.wav"
                write_wav(source, self.tone(2000))
                original = source.read_bytes()
                output = root / "report.json"
                if alias == "hardlink":
                    output.hardlink_to(source)
                elif alias == "file-symlink":
                    symlink_or_skip(output, source)
                else:
                    directory_alias = root / "alias"
                    symlink_or_skip(directory_alias, root, target_is_directory=True)
                    output = directory_alias / source.name
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(main([str(source), "--output", str(output)]), 1)
                self.assertIn("must differ from input files", errors.getvalue())
                self.assertEqual(source.read_bytes(), original)
                self.assertTrue(output.samefile(source))
                if alias == "file-symlink":
                    self.assertTrue(output.is_symlink())

    def test_cli_can_replace_an_existing_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.wav"
            write_wav(source, self.tone(2000))
            original = source.read_bytes()
            output = root / "report.json"
            output.write_text("old report")
            for _ in range(2):
                with redirect_stdout(StringIO()):
                    self.assertEqual(main([str(source), "--output", str(output)]), 0)
                self.assertEqual(
                    json.loads(output.read_text()), analyze_reference_set([source])
                )
                self.assertEqual(source.read_bytes(), original)

    def test_cli_returns_failure_for_an_invalid_wave(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "invalid.wav"
            source.write_text("not wave", encoding="utf-8")

            exit_code = main([str(source), "--output", str(root / "report.json")])

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
