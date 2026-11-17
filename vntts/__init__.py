"""Configure native runtime defaults before optional dependencies load."""

import os

# Avoid ONNX Runtime's background telemetry uploader racing process teardown.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
