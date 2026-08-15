import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vntts.moss_compat import install_moss_quantized_codec_compat


class FakeTokenizerConfig:
    @classmethod
    def from_file(cls, path):
        return {"path": path}


class FakeTokenizer:
    @classmethod
    def from_pretrained(cls, _source):
        raise AssertionError("The original loader should be replaced")

    def __init__(self, config, *, projection_keys):
        self.config = config
        self.projection_keys = projection_keys
        self.loaded_weights = None
        self.evaluated = False

    def load_weights(self, weights, *, strict):
        self.loaded_weights = (weights, strict)

    def parameters(self):
        return {"weight": 1}

    def eval(self):
        self.evaluated = True


class MossCompatibilityTest(unittest.TestCase):
    def test_quantizes_codec_structure_before_strict_weight_load(self):
        with TemporaryDirectory() as temporary_directory:
            model_directory = Path(temporary_directory)
            (model_directory / "config.json").write_text(
                json.dumps({"quantization": {"group_size": 64, "bits": 8}}),
                encoding="utf-8",
            )
            weights = {
                "encoder.0.input_proj.weight": "weight",
                "encoder.0.input_proj.scales": "scales",
            }
            codec_module = SimpleNamespace(
                MossAudioTokenizer=FakeTokenizer,
                AudioTokenizerConfig=FakeTokenizerConfig,
                _resolve_audio_tokenizer_dir=lambda source: Path(source),
                _load_weights_from_dir=lambda _path: weights,
                _sanitize_audio_tokenizer_weights=lambda value: value,
            )
            mx_module = SimpleNamespace(eval=Mock())
            quantization_applier = Mock()

            installed = install_moss_quantized_codec_compat(
                codec_module=codec_module,
                mx_module=mx_module,
                quantization_applier=quantization_applier,
            )
            model = FakeTokenizer.from_pretrained(model_directory)

            self.assertTrue(installed)
            quantization_applier.assert_called_once_with(
                model,
                {"quantization": {"group_size": 64, "bits": 8}},
                weights,
            )
            self.assertEqual(
                model.projection_keys,
                {"encoder.0.input_proj.weight"},
            )
            self.assertEqual(model.loaded_weights, (list(weights.items()), True))
            mx_module.eval.assert_called_once_with({"weight": 1})
            self.assertTrue(model.evaluated)
            self.assertFalse(
                install_moss_quantized_codec_compat(
                    codec_module=codec_module,
                    mx_module=mx_module,
                    quantization_applier=quantization_applier,
                )
            )

    def test_leaves_native_upstream_quantization_support_untouched(self):
        original_loader = FakeTokenizer.__dict__["from_pretrained"]
        codec_module = SimpleNamespace(MossAudioTokenizer=FakeTokenizer)

        with patch(
            "vntts.moss_compat.inspect.getsource",
            return_value="apply_quantization(model, config, weights)",
        ):
            installed = install_moss_quantized_codec_compat(
                codec_module=codec_module,
                mx_module=SimpleNamespace(),
                quantization_applier=Mock(),
            )

        self.assertFalse(installed)
        self.assertIs(FakeTokenizer.__dict__["from_pretrained"], original_loader)


if __name__ == "__main__":
    unittest.main()
