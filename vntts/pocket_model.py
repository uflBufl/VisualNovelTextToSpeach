"""One-time original Pocket model setup; reading trusts the admitted local cache."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from vntts.runtime_preparation import check_runtime_cancelled
from vntts.subprocess_utils import terminate_running_process

MODEL_REPO = "kyutai/pocket-tts"
MODEL_REVISION = "39592ff23c9ef80098bb74895d104c26275fe2c9"
PUBLIC_REPO = "kyutai/pocket-tts-without-voice-cloning"
PUBLIC_REVISION = "d29db7978e464fb90cb3359ee0c69a273b9142cc"
VOICE_REVISION = "e81d79e8194ad4c7ce879c87a4258ef20cbf2487"
MODEL_URL = "https://huggingface.co/kyutai/pocket-tts"


def model_assets() -> tuple[tuple[str, str, str], ...]:
    from vntts.voices import pocket_tts_preset_voices

    return (
        (MODEL_REPO, MODEL_REVISION, "languages/english/model.safetensors"),
        (PUBLIC_REPO, PUBLIC_REVISION, "languages/english/tokenizer.model"),
        *(
            (
                PUBLIC_REPO,
                VOICE_REVISION,
                f"languages/english/embeddings/{voice}.safetensors",
            )
            for voice in pocket_tts_preset_voices
        ),
    )


def asset_path(cache: Path, asset: tuple[str, str, str]) -> Path:
    repo, revision, filename = asset
    return (
        cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision / filename
    )


def cache_roots() -> tuple[Path, ...]:
    from vntts.model_assets import ModelAssetManager

    environment = ModelAssetManager().huggingface_environment()
    managed = Path(environment["HF_HOME"]) / "hub"
    standard_home = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface"
    )
    roots = (
        Path(
            environment.get("HF_HUB_CACHE")
            or environment.get("HUGGINGFACE_HUB_CACHE")
            or managed
        ),
        managed,
        standard_home / "hub",
    )
    return tuple(dict.fromkeys(root.expanduser().absolute() for root in roots))


@lru_cache(maxsize=1)
def pocket_model_cache() -> Path | None:
    return next(
        (
            root
            for root in cache_roots()
            if all(asset_path(root, asset).is_file() for asset in model_assets())
        ),
        None,
    )


def pocket_model_ready() -> bool:
    return pocket_model_cache() is not None


def _setup_environment() -> dict[str, str]:
    from vntts.model_assets import ModelAssetManager
    from vntts.path_safety import read_regular_text

    environment = ModelAssetManager().huggingface_environment()
    standard_home = (
        Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface"
    )
    if (
        not environment.get("HF_TOKEN_PATH")
        and not environment.get("HF_TOKEN")
        and not environment.get("HUGGING_FACE_HUB_TOKEN")
        and not (Path(environment["HF_HOME"]) / "token").is_file()
        and (standard_home / "token").is_file()
    ):
        # Reuse the old login without replacing its token during app authorization.
        environment["HF_TOKEN"] = read_regular_text(standard_home / "token").strip()
    environment.pop("HF_HUB_OFFLINE", None)
    environment.pop("HF_HUB_DISABLE_IMPLICIT_TOKEN", None)
    environment["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    environment["HF_ENDPOINT"] = "https://huggingface.co"
    return environment


def _consume_setup_events(
    process, events, cancel_event, continue_event, progress, authorize
) -> bool:
    from vntts.services.tts_engine import TTSConfigurationError

    completed = False
    while True:
        check_runtime_cancelled(cancel_event)
        try:
            line = events.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            break
        event = json.loads(line)
        kind = event["kind"]
        if kind == "progress":
            progress(event["message"])
        elif kind == "authorize":
            authorize(event["url"], event["code"])
        elif kind == "terms":
            continue_event.clear()
            authorize(MODEL_URL, "")
            while not continue_event.wait(0.1):
                check_runtime_cancelled(cancel_event)
            assert process.stdin is not None
            process.stdin.write("continue\n")
            process.stdin.flush()
        elif kind == "error":
            raise TTSConfigurationError(event["message"])
        elif kind == "ready":
            completed = True
    return completed


def _setup_event_reader(process):
    events: queue.Queue[str | None] = queue.Queue()

    def read_events() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                events.put(line)
        except OSError, ValueError:
            pass
        finally:
            events.put(None)

    reader = threading.Thread(
        target=read_events, daemon=True, name="pocket-model-setup"
    )
    reader.start()
    return events, reader


def prepare_pocket_model(
    *,
    cancel_event: threading.Event,
    continue_event: threading.Event,
    progress: Callable[[str], None],
    authorize: Callable[[str, str], None],
) -> None:
    """Run the pinned runtime's HF client without changing the application's environment."""
    from vntts.runtime_installation import ensure_speech_runtime
    from vntts.runtime_ownership import claim_runtime
    from vntts.runtime_paths import get_bundle_root
    from vntts.services.tts_engine import TTSConfigurationError

    check_runtime_cancelled(cancel_event)
    pocket_model_cache.cache_clear()
    if pocket_model_ready():
        progress("Pocket TTS is ready")
        return
    _root, interpreter, _site = ensure_speech_runtime(
        "pocket-tts", cancellation=cancel_event, progress=progress
    )
    environment = _setup_environment()
    bundle = get_bundle_root()
    source = bundle or Path(__file__).resolve().parents[1]
    bootstrap = "import sys;sys.path.insert(0,sys.argv.pop(1));from vntts.pocket_model_worker import main;main()"
    runtime_use = claim_runtime("pocket-tts", _root)
    if runtime_use is not None:
        runtime_use.begin_launch()
    try:
        process = subprocess.Popen(
            [str(interpreter), "-I", "-B", "-u", "-c", bootstrap, str(source)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32"
            else 0,
        )
    except BaseException:
        if runtime_use is not None:
            runtime_use.launched(None)
            runtime_use.close()
        raise
    events, reader = _setup_event_reader(process)
    completed = False
    try:
        if runtime_use is not None:
            runtime_use.launched(process)
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "roots": [str(path) for path in cache_roots()],
                    "assets": model_assets(),
                    "model_url": MODEL_URL,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        completed = _consume_setup_events(
            process, events, cancel_event, continue_event, progress, authorize
        )
        process.wait(timeout=5)
        if process.returncode or not completed:
            raise TTSConfigurationError(
                "Pocket setup did not finish. Retry model setup."
            )
        check_runtime_cancelled(cancel_event)
        pocket_model_cache.cache_clear()
        if not pocket_model_ready():
            raise TTSConfigurationError(
                "Pocket model files are unavailable. Retry model setup."
            )
    finally:
        terminate_running_process(process)
        process.stdin.close()
        process.stdout.close()
        reader.join(timeout=1)
        if runtime_use is not None:
            runtime_use.close()


def configure_pocket_playback_environment(
    environment: dict[str, str], *, packaged: bool
) -> None:
    cache = pocket_model_cache()
    if cache is not None:
        environment["HF_HUB_CACHE"] = str(cache)
        environment["HF_HUB_OFFLINE"] = "1"
    elif packaged:
        for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE"):
            environment.pop(name, None)
    environment["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    environment.pop("HF_TOKEN", None)
    environment.pop("HUGGING_FACE_HUB_TOKEN", None)
