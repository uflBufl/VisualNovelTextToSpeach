"""Compatibility for pre-quantized MOSS Audio Tokenizer checkpoints."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from types import ModuleType
from typing import Protocol


class _Tokenizer(Protocol):
    def load_weights(self, weights: object, *, strict: bool) -> object: ...

    def parameters(self) -> object: ...

    def eval(self) -> object: ...


def install_moss_quantized_codec_compat(
    *,
    codec_module: ModuleType | None = None,
    mx_module: ModuleType | None = None,
    quantization_applier: Callable[[object, object, object], object] | None = None,
) -> bool:
    """Teach mlx-audio 0.4.6 to rebuild quantized codec layers before load.

    Upstream's loader applies checkpoint weights strictly to floating-point
    layers. The int8 MOSS codec stores MLX ``scales`` and ``biases`` tensors,
    so its layer structure must be quantized first. This guarded override can
    be removed once the same handling ships in an official mlx-audio release.
    """
    if codec_module is None:
        from mlx_audio.codec.models.moss_audio_tokenizer import (
            moss_audio_tokenizer as loaded_codec_module,
        )

        codec_module = loaded_codec_module
    if mx_module is None:
        import mlx.core as loaded_mx_module

        mx_module = loaded_mx_module
    if quantization_applier is None:
        from mlx_audio.utils import apply_quantization as loaded_quantization_applier

        quantization_applier = loaded_quantization_applier

    assert codec_module is not None
    assert mx_module is not None
    assert quantization_applier is not None

    tokenizer_class = codec_module.MossAudioTokenizer
    if getattr(tokenizer_class, "_vntts_quantized_load_compat", False):
        return False
    try:
        upstream_source = inspect.getsource(tokenizer_class.from_pretrained)
    except OSError, TypeError:
        upstream_source = ""
    if "apply_quantization" in upstream_source:
        return False

    def from_pretrained(cls: Callable[..., _Tokenizer], source: object) -> _Tokenizer:
        model_dir = codec_module._resolve_audio_tokenizer_dir(source)
        config = codec_module.AudioTokenizerConfig.from_file(model_dir / "config.json")
        weights = codec_module._sanitize_audio_tokenizer_weights(
            codec_module._load_weights_from_dir(model_dir)
        )
        projection_keys = {
            key
            for key in weights
            if key.endswith(".input_proj.weight") or key.endswith(".output_proj.weight")
        }
        model = cls(config, projection_keys=projection_keys)
        raw_config = json.loads((model_dir / "config.json").read_text())
        if raw_config.get("quantization") or raw_config.get("quantization_config"):
            quantization_applier(model, raw_config, weights)
        model.load_weights(list(weights.items()), strict=True)
        mx_module.eval(model.parameters())
        model.eval()
        return model

    tokenizer_class.from_pretrained = classmethod(from_pretrained)
    tokenizer_class._vntts_quantized_load_compat = True
    return True
