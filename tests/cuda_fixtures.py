"""CUDA provenance double shared by probe and runtime-smoke tests."""

from dataclasses import dataclass


class _Properties:
    name = "Test GPU"


@dataclass
class _Version:
    cuda: str | None


class _Cudnn:
    @staticmethod
    def version() -> int:
        return 91002


@dataclass
class _Backends:
    cudnn: _Cudnn


class FakeCuda:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self) -> bool:
        return self.available

    def current_device(self) -> int:
        return 1

    def get_device_properties(self, index: int) -> _Properties:
        assert index == 1
        return _Properties()

    def mem_get_info(self, index: int) -> tuple[int, int]:
        assert index == 1
        return 12, 34

    def get_device_capability(self, index: int) -> tuple[int, int]:
        assert index == 1
        return 8, 9

    def is_bf16_supported(self) -> bool:
        return True


class FakeTorch:
    __version__ = "2.9.0+cu128"

    def __init__(
        self, available: bool = True, cuda_runtime: str | None = "12.8"
    ) -> None:
        self.cuda = FakeCuda(available)
        self.version = _Version(cuda_runtime)
        self.backends = _Backends(_Cudnn())
