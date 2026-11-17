import importlib
import os
import subprocess
import sys
import unittest


def report_worker_environment(connection):
    importlib.import_module("vntts.ocr_worker")
    importlib.import_module("vntts.text_detection_worker")

    connection.send(
        (os.environ.get("ORT_DISABLE_TELEMETRY"), "onnxruntime" in sys.modules)
    )
    connection.close()


class NativeRuntimeBootstrapTest(unittest.TestCase):
    def run_fresh(self, script, initial=None):
        env = os.environ.copy()
        env.pop("ORT_DISABLE_TELEMETRY", None)
        if initial is not None:
            env["ORT_DISABLE_TELEMETRY"] = initial
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_import_disables_uploader_before_loading_native_dependencies(self):
        for initial in (None, "0", "1"):
            with self.subTest(initial=initial):
                self.run_fresh(
                    """
import os, sys
import vntts
assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
assert "onnxruntime" not in sys.modules
assert "rapidocr" not in sys.modules
""",
                    initial,
                )

    def test_spawned_ocr_imports_apply_bootstrap_before_native_loading(self):
        self.run_fresh(
            """
import multiprocessing, os
from tests.test_native_runtime_bootstrap import report_worker_environment
context = multiprocessing.get_context("spawn")
receive, send = context.Pipe(duplex=False)
os.environ["ORT_DISABLE_TELEMETRY"] = "0"
worker = context.Process(target=report_worker_environment, args=(send,))
worker.start()
send.close()
try:
    assert receive.poll(10), "OCR worker import did not finish"
    assert receive.recv() == ("1", False)
    worker.join(10)
    assert worker.exitcode == 0, worker.exitcode
finally:
    if worker.is_alive():
        worker.terminate()
        worker.join(5)
    worker.close()
    receive.close()
"""
        )


if __name__ == "__main__":
    unittest.main()
