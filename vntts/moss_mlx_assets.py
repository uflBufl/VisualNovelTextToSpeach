"""Resolve MOSS MLX assets inside the isolated model runtime."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from vntts.huggingface_assets import download_snapshot
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
            directory = Path(
                download_snapshot(
                    source, asset=asset, report=report, allow_patterns=patterns
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
