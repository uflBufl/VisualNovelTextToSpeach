"""Experimental Qwen3-TTS Base voice cloning on Apple Silicon."""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Protocol, runtime_checkable

from vntts.audio_output import AudioOutput
from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    MossTTSPreparedSpeech,
    MossTTSVoiceRouterBackend,
    _MossGeneratedChunk,
    _MossTTSModel,
    _MossTTSModelFactory,
    moss_generation_limits,
)
from vntts.speech_backend_contract import SpeechBackendCapabilities
from vntts.synthesis import SynthesisRequest
from vntts.voices import CharacterVoiceRegistry, resolve_required_voice_reference

QWEN_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"
QWEN_REVISION = "e7dd0585652209fa0d7783659aad4e8a324de11c"


class _QwenGeneratedResult(Protocol):
    audio: object
    token_count: int


@runtime_checkable
class _QwenSpeechTokenizer(Protocol):
    has_encoder: bool


@runtime_checkable
class _QwenTTSModel(Protocol):
    sample_rate: int
    speech_tokenizer: _QwenSpeechTokenizer

    def generate(
        self,
        *,
        text: str,
        ref_audio: str,
        ref_text: str,
        lang_code: str,
        temperature: float,
        top_k: int,
        top_p: float,
        repetition_penalty: float,
        max_tokens: int,
        stream: bool,
    ) -> Iterator[_QwenGeneratedResult]: ...


@dataclass
class _QwenGeneratedChunk:
    audio: object
    generation_limited: object


def _require_qwen_model(model: object) -> _QwenTTSModel:
    if not isinstance(model, _QwenTTSModel):
        raise TTSConfigurationError("Qwen Base reference encoder is missing")
    return model


def _reference_prompt_codes(value: object) -> tuple[str, str]:
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(isinstance(part, str) for part in value)
    ):
        return value
    raise TTSConfigurationError("Qwen Base reference encoder is missing")


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
        narrator_reference: str | Path | None = None,
        language: object = "English",
        model_name: object = None,
        volume: int | float = 1.0,
        model_factory: _MossTTSModelFactory | None = None,
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
        if sys.platform != "darwin":
            raise TTSConfigurationError("Qwen MLX requires macOS on Apple Silicon")
        del model_name, generation_profile  # This backend pins one tested model.
        metal_available = False
        resolved_model_factory: _MossTTSModelFactory
        if model_factory is None:
            import mlx.core as mx
            from huggingface_hub import snapshot_download
            from mlx_audio.tts.utils import load_model

            if not mx.metal.is_available():
                raise TTSConfigurationError("Qwen MLX requires an Apple Silicon GPU")
            metal_available = True
            model_path = snapshot_download(QWEN_MODEL, revision=QWEN_REVISION)

            def load_qwen_model(model_name: str, *, lazy: bool) -> _MossTTSModel:
                return load_model(model_path, lazy=lazy)

            resolved_model_factory = load_qwen_model
        else:
            model_path = QWEN_MODEL
            resolved_model_factory = model_factory
        super().__init__(
            registry,
            narrator_reference=narrator_reference,
            language=language,
            model_name=model_path,
            volume=volume,
            model_factory=resolved_model_factory,
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
        self.device = "metal" if metal_available else "unknown"
        self._qwen_model = _require_qwen_model(self.model)
        if not self._qwen_model.speech_tokenizer.has_encoder:
            raise TTSConfigurationError("Qwen Base reference encoder is missing")

    def _resolve_prompt_codes(self, character: str) -> tuple[str, object]:
        voice_key, source = self._resolve_voice_source(character)
        voice = self.registry.resolve(character)
        transcript = getattr(voice, "reference_transcript", None)
        if not transcript or not transcript.strip():
            raise TTSConfigurationError(
                f"Qwen needs the exact transcript of {voice_key}'s reference audio"
            )
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
    ) -> Iterator[_MossGeneratedChunk]:
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
