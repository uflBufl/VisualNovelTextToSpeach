import unittest
from unittest.mock import patch

from vntts.cuda_probe import CudaProbeError, inspect_cuda, main


class FakeCuda:
    def __init__(self, available=True):
        self.available = available

    def is_available(self):
        return self.available

    def current_device(self):
        return 1

    def get_device_properties(self, index):
        assert index == 1
        return type("Properties", (), {"name": "Test GPU"})()

    def mem_get_info(self, index):
        assert index == 1
        return 12, 34

    def get_device_capability(self, index):
        assert index == 1
        return 8, 9

    def is_bf16_supported(self):
        return True


class FakeTorch:
    __version__ = "2.9.0+cu128"

    def __init__(self, available=True):
        self.cuda = FakeCuda(available)
        self.version = type("Version", (), {"cuda": "12.8"})()
        cudnn = type("Cudnn", (), {"version": staticmethod(lambda: 91002)})()
        self.backends = type("Backends", (), {"cudnn": cudnn})()


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

    def test_rejects_cpu_runtime_before_model_loading(self):
        with self.assertRaisesRegex(CudaProbeError, "unavailable"):
            inspect_cuda(FakeTorch(available=False))

    def test_cli_returns_failure_without_traceback(self):
        with patch(
            "vntts.cuda_probe.inspect_cuda", side_effect=CudaProbeError("no GPU")
        ):
            self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
