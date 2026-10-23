import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, UnidentifiedImageError

from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from tests.symlink_support import symlink_or_skip
from vntts import ocr_benchmark
from vntts.ocr import DialogRegion, OCRRegions, OCRResult, VoiceRegistry
from vntts.ocr_benchmark import benchmark_ocr, main, write_report


class FakeOCRBackend:
    name = "fake-ocr"

    def recognize(
        self,
        image: Image.Image,
        voice_registry: VoiceRegistry | None = None,
        **options: object,
    ) -> OCRResult:
        del image, voice_registry, options
        return OCRResult("Kamuta", "Paddle out to Itiiti.", 94.0, "balanced", 1)


class OCRBenchmarkTest(unittest.TestCase):
    def test_records_latency_confidence_and_expected_output_similarity(self):
        with TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "dialog.png"
            Image.new("RGB", (640, 160), "black").save(image_path)
            times = iter([1.0, 1.1, 2.0, 2.3])
            cpu_times = iter([10.0, 10.05, 20.0, 20.2])

            report = benchmark_ocr(
                [image_path],
                backend=FakeOCRBackend(),
                repeats=2,
                warmups=0,
                expectations={
                    "dialog.png": {
                        "speaker": "Kamuta",
                        "text": "Paddle out to Itiiti.",
                    }
                },
                clock=lambda: next(times),
                cpu_clock=lambda: next(cpu_times),
            )

        median_latency = report["summary"]["median_latency_ms"]
        p95_latency = report["summary"]["p95_latency_ms"]
        cpu_utilization = report["summary"]["median_process_cpu_utilization_percent"]
        assert median_latency is not None
        assert p95_latency is not None
        assert cpu_utilization is not None
        self.assertEqual(report["version"], 2)
        self.assertEqual(
            report["cpu_scope"], "current_process_all_threads_excluding_children"
        )
        self.assertFalse(report["total_cpu_available"])
        self.assertNotIn("cpu_ms", report["samples"][0])
        self.assertAlmostEqual(report["samples"][0]["process_cpu_ms"]["median"], 125.0)
        self.assertAlmostEqual(median_latency, 200.0)
        self.assertAlmostEqual(p95_latency, 300.0)
        self.assertAlmostEqual(cpu_utilization, 58.33333333333333)
        self.assertTrue(report["samples"][0]["speaker_match"])
        self.assertEqual(report["samples"][0]["text_similarity"], 1.0)

    def test_explicit_regions_and_live_threshold_reach_warmups_and_measurements(self):
        regions = OCRRegions(DialogRegion(0, 0, 1, 0.2), DialogRegion(0, 0.3, 1, 0.7))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dialog.png"
            Image.new("RGB", (20, 10), "white").save(path)
            backend = Mock(wraps=FakeOCRBackend())
            backend.name = "fake-ocr"
            report = benchmark_ocr(
                [path],
                backend=backend,
                repeats=2,
                warmups=1,
                minimum_confidence=60,
                expectations={path.name: {"ocr_regions": regions.to_json()}},
            )
        self.assertEqual(backend.recognize.call_count, 3)
        for call in backend.recognize.call_args_list:
            self.assertEqual(call.kwargs["ocr_regions"], regions)
            self.assertEqual(call.kwargs["minimum_confidence"], 60)
        self.assertEqual(report["minimum_confidence"], 60)
        self.assertEqual(report["samples"][0]["ocr_regions"], regions.to_json())
        self.assertIsNone(report["samples"][0]["text_similarity"])

    def test_rejects_invalid_minimum_confidence_before_recognition(self):
        for confidence in (-1, 101, float("nan"), float("inf")):
            with self.subTest(confidence=confidence):
                backend = Mock()
                with self.assertRaisesRegex(ValueError, "minimum confidence"):
                    benchmark_ocr([], backend=backend, minimum_confidence=confidence)
                backend.recognize.assert_not_called()

    def test_malformed_regions_are_rejected_before_image_decode(self):
        backend = Mock()
        with self.assertRaisesRegex(ValueError, "ocr_regions must be an object"):
            benchmark_ocr(
                ["missing.png"],
                backend=backend,
                expectations={"missing.png": {"ocr_regions": []}},
            )
        backend.recognize.assert_not_called()

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_image_acquisition_refuses_fifo_before_ocr_and_recovers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "dialog.png"
            Image.new("RGB", (20, 10), "white").save(path)
            backend = Mock(wraps=FakeOCRBackend())
            backend.name = "fake-ocr"
            gate: RegularFileGate = {"path": path, "armed": True}
            with (
                swap_at_acquisition(self, gate),
                self.assertRaisesRegex(OSError, "not a regular file"),
            ):
                benchmark_ocr([path], backend=backend, repeats=1, warmups=0)
            backend.recognize.assert_not_called()
            report = benchmark_ocr([path], backend=backend, repeats=1, warmups=0)
            self.assertEqual(report["summary"]["images"], 1)
            backend.recognize.assert_called_once()

    def test_image_source_ownership_retains_pixels_and_failure_cleanup(self):
        for role in ("PNG", "JPEG", "TIFF", "unidentified", "conversion", "missing"):
            with self.subTest(role=role), TemporaryDirectory() as directory:
                path = Path(directory).resolve() / "dialog.image"
                if role == "unidentified":
                    path.write_bytes(b"not an image")
                elif role != "missing":
                    Image.new("L", (20, 10), 123).save(
                        path, format=role if role in {"PNG", "JPEG", "TIFF"} else "PNG"
                    )
                sources = []
                acquire = ocr_benchmark.open_regular_binary
                decode = Image.open

                @contextlib.contextmanager
                def opened(candidate):
                    with acquire(candidate) as source:
                        sources.append(source)
                        yield source

                def decoded(source, *args, **kwargs):
                    self.assertIs(source, sources[-1])
                    self.assertFalse(source.closed)
                    return decode(source, *args, **kwargs)

                def recognize(image, *_args, **_kwargs):
                    self.assertTrue(sources[-1].closed)
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.size, (20, 10))
                    self.assertEqual(image.tobytes(), bytes([123, 123, 123]) * 200)
                    return OCRResult("Kamuta", "Dialog text.", 94.0, "balanced", 1)

                backend = Mock()
                backend.name = "fake-ocr"
                backend.distribution_names = ()
                backend.recognize.side_effect = recognize
                error = ValueError("forced RGB failure")
                with (
                    patch.object(ocr_benchmark, "open_regular_binary", opened),
                    patch.object(Image, "open", decoded),
                    patch.object(Image.Image, "convert", side_effect=error)
                    if role == "conversion"
                    else contextlib.nullcontext(),
                ):
                    if role in {"unidentified", "conversion", "missing"}:
                        error_type = {
                            "unidentified": UnidentifiedImageError,
                            "conversion": ValueError,
                            "missing": FileNotFoundError,
                        }[role]
                        with self.assertRaises(error_type) as caught:
                            benchmark_ocr([path], backend=backend, repeats=1, warmups=0)
                        if role == "conversion":
                            self.assertIs(caught.exception, error)
                        elif role == "missing":
                            assert isinstance(caught.exception, FileNotFoundError)
                            self.assertEqual(caught.exception.filename, str(path))
                        backend.recognize.assert_not_called()
                    else:
                        benchmark_ocr([path], backend=backend, repeats=1, warmups=1)
                        self.assertEqual(backend.recognize.call_count, 2)
                self.assertEqual(len(sources), 0 if role == "missing" else 1)
                self.assertTrue(all(source.closed for source in sources))

    def test_report_is_written_atomically(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "nested" / "report.json"

            result = write_report({"version": 1}, output)

            self.assertEqual(result, output.resolve())
            self.assertIn('"version": 1', output.read_text(encoding="utf-8"))

    def test_rejects_non_text_expectation_fields(self):
        with TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "dialog.png"
            Image.new("RGB", (10, 10), "black").save(image_path)
            backend = Mock(wraps=FakeOCRBackend())

            with self.assertRaisesRegex(ValueError, "expected text"):
                benchmark_ocr(
                    [image_path],
                    backend=backend,
                    repeats=1,
                    warmups=0,
                    expectations={"dialog.png": {"text": 42}},
                    clock=lambda: 0.0,
                    cpu_clock=lambda: 0.0,
                )

            backend.recognize.assert_not_called()

    def test_rejects_selected_malformed_expectation_before_image_decode(self):
        with TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "missing.png"
            backend = Mock()

            with self.assertRaisesRegex(ValueError, "expected text"):
                benchmark_ocr(
                    [image_path],
                    backend=backend,
                    repeats=1,
                    warmups=0,
                    expectations={"missing.png": {"text": 42}},
                    clock=lambda: 0.0,
                    cpu_clock=lambda: 0.0,
                )

            backend.recognize.assert_not_called()

    def test_cpu_utilization_is_none_without_positive_wall_intervals(self):
        with TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "dialog.png"
            Image.new("RGB", (10, 10), "black").save(image_path)

            report = benchmark_ocr(
                [image_path],
                backend=FakeOCRBackend(),
                repeats=1,
                warmups=0,
                clock=lambda: 1.0,
                cpu_clock=lambda: 1.0,
            )

        self.assertIsNone(report["summary"]["median_process_cpu_utilization_percent"])

    def test_empty_image_input_has_empty_timing_summary(self):
        report = benchmark_ocr(
            [],
            backend=FakeOCRBackend(),
            repeats=1,
            warmups=0,
            clock=lambda: 1.0,
            cpu_clock=lambda: 1.0,
        )

        self.assertEqual(report["summary"]["images"], 0)
        self.assertIsNone(report["summary"]["median_process_cpu_utilization_percent"])

    def test_cli_refuses_report_over_input_files_before_backend_creation(self):
        for input_name in ("dialog.png", "expectations.json", "manifest.json"):
            for alias in ("direct", "hardlink", "symlink"):
                with (
                    self.subTest(input=input_name, alias=alias),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    image = root / "dialog.png"
                    Image.new("RGB", (10, 10), "black").save(image)
                    expectations = root / "expectations.json"
                    expectations.write_text("{}", encoding="utf-8")
                    manifest = root / "manifest.json"
                    manifest.write_text("{}", encoding="utf-8")
                    source = root / input_name
                    output = source if alias == "direct" else root / "report.json"
                    if alias == "hardlink":
                        os.link(source, output)
                    elif alias == "symlink":
                        symlink_or_skip(output, source)
                    original = {
                        path: path.read_bytes()
                        for path in (image, expectations, manifest)
                    }
                    errors = io.StringIO()
                    with (
                        patch.object(
                            ocr_benchmark,
                            "find_default_voice_manifest",
                            return_value=manifest,
                        ),
                        patch.object(
                            ocr_benchmark.CharacterVoiceRegistry, "from_file"
                        ) as registry,
                        patch.object(ocr_benchmark, "TesseractOCRBackend") as backend,
                        patch.object(ocr_benchmark, "benchmark_ocr") as benchmark,
                        contextlib.redirect_stderr(errors),
                    ):
                        self.assertEqual(
                            main(
                                [
                                    str(image),
                                    "--expectations",
                                    str(expectations),
                                    "--output",
                                    str(output),
                                ]
                            ),
                            1,
                        )
                    self.assertIn(
                        "Output must differ from input files", errors.getvalue()
                    )
                    registry.assert_not_called()
                    backend.assert_not_called()
                    benchmark.assert_not_called()
                    self.assertEqual(
                        {path: path.read_bytes() for path in original}, original
                    )
                    self.assertEqual(output.read_bytes(), original[source])

    def test_cli_replaces_an_existing_report_with_fake_backend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "dialog.png"
            Image.new("RGB", (10, 10), "black").save(image)
            original = image.read_bytes()
            output = root / "report.json"
            output.write_text("old report", encoding="utf-8")
            with (
                patch.object(
                    ocr_benchmark, "find_default_voice_manifest", return_value=None
                ),
                patch.object(
                    ocr_benchmark, "TesseractOCRBackend", return_value=FakeOCRBackend()
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            str(image),
                            "--output",
                            str(output),
                            "--repeats",
                            "1",
                            "--minimum-confidence",
                            "60",
                            "--warmups",
                            "0",
                        ]
                    ),
                    0,
                )
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["images"], 1)
            self.assertEqual(report["minimum_confidence"], 60)
            self.assertEqual(image.read_bytes(), original)

    def test_cli_reports_malformed_expectations_without_traceback(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            expectations = root / "expectations.json"
            expectations.write_text("{", encoding="utf-8")
            output = root / "report.json"
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        str(root / "missing.png"),
                        "--expectations",
                        str(expectations),
                        "--output",
                        str(output),
                    ]
                )

        self.assertEqual(result, 1)
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
