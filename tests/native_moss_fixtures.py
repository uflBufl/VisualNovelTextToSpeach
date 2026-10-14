"""Native MOSS probe backend and server doubles with exact stereo responses."""

import io
import wave
from _thread import LockType
from collections.abc import Mapping
from threading import Lock
from typing import ClassVar

import numpy as np

from scripts import moss_native_pause_probe as probe
from tests.pregeneration_fixtures import CollectedResult
from vntts.synthesis import (
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoiceRegistry


def _stereo_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(
            (np.array([[1000, -1000]], dtype="<i2").repeat(480, 0)).tobytes()
        )
    return output.getvalue()


class FakeNativeBackend:
    instances: ClassVar[list["FakeNativeBackend"]] = []

    def __init__(self, _registry: CharacterVoiceRegistry, **options: object) -> None:
        self.registry = _registry
        self.options = options
        self.requests: list[SynthesisRequest] = []
        self.shutdown_called = False
        self.server: probe._OwnedServer | None = None
        self.server_lock: LockType = Lock()
        self.server_info: dict[str, object] | None = None
        self.runtime_status: str | None = None
        type(self).instances.append(self)

    def _http(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        assert timeout is None
        if path == "/tts":
            return 200, {"X-MOSS-Audio-Frames": "2"}, _stereo_wav()
        raise AssertionError(path)

    def render(self, request: SynthesisRequest) -> CollectedResult:
        self.requests.append(request)
        self._http("POST", "/tts", {"text": request.text})
        return self._result(request, SynthesisCompletion.COMPLETE)

    @staticmethod
    def _result(
        request: SynthesisRequest, completion: SynthesisCompletion
    ) -> CollectedResult:
        pcm = np.tile(np.array([[0.1, -0.1]], dtype=np.float32), (480, 1))
        return CollectedResult(
            SynthesisResult(
                pcm=pcm,
                sample_rate=48000,
                completion=completion,
                limits=SynthesisLimits(*probe.moss_generation_limits(request.text)),
                timing=SynthesisTiming(1.0, 2.0),
                diagnostics=SynthesisDiagnostics(
                    "moss-cpp",
                    "fresh-generation",
                    request.generation_profile,
                    request.seed,
                    1,
                    len(pcm),
                ),
            )
        )

    def shutdown(self) -> None:
        self.shutdown_called = True


class OwnedServer:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode
