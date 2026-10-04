"""Ephemeral generated previews for blinded failed-reference audits."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict

from durable_file import sha256_file
from vntts_artifacts.audio import Pcm16MonoWavError, write_pcm16_wav

from vntts.authoring.bulk_generation import (
    generated_mono_pcm,
    normalize_short_trailing_ellipsis,
)
from vntts.authoring.failure_reference_audit import (
    FailureReferenceAudio,
    FailureReferenceAudit,
    _load_validated_audit,
    _prepare_failure_reference_audio,
    load_failure_reference_audit,
    prepare_failure_reference_audio,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    load_workspace_authority,
)
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisChunkStream,
    SynthesisCompletion,
    SynthesisRequest,
    SynthesisResult,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class FailureReferencePreviewError(RuntimeError):
    """A generated reference preview was unsafe, incomplete or unavailable."""


class FailureReferencePreviewCancelled(FailureReferencePreviewError):
    """The operator cancelled the current preview generation."""


class FailureReferencePreviewIncomplete(FailureReferencePreviewError):
    """The typed renderer ended without a publishable complete result."""


JsonDocument: TypeAlias = dict[str, object]


class _PreviewCase(TypedDict):
    text: str


class _PreviewCandidate(TypedDict):
    candidate_id: str
    sha256: str


class _PreviewGroup(TypedDict):
    group_id: str
    synthesis_voice_character: str
    cases: list[_PreviewCase]
    candidates: list[_PreviewCandidate]


def _document(value: object, message: str) -> JsonDocument:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FailureReferencePreviewError(message)
    return {key: item for key, item in value.items()}


def _documents(value: object, message: str) -> list[JsonDocument]:
    if not isinstance(value, list):
        raise FailureReferencePreviewError(message)
    return [_document(item, message) for item in value]


def _preview_groups(value: object) -> list[_PreviewGroup]:
    groups: list[_PreviewGroup] = []
    for raw_group in _documents(value, "Reference audit group is malformed"):
        cases: list[_PreviewCase] = [
            _PreviewCase(text=_required_text(case.get("text"), "Preview text"))
            for case in _documents(
                raw_group.get("cases"), "Reference audit group is malformed"
            )
        ]
        candidates: list[_PreviewCandidate] = [
            _PreviewCandidate(
                candidate_id=_required_text(
                    candidate.get("candidate_id"), "Reference audit candidate"
                ),
                sha256=_required_text(
                    candidate.get("sha256"), "Reference audit candidate"
                ),
            )
            for candidate in _documents(
                raw_group.get("candidates"), "Reference audit group is malformed"
            )
        ]
        groups.append(
            {
                "group_id": _required_text(
                    raw_group.get("group_id"), "Reference audit group"
                ),
                "synthesis_voice_character": _required_text(
                    raw_group.get("synthesis_voice_character"),
                    "Preview synthesis voice character",
                ),
                "cases": cases,
                "candidates": candidates,
            }
        )
    return groups


class _PreviewBackend(Protocol):
    registry: CharacterVoiceRegistry

    def render(self, request: SynthesisRequest) -> SynthesisChunkStream: ...


class _PreviewBackendFactory(Protocol):
    def __call__(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        cache_root: Path,
        *,
        model_name: str | None = None,
        startup_cancellation: threading.Event | None = None,
    ) -> _PreviewBackend: ...


@dataclass(frozen=True)
class FailureReferencePreview:
    group_id: str
    candidate_id: str
    text: str
    synthesis_text: str
    text_sha256: str
    backend: str
    model: str
    generation_profile: str
    seed: int
    sample_rate: int
    audio_sha256: str
    payload: bytes
    candidate_group_id: str | None = None


@dataclass(frozen=True)
class _PreviewControls:
    backend: str
    model: str
    generation_profile: str


@dataclass(frozen=True)
class _PreviewKey:
    audit_id: str
    group_id: str
    candidate_group_id: str
    candidate_id: str
    candidate_sha256: str
    text: str
    controls: _PreviewControls


@dataclass(frozen=True)
class _PreviewPlan:
    audit: FailureReferenceAudit
    document: JsonDocument
    key: _PreviewKey
    synthesis_text: str


class FailureReferencePreviewService:
    """Own one lazy backend and memory-only preview cache for a dialog lifetime."""

    def __init__(
        self,
        audit_directory: str | Path,
        *,
        backend_factory: _PreviewBackendFactory = create_backend,
    ) -> None:
        self.audit_directory = Path(audit_directory).expanduser().resolve()
        self.backend_factory = backend_factory
        self._root = Path(tempfile.mkdtemp(prefix="vntts-reference-preview-")).resolve()
        self._backend: _PreviewBackend | None = None
        self._backend_config: _PreviewControls | None = None
        self._cache: dict[_PreviewKey, FailureReferencePreview] = {}
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._closed = False

    def generate(
        self,
        group_id: str,
        candidate_id: str,
        text: str,
        *,
        candidate_group_id: str | None = None,
    ) -> FailureReferencePreview:
        """Generate or return one exact in-memory preview without authoring writes."""
        text = str(text)
        if not text.strip():
            raise FailureReferencePreviewError("Preview text must not be blank")
        with self._lock:
            if self._closed:
                raise FailureReferencePreviewError("Preview service is closed")
            self._cancel.clear()
            plan = self._preview_plan(group_id, candidate_id, text, candidate_group_id)
            cached = self._cache.get(plan.key)
            if cached is not None:
                return cached
            source = _prepare_failure_reference_audio(
                plan.audit, plan.document, plan.key.candidate_group_id, candidate_id
            )
            preview = self._render_preview(plan, source)
            self._cache[plan.key] = preview
            return preview

    def _preview_plan(
        self,
        group_id: str,
        candidate_id: str,
        text: str,
        candidate_group_id: str | None,
    ) -> _PreviewPlan:
        audit, document = _load_validated_audit(self.audit_directory)
        groups = {
            group["group_id"]: group
            for group in _preview_groups(document.get("groups"))
        }
        group = _preview_group(groups, group_id)
        source_group_id = candidate_group_id or group_id
        source_group = _preview_group(groups, source_group_id)
        if source_group_id != group_id and _reference_family(
            source_group
        ) != _reference_family(group):
            raise FailureReferencePreviewError(
                "Cross-group preview candidates must belong to the same exact "
                "source-reference character family"
            )
        if text not in {value["text"] for value in group["cases"]}:
            raise FailureReferencePreviewError(
                "Preview text is not an affected line in this reference group"
            )
        candidate = next(
            (
                value
                for value in source_group["candidates"]
                if value["candidate_id"] == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise FailureReferencePreviewError(
                f"Reference candidate is unknown: {candidate_id}"
            )
        _directory, workspace = self._load_workspace(document)
        run_config = _document(
            workspace.get("run_config"), "Preview workspace run configuration"
        )
        controls = _PreviewControls(
            backend=_required_text(run_config.get("backend"), "Preview backend"),
            model=_required_text(run_config.get("model"), "Preview model"),
            generation_profile=_required_text(
                run_config.get("generation_profile"), "Preview generation profile"
            ),
        )
        key = _PreviewKey(
            audit_id=audit.audit_id,
            group_id=group_id,
            candidate_group_id=source_group_id,
            candidate_id=candidate_id,
            candidate_sha256=candidate["sha256"],
            text=text,
            controls=controls,
        )
        synthesis_text = (
            normalize_short_trailing_ellipsis(text)
            if controls.backend == "moss-tts"
            else text
        )
        return _PreviewPlan(audit, document, key, synthesis_text)

    def _preview_backend(
        self, source: FailureReferenceAudio, controls: _PreviewControls, voice: str
    ) -> _PreviewBackend:
        reference = self._copy_reference(source)
        registry = CharacterVoiceRegistry(
            (
                CharacterVoice(
                    character=voice,
                    speaker=f"reference-preview:{source.sha256}",
                    references=(reference,),
                ),
            )
        )
        if self._backend is None or self._backend_config != controls:
            shutdown_speech_backend(self._backend)
            self._backend = None
            self._backend_config = None
            self._backend = self.backend_factory(
                controls.backend,
                registry,
                self._root / "cache",
                model_name=controls.model,
                startup_cancellation=self._cancel,
            )
            self._backend_config = controls
        else:
            self._backend.registry = registry
        return self._backend

    def _render_preview(
        self, plan: _PreviewPlan, source: FailureReferenceAudio
    ) -> FailureReferencePreview:
        key = plan.key
        controls = key.controls
        request = SynthesisRequest(
            voice=f"Reference candidate {source.sha256[:16]}",
            text=plan.synthesis_text,
            seed=0,
            generation_profile=controls.generation_profile,
            cancellation=self._cancel,
            cache_policy=SynthesisCachePolicy.BYPASS,
        )
        backend = self._preview_backend(source, controls, request.voice)
        result = backend.render(request).collect()
        payload = self._preview_payload(result, controls)
        final_source = prepare_failure_reference_audio(
            plan.audit.directory, key.candidate_group_id, key.candidate_id
        )
        if final_source.sha256 != source.sha256:
            raise FailureReferencePreviewError(
                "Reference candidate changed while its preview was generated"
            )
        final_audit = load_failure_reference_audit(plan.audit.directory)
        if final_audit.audit_id != plan.audit.audit_id:
            raise FailureReferencePreviewError(
                "Reference audit changed while its preview was generated"
            )
        return FailureReferencePreview(
            group_id=key.group_id,
            candidate_group_id=key.candidate_group_id,
            candidate_id=key.candidate_id,
            text=key.text,
            synthesis_text=plan.synthesis_text,
            text_sha256=hashlib.sha256(key.text.encode("utf-8")).hexdigest(),
            backend=controls.backend,
            model=controls.model,
            generation_profile=controls.generation_profile,
            seed=0,
            sample_rate=int(result.sample_rate),
            audio_sha256=hashlib.sha256(payload).hexdigest(),
            payload=payload,
        )

    def _preview_payload(
        self, result: SynthesisResult, controls: _PreviewControls
    ) -> bytes:
        if self._cancel.is_set() or result.completion is SynthesisCompletion.CANCELLED:
            raise FailureReferencePreviewCancelled("Preview generation was cancelled")
        if result.completion is not SynthesisCompletion.COMPLETE:
            raise FailureReferencePreviewIncomplete(
                "Preview generation did not complete within its typed limits"
            )
        diagnostics = result.diagnostics
        if (
            diagnostics.backend != controls.backend
            or diagnostics.generation_profile != controls.generation_profile
            or diagnostics.seed != 0
        ):
            raise FailureReferencePreviewError(
                "Preview render diagnostics differ from the requested controls"
            )
        pcm = generated_mono_pcm(result.pcm)
        if not len(pcm):
            raise FailureReferencePreviewError("Preview render produced no audio")
        output = self._root / "preview.wav"
        try:
            write_pcm16_wav(output, pcm, result.sample_rate)
        except Pcm16MonoWavError as error:
            raise FailureReferencePreviewError(str(error)) from error
        payload = output.read_bytes()
        output.unlink(missing_ok=True)
        return payload

    def cancel(self) -> None:
        """Request cancellation of backend startup or the active render."""
        self._cancel.set()

    def close(self) -> None:
        """Release the worker and all ephemeral reference/preview files."""
        self.cancel()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._cache.clear()
            try:
                shutdown_speech_backend(self._backend)
            finally:
                self._backend = None
                self._backend_config = None
                shutil.rmtree(self._root, ignore_errors=True)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _load_workspace(self, document: JsonDocument) -> tuple[Path, JsonDocument]:
        expected = (
            Path(_required_text(document.get("workspace"), "Reference audit workspace"))
            .expanduser()
            .resolve()
        )
        try:
            directory, workspace, _workspace_sha256 = load_workspace_authority(expected)
        except AuthoringWorkbenchError as error:
            raise FailureReferencePreviewError(str(error)) from error
        if directory != expected:
            raise FailureReferencePreviewError(
                "Reference audit workspace identity changed"
            )
        return directory, workspace

    def _copy_reference(self, audio: FailureReferenceAudio) -> Path:
        suffix = audio.path.suffix.lower() or ".wav"
        target = self._root / f"reference-{audio.sha256}{suffix}"
        if target.exists():
            if target.is_symlink() or sha256_file(target) != audio.sha256:
                raise FailureReferencePreviewError(
                    "Ephemeral preview reference changed"
                )
            return target
        target.write_bytes(audio.payload)
        if sha256_file(target) != audio.sha256:
            raise FailureReferencePreviewError(
                "Unable to bind the ephemeral preview reference"
            )
        return target


def _preview_group(groups: dict[str, _PreviewGroup], group_id: str) -> _PreviewGroup:
    try:
        return groups[group_id]
    except KeyError as error:
        raise FailureReferencePreviewError(
            f"Reference audit group is unknown: {group_id}"
        ) from error


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FailureReferencePreviewError(f"{label} must be non-empty text")
    return value.strip()


def _reference_family(group: _PreviewGroup) -> str:
    value = group["synthesis_voice_character"]
    prefix = "Source reference "
    marker = " cluster-"
    if not value.startswith(prefix) or marker not in value:
        raise FailureReferencePreviewError(
            "Cross-group preview is restricted to source-reference character families"
        )
    character, separator, _cluster = value[len(prefix) :].partition(marker)
    if not separator or not character:
        raise FailureReferencePreviewError(
            "Cross-group preview source-reference identity is malformed"
        )
    return character


__all__ = [
    "FailureReferencePreview",
    "FailureReferencePreviewCancelled",
    "FailureReferencePreviewError",
    "FailureReferencePreviewIncomplete",
    "FailureReferencePreviewService",
]
