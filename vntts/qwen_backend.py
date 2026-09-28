"""Experimental Qwen3-TTS Base voice cloning on Apple Silicon and CUDA."""

from __future__ import annotations

import math
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
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


class QwenTTSVoiceRouterBackend(MossTTSVoiceRouterBackend):
    """Reuse the project's bounded renderer and playback with Qwen conditioning."""

    name = "qwen-tts"
    _generation_profiles = {"stable": {}}
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
        model_factory: Any = None,
        runtime_directory: str | Path | None = None,
        **options: Any,
    ) -> None:
        if sys.platform not in {"darwin", "win32"}:
            raise TTSConfigurationError(
                "Qwen3-TTS requires Apple Silicon or Windows CUDA"
            )
        options.pop(
            "model_name", None
        )  # This experimental backend pins one tested model.
        options["generation_profile"] = "stable"
        metal_available = False
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

                def model_factory(_name: str, *, lazy: bool = False) -> Any:
                    return load_model(model_path, lazy=lazy)

            else:
                _require_cuda_device()
                model_path = snapshot_download(
                    QWEN_CUDA_MODEL, revision=QWEN_CUDA_REVISION
                )

                def model_factory(_name: str, *, lazy: bool = False) -> Any:
                    return _load_cuda_qwen_model(model_path)
        else:
            model_path = QWEN_MODEL if sys.platform == "darwin" else QWEN_CUDA_MODEL
        super().__init__(
            registry,
            model_name=model_path,
            model_factory=model_factory,
            runtime_directory=runtime_directory,
            **options,
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
        if sys.platform == "darwin" and not getattr(
            getattr(self.model, "speech_tokenizer", None), "has_encoder", False
        ):
            raise TTSConfigurationError("Qwen Base reference encoder is missing")

    def _resolve_prompt_codes(self, character: str) -> tuple[str, object]:
        voice_key, source = self._resolve_voice_source(character)
        voice = self.registry.resolve(character)
        transcript = getattr(voice, "reference_transcript", None)
        if not transcript or not transcript.strip():
            raise TTSConfigurationError(
                f"Qwen needs the exact transcript of {voice_key}'s reference audio"
            )
        if sys.platform == "win32":
            identity = f"{voice_key}:{source}:{transcript.strip()}"
            prompt = self.prompt_audio_codes.get(identity)
            if prompt is None:
                prompt = self.model.create_voice_clone_prompt(
                    ref_audio=str(source),
                    ref_text=transcript.strip(),
                    x_vector_only_mode=False,
                )
                self.prompt_audio_codes[identity] = prompt
            return voice_key, prompt
        return voice_key, (str(source), transcript.strip())

    def _resolve_voice_source(self, character: str) -> tuple[str, Path]:
        voice_key, source = resolve_required_voice_reference(
            self.registry,
            character,
            self.narrator_reference,
            backend_name="Qwen3-TTS",
            missing_message="Qwen3-TTS requires a voice reference with an exact transcript.",
            error_type=TTSConfigurationError,
        )
        return str(voice_key), Path(source)

    def _persistent_cache_key(
        self,
        voice_key: str,
        text: str,
        source: str | Path,
        *,
        seed: int | None = None,
        generation_profile: str | None = None,
        generation_options: object = None,
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
                and Path(voice.references[0]).resolve() == Path(source).resolve()
                and voice.reference_transcript
            ),
            None,
        )
        if not transcript or not transcript.strip():
            raise TTSConfigurationError(
                f"Qwen needs the exact transcript of {voice_key}'s reference audio"
            )
        max_seconds = moss_generation_limits(text)[1]
        return self.persistent_cache_keys.key(
            voice_key=voice_key,
            source=source,
            text=text,
            speed=self.speed,
            reference_transcript=transcript.strip(),
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
    ) -> Iterator[Any]:
        if sys.platform == "win32":
            import torch

            if request.seed is not None:
                torch.manual_seed(request.seed)
            with torch.inference_mode():
                wavs, sample_rate = self.model.generate_voice_clone(
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
            yield SimpleNamespace(audio=wavs[0], generation_limited=False)
            return
        if request.seed is not None:
            import mlx.core as mx

            mx.random.seed(request.seed)
        reference_audio, reference_text = cast(
            tuple[str, str], prepared.prompt_audio_codes
        )
        mlx_model: Any = self.model
        for result in mlx_model.generate(
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
            yield SimpleNamespace(
                audio=result.audio,
                generation_limited=result.token_count >= prepared.max_tokens,
            )
