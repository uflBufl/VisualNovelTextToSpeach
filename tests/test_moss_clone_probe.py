import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from scripts import moss_clone_probe
from tests.authoring_fixtures import tree_hashes


def probe_arguments(root, output, reference, cached):
    return [
        "moss_clone_probe",
        "--model",
        str(root / "model"),
        "--reference",
        str(reference),
        "--reference-text",
        "Original reference",
        "--text",
        "Probe sentence",
        "--output",
        str(output),
        "--cached-prompt",
        str(cached),
    ]


class MossCloneProbeTest(unittest.TestCase):
    def test_existing_output_refused_before_native_work_without_changing_inputs(self):
        for collision in ("empty", "cached-prompt", "roundtrip", "generated"):
            with self.subTest(collision=collision), TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                output.mkdir()
                reference = root / "reference.wav"
                cached = root / "cached-prompt.safetensors"
                if collision == "cached-prompt":
                    cached = output / "fresh-prompt-codes.safetensors"
                elif collision == "roundtrip":
                    reference = output / "00-reference-codec-roundtrip.wav"
                elif collision == "generated":
                    reference = output / "01-current-streaming-temp-1.7.wav"
                reference.write_bytes(b"original reference")
                cached.write_bytes(b"original cached prompt")
                before = tree_hashes(root)
                with (
                    patch.object(
                        sys, "argv", probe_arguments(root, output, reference, cached)
                    ),
                    patch.dict(
                        sys.modules,
                        {
                            "mlx": None,
                            "mlx.core": None,
                            "mlx_audio": None,
                            "mlx_audio.audio_io": None,
                            "mlx_audio.tts": None,
                        },
                    ),
                    patch.object(
                        moss_clone_probe, "install_moss_quantized_codec_compat"
                    ) as compat,
                    self.assertRaises(FileExistsError),
                ):
                    moss_clone_probe.main()
                compat.assert_not_called()
                self.assertEqual(tree_hashes(root), before)

    def test_fresh_output_preserves_inputs_and_writes_cloning_matrix(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "nested" / "output"
            reference = root / "reference.wav"
            reference.write_bytes(b"original reference")
            cached = root / "cached-prompt.safetensors"
            cached.write_bytes(b"original cached prompt")
            codes = np.array([[1, 2]], dtype=np.int32)
            audio = np.array([0.1, 0.2], dtype=np.float32)
            model = SimpleNamespace(
                sample_rate=16_000,
                encode_reference_audio=Mock(return_value=codes),
                decode_audio_token_ids=Mock(return_value=audio),
                generate=Mock(return_value=[SimpleNamespace(audio=audio)]),
            )
            core = SimpleNamespace(
                eval=Mock(),
                load=Mock(return_value={"prompt_audio_codes": codes}),
                save_safetensors=Mock(
                    side_effect=lambda path, values: Path(path).write_bytes(
                        b"fresh codes"
                    )
                ),
                random=SimpleNamespace(seed=Mock()),
            )
            load = Mock(return_value=model)
            write_audio = Mock(
                side_effect=lambda path, samples, rate: Path(path).write_bytes(b"audio")
            )
            with (
                patch.object(
                    sys, "argv", probe_arguments(root, output, reference, cached)
                ),
                patch.dict(
                    sys.modules,
                    {
                        "mlx": SimpleNamespace(core=core),
                        "mlx.core": core,
                        "mlx_audio": SimpleNamespace(),
                        "mlx_audio.audio_io": SimpleNamespace(write=write_audio),
                        "mlx_audio.tts": SimpleNamespace(load=load),
                    },
                ),
                patch.object(
                    moss_clone_probe, "install_moss_quantized_codec_compat"
                ) as compat,
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(moss_clone_probe.main(), 0)
            compat.assert_called_once()
            load.assert_called_once_with(str((root / "model").resolve()), lazy=False)
            model.encode_reference_audio.assert_called_once_with(
                str(reference.resolve())
            )
            core.load.assert_called_once_with(str(cached.resolve()))
            self.assertEqual(model.generate.call_count, 4)
            self.assertEqual(write_audio.call_count, 5)
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["reference"], str(reference.resolve()))
            self.assertEqual(report["prompt_code_shape"], [1, 2])
            self.assertEqual(
                report["cached_prompt_comparison"]["matching_fraction"], 1.0
            )
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "fresh-prompt-codes.safetensors",
                    "00-reference-codec-roundtrip.wav",
                    *(variant["name"] for variant in report["variants"]),
                    "report.json",
                },
            )
            self.assertEqual(reference.read_bytes(), b"original reference")
            self.assertEqual(cached.read_bytes(), b"original cached prompt")


if __name__ == "__main__":
    unittest.main()
