"""HF authorization/download task executed only by explicit model setup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

from huggingface_hub.utils.tqdm import tqdm

from vntts.path_safety import copy_regular_file


def _event(kind: str, **fields: object) -> None:
    print(json.dumps({"kind": kind, **fields}), flush=True)


class _DownloadProgress(tqdm):
    def __init__(self, *args, **kwargs):
        self._description = kwargs.get("desc") or "Downloading original Pocket files"
        self._received = kwargs.get("initial", 0)
        self._reported_at = 0.0
        super().__init__(*args, **kwargs)

    def update(self, amount=1):
        self._received += amount
        now = monotonic()
        if self.total and (
            now - self._reported_at >= 0.5 or self._received >= self.total
        ):
            self._reported_at = now
            percent = min(100, int(self._received * 100 / self.total))
            _event(
                "progress",
                message=f"{self._description}: {percent}%",
            )
        return super().update(amount)


def _asset_path(cache: Path, asset: list[str]) -> Path:
    repo, revision, filename = asset
    return (
        cache / f"models--{repo.replace('/', '--')}" / "snapshots" / revision / filename
    )


def _download_originals(config: dict) -> None:
    roots = [Path(path) for path in config["roots"]]
    target = roots[0]
    assets = config["assets"]
    for number, asset in enumerate(assets, 1):
        repo, revision, filename = asset
        _event(
            "progress",
            message=f"Preparing Pocket model files ({number}/{len(assets)}): {Path(filename).name}",
        )
        destination = _asset_path(target, asset)
        if destination.is_file():
            continue
        previous = next(
            (
                _asset_path(root, asset)
                for root in roots[1:]
                if _asset_path(root, asset).is_file()
            ),
            None,
        )
        if previous is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{uuid4().hex}.copying"
            )
            try:
                copy_regular_file(previous, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            continue
        _download_asset(repo, revision, filename, target, config["model_url"])


def _download_asset(repo, revision, filename, target, model_url) -> None:
    from huggingface_hub import get_token, hf_hub_download
    from huggingface_hub.errors import DeviceCodeError, HfHubHTTPError

    authenticated = False
    confirmed = False
    while True:
        try:
            hf_hub_download(
                repo_id=repo,
                filename=filename,
                revision=revision,
                cache_dir=target,
                tqdm_class=_DownloadProgress,
            )
            break
        except DeviceCodeError:
            if authenticated:
                raise
            _authorize()
            authenticated = True
        except HfHubHTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if repo != "kyutai/pocket-tts" or status not in (401, 403):
                raise
            if status == 401 or not get_token():
                if authenticated:
                    raise
                _authorize()
                authenticated = True
            else:
                if confirmed:
                    raise
                _event("terms", url=model_url)
                if not sys.stdin.readline():
                    raise RuntimeError("Pocket model setup cancelled") from error
                confirmed = True


def _authorize() -> None:
    from huggingface_hub._login import _save_oauth_token
    from huggingface_hub.utils._oauth_device import (
        poll_device_token,
        request_device_code,
    )

    info = request_device_code()
    _event("authorize", url=info["verification_uri_complete"], code=info["user_code"])
    _save_oauth_token(poll_device_token(info))
    # The new browser login supersedes an expired inherited token in this child.
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)


def main() -> None:
    try:
        _download_originals(json.loads(sys.stdin.readline()))
    except Exception as error:
        # SDK exceptions can contain authentication response data; never export it.
        from huggingface_hub.errors import (
            DeviceCodeError,
            GatedRepoError,
            HfHubHTTPError,
        )

        if isinstance(error, GatedRepoError):
            message = "Access to the original Pocket model is not available. Confirm its terms in Hugging Face, then retry setup."
        elif isinstance(error, HfHubHTTPError):
            status = (
                error.response.status_code if error.response is not None else "unknown"
            )
            message = f"Pocket model download failed (HTTP {status}). Check the connection and model access, then retry."
        elif isinstance(error, DeviceCodeError):
            reason = getattr(error, "error_code", None)
            if reason == "access_denied":
                message = "Hugging Face sign-in was declined. Close this dialog and retry when ready."
            elif reason == "expired_token":
                message = "The Hugging Face sign-in code expired. Close this dialog and retry to get a new code."
            else:
                message = "Unable to complete Hugging Face sign-in. Check your connection and retry browser authorization."
        elif isinstance(error, OSError):
            message = f"Unable to store Pocket model files (OS error {error.errno}). Check free disk space and file permissions, then retry."
        else:
            message = f"Pocket setup failed ({type(error).__name__}). Check your connection or retry browser authorization."
        _event("error", message=message)
        raise SystemExit(1) from None
    _event("ready")
