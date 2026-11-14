import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.test_assets import MemoryOpener
from vntts.coqui_models import COQUI_MODEL_SOURCES
from vntts.model_assets import ModelAssetManager, load_coqui_model_asset


class CoquiRuntimeTest(unittest.TestCase):
    def test_real_coqui_constructor_loads_all_verified_models_offline(self):
        from vntts.coqui_runtime import ManagedCoquiTTS

        for name, source in COQUI_MODEL_SOURCES.items():
            with self.subTest(model=name), TemporaryDirectory() as directory:
                asset = load_coqui_model_asset(name)
                manager = ModelAssetManager(
                    directory,
                    opener=MemoryOpener({url: url.encode() for url in asset.urls}),
                )
                path = manager.download(name)
                originals = {p.name: p.read_bytes() for p in path.iterdir()}
                manager.opener = Mock(side_effect=AssertionError("Unexpected download"))
                with (
                    patch.dict(
                        os.environ, {"TTS_HOME": directory, "COQUI_TOS_AGREED": "1"}
                    ),
                    patch(
                        "vntts.coqui_runtime.ModelAssetManager", return_value=manager
                    ),
                    patch("TTS.api.Synthesizer") as synthesizer,
                ):
                    tts = ManagedCoquiTTS(model_name=name)
                arguments = synthesizer.call_args.kwargs
                key = (
                    "vc_checkpoint"
                    if name.startswith("voice_conversion_models/")
                    else "tts_checkpoint"
                )
                expected = path / source.checkpoint if source.checkpoint else path
                self.assertEqual(arguments[key], expected)
                voices = manager.coqui_cache_root / name.replace("/", "--") / "voices"
                self.assertEqual(arguments["voice_dir"], voices)
                self.assertEqual(tts.voice_dir, voices)
                self.assertEqual(tts.model_name, name)
                manager.opener.assert_not_called()
                self.assertEqual(
                    {p.name: p.read_bytes() for p in path.iterdir()}, originals
                )

    def test_tampered_offline_checkpoint_never_reaches_synthesizer(self):
        from vntts.coqui_runtime import ManagedCoquiTTS

        name = "tts_models/multilingual/multi-dataset/xtts_v2"
        asset = load_coqui_model_asset(name)
        with TemporaryDirectory() as directory:
            manager = ModelAssetManager(
                directory, opener=MemoryOpener({url: b"original" for url in asset.urls})
            )
            path = manager.download(name)
            (path / "model.pth").write_bytes(b"tampered")
            manager.opener = Mock(side_effect=OSError("offline"))
            with (
                patch.dict(
                    os.environ, {"TTS_HOME": directory, "COQUI_TOS_AGREED": "1"}
                ),
                patch("vntts.coqui_runtime.ModelAssetManager", return_value=manager),
                patch("TTS.api.Synthesizer") as synthesizer,
                self.assertRaisesRegex(OSError, "offline"),
            ):
                ManagedCoquiTTS(model_name=name)
            synthesizer.assert_not_called()

    def test_constructor_respects_configured_storage_and_terms(self):
        from vntts.coqui_runtime import ManagedCoquiTTS

        name = "tts_models/multilingual/multi-dataset/xtts_v2"
        with TemporaryDirectory() as directory:
            manager = ModelAssetManager(directory)
            with (
                patch.dict(
                    os.environ, {"TTS_HOME": directory, "COQUI_TOS_AGREED": "0"}
                ),
                patch.object(
                    ModelAssetManager, "download", return_value=manager.model_path(name)
                ) as download,
                patch(
                    "vntts.coqui_runtime.ModelAssetManager", wraps=ModelAssetManager
                ) as factory,
                patch("TTS.api.ModelManager.ask_tos", return_value=False),
                patch("TTS.api.Synthesizer") as synthesizer,
                self.assertRaisesRegex(RuntimeError, "terms of service"),
            ):
                ManagedCoquiTTS(model_name=name)
            synthesizer.assert_not_called()
            factory.assert_called_once_with(storage_root=directory)
            download.assert_not_called()

    def test_unmanaged_model_retains_legacy_voices_and_upstream_vocoder(self):
        from vntts.coqui_runtime import ManagedCoquiTTS

        name = "tts_models/en/vctk/vits"
        with TemporaryDirectory() as directory:
            path = Path(directory)
            with (
                patch.dict(os.environ, {"TTS_HOME": directory}),
                patch("TTS.api.ModelManager.download_model") as download,
                patch("TTS.api.Synthesizer") as synthesizer,
            ):
                download.side_effect = [
                    (
                        path / "model.pth",
                        path / "config.json",
                        {
                            "default_vocoder": "default-vocoder",
                            "model_type": "tts_models",
                        },
                    ),
                    (path / "vocoder.pth", path / "vocoder.json", {}),
                ]
                tts = ManagedCoquiTTS(model_name=name, vocoder_name="custom-vocoder")
            self.assertEqual(download.call_args_list[0].args, (name,))
            self.assertEqual(download.call_args_list[1].args, ("custom-vocoder",))
            self.assertEqual(
                synthesizer.call_args.kwargs["vocoder_checkpoint"], path / "vocoder.pth"
            )
            self.assertEqual(
                tts.voice_dir,
                path.resolve() / "tts" / name.replace("/", "--") / "voices",
            )
