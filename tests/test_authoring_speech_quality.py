import io
import unittest
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.speech_quality import (
    SpeechQuality,
    SpeechSilenceSpan,
    SpeechSilenceValidationError,
    analyze_generated_speech_samples,
    inspect_generated_speech,
    inspect_generated_speech_samples,
    measure_generated_speech,
    measure_generated_speech_bytes,
    measure_generated_speech_samples,
)


def wav_bytes(samples, sample_rate=125):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return output.getvalue()


class AuthoringSpeechQualityTest(unittest.TestCase):
    def test_sample_inspection_preserves_path_gate_and_diagnosis(self):
        for samples in (np.full(13, 1000), np.zeros(153)):
            with TemporaryDirectory() as directory:
                path = Path(directory) / "speech.wav"
                path.write_bytes(wav_bytes(samples))
                for version in (1, 2):
                    with self.subTest(count=len(samples), version=version):
                        options = {"analysis_version": version, "text": "One. Two."}
                        if samples.any():
                            self.assertEqual(
                                inspect_generated_speech_samples(
                                    samples,
                                    sample_rate=125,
                                    duration_seconds=len(samples) / 125,
                                    **options,
                                ),
                                inspect_generated_speech(path, **options),
                            )
                        else:
                            with self.assertRaises(
                                SpeechSilenceValidationError
                            ) as path_error:
                                inspect_generated_speech(path, **options)
                            with self.assertRaises(
                                SpeechSilenceValidationError
                            ) as sample_error:
                                inspect_generated_speech_samples(
                                    samples,
                                    sample_rate=125,
                                    duration_seconds=len(samples) / 125,
                                    **options,
                                )
                            self.assertEqual(
                                sample_error.exception.quality,
                                path_error.exception.quality,
                            )
                            self.assertEqual(
                                sample_error.exception.failures,
                                path_error.exception.failures,
                            )
                            self.assertEqual(
                                sample_error.exception.diagnosis,
                                path_error.exception.diagnosis,
                            )

    def test_measurements_skip_diagnostic_spans_but_inspection_keeps_them(self):
        samples = np.zeros(160, dtype=np.int16)
        payload = wav_bytes(samples)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "silent.wav"
            path.write_bytes(payload)
            for version in (1, 2):
                with self.subTest(version=version):
                    expected = SpeechQuality(1.0, 1.28, 1.28, 0.0, version)
                    with patch(
                        "vntts.authoring.speech_quality.SpeechSilenceSpan",
                        wraps=SpeechSilenceSpan,
                    ) as create_span:
                        self.assertEqual(
                            measure_generated_speech(path, analysis_version=version),
                            expected,
                        )
                        self.assertEqual(
                            measure_generated_speech_bytes(
                                payload, analysis_version=version
                            ),
                            expected,
                        )
                        self.assertEqual(
                            measure_generated_speech_samples(
                                samples,
                                sample_rate=125,
                                duration_seconds=1.28,
                                analysis_version=version,
                            ),
                            expected,
                        )
                        create_span.assert_not_called()
                    with self.assertRaises(SpeechSilenceValidationError) as rejected:
                        inspect_generated_speech(path, analysis_version=version)
                    self.assertEqual(rejected.exception.quality, expected)
                    self.assertEqual(
                        rejected.exception.diagnosis.spans,
                        (SpeechSilenceSpan("all_silent", 0.0, 1.28, 1.28),),
                    )

    def test_versions_retain_threshold_and_partial_frame_metrics(self):
        # At 125 Hz each analysis frame is ten samples. 184 PCM16 units
        # falls below -45 dBFS, while 185 falls above it.
        samples = np.concatenate((np.full(70, 184), np.full(10, 185), np.full(7, 184)))
        current, spans = analyze_generated_speech_samples(
            samples, sample_rate=125, duration_seconds=87 / 125, analysis_version=2
        )
        self.assertEqual(current, SpeechQuality(0.8889, 0.56, 0.08, 0.0, 2))
        self.assertEqual(spans, (SpeechSilenceSpan("leading", 0.0, 0.56, 0.56),))
        legacy, legacy_spans = analyze_generated_speech_samples(
            samples, sample_rate=125, duration_seconds=87 / 125, analysis_version=1
        )
        self.assertEqual(legacy, SpeechQuality(0.0, 0.0, 0.0, 0.0, 1))
        self.assertEqual(legacy_spans, ())

    def test_internal_and_trailing_spans_keep_recorded_end(self):
        samples = np.concatenate(
            (np.full(10, 1000), np.zeros(70), np.full(10, 1000), np.zeros(63))
        )
        for version in (1, 2):
            with self.subTest(version=version):
                quality, spans = analyze_generated_speech_samples(
                    samples,
                    sample_rate=125,
                    duration_seconds=153 / 125,
                    analysis_version=version,
                )
                self.assertEqual(
                    quality, SpeechQuality(0.875, 0.0, 0.56, 0.56, version)
                )
                self.assertEqual(
                    spans,
                    (
                        SpeechSilenceSpan("internal", 0.08, 0.64, 0.56),
                        SpeechSilenceSpan("trailing", 0.72, 1.224, 0.504),
                    ),
                )

    def test_all_silent_and_short_recordings_keep_duration(self):
        for count in (1, 7, 71):
            for version in (1, 2):
                with self.subTest(count=count, version=version):
                    duration = count / 125
                    quality, spans = analyze_generated_speech_samples(
                        np.zeros(count),
                        sample_rate=125,
                        duration_seconds=duration,
                        analysis_version=version,
                    )
                    self.assertEqual(
                        quality, SpeechQuality(1.0, duration, duration, 0.0, version)
                    )
                    expected = (
                        (SpeechSilenceSpan("all_silent", 0.0, duration, duration),)
                        if duration >= 0.5
                        else ()
                    )
                    self.assertEqual(spans, expected)

    def test_public_entry_points_agree_and_do_not_modify_samples(self):
        values = np.full(13, 1000, dtype=np.float32)
        values.setflags(write=False)
        expected = SpeechQuality(0.0, 0.0, 0.0, 0.0, 2)
        self.assertEqual(
            measure_generated_speech_samples(
                values, sample_rate=125, duration_seconds=13 / 125, analysis_version=2
            ),
            expected,
        )
        np.testing.assert_array_equal(values, np.full(13, 1000, dtype=np.float32))
        content = wav_bytes(values)
        self.assertEqual(measure_generated_speech_bytes(content), expected)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "speech.wav"
            path.write_bytes(content)
            self.assertEqual(measure_generated_speech(path), expected)
            self.assertEqual(inspect_generated_speech(path), expected)

    def test_manifest_rounded_duration_remains_supported(self):
        quality = measure_generated_speech_samples(
            np.ones(1), sample_rate=44100, duration_seconds=0.0, analysis_version=1
        )
        self.assertEqual(quality, SpeechQuality(0.0, 0.0, 0.0, 0.0, 1))
        measure_generated_speech_samples(
            np.zeros(37),
            sample_rate=44100,
            duration_seconds=round(37 / 44100, 4),
            analysis_version=2,
        )

    def test_invalid_versions_fail_before_path_or_payload_decode(self):
        for version in (True, 1.0, [], {}, None, 3):
            with self.subTest(version=version):
                for operation in (
                    lambda: measure_generated_speech(
                        "missing.wav", analysis_version=version
                    ),
                    lambda: inspect_generated_speech(
                        "missing.wav", analysis_version=version
                    ),
                    lambda: measure_generated_speech_bytes(
                        b"bad WAV", analysis_version=version
                    ),
                    lambda: measure_generated_speech_samples(
                        [1],
                        sample_rate=125,
                        duration_seconds=1 / 125,
                        analysis_version=version,
                    ),
                    lambda: inspect_generated_speech_samples(
                        ["invalid"],
                        sample_rate=125,
                        duration_seconds=1 / 125,
                        analysis_version=version,
                    ),
                ):
                    with self.assertRaisesRegex(
                        BulkGenerationError, "analysis version"
                    ):
                        operation()

    def test_invalid_samples_and_timing_raise_domain_errors(self):
        cases = (
            ([], 125, 0.0),
            ([[1]], 125, 1 / 125),
            ([float("nan")], 125, 1 / 125),
            ([float("inf")], 125, 1 / 125),
            (["bad"], 125, 1 / 125),
            ([1], 0, 1.0),
            ([1], True, 1.0),
            ([1], 125.0, 1 / 125),
            ([1], 125, float("nan")),
            ([1], 125, float("inf")),
            ([1], 125, True),
            ([1], 125, -1.0),
            ([1], 125, "bad"),
            ([1], 125, 1.0),
        )
        for samples, rate, duration in cases:
            with self.subTest(samples=samples, rate=rate, duration=duration):
                with self.assertRaises(BulkGenerationError):
                    measure_generated_speech_samples(
                        samples,
                        sample_rate=rate,
                        duration_seconds=duration,
                        analysis_version=2,
                    )
        with self.assertRaises(BulkGenerationError):
            measure_generated_speech_bytes(wav_bytes([]))
