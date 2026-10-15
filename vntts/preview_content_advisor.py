"""Optional, checksum-bound ASR advice for already generated voice previews."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from durable_file import atomic_write_json

from vntts.application_directories import get_local_data_directory
from vntts.authoring.advisory_lock import exclusive_advisory_lock
from vntts.authoring.asr_model import (
    WHISPER_TINY_EN,
    install_managed_asr_model,
    managed_asr_status,
    resolve_managed_asr_model,
)
from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256
from vntts.path_safety import open_regular_binary
from vntts.speech_content_advice import (
    SPEECH_CONTENT_ADVICE_VERSION,
    SpeechContentAdvice,
    assess_speech_content,
)


class PreviewContentCheckError(RuntimeError):
    """Content advice could not be established for this exact preview."""


class PreviewContentCheckCancelled(PreviewContentCheckError):
    """The optional content check was cancelled."""


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


class _Preview(Protocol):
    @property
    def identity(self) -> str: ...

    @property
    def candidate_source_id(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def audio_sha256(self) -> str: ...

    @property
    def path(self) -> Path: ...

    @property
    def seed(self) -> int | None: ...


@dataclass(frozen=True)
class PreviewContentCheck:
    source_id: str
    preview_identity: str
    audio_sha256: str
    text: str
    seed: int | None
    advice: SpeechContentAdvice


@dataclass(frozen=True)
class _SavedPreview:
    identity: str
    candidate_source_id: str
    text: str
    audio_sha256: str
    path: Path
    seed: int | None


def _raise_if_cancelled(cancel_event: _Cancellation | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise PreviewContentCheckCancelled("Preview content check cancelled")


def _whisper_transcriber(model_directory: Path) -> Callable[[bytes], str]:
    from vntts.authoring.robustness_asr import _WhisperTranscriber

    return _WhisperTranscriber(model_directory)


class PreviewContentAdvisor:
    """Reuse raw transcripts, loading the optional local checker only in check()."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        transcriber_factory: Callable[[Path], Callable[[bytes], str]] | None = None,
        model_resolver: Callable[[], Path] | None = None,
        model_installer: Callable[[], object] | None = None,
        model_status: Callable[[], object] | None = None,
    ) -> None:
        self.root = (
            Path(
                root or get_local_data_directory() / "pregeneration" / "voice-auditions"
            )
            .expanduser()
            .resolve()
        )
        self._transcriber_factory = transcriber_factory or _whisper_transcriber
        self._model_resolver = model_resolver or resolve_managed_asr_model
        self._model_installer = model_installer or install_managed_asr_model
        self._model_status = model_status or managed_asr_status

    @staticmethod
    def _analyzer() -> dict[str, object]:
        return {
            "schema": "vntts.preview-content-advice",
            "schema_version": 1,
            "advice_version": SPEECH_CONTENT_ADVICE_VERSION,
            "model_id": WHISPER_TINY_EN.model_id,
            "repository": WHISPER_TINY_EN.repository,
            "revision": WHISPER_TINY_EN.revision,
            "tree_sha256": WHISPER_TINY_EN.tree_sha256,
        }

    def _cache_path(self, identity: str) -> Path:
        key = canonical_document_sha256(
            {"analyzer": self._analyzer(), "preview_identity": identity}
        )
        return self.root / "content-advice" / f"{key}.json"

    def _read_cache(self, identity: str) -> dict[str, object] | None:
        try:
            document = self._read_document(self._cache_path(identity), 1024 * 1024)
            if canonical_document_sha256(document.get("analyzer")) != (
                canonical_document_sha256(self._analyzer())
            ):
                return None
        except OSError, UnicodeError, ValueError, TypeError, RecursionError:
            return None
        return document

    @staticmethod
    def _read_document(path: Path, limit: int) -> dict[str, object]:
        with open_regular_binary(path) as source:
            payload = source.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("Preview metadata is too large")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("Preview metadata must be an object")
        return document

    @staticmethod
    def _valid_seed(seed: object) -> bool:
        return seed is None or (type(seed) is int and 0 <= seed < 2**64)

    @classmethod
    def _metadata(cls, path: Path, identity: str) -> dict[str, object]:
        manifest = path.with_suffix(".json")
        if manifest.is_symlink():
            raise ValueError("Preview manifest must not be a symbolic link")
        document = cls._read_document(manifest, 1024)
        if (
            not is_lowercase_sha256(identity)
            or document.get("identity") != identity
            or not is_lowercase_sha256(document.get("audio_sha256"))
            or "seed" not in document
            or not cls._valid_seed(document["seed"])
        ):
            raise ValueError("Preview manifest does not match its input")
        return document

    @classmethod
    def _audio(cls, preview: _Preview) -> bytes:
        if (
            not is_lowercase_sha256(preview.identity)
            or not is_lowercase_sha256(preview.audio_sha256)
            or not isinstance(preview.text, str)
            or not preview.text.strip()
            or not isinstance(preview.candidate_source_id, str)
            or not preview.candidate_source_id.strip()
            or not cls._valid_seed(preview.seed)
        ):
            raise PreviewContentCheckError(
                "Preview identity or generation data is unavailable"
            )
        try:
            metadata = cls._metadata(preview.path, preview.identity)
            if (
                metadata["audio_sha256"] != preview.audio_sha256
                or metadata["seed"] != preview.seed
            ):
                raise ValueError("Preview generation data changed; generate it again")
            with open_regular_binary(preview.path) as source:
                payload = source.read(64 * 1024 * 1024 + 1)
            if len(payload) > 64 * 1024 * 1024:
                raise ValueError("Preview audio is too large")
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
            raise PreviewContentCheckError(
                f"Preview generation data is unavailable: {error}"
            ) from error
        if hashlib.sha256(payload).hexdigest() != preview.audio_sha256:
            raise PreviewContentCheckError("Preview audio changed; generate it again")
        return payload

    def _binding(self, preview: _Preview) -> dict[str, object]:
        return {
            "analyzer": self._analyzer(),
            "preview_identity": preview.identity,
            "source_id": preview.candidate_source_id,
            "text": preview.text,
            "audio_sha256": preview.audio_sha256,
            "seed": preview.seed,
        }

    def lookup(self, preview: _Preview) -> PreviewContentCheck | None:
        """Admit saved evidence against current audio bytes without loading ASR."""
        try:
            self._audio(preview)
        except PreviewContentCheckError:
            return None
        document = self._read_cache(preview.identity)
        if document is None or not self._valid_seed(document.get("seed")):
            return None
        binding = self._binding(preview)
        if any(
            key not in document or document[key] != value
            for key, value in binding.items()
        ):
            return None
        observed = document.get("observed_text")
        if not isinstance(observed, str):
            return None
        return PreviewContentCheck(
            preview.candidate_source_id,
            preview.identity,
            preview.audio_sha256,
            preview.text,
            preview.seed,
            assess_speech_content(preview.text, observed),
        )

    def lookup_saved(
        self, identity: str, text: str, source_id: str
    ) -> PreviewContentCheck | None:
        """Look up an owned preview by identity for cache-only planner advice."""
        if not is_lowercase_sha256(identity):
            return None
        path = self.root / f"{identity}.wav"
        try:
            metadata = self._metadata(path, identity)
        except OSError, UnicodeError, ValueError, TypeError, RecursionError:
            return None
        return self.lookup(
            _SavedPreview(
                identity,
                source_id,
                text,
                cast(str, metadata["audio_sha256"]),
                path,
                cast(int | None, metadata["seed"]),
            )
        )

    def _load_transcriber(self, install_model: bool) -> Callable[[bytes], str]:
        try:
            if install_model:
                status = self._model_status()
                if isinstance(status, dict) and status.get("status") == "missing":
                    self._model_installer()
            model_directory = self._model_resolver()
            return self._transcriber_factory(model_directory)
        except Exception as error:
            raise PreviewContentCheckError(
                "Preview content checker is unavailable. Install the optional "
                f"ASR checker to enable content advice. {error}"
            ) from error

    def check(
        self,
        previews: tuple[_Preview, ...],
        *,
        install_model: bool = False,
        cancel_event: _Cancellation | None = None,
    ) -> tuple[PreviewContentCheck, ...]:
        """Check in a worker, sharing one loaded model for this bounded batch."""
        with exclusive_advisory_lock(
            self.root / "content-advice" / "checker.lock",
            blocking=True,
            check_cancelled=lambda: _raise_if_cancelled(cancel_event),
        ):
            return self._check_batch(previews, install_model, cancel_event)

    def _check_batch(
        self,
        previews: tuple[_Preview, ...],
        install_model: bool,
        cancel_event: _Cancellation | None,
    ) -> tuple[PreviewContentCheck, ...]:
        results: list[PreviewContentCheck] = []
        transcriber: Callable[[bytes], str] | None = None
        try:
            for preview in previews:
                _raise_if_cancelled(cancel_event)
                cache_path = self._cache_path(preview.identity)
                with exclusive_advisory_lock(
                    cache_path.with_suffix(".lock"),
                    blocking=True,
                    check_cancelled=lambda: _raise_if_cancelled(cancel_event),
                ):
                    cached = self.lookup(preview)
                    _raise_if_cancelled(cancel_event)
                    if cached is not None:
                        results.append(cached)
                        continue
                    payload = self._audio(preview)
                    if transcriber is None:
                        transcriber = self._load_transcriber(install_model)
                    _raise_if_cancelled(cancel_event)
                    try:
                        observed = transcriber(payload)
                    except Exception as error:
                        raise PreviewContentCheckError(
                            f"Unable to check preview content: {error}"
                        ) from error
                    _raise_if_cancelled(cancel_event)
                    if not isinstance(observed, str):
                        raise PreviewContentCheckError(
                            "ASR returned no transcript text"
                        )
                    # Re-admit the file after inference; never attach old bytes to new audio.
                    self._audio(preview)
                    result = PreviewContentCheck(
                        preview.candidate_source_id,
                        preview.identity,
                        preview.audio_sha256,
                        preview.text,
                        preview.seed,
                        assess_speech_content(preview.text, observed),
                    )
                    _raise_if_cancelled(cancel_event)
                    atomic_write_json(
                        cache_path,
                        {**self._binding(preview), "observed_text": observed},
                    )
                    results.append(result)
            _raise_if_cancelled(cancel_event)
            return tuple(results)
        finally:
            # The optional checker must not hold model memory after a batch finishes.
            transcriber = None
