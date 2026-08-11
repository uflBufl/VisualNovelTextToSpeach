import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from vntts.ocr import OCRResult
from vntts.ocr_benchmark import benchmark_ocr, write_report


class FakeOCRBackend:
    name = "fake-ocr"

    def recognize(self, image, registry=None, **options):
        del image, registry, options
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

        self.assertAlmostEqual(report["summary"]["median_latency_ms"], 200.0)
        self.assertAlmostEqual(report["summary"]["p95_latency_ms"], 300.0)
        self.assertAlmostEqual(
            report["summary"]["median_cpu_utilization_percent"],
            58.33333333333333,
        )
        self.assertTrue(report["samples"][0]["speaker_match"])
        self.assertEqual(report["samples"][0]["text_similarity"], 1.0)

    def test_report_is_written_atomically(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "nested" / "report.json"

            result = write_report({"version": 1}, output)

            self.assertEqual(result, output.resolve())
            self.assertIn('"version": 1', output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
