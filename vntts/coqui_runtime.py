import os
from pathlib import Path

from TTS.api import TTS
from TTS.utils.manage import ModelItem

from vntts.coqui_models import COQUI_MODEL_SOURCES
from vntts.model_assets import ModelAssetManager


class ManagedCoquiTTS(TTS):
    """Load the same verified, revision-pinned files as the in-app downloader."""

    def download_model_by_name(
        self, model_name: str, vocoder_name: str | None = None
    ) -> tuple[Path, Path | None, Path | None, Path | None]:
        self.voice_dir = (
            Path(self.manager.output_prefix) / model_name.replace("/", "--") / "voices"
        )
        source = COQUI_MODEL_SOURCES.get(model_name)
        if source is None:
            paths: tuple[Path, Path | None, Path | None, Path | None] = (
                super().download_model_by_name(model_name, vocoder_name)
            )
            return paths
        assets = ModelAssetManager(storage_root=os.environ.get("TTS_HOME"))
        model_path = assets.model_path(model_name)
        assets._check_model_path(model_path)
        model_type, language, dataset, name = model_name.split("/")
        item: ModelItem = self.manager.models_dict[model_type][language][dataset][name]
        if not self.manager.tos_agreed(item, model_path):
            model_path.mkdir(parents=True, exist_ok=True)
            assets._check_model_file(model_path / "tos_agreed.txt", "tos_agreed.txt")
            if not self.manager.ask_tos(model_path):
                raise RuntimeError(
                    "You must agree to the terms of service to use this model."
                )
        model_path = assets.download(model_name)
        checkpoint = model_path / source.checkpoint if source.checkpoint else model_path
        vocoder_path = vocoder_config = None
        if vocoder_name is not None:
            vocoder_path, vocoder_config, _ = self.manager.download_model(vocoder_name)
        return checkpoint, model_path / "config.json", vocoder_path, vocoder_config
