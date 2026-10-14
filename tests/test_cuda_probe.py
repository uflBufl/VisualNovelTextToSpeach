import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.cuda_fixtures import FakeTorch
from vntts.cuda_probe import CudaProbeError, inspect_cuda, main


class CudaProbeTest(unittest.TestCase):
    def test_reports_exact_runtime_and_device(self):
        report = inspect_cuda(FakeTorch())

        self.assertEqual(report["torch"], "2.9.0+cu128")
        self.assertEqual(report["cuda_runtime"], "12.8")
        self.assertEqual(report["device_name"], "Test GPU")
        self.assertEqual(report["compute_capability"], [8, 9])
        self.assertEqual(report["free_vram_bytes"], 12)
        self.assertEqual(report["total_vram_bytes"], 34)
        self.assertTrue(report["bf16_supported"])

    def test_rejects_cpu_only_torch_before_model_loading(self):
        with self.assertRaisesRegex(CudaProbeError, "CPU-only PyTorch"):
            inspect_cuda(FakeTorch(cuda_runtime=None))

    def test_reports_cuda_wheel_without_visible_device(self):
        with self.assertRaisesRegex(CudaProbeError, "includes CUDA 12.8"):
            inspect_cuda(FakeTorch(available=False))

    def test_accepts_cuda_without_optional_bf16_probe(self):
        torch = FakeTorch()
        torch.cuda.is_bf16_supported = None

        report = inspect_cuda(torch)

        self.assertIsNone(report["bf16_supported"])

    def test_rejects_malformed_availability_before_device_inspection(self):
        for value in ("false", 1, None):
            with self.subTest(value=value):
                torch = FakeTorch()
                torch.cuda.is_available = Mock(return_value=value)
                torch.cuda.current_device = Mock()

                with self.assertRaisesRegex(CudaProbeError, "boolean"):
                    inspect_cuda(torch)

                torch.cuda.current_device.assert_not_called()

    def test_requires_boolean_bf16_probe_result(self):
        for value in ("false", 1, None):
            with self.subTest(value=value):
                torch = FakeTorch()
                torch.cuda.is_bf16_supported = Mock(return_value=value)

                with self.assertRaisesRegex(CudaProbeError, "boolean"):
                    inspect_cuda(torch)

        for value in (True, False):
            with self.subTest(value=value):
                torch = FakeTorch()
                torch.cuda.is_bf16_supported = Mock(return_value=value)

                report = inspect_cuda(torch)

                self.assertIs(report["bf16_supported"], value)

    def test_rejects_malformed_cudnn_version(self):
        torch = FakeTorch()
        torch.backends.cudnn.version = Mock(return_value=True)

        with self.assertRaisesRegex(CudaProbeError, "non-integer"):
            inspect_cuda(torch)

    def test_rejects_malformed_vram_values(self):
        torch = FakeTorch()
        torch.cuda.mem_get_info = Mock(return_value=("invalid", 34))

        with self.assertRaisesRegex(CudaProbeError, "invalid literal"):
            inspect_cuda(torch)

    def test_cli_reports_probe_failures_without_writing_report(self):
        cases = (
            ("is_available", "availability failed"),
            ("cudnn.version", "cuDNN failed"),
            ("is_bf16_supported", "BF16 failed"),
        )
        for probe, message in cases:
            with self.subTest(probe=probe), TemporaryDirectory() as directory:
                torch = FakeTorch()
                if probe == "is_available":
                    torch.cuda.is_available = Mock(side_effect=RuntimeError(message))
                elif probe == "cudnn.version":
                    torch.backends.cudnn.version = Mock(
                        side_effect=RuntimeError(message)
                    )
                else:
                    torch.cuda.is_bf16_supported = Mock(
                        side_effect=RuntimeError(message)
                    )
                output = Path(directory) / "report.json"
                stderr = StringIO()
                with patch.dict(sys.modules, {"torch": torch}), redirect_stderr(stderr):
                    self.assertEqual(main(["--output", str(output)]), 2)
                self.assertIn(message, stderr.getvalue())
                self.assertFalse(output.exists())

    def test_cli_atomic_output_failure_keeps_existing_report(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "report.json"
            output.parent.mkdir()
            output.write_text('{"status": "old"}\n', encoding="utf-8")
            stderr = StringIO()
            torch = FakeTorch()

            with (
                patch.dict(sys.modules, {"torch": torch}),
                patch(
                    "durable_file.publication.os.replace",
                    side_effect=OSError("replace failed"),
                ),
                redirect_stderr(stderr),
            ):
                result = main(["--output", str(output)])

            self.assertEqual(result, 2)
            self.assertIn("replace failed", stderr.getvalue())
            self.assertEqual(output.read_text(encoding="utf-8"), '{"status": "old"}\n')
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_cli_nested_output_preserves_json_and_stdout(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "report.json"
            stdout = StringIO()
            torch = FakeTorch()

            with patch.dict(sys.modules, {"torch": torch}), redirect_stdout(stdout):
                result = main(["--output", str(output)])

            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(output.read_text(encoding="utf-8"), stdout.getvalue())
            self.assertEqual(payload["schema"], "vntts.cuda-probe")
            self.assertEqual(payload["schema_version"], 1)

    def test_cli_returns_failure_without_traceback(self):
        with patch(
            "vntts.cuda_probe.inspect_cuda", side_effect=CudaProbeError("no GPU")
        ):
            self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
