"""Experimental Qwen3-TTS Base voice cloning on Apple Silicon and CUDA."""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Protocol, cast

from vntts.audio_output import AudioOutput
from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
    _MossGeneratedChunk,
    _MossTTSModelFactory,
    moss_generation_limits,
)
from vntts.speech_backend_contract import SpeechBackendCapabilities
from vntts.synthesis import SynthesisRequest
from vntts.voices import CharacterVoiceRegistry, resolve_required_voice_reference

QWEN_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"
QWEN_REVISION = "e7dd0585652209fa0d7783659aad4e8a324de11c"
QWEN_CUDA_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
QWEN_CUDA_REVISION = "5d83992436eae1d760afd27aff78a71d676296fc"


class _CudaQwenModel:
    sample_rate = 24_000

    def __init__(self, model: Any) -> None:
        self.model = model

    def create_voice_clone_prompt(self, **options: Any) -> Any:
        return self.model.create_voice_clone_prompt(**options)

    def generate_voice_clone(self, **options: Any) -> Any:
        return self.model.generate_voice_clone(**options)


def _require_cuda_device() -> None:
    import torch

    if not torch.cuda.is_available():
        raise TTSConfigurationError("Qwen3-TTS requires an NVIDIA CUDA GPU on Windows")
    if torch.cuda.get_device_capability(0) < (7, 5):
        raise TTSConfigurationError(
            "Qwen3-TTS requires NVIDIA Turing (RTX 20 series) or newer"
        )


def _load_cuda_qwen_model(model_path: str) -> _CudaQwenModel:
    import torch
    from qwen_tts import Qwen3TTSModel

    _require_cuda_device()
    return _CudaQwenModel(
        Qwen3TTSModel.from_pretrained(
            model_path,
            device_map="cuda:0",
            dtype=torch.float16,
            attn_implementation="sdpa",
        )
    )


class _QwenGeneratedResult(_MossGeneratedChunk, Protocol):
    token_count: int


class _QwenSpeechTokenizer(Protocol):
    has_encoder: bool


class _QwenTTSModel(Protocol):
    sample_rate: int
    speech_tokenizer: _QwenSpeechTokenizer

    def generate(
        self,
        *,
        text: str,
        ref_audio: str,
        ref_text: str | None,
        lang_code: str,
        temperature: float,
        top_k: int,
        top_p: float,
        repetition_penalty: float,
        max_tokens: int,
        stream: bool,
    ) -> Iterator[_QwenGeneratedResult]: ...


class _QwenTTSModelFactory(Protocol):
    # MLX and CUDA return different provider models. The constructor validates
    # the platform-specific interface after shared backend initialization.
    def __call__(self, model_name: str, *, lazy: bool) -> object: ...


@dataclass
class _QwenGeneratedChunk:
    audio: object
    generation_limited: bool


def _require_qwen_model(model: object) -> _QwenTTSModel:
    tokenizer = getattr(model, "speech_tokenizer", None)
    if (
        not isinstance(getattr(model, "sample_rate", None), int)
        or not callable(getattr(model, "generate", None))
        or not getattr(tokenizer, "has_encoder", False)
    ):
        raise TTSConfigurationError("Qwen Base reference encoder is missing")
    return cast(_QwenTTSModel, model)


def _reference_prompt_codes(value: object) -> tuple[str, str | None]:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and (value[1] is None or isinstance(value[1], str))
    ):
        return value
    raise TTSConfigurationError("Qwen Base reference encoder is missing")


class QwenTTSVoiceRouterBackend(MossTTSVoiceRouterBackend):
    """Reuse the project's bounded renderer and playback with Qwen conditioning."""

    name = "qwen-tts"
    _generation_profiles: ClassVar[Mapping[str, Mapping[str, float]]] = {"stable": {}}
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
        language: object = "English",
        model_name: object = None,
        volume: int | float = 1.0,
        model_factory: _QwenTTSModelFactory | None = None,
        audio_output: AudioOutput | None = None,
        clock: Callable[[], float] = monotonic,
        audio_cache_size: int = 32,
        playback_latency: object = "low",
        runtime_directory: str | Path | None = None,
        prompt_cache_directory: str | Path | None = None,
        persistent_audio_cache_directory: str | Path | None = None,
        persistent_audio_cache_max_entries: int | None = None,
        prompt_code_loader: Callable[[Path], object] | None = None,
        prompt_code_saver: Callable[[object, Path], object] | None = None,
        array_evaluator: Callable[[object], object] | None = None,
        cached_stream_chunk_seconds: float = 0.2,
        streaming_first_chunk_frames: int = 4,
        streaming_interval: float = 0.25,
        generation_profile: object = "stable",
        playback_consumer_join_timeout: float = 5.0,
    ) -> None:
        if sys.platform not in {"darwin", "win32"}:
            raise TTSConfigurationError(
                "Qwen3-TTS requires Apple Silicon or Windows CUDA"
            )
        del model_name, generation_profile  # This backend pins one tested model.
        metal_available = False
        resolved_model_factory: _QwenTTSModelFactory
        if model_factory is None:
            from huggingface_hub import snapshot_download

            if sys.platform == "darwin":
                import mlx.core as mx
                from mlx_audio.tts.utils import load_model

                if not mx.metal.is_available():
                    raise TTSConfigurationError(
                        "Qwen MLX requires an Apple Silicon GPU"
                    )
                metal_available = True
                model_path = snapshot_download(QWEN_MODEL, revision=QWEN_REVISION)

                def load_qwen_model(model_name: str, *, lazy: bool) -> object:
                    return load_model(model_path, lazy=lazy)

                resolved_model_factory = load_qwen_model

            else:
                _require_cuda_device()
                model_path = snapshot_download(
                    QWEN_CUDA_MODEL, revision=QWEN_CUDA_REVISION
                )

                def load_cuda_model(model_name: str, *, lazy: bool) -> _CudaQwenModel:
                    return _load_cuda_qwen_model(model_path)

                resolved_model_factory = load_cuda_model
        else:
            model_path = QWEN_MODEL if sys.platform == "darwin" else QWEN_CUDA_MODEL
            resolved_model_factory = model_factory
        super().__init__(
            registry,
            narrator_reference=narrator_reference,
            language=language,
            model_name=model_path,
            volume=volume,
            # The base constructor only needs common model state before Qwen
            # overrides all MOSS-specific generation and prompt-code hooks.
            model_factory=cast(_MossTTSModelFactory, resolved_model_factory),
            audio_output=audio_output,
            clock=clock,
            audio_cache_size=audio_cache_size,
            playback_latency=playback_latency,
            runtime_directory=runtime_directory,
            prompt_cache_directory=prompt_cache_directory,
            persistent_audio_cache_directory=persistent_audio_cache_directory,
            persistent_audio_cache_max_entries=persistent_audio_cache_max_entries,
            prompt_code_loader=prompt_code_loader,
            prompt_code_saver=prompt_code_saver,
            array_evaluator=array_evaluator,
            cached_stream_chunk_seconds=cached_stream_chunk_seconds,
            streaming_first_chunk_frames=streaming_first_chunk_frames,
            streaming_interval=streaming_interval,
            generation_profile="stable",
            playback_consumer_join_timeout=playback_consumer_join_timeout,
        )
        self.narrator_speaker = "Qwen reference voice"
        self.language = "English"
        self.device = (
            "cuda"
            if sys.platform == "win32"
            else "metal"
            if metal_available
            else "unknown"
        )
        if sys.platform == "win32":
            import torch

            self.torch = torch
        else:
            self._qwen_model = _require_qwen_model(self.model)
            if not self._qwen_model.speech_tokenizer.has_encoder:
                raise TTSConfigurationError("Qwen Base reference encoder is missing")

    def _resolve_prompt_codes(self, character: str) -> tuple[str, object]:
        voice_key, source = self._resolve_voice_source(character)
        voice = self.registry.resolve(character)
        transcript = (
            getattr(voice, "reference_transcript", None) or ""
        ).strip() or None
        if sys.platform == "win32":
            identity = f"{voice_key}:{source}:{transcript}"
            prompt = self.prompt_audio_codes.get(identity)
            if prompt is None:
                prompt = self.model.create_voice_clone_prompt(
                    ref_audio=str(source),
                    ref_text=transcript,
                    x_vector_only_mode=transcript is None,
                )
                self.prompt_audio_codes[identity] = prompt
            return voice_key, prompt
        return voice_key, (str(source), transcript)

    def _resolve_voice_source(self, character: str) -> tuple[str, Path]:
        voice_key, source = resolve_required_voice_reference(
            self.registry,
            character,
            self.narrator_reference,
            backend_name="Qwen3-TTS",
            missing_message="Qwen3-TTS requires a voice reference recording.",
            error_type=TTSConfigurationError,
        )
        return str(voice_key), Path(source)

    def _persistent_cache_key(
        self,
        voice_key: str,
        text: str,
        source: Path,
        *,
        seed: int | None = None,
        generation_profile: str | None = None,
        generation_options: Mapping[str, float] | None = None,
    ) -> str:
        candidates = (
            *self.registry.unique_voices(),
            *(
                voice
                for voice in self.registry.assignments.values()
                if voice is not None
            ),
        )
        transcript = next(
            (
                voice.reference_transcript
                for voice in candidates
                if voice.references
                and Path(voice.references[0]).resolve() == source
                and voice.reference_transcript
            ),
            None,
        )
        transcript = (transcript or "").strip() or None
        max_seconds = moss_generation_limits(text)[1]
        return self.persistent_cache_keys.key(
            voice_key=voice_key,
            source=source,
            text=text,
            speed=self.speed,
            reference_transcript=transcript,
            conditioning_mode="full-reference" if transcript else "speaker-only",
            language="English",
            profile=generation_profile or self.generation_profile,
            seed=seed,
            max_tokens=max(1, math.floor(max_seconds * 12.5)),
            temperature=0.9,
            top_k=50,
            top_p=1.0,
            repetition_penalty=1.5,
        )

    def _prepare_request(self, request: SynthesisRequest) -> MossTTSPreparedSpeech:
        prepared = super()._prepare_request(request)
        return replace(
            prepared,
            max_tokens=max(1, math.floor(prepared.max_audio_seconds * 12.5)),
        )

    def _generate(
        self, prepared: MossTTSPreparedSpeech, request: SynthesisRequest
    ) -> Iterator[_MossGeneratedChunk]:
        if sys.platform == "win32":
            import torch

            if request.seed is not None:
                torch.manual_seed(request.seed)
            with torch.inference_mode():
                wavs, sample_rate = cast(
                    _CudaQwenModel, self.model
                ).generate_voice_clone(
                    text=prepared.text,
                    language="English",
                    voice_clone_prompt=prepared.prompt_audio_codes,
                    max_new_tokens=prepared.max_tokens,
                    temperature=0.9,
                    top_k=50,
                    top_p=1.0,
                    repetition_penalty=1.5,
                )
            if sample_rate != self.sample_rate:
                raise TTSConfigurationError(
                    f"Qwen3-TTS returned unexpected sample rate {sample_rate}"
                )
            yield _QwenGeneratedChunk(audio=wavs[0], generation_limited=False)
            return
        if request.seed is not None:
            import mlx.core as mx

            mx.random.seed(request.seed)
        reference_audio, reference_text = _reference_prompt_codes(
            prepared.prompt_audio_codes
        )
        for result in self._qwen_model.generate(
            text=prepared.text,
            ref_audio=reference_audio,
            ref_text=reference_text,
            lang_code="English",
            temperature=0.9,
            top_k=50,
            top_p=1.0,
            repetition_penalty=1.5,
            max_tokens=prepared.max_tokens,
            stream=False,
        ):
            yield _QwenGeneratedChunk(
                audio=result.audio,
                generation_limited=result.token_count >= prepared.max_tokens,
            )
