"""Fail-fast CUDA capability report for isolated speech runtimes."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

CUDA_PROBE_SCHEMA = "vntts.cuda-probe"
SCHEMA_VERSION = 1


class CudaProbeError(RuntimeError):
    """The selected runtime cannot safely start a CUDA experiment."""


def inspect_cuda(torch_module=None):
    """Return stable CUDA provenance without loading model weights."""
    if torch_module is None:
        try:
            import torch
        except ImportError as error:
            raise CudaProbeError("PyTorch is not installed in this runtime") from error
        torch_module = torch
    cuda = getattr(torch_module, "cuda", None)
    if cuda is None or not callable(getattr(cuda, "is_available", None)):
        raise CudaProbeError("This PyTorch build does not expose CUDA")
    if not cuda.is_available():
        raise CudaProbeError("CUDA is unavailable; model weights were not loaded")
    try:
        device_index = int(cuda.current_device())
        properties = cuda.get_device_properties(device_index)
        free_memory, total_memory = cuda.mem_get_info(device_index)
        capability = tuple(
            int(value) for value in cuda.get_device_capability(device_index)
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise CudaProbeError(f"Unable to inspect the CUDA device: {error}") from error
    version = getattr(torch_module, "version", None)
    cudnn = getattr(getattr(torch_module, "backends", None), "cudnn", None)
    cudnn_version = (
        cudnn.version() if callable(getattr(cudnn, "version", None)) else None
    )
    bf16_supported = (
        bool(cuda.is_bf16_supported())
        if callable(getattr(cuda, "is_bf16_supported", None))
        else None
    )
    return {
        "schema": CUDA_PROBE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch": str(getattr(torch_module, "__version__", "unknown")),
        "cuda_runtime": str(getattr(version, "cuda", None) or "unknown"),
        "cudnn": cudnn_version,
        "device_index": device_index,
        "device_name": str(getattr(properties, "name", "unknown")),
        "compute_capability": list(capability),
        "bf16_supported": bf16_supported,
        "free_vram_bytes": int(free_memory),
        "total_vram_bytes": int(total_memory),
    }


def create_parser():
    parser = argparse.ArgumentParser(
        description="Verify CUDA before downloading or loading model weights"
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv=None):
    arguments = create_parser().parse_args(argv)
    try:
        report = inspect_cuda()
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if arguments.output is not None:
            output = arguments.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
    except (CudaProbeError, OSError) as error:
        print(f"CUDA preflight failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
