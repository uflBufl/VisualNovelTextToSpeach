"""Resolve MOSS MLX assets inside the isolated model runtime."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from vntts.services.tts_engine import TTSConfigurationError


def codec_source(model_dir: Path, configured: str | None) -> str:
    from mlx_audio.codec.models.moss_audio_tokenizer.moss_audio_tokenizer import (
        _is_audio_tokenizer_dir,
    )
    from mlx_audio.tts.models.moss_tts.config import DEFAULT_AUDIO_TOKENIZER_REPO

    for candidate in (model_dir / "audio_tokenizer", model_dir):
        if _is_audio_tokenizer_dir(candidate):
            return str(candidate)
    return configured or str(DEFAULT_AUDIO_TOKENIZER_REPO)


@contextmanager
def open_asset(
    kind: str, source: str, patterns: list[str], report: Callable[[str], None]
) -> Iterator[Path]:
    from mlx_audio.utils import _is_local_path

    directory = Path(source).expanduser()
    asset = f"MOSS {kind} {source}"
    try:
        if not directory.exists() and not _is_local_path(source):
            from huggingface_hub import snapshot_download
            from huggingface_hub.utils.tqdm import tqdm

            report(f"Resolving {asset}...")
            reported = False
            lock = Lock()

            class DownloadProgress(tqdm):
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    self.byte_progress = kwargs.get("unit") == "B"
                    super().__init__(*args, **kwargs)

                def refresh(self, *args: Any, **kwargs: Any) -> bool | None:
                    nonlocal reported
                    # The Hub assigns a byte total only when a file needs fetching.
                    if self.byte_progress and self.total:
                        with lock:
                            if not reported:
                                report(f"Downloading {asset}...")
                                reported = True
                    refreshed: bool | None = super().refresh(*args, **kwargs)
                    return refreshed

            directory = Path(
                snapshot_download(
                    source, allow_patterns=patterns, tqdm_class=DownloadProgress
                )
            ).resolve()
            asset = f"{asset} from {directory}"
        else:
            directory = directory.resolve(strict=True)
            asset = f"MOSS {kind} {directory}"
        report(f"Loading {asset}")
        yield directory
    except Exception as error:
        raise TTSConfigurationError(f"Could not load {asset}: {error}") from error
