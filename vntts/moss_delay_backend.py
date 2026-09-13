"""Typed render-only adapter for the upstream MOSS Delay 8B model."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from threading import Event, Lock
from time import monotonic
from typing import Protocol, TypeAlias

import numpy as np
from numpy.typing import NDArray

from vntts.speech_backend import (
    SpeechBackendCapabilities,
    TTSConfigurationError,
    TTSSynthesisError,
    get_moss_tts_generation_profile,
    moss_generation_limits,
    normalize_moss_language,
)
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunk,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisDiagnostics,
    SynthesisLimits,
    SynthesisRequest,
    SynthesisResult,
    SynthesisTiming,
)
from vntts.voices import CharacterVoiceRegistry, resolve_required_voice_reference

AudioArray: TypeAlias = NDArray[np.float32]
Clock: TypeAlias = Callable[[], float]


class _Tensor(Protocol):
    shape: tuple[int, ...]

    def to(self, device: str) -> _Tensor: ...


class _AudioTokenizer(Protocol):
    def to(self, device: str) -> object: ...


class _ModelConfig(Protocol):
    sampling_rate: int | str


class _DecodedItem(Protocol):
    audio_codes_list: Sequence[object]


class _DelayProcessor(Protocol):
    audio_tokenizer: _AudioTokenizer
    model_config: _ModelConfig

    def build_user_message(
        self, *, text: str, reference: list[str], language: str
    ) -> object: ...

    def __call__(
        self, messages: list[list[object]], *, mode: str
    ) -> Mapping[str, _Tensor]: ...

    def decode(self, outputs: _Tensor) -> Sequence[_DecodedItem]: ...


class _DelayModel(Protocol):
    def to(self, device: str) -> _DelayModel: ...

    def eval(self) -> object: ...

    def generate(
        self,
        *,
        input_ids: _Tensor,
        attention_mask: _Tensor,
        max_new_tokens: int,
        **options: object,
    ) -> _Tensor: ...


class _NoGrad(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, *args: object) -> None: ...


class _Cuda(Protocol):
    def is_available(self) -> bool: ...

    def is_bf16_supported(self) -> bool: ...

    def get_device_capability(self) -> tuple[int, int]: ...

    def manual_seed_all(self, seed: int) -> object: ...


class _Torch(Protocol):
    cuda: _Cuda
    bfloat16: object
    float32: object

    def manual_seed(self, seed: int) -> object: ...

    def no_grad(self) -> _NoGrad: ...


class _AutoModel(Protocol):
    def from_pretrained(self, model_name: str, **options: object) -> _DelayModel: ...


class _AutoProcessor(Protocol):
    def from_pretrained(
        self, model_name: str, **options: object
    ) -> _DelayProcessor: ...


default_moss_tts_delay_model = "OpenMOSS-Team/MOSS-TTS-v1.5"
delay_audio_tokens_per_second = 12.5


class MossTTSDelayVoiceRouterBackend:
    """MOSS Delay 8B inference without playback or Local-model cache reuse."""

    name = "moss-tts-delay"
    capabilities = SpeechBackendCapabilities(
        voice_cloning=True,
        streaming=False,
        concurrent_prepare_and_play=False,
        interrupt_on_dialog_replacement=True,
    )

    def __init__(
        self,
        registry: CharacterVoiceRegistry,
        *,
        narrator_reference: str | Path | None = None,
        language: str = "English",
        model_name: str | None = None,
        generation_profile: str = "expressive",
        require_cuda: bool = False,
        model_revision: str | None = None,
        model: _DelayModel | None = None,
        processor: _DelayProcessor | None = None,
        torch_module: _Torch | None = None,
        auto_model: _AutoModel | None = None,
        auto_processor: _AutoProcessor | None = None,
        clock: Clock = monotonic,
    ) -> None:
        self.registry = registry
        self.narrator_reference = narrator_reference
        self.narrator_speaker = "MOSS Delay reference voice"
        self.language = normalize_moss_language(language)
        self.model_name = str(model_name or default_moss_tts_delay_model)
        self.model_revision = str(model_revision) if model_revision else None
        self.generation_profile, _options = get_moss_tts_generation_profile(
            generation_profile
        )
        self.clock = clock
        self.model_lock = Lock()
        self.stop_requested = Event()

        if torch_module is None:
            try:
                import torch
            except ImportError as error:
                raise TTSConfigurationError(
                    "MOSS Delay requires its isolated PyTorch runtime. Run "
                    "`uv sync --project backends/moss-tts-delay`."
                ) from error
            torch_module = torch
        self.torch = torch_module
        self.device = self._select_device(torch_module)
        if require_cuda and self.device != "cuda":
            raise TTSConfigurationError(
                "MOSS Delay comparison requires CUDA; refusing CPU model loading"
            )
        if require_cuda:
            supports_bf16 = getattr(torch_module.cuda, "is_bf16_supported", None)
            if not callable(supports_bf16) or not supports_bf16():
                raise TTSConfigurationError(
                    "MOSS Delay comparison requires CUDA BF16 support; "
                    "refusing model loading"
                )
        self.dtype = (
            torch_module.bfloat16 if self.device == "cuda" else torch_module.float32
        )

        if processor is None or model is None:
            if auto_model is None or auto_processor is None:
                try:
                    from transformers import AutoModel, AutoProcessor
                except ImportError as error:
                    raise TTSConfigurationError(
                        "MOSS Delay requires Transformers in its isolated runtime. "
                        "Run `uv sync --project backends/moss-tts-delay`."
                    ) from error
                auto_model = AutoModel
                auto_processor = AutoProcessor
            try:
                processor = auto_processor.from_pretrained(
                    self.model_name,
                    trust_remote_code=True,
                    **(
                        {"revision": self.model_revision}
                        if self.model_revision is not None
                        else {}
                    ),
                )
                processor.audio_tokenizer = processor.audio_tokenizer.to(self.device)
                model = auto_model.from_pretrained(
                    self.model_name,
                    trust_remote_code=True,
                    attn_implementation=self._attention_implementation(),
                    torch_dtype=self.dtype,
                    **(
                        {"revision": self.model_revision}
                        if self.model_revision is not None
                        else {}
                    ),
                ).to(self.device)
                model.eval()
            except Exception as error:
                raise TTSConfigurationError(
                    f"MOSS Delay could not load {self.model_name!r}: {error}"
                ) from error
        self.processor = processor
        self.model = model
        try:
            self.sample_rate = int(self.processor.model_config.sampling_rate)
        except (AttributeError, TypeError, ValueError) as error:
            raise TTSConfigurationError(
                "MOSS Delay processor does not declare a valid sampling rate"
            ) from error
        if self.sample_rate <= 0:
            raise TTSConfigurationError(
                "MOSS Delay processor does not declare a valid sampling rate"
            )

    @staticmethod
    def _select_device(torch_module: _Torch) -> str:
        cuda = getattr(torch_module, "cuda", None)
        if cuda is not None and callable(getattr(cuda, "is_available", None)):
            if cuda.is_available():
                return "cuda"
        return "cpu"

    def _attention_implementation(self) -> str:
        if self.device != "cuda":
            return "eager"
        if importlib.util.find_spec("flash_attn") is not None:
            try:
                major, _minor = self.torch.cuda.get_device_capability()
            except AttributeError, RuntimeError, TypeError, ValueError:
                major = 0
            if major >= 8:
                return "flash_attention_2"
        return "sdpa"

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream:
        if not isinstance(request, SynthesisRequest):
            raise TTSConfigurationError("MOSS Delay received an invalid request")
        return SynthesisChunkStream(self._render_chunks(request))

    def _render_chunks(
        self, request: SynthesisRequest
    ) -> Generator[SynthesisChunk, None, SynthesisResult]:
        started = self.clock()
        text = " ".join((request.text or "").split())
        if not text:
            raise TTSSynthesisError("MOSS Delay received empty text")
        profile, options = get_moss_tts_generation_profile(request.generation_profile)
        try:
            cache_policy = SynthesisCachePolicy(request.cache_policy)
        except ValueError as error:
            raise TTSConfigurationError(
                f"Unknown synthesis cache policy {request.cache_policy!r}"
            ) from error
        if cache_policy is not SynthesisCachePolicy.BYPASS:
            raise TTSConfigurationError(
                "MOSS Delay currently supports only checksum-bound BYPASS renders"
            )
        _voice_key, reference = self._resolve_voice_source(request.voice)
        _local_tokens, max_audio_seconds = moss_generation_limits(text)
        max_new_tokens = max(
            1,
            round(max_audio_seconds * delay_audio_tokens_per_second),
        )
        if request.cancellation_requested() or self.stop_requested.is_set():
            return self._result(
                np.empty((0, 1), dtype=np.float32),
                SynthesisCompletion.CANCELLED,
                profile,
                request.seed,
                max_new_tokens,
                max_audio_seconds,
                started,
                None,
            )
        try:
            message = self.processor.build_user_message(
                text=text,
                reference=[str(reference)],
                language=self.language,
            )
            batch = self.processor([[message]], mode="generation")
            input_ids = batch["input_ids"].to(self.device)
            attention_mask = batch["attention_mask"].to(self.device)
            if request.seed is not None:
                self.torch.manual_seed(request.seed)
                if self.device == "cuda":
                    self.torch.cuda.manual_seed_all(request.seed)
            with self.model_lock, self.torch.no_grad():
                outputs = self.model.generate(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    max_new_tokens=max_new_tokens,
                    **options,
                )
                decoded = self.processor.decode(outputs)
        except TTSConfigurationError, TTSSynthesisError:
            raise
        except Exception as error:
            if request.cancellation_requested() or self.stop_requested.is_set():
                return self._result(
                    np.empty((0, 1), dtype=np.float32),
                    SynthesisCompletion.CANCELLED,
                    profile,
                    request.seed,
                    max_new_tokens,
                    max_audio_seconds,
                    started,
                    None,
                )
            raise TTSSynthesisError(f"MOSS Delay generation failed: {error}") from error
        if not isinstance(decoded, (list, tuple)) or len(decoded) != 1:
            raise TTSSynthesisError("MOSS Delay returned an invalid decoded batch")
        try:
            pcm = self._to_numpy(decoded[0].audio_codes_list[0])
        except (AttributeError, IndexError, TypeError, ValueError) as error:
            raise TTSSynthesisError("MOSS Delay returned no decoded audio") from error
        if not pcm.size:
            raise TTSSynthesisError("MOSS Delay returned no decoded audio")
        limited = self._generated_token_count(outputs, input_ids) >= max_new_tokens
        completion = (
            SynthesisCompletion.LIMITED if limited else SynthesisCompletion.COMPLETE
        )
        first_chunk_ms = (self.clock() - started) * 1000
        yield SynthesisChunk(pcm, self.sample_rate, 0, first_chunk_ms)
        return self._result(
            pcm,
            completion,
            profile,
            request.seed,
            max_new_tokens,
            max_audio_seconds,
            started,
            first_chunk_ms,
        )

    def _resolve_voice_source(self, character: str) -> tuple[str, Path]:
        voice_key, source = resolve_required_voice_reference(
            self.registry,
            character,
            self.narrator_reference,
            backend_name="MOSS Delay",
            missing_message=(
                "MOSS Delay requires one voice reference. Use a model variant "
                "voice override for narration or configure a narrator reference."
            ),
            error_type=TTSConfigurationError,
        )
        return str(voice_key), Path(source)

    @staticmethod
    def _generated_token_count(outputs: _Tensor, input_ids: _Tensor) -> int:
        try:
            return max(0, int(outputs.shape[-1]) - int(input_ids.shape[-1]))
        except (AttributeError, TypeError, ValueError) as error:
            raise TTSSynthesisError(
                "MOSS Delay did not expose generated token length"
            ) from error

    @staticmethod
    def _to_numpy(value: object) -> AudioArray:
        detach = getattr(value, "detach", None)
        if callable(detach):
            value = detach()
        cpu = getattr(value, "cpu", None)
        if callable(cpu):
            value = cpu()
        numpy = getattr(value, "numpy", None)
        if callable(numpy):
            value = numpy()
        audio = np.asarray(value, dtype=np.float32)
        if audio.ndim == 1:
            audio = audio[:, None]
        elif audio.ndim == 2 and audio.shape[0] <= 2 and audio.shape[1] > 2:
            audio = audio.T
        elif audio.ndim != 2:
            raise TTSSynthesisError("MOSS Delay returned invalid PCM dimensions")
        if not np.isfinite(audio).all():
            raise TTSSynthesisError("MOSS Delay returned non-finite PCM")
        return np.ascontiguousarray(audio, dtype=np.float32)

    def _result(
        self,
        pcm: AudioArray,
        completion: SynthesisCompletion,
        profile: str,
        seed: int | None,
        max_new_tokens: int,
        max_audio_seconds: float,
        started: float,
        first_chunk_ms: float | None,
    ) -> SynthesisResult:
        return SynthesisResult(
            pcm=pcm,
            sample_rate=self.sample_rate,
            completion=completion,
            limits=SynthesisLimits(max_new_tokens, max_audio_seconds),
            timing=SynthesisTiming(first_chunk_ms, (self.clock() - started) * 1000),
            diagnostics=SynthesisDiagnostics(
                backend=self.name,
                cache_source="fresh-generation",
                generation_profile=profile,
                seed=seed,
                chunk_count=1 if pcm.size else 0,
                sample_count=len(pcm),
            ),
        )

    def prime(self, character: str) -> bool:
        self._resolve_voice_source(character)
        return False

    def stop(self) -> bool:
        was_requested = self.stop_requested.is_set()
        self.stop_requested.set()
        return not was_requested

    def set_live_mode_active(self, _active: bool) -> bool:
        return False
