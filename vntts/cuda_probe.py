"""Fail-fast CUDA capability report for isolated speech runtimes."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

from durable_file import atomic_write_text

CUDA_PROBE_SCHEMA = "vntts.cuda-probe"
SCHEMA_VERSION = 1


class CudaProbeError(RuntimeError):
    """The selected runtime cannot safely start a CUDA experiment."""


class CudaProbeReport(TypedDict):
    schema: str
    schema_version: int
    python: str
    platform: str
    torch: str
    cuda_runtime: str
    cudnn: int | None
    device_index: int
    device_name: str
    compute_capability: list[int]
    bf16_supported: bool | None
    free_vram_bytes: int
    total_vram_bytes: int


class _CudaDeviceProperties(Protocol):
    name: object


@runtime_checkable
class _CudaAvailability(Protocol):
    def is_available(self) -> bool: ...


@runtime_checkable
class _CudaInspection(Protocol):
    def current_device(self) -> int: ...

    def get_device_properties(self, index: int) -> _CudaDeviceProperties: ...

    def mem_get_info(self, index: int) -> tuple[int, int]: ...

    def get_device_capability(self, index: int) -> tuple[int, ...]: ...


def inspect_cuda(torch_module: object | None = None) -> CudaProbeReport:
    """Return stable CUDA provenance without loading model weights."""
    if torch_module is None:
        try:
            import torch
        except ImportError as error:
            raise CudaProbeError("PyTorch is not installed in this runtime") from error
        torch_module = torch
    cuda = getattr(torch_module, "cuda", None)
    if not isinstance(cuda, _CudaAvailability):
        raise CudaProbeError("This PyTorch build does not expose CUDA")
    version = getattr(torch_module, "version", None)
    cuda_runtime = getattr(version, "cuda", None)
    if not cuda_runtime:
        raise CudaProbeError(
            "This runtime has a CPU-only PyTorch build "
            f"({getattr(torch_module, '__version__', 'unknown')}); run the probe "
            "from the CUDA speech runtime"
        )
    try:
        if not cuda.is_available():
            raise CudaProbeError(
                f"PyTorch includes CUDA {cuda_runtime}, but no CUDA device is "
                "available; check the NVIDIA driver, GPU visibility and selected "
                "runtime. Model weights were not loaded"
            )
        if not isinstance(cuda, _CudaInspection):
            raise AttributeError("CUDA device inspection is unavailable")
        device_index = int(cuda.current_device())
        properties = cuda.get_device_properties(device_index)
        free_memory_raw, total_memory_raw = cuda.mem_get_info(device_index)
        free_memory = int(free_memory_raw)
        total_memory = int(total_memory_raw)
        capability = tuple(
            int(value) for value in cuda.get_device_capability(device_index)
        )
        backends: object = getattr(torch_module, "backends", None)
        cudnn: object = getattr(backends, "cudnn", None)
        cudnn_version_probe = getattr(cudnn, "version", None)
        cudnn_version: object = (
            cudnn_version_probe() if callable(cudnn_version_probe) else None
        )
        if cudnn_version is not None and type(cudnn_version) is not int:
            raise TypeError("cuDNN version probe returned a non-integer")
        bf16_probe = getattr(cuda, "is_bf16_supported", None)
        bf16_supported = bool(bf16_probe()) if callable(bf16_probe) else None
    except CudaProbeError:
        raise
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        raise CudaProbeError(f"Unable to inspect the CUDA device: {error}") from error
    return {
        "schema": CUDA_PROBE_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch": str(getattr(torch_module, "__version__", "unknown")),
        "cuda_runtime": str(cuda_runtime),
        "cudnn": cudnn_version,
        "device_index": device_index,
        "device_name": str(getattr(properties, "name", "unknown")),
        "compute_capability": list(capability),
        "bf16_supported": bf16_supported,
        "free_vram_bytes": free_memory,
        "total_vram_bytes": total_memory,
    }


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify CUDA before downloading or loading model weights"
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = create_parser().parse_args(argv)
    try:
        report = inspect_cuda()
        payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        if arguments.output is not None:
            output = arguments.output.expanduser().resolve()
            atomic_write_text(output, payload + "\n")
        print(payload)
    except (CudaProbeError, OSError) as error:
        print(f"CUDA preflight failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
