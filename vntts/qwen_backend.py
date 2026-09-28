"""Experimental Qwen3-TTS Base voice cloning on Apple Silicon."""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from vntts.services.tts_engine import TTSConfigurationError
from vntts.speech_backend import (
    MossTTSVoiceRouterBackend,
    moss_generation_limits,
)
from vntts.speech_backend_contract import SpeechBackendCapabilities
from vntts.voices import CharacterVoiceRegistry, resolve_required_voice_reference

QWEN_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"
QWEN_REVISION = "e7dd0585652209fa0d7783659aad4e8a324de11c"


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
        model_factory=None,
        runtime_directory=None,
        **options,
    ) -> None:
        if sys.platform != "darwin":
            raise TTSConfigurationError("Qwen MLX requires macOS on Apple Silicon")
        options.pop(
            "model_name", None
        )  # This experimental backend pins one tested model.
        options["generation_profile"] = "stable"
        metal_available = False
        if model_factory is None:
            import mlx.core as mx
            from huggingface_hub import snapshot_download
            from mlx_audio.tts.utils import load_model

            if not mx.metal.is_available():
                raise TTSConfigurationError("Qwen MLX requires an Apple Silicon GPU")
            metal_available = True
            model_path = snapshot_download(QWEN_MODEL, revision=QWEN_REVISION)

            def model_factory(_name, *, lazy=False):
                return load_model(model_path, lazy=lazy)
        else:
            model_path = QWEN_MODEL
        super().__init__(
            registry,
            model_name=model_path,
            model_factory=model_factory,
            runtime_directory=runtime_directory,
            **options,
        )
        self.narrator_speaker = "Qwen reference voice"
        self.language = "English"
        self.device = "metal" if metal_available else "unknown"
        if not self.model.speech_tokenizer.has_encoder:
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

    def _resolve_voice_source(self, character):
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
        voice_key,
        text,
        source,
        *,
        seed=None,
        generation_profile=None,
        generation_options=None,
    ):
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

    def _prepare_request(self, request):
        prepared = super()._prepare_request(request)
        return replace(
            prepared,
            max_tokens=max(1, math.floor(prepared.max_audio_seconds * 12.5)),
        )

    def _generate(self, prepared, request):
        if request.seed is not None:
            import mlx.core as mx

            mx.random.seed(request.seed)
        reference_audio, reference_text = prepared.prompt_audio_codes
        for result in self.model.generate(
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
