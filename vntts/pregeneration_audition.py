"""Cancellable, checksum-bound voice auditions for self-service pregeneration."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from io import BytesIO
from pathlib import Path
from stat import S_ISREG
from time import monotonic, process_time
from typing import BinaryIO, NamedTuple, Protocol, TypeAlias, TypedDict

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.atomic_io import atomic_output_path
from vntts_artifacts.audio import (
    Pcm16MonoWavError,
    probe_pcm16_mono_wav,
    write_pcm16_wav,
)
from vntts_artifacts.voice_manifest import VoiceManifestError

from vntts.application_directories import get_local_data_directory
from vntts.audio_cache import BoundedCache
from vntts.authoring.advisory_lock import (
    AdvisoryLockBusyError,
    exclusive_advisory_lock,
)
from vntts.authoring.authority import AuthoringAuthorityError, capture_authority_file
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_manifest import (
    AudioQuality,
    inspect_generated_wav_with_samples,
)
from vntts.authoring.speech_quality import (
    MAX_INTERNAL_SILENCE_SECONDS,
    MAX_LEADING_SILENCE_SECONDS,
    MAX_SILENCE_RATIO,
    MAX_TRAILING_SILENCE_SECONDS,
    SpeechQuality,
    SpeechSilenceValidationError,
    inspect_generated_speech_samples,
)
from vntts.cleanup import attempt_cleanup, cleanup_on_exit
from vntts.document_identity import (
    canonical_document_sha256,
    file_sha256,
    is_lowercase_sha256,
    sha256_file,
)
from vntts.json_types import decode_json
from vntts.path_safety import open_regular_binary, read_regular_bytes
from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
from vntts.reference_quality import analyze_reference_bytes
from vntts.settings import AppSettings
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.support import performance_operation_id, record_background_operation
from vntts.synthesis import (
    SynthesisCachePolicy,
    SynthesisCompletion,
    SynthesisRequest,
    SynthesisResult,
)
from vntts.tts_benchmark import create_backend
from vntts.voices import CharacterVoiceRegistry, read_voice_reference_bytes

COMPARISON_PREVIEW_TEXT = "The storm has passed. We can continue our journey."

_audition_counts: ContextVar[dict[str, int] | None] = ContextVar(
    "audition_counts", default=None
)
_audition_phase_name: ContextVar[str | None] = ContextVar(
    "audition_phase_name", default=None
)


def _count_audition(name: str, value: int = 1) -> None:
    counts = _audition_counts.get()
    if counts is not None:
        counts[name] = counts.get(name, 0) + value


@contextmanager
def measure_audition_phase(name: str) -> Iterator[None]:
    """Keep bounded correlated wall/CPU spans and checksum-admission counters."""
    parent_counts = _audition_counts.get()
    parent_name = _audition_phase_name.get()
    counts: dict[str, int] = dict.fromkeys(
        (
            "cache_hits",
            "cache_misses",
            "registry_builds",
            "reference_reads",
            "reference_preflights",
        ),
        0,
    )
    counts_token = _audition_counts.set(counts)
    phase_token = _audition_phase_name.set(name)
    operation_token = None
    if performance_operation_id.get() is None:
        operation_token = performance_operation_id.set(secrets.token_hex(16))
    started, cpu_started = monotonic(), process_time()
    outcome = "complete"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        record_background_operation(
            f"pregeneration-audition-{name}",
            (monotonic() - started) * 1000,
            outcome,
            operation_id=performance_operation_id.get(),
            parent_operation=parent_name,
            timing_scope="inclusive",
            cpu_ms=(process_time() - cpu_started) * 1000,
            **counts,
        )
        if parent_counts is not None:
            for key, value in counts.items():
                parent_counts[key] = parent_counts.get(key, 0) + value
        _audition_counts.reset(counts_token)
        _audition_phase_name.reset(phase_token)
        if operation_token is not None:
            performance_operation_id.reset(operation_token)


class VoiceAuditionError(RuntimeError):
    """An exact player voice audition could not be rendered safely."""


class VoiceAuditionCancelled(VoiceAuditionError):
    """The player cancelled audition generation."""


class VoiceAuditionIncomplete(VoiceAuditionError):
    """The provider stopped without producing a complete audition."""


class VoiceAuditionRejected(VoiceAuditionError):
    """Reference or generated audio failed an objective audio check."""


class _Cancellation(Protocol):
    def is_set(self) -> bool: ...


class _CollectedRender(Protocol):
    def collect(self) -> SynthesisResult: ...


class _PreviewBackend(Protocol):
    registry: CharacterVoiceRegistry

    def render(self, request: SynthesisRequest) -> _CollectedRender: ...


class _BackendFactory(Protocol):
    def __call__(
        self,
        name: str,
        registry: CharacterVoiceRegistry,
        cache_root: Path,
        *,
        model_name: str | None = None,
        language: str | None = None,
        startup_cancellation: _Cancellation | None = None,
        startup_progress: Callable[[str], object] | None = None,
        terms_accepted: bool = False,
        allow_gated_model_access: bool = False,
        allow_download: bool = True,
    ) -> _PreviewBackend: ...


NativePreviewContext: TypeAlias = dict[str, object]


class BackendConfig(NamedTuple):
    backend: str
    model: str | None
    language: str | None
    profile: str
    pocket_voice_cloning: bool
    xtts_terms_accepted: bool


ProgressReporter: TypeAlias = Callable[[str], object]


class _DownloadOptions(TypedDict, total=False):
    allow_download: bool


@dataclass(frozen=True)
class VoiceAuditionReference:
    path: Path
    payload: bytes


@dataclass(frozen=True)
class VoiceAuditionPreview:
    identity: str
    group_id: str
    candidate_source_id: str
    text: str
    backend: str
    model: str | None
    generation_profile: str
    seed: int | None
    path: Path
    audio_sha256: str
    sample_rate: int
    duration_seconds: float
    reused: bool


@dataclass
class _PreviewTelemetry:
    seed: int | None = None
    outcome: str = "backend_failed"
    reason: str = "backend-failed"
    stage: str = "reference-preflight"
    cache_source: str | None = None


class VoiceAuditionPreviewService:
    """Render at most one persistent preview for each exact candidate input."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        backend_factory: _BackendFactory = create_backend,
    ) -> None:
        self.root = (
            Path(
                root or get_local_data_directory() / "pregeneration" / "voice-auditions"
            )
            .expanduser()
            .resolve()
        )
        self.backend_factory: _BackendFactory = backend_factory
        self._backend: _PreviewBackend | None = None
        self._backend_config: BackendConfig | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._failed_preview_attempts: dict[str, int] = {}
        self._preview_salt = secrets.token_bytes(32)
        self._reference_registries: BoundedCache[
            tuple[Path, str], CharacterVoiceRegistry
        ] = BoundedCache(2)
        self._reference_preflights: BoundedCache[str, bool] = BoundedCache(256)

    @property
    def backend(self) -> _PreviewBackend | None:
        """The currently loaded preview engine, for read-only UI status."""
        return self._backend

    def can_prefetch(self, plan: VoicePlan) -> bool:
        """Speculation may reuse an explicitly started engine, never start one."""
        return (
            not self._closed
            and self._backend is not None
            and self._backend_config == _backend_config(plan)
        )

    def warmup(
        self,
        settings: AppSettings,
        *,
        cancel_event: _Cancellation,
        progress: ProgressReporter | None = None,
    ) -> None:
        """Load the selected MOSS engine from installed assets before voice matching."""
        if settings.speech_backend != "moss-tts":
            return
        config = BackendConfig(
            settings.speech_backend,
            settings.tts_model,
            settings.tts_language,
            settings.tts_profile,
            settings.pocket_gated_model_accepted,
            settings.xtts_terms_accepted,
        )
        with self._lock:
            if self._closed:
                return
            cancellation = _CombinedCancellation(cancel_event)
            _raise_if_cancelled(cancellation)
            from vntts.moss_cpp_backend import moss_cpp_requested

            if moss_cpp_requested(config.model) and not getattr(
                self._backend, "runtime_status", None
            ):
                self._backend_config = None
            self._ensure_backend(
                config,
                CharacterVoiceRegistry(),
                cancellation,
                progress,
                progress or (lambda _message: None),
                allow_download=False,
            )

    def has_cached_preview(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> bool:
        """Check absence before admitting any manifest or reference bytes."""
        candidate = _validate_request(plan, group, candidate_source_id)
        identity = preview_identity(
            candidate,
            _preview_text(group, text),
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            language=plan.synthesis_language,
            profile=plan.synthesis_profile,
        )
        exists = (self.root / f"{identity}.wav").exists()
        if not exists:
            _count_audition("cache_misses")
        return exists

    @measure_audition_phase("cache-lookup")
    def lookup(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
        cancel_event: _Cancellation | None = None,
    ) -> VoiceAuditionPreview | None:
        """Validate a saved preview without acquiring the model/render lock."""
        if self._closed:
            raise VoiceAuditionError("Voice audition service is closed")
        candidate = _validate_request(plan, group, candidate_source_id)
        preview_text = _preview_text(group, text)
        identity = preview_identity(
            candidate,
            preview_text,
            backend=plan.synthesis_backend,
            model=plan.synthesis_model,
            language=plan.synthesis_language,
            profile=plan.synthesis_profile,
        )
        target = self.root / f"{identity}.wav"
        if not target.exists():
            _count_audition("cache_misses")
            _raise_if_cancelled(_CombinedCancellation(cancel_event))
            return None
        _count_audition("cache_hits")
        stamp = self.authorization_stamp(plan, group, candidate_source_id)
        self._preflight_preview(plan, candidate, _CombinedCancellation(cancel_event))
        try:
            with exclusive_advisory_lock(self.root / f"{identity}.lock"):
                preview, _seed = _cached_preview_for_request(
                    target,
                    identity,
                    plan,
                    group,
                    candidate,
                    preview_text,
                    lambda _message: None,
                )
        except AdvisoryLockBusyError:
            return None
        _raise_if_cancelled(_CombinedCancellation(cancel_event))
        if not self.authorization_unchanged(stamp):
            raise VoiceAuditionError("Voice inputs changed during preview admission")
        return preview

    def authorization_stamp(
        self, plan: VoicePlan, group: VoiceGroup, candidate_source_id: str
    ) -> tuple[tuple[object, ...], ...]:
        """Cheap change detection after checksum admission, never an admission check."""
        candidate = _validate_request(plan, group, candidate_source_id)
        key = (
            (
                Path(plan.voice_manifest).expanduser().resolve(),
                plan.voice_manifest_sha256,
            )
            if plan.voice_manifest and plan.voice_manifest_sha256 is not None
            else None
        )
        registry = self._reference_registries.get(key) if key is not None else None
        if registry is None:
            registry = _load_manifest_registry(plan, self._reference_registries)
        voice = registry.resolve_source(candidate.source_id)
        if voice is None:
            raise VoiceAuditionError("Voice candidate is unavailable")
        paths = ([Path(plan.voice_manifest)] if plan.voice_manifest else []) + [
            Path(path) for path in voice.references
        ]
        return _file_stamps(paths)

    def authorization_unchanged(self, stamp: tuple[tuple[object, ...], ...]) -> bool:
        return stamp == _file_stamps([Path(str(entry[0])) for entry in stamp])

    def audio_stamp(
        self, preview: VoiceAuditionPreview
    ) -> tuple[tuple[object, ...], ...]:
        return _file_stamps([preview.path, _preview_manifest_path(preview.path)])

    @measure_audition_phase("generate")
    def generate(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
        cancel_event: _Cancellation | None = None,
        progress: ProgressReporter | None = None,
    ) -> VoiceAuditionPreview:
        """Generate or reuse the group's one representative candidate phrase."""
        with self._lock:
            if self._closed:
                raise VoiceAuditionError("Voice audition service is closed")
            self._cancel.clear()
            notify: ProgressReporter = progress or (lambda _message: None)
            cancellation = _CombinedCancellation(self._cancel, cancel_event)
            _raise_if_cancelled(cancellation)
            candidate = _validate_request(plan, group, candidate_source_id)
            preview_text = _preview_text(group, text)
            identity = preview_identity(
                candidate,
                preview_text,
                backend=plan.synthesis_backend,
                model=plan.synthesis_model,
                language=plan.synthesis_language,
                profile=plan.synthesis_profile,
            )
            target = self.root / f"{identity}.wav"
            native_context = self._native_preview_context(plan, identity)
            from vntts.support import native_speech_context

            context_token = (
                native_speech_context.set(native_context)
                if native_context is not None
                else None
            )
            started = monotonic()
            telemetry = _PreviewTelemetry()
            try:
                telemetry.outcome, telemetry.reason = (
                    "reference_preflight_failed",
                    "reference-preflight-failed",
                )
                stamp = self.authorization_stamp(plan, group, candidate_source_id)
                registry = self._preflight_preview(plan, candidate, cancellation)
                telemetry.outcome, telemetry.reason = "backend_failed", "backend-failed"
                with exclusive_advisory_lock(self.root / f"{identity}.lock"):
                    if target.exists():
                        telemetry.outcome, telemetry.reason = (
                            "cache_validation_failed",
                            "cache-validation-failed",
                        )
                        preview, telemetry.seed = _cached_preview_for_request(
                            target,
                            identity,
                            plan,
                            group,
                            candidate,
                            preview_text,
                            notify,
                        )
                        (
                            telemetry.outcome,
                            telemetry.reason,
                            telemetry.stage,
                            telemetry.cache_source,
                        ) = (
                            "success",
                            "accepted",
                            "cache",
                            "preview-file",
                        )
                    else:
                        preview = self._generate_new_preview(
                            plan,
                            group,
                            candidate,
                            preview_text,
                            target,
                            identity,
                            registry,
                            cancellation,
                            progress,
                            notify,
                            telemetry,
                        )
                    _raise_if_cancelled(cancellation)
                    if not self.authorization_unchanged(stamp):
                        telemetry.outcome, telemetry.reason = (
                            "reference_preflight_failed",
                            "inputs-changed",
                        )
                        raise VoiceAuditionError(
                            "Voice inputs changed during preview preparation"
                        )
                    return preview
            except AdvisoryLockBusyError as error:
                raise VoiceAuditionError(
                    "This voice preview is already being generated; retry shortly"
                ) from error
            except VoiceAuditionCancelled:
                telemetry.outcome, telemetry.reason = "cancelled", "cancelled"
                raise
            finally:
                try:
                    self._record_native_preview_outcome(
                        plan,
                        native_context,
                        seed=telemetry.seed,
                        outcome=telemetry.outcome,
                        reason=telemetry.reason,
                        stage=telemetry.stage,
                        cache_source=telemetry.cache_source,
                        elapsed_ms=round((monotonic() - started) * 1000),
                    )
                finally:
                    if context_token is not None:
                        native_speech_context.reset(context_token)

    def _generate_new_preview(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate: VoiceCandidate,
        preview_text: str,
        target: Path,
        identity: str,
        registry: CharacterVoiceRegistry,
        cancellation: _Cancellation,
        progress: ProgressReporter | None,
        notify: ProgressReporter,
        telemetry: _PreviewTelemetry,
    ) -> VoiceAuditionPreview:
        self.root.mkdir(parents=True, exist_ok=True)
        telemetry.stage = "startup"
        self._ensure_preview_backend(plan, registry, cancellation, progress, notify)
        telemetry.stage = "render"
        attempt = self._failed_preview_attempts.get(identity, 0)
        telemetry.seed = _preview_seed(plan, attempt)
        voice = registry.resolve_source(candidate.source_id)
        assert voice is not None
        result = self._render_preview(
            plan,
            voice.character,
            preview_text,
            telemetry.seed,
            attempt,
            cancellation,
            notify,
        )
        if result.completion is not SynthesisCompletion.COMPLETE:
            self._record_failed_preview_attempt(plan, identity)
            telemetry.outcome, telemetry.reason = "limited", "generation-limit"
            raise VoiceAuditionIncomplete(
                "The voice preview did not complete within its generation limits"
            )
        _validate_preview_result(result, plan, telemetry.seed)
        telemetry.stage = "quality"
        audio_sha256 = self._publish_preview(
            plan,
            candidate,
            preview_text,
            target,
            identity,
            result,
            cancellation,
            telemetry,
            notify,
        )
        preview = _cached_preview(
            target,
            identity,
            plan,
            group,
            candidate,
            preview_text,
            reused=False,
            seed=telemetry.seed,
            expected_audio_sha256=audio_sha256,
        )
        telemetry.outcome, telemetry.reason = "success", "accepted"
        return preview

    def _ensure_preview_backend(
        self,
        plan: VoicePlan,
        registry: CharacterVoiceRegistry,
        cancellation: _Cancellation,
        progress: ProgressReporter | None,
        notify: ProgressReporter,
    ) -> None:
        self._ensure_backend(
            _backend_config(plan), registry, cancellation, progress, notify
        )

    @measure_audition_phase("model-startup")
    def _ensure_backend(
        self,
        config: BackendConfig,
        registry: CharacterVoiceRegistry,
        cancellation: _Cancellation,
        progress: ProgressReporter | None,
        notify: ProgressReporter,
        *,
        allow_download: bool = True,
    ) -> None:
        if self._backend is not None and self._backend_config == config:
            _count_audition("cache_hits")
            self._backend.registry = registry
            return
        _count_audition("cache_misses")
        notify("Starting the preview model. First use also loads its weights...")
        shutdown_speech_backend(self._backend)
        self._backend = None
        self._backend_config = None
        download_options: _DownloadOptions = (
            {} if allow_download else {"allow_download": False}
        )
        try:
            if progress is None:
                self._backend = self.backend_factory(
                    config.backend,
                    registry,
                    self.root / "synthesis-cache",
                    model_name=config.model,
                    language=config.language,
                    startup_cancellation=cancellation,
                    allow_gated_model_access=config.pocket_voice_cloning,
                    terms_accepted=config.xtts_terms_accepted,
                    **download_options,
                )
            else:
                self._backend = self.backend_factory(
                    config.backend,
                    registry,
                    self.root / "synthesis-cache",
                    model_name=config.model,
                    language=config.language,
                    startup_cancellation=cancellation,
                    startup_progress=progress,
                    allow_gated_model_access=config.pocket_voice_cloning,
                    terms_accepted=config.xtts_terms_accepted,
                    **download_options,
                )
        except Exception as error:
            if cancellation.is_set():
                raise VoiceAuditionCancelled(
                    "Voice audition generation was cancelled"
                ) from error
            raise VoiceAuditionError(
                f"Unable to start the voice preview model: {error}"
            ) from error
        if cancellation.is_set():
            shutdown_speech_backend(self._backend)
            self._backend = None
            _raise_if_cancelled(cancellation)
        self._backend_config = config

    @measure_audition_phase("render")
    def _render_preview(
        self,
        plan: VoicePlan,
        voice_character: str,
        preview_text: str,
        seed: int | None,
        attempt: int,
        cancellation: _Cancellation,
        notify: ProgressReporter,
    ) -> SynthesisResult:
        request = SynthesisRequest(
            voice=voice_character,
            text=preview_text,
            seed=seed,
            generation_profile=plan.synthesis_profile,
            cancellation=cancellation,
            cache_policy=(
                SynthesisCachePolicy.REFRESH
                if attempt and plan.synthesis_backend == "moss-tts"
                else SynthesisCachePolicy.USE
            ),
        )
        notify(
            f"Trying a new sample (attempt {attempt + 1}) with the loaded model..."
            if attempt
            else "Generating preview audio with the loaded model..."
        )
        try:
            if self._backend is None:
                raise VoiceAuditionError("Voice preview backend is unavailable")
            result = self._backend.render(request).collect()
        except Exception as error:
            if cancellation.is_set():
                raise VoiceAuditionCancelled(
                    "Voice audition generation was cancelled"
                ) from error
            raise VoiceAuditionError(
                f"Unable to generate the voice preview: {error}"
            ) from error
        if cancellation.is_set() or result.completion is SynthesisCompletion.CANCELLED:
            raise VoiceAuditionCancelled("Voice audition generation was cancelled")
        return result

    def _publish_preview(
        self,
        plan: VoicePlan,
        candidate: VoiceCandidate,
        preview_text: str,
        target: Path,
        identity: str,
        result: SynthesisResult,
        cancellation: _Cancellation,
        telemetry: _PreviewTelemetry,
        notify: ProgressReporter,
    ) -> str:
        samples = _mono_pcm(result.pcm)
        if not len(samples):
            raise VoiceAuditionError("Voice preview generation produced no audio")
        staging = _staging_path(target)
        with cleanup_on_exit(
            lambda: staging.unlink(missing_ok=True),
            description="voice preview staging cleanup",
        ):
            try:
                write_pcm16_wav(staging, samples, result.sample_rate)
            except Pcm16MonoWavError as error:
                raise VoiceAuditionError(str(error)) from error
            notify("Checking generated audio for silence and other failures...")
            try:
                _inspect_preview(staging, preview_text)
            except VoiceAuditionError as error:
                if cancellation.is_set():
                    raise VoiceAuditionCancelled(
                        "Voice audition generation was cancelled"
                    ) from error
                self._record_failed_preview_attempt(plan, identity)
                telemetry.outcome, telemetry.reason = (
                    "quality_failed",
                    "quality-rejected",
                )
                raise
            _load_candidate_registry(
                plan, candidate, registry_cache=self._reference_registries
            )
            _raise_if_cancelled(cancellation)
            telemetry.stage = "publish"
            try:
                audio_sha256 = sha256_file(staging)
                _write_preview_manifest(target, identity, telemetry.seed, audio_sha256)
                os.replace(staging, target)
            except BaseException as error:
                attempt_cleanup(
                    lambda: target.unlink(missing_ok=True),
                    description="voice preview target cleanup",
                    primary_error=error,
                )
                attempt_cleanup(
                    lambda: _preview_manifest_path(target).unlink(missing_ok=True),
                    description="voice preview manifest cleanup",
                    primary_error=error,
                )
                raise
            telemetry.cache_source = result.diagnostics.cache_source
            return audio_sha256

    def cancel(self) -> None:
        self._cancel.set()

    def _preflight_preview(
        self, plan: VoicePlan, candidate: VoiceCandidate, cancellation: _Cancellation
    ) -> CharacterVoiceRegistry:
        _raise_if_cancelled(cancellation)
        registry = _load_candidate_registry(
            plan,
            candidate,
            registry_cache=self._reference_registries,
            preflight_cache=self._reference_preflights,
            cancellation=cancellation,
        )
        _raise_if_cancelled(cancellation)
        return registry

    def _record_failed_preview_attempt(self, plan: VoicePlan, identity: str) -> None:
        if plan.synthesis_backend == "moss-tts":
            # ponytail: keep 256 current dialog candidates; widen only if that UI grows.
            if (
                identity not in self._failed_preview_attempts
                and len(self._failed_preview_attempts) >= 256
            ):
                self._failed_preview_attempts.pop(
                    next(iter(self._failed_preview_attempts))
                )
            self._failed_preview_attempts[identity] = (
                self._failed_preview_attempts.get(identity, 0) + 1
            )

    def _native_preview_context(
        self, plan: VoicePlan, identity: str
    ) -> NativePreviewContext | None:
        if not _native_moss_preview(plan):
            return None
        logical_key = hashlib.sha256(
            self._preview_salt + identity.encode("ascii")
        ).hexdigest()[:24]
        return {"logical_key": logical_key, "attempt_id": secrets.token_hex(12)}

    @staticmethod
    def _record_native_preview_outcome(
        plan: VoicePlan,
        context: NativePreviewContext | None,
        *,
        seed: int | None,
        outcome: str,
        reason: str,
        stage: str,
        cache_source: str | None,
        elapsed_ms: int,
    ) -> None:
        if context is None:
            return
        from vntts.support import record_native_speech

        record_native_speech(
            operation="preview-outcome",
            backend=plan.synthesis_backend,
            profile=plan.synthesis_profile,
            requested_seed=seed,
            outcome=outcome,
            reason=reason,
            stage=stage,
            cache_source=cache_source,
            elapsed_ms=elapsed_ms,
        )

    def reference_audio(
        self, plan: VoicePlan, group: VoiceGroup, candidate_source_id: str
    ) -> VoiceAuditionReference | None:
        """Return exact checksum-verified original bytes without starting TTS."""
        candidate = _validate_request(plan, group, candidate_source_id)
        _registry, reference = _load_candidate(
            plan, candidate, registry_cache=self._reference_registries
        )
        if reference is None:
            return None
        try:
            probe_pcm16_mono_wav(BytesIO(reference.payload))
            return reference
        except (OSError, ValueError, VoiceManifestError) as error:
            raise VoiceAuditionError(
                f"Original voice anchor is not playable: {error}"
            ) from error

    def close(self) -> None:
        self.cancel()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                shutdown_speech_backend(self._backend)
            finally:
                self._backend = None
                self._backend_config = None
                self._reference_registries.clear()
                self._reference_preflights.clear()


def _cached_preview_for_request(
    target: Path,
    identity: str,
    plan: VoicePlan,
    group: VoiceGroup,
    candidate: VoiceCandidate,
    preview_text: str,
    notify: ProgressReporter,
) -> tuple[VoiceAuditionPreview, int | None]:
    notify("Checking the saved preview...")
    seed, audio_sha256 = _cached_preview_metadata(target, identity, plan)
    return (
        _cached_preview(
            target,
            identity,
            plan,
            group,
            candidate,
            preview_text,
            seed=seed,
            expected_audio_sha256=audio_sha256,
        ),
        seed,
    )


def _validate_preview_result(
    result: SynthesisResult, plan: VoicePlan, seed: int | None
) -> None:
    if (
        result.diagnostics.backend != plan.synthesis_backend
        or result.diagnostics.generation_profile != plan.synthesis_profile
        or result.diagnostics.seed != seed
    ):
        raise VoiceAuditionError(
            "Voice preview diagnostics differ from the planned controls"
        )


class _CombinedCancellation:
    def __init__(self, *events: _Cancellation | None) -> None:
        self.events: tuple[_Cancellation, ...] = tuple(
            event for event in events if event is not None
        )

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


def _validate_request(
    plan: VoicePlan, group: VoiceGroup, candidate_source_id: str
) -> VoiceCandidate:
    if not isinstance(plan, VoicePlan) or not isinstance(group, VoiceGroup):
        raise VoiceAuditionError("Voice audition inputs are invalid")
    if group not in plan.groups:
        raise VoiceAuditionError("Voice audition group is not part of this plan")
    candidates_by_source = {
        candidate.source_id: candidate
        for candidate in (*group.candidates, *group.candidate_inventory)
    }
    candidate = (
        candidates_by_source.get(candidate_source_id)
        if isinstance(candidate_source_id, str)
        else None
    )
    if (
        candidate is None
        and group.narrator_candidate is not None
        and group.narrator_candidate.source_id == candidate_source_id
    ):
        candidate = group.narrator_candidate
    if candidate is None:
        raise VoiceAuditionError("Voice audition candidate is not uniquely available")
    return candidate


def _preview_text(group: VoiceGroup, text: str | None) -> str:
    selected = group.sample_text if text is None else text
    if (
        not isinstance(selected, str)
        or not selected.strip()
        or selected
        not in {COMPARISON_PREVIEW_TEXT, group.sample_text, group.alternate_sample_text}
    ):
        raise VoiceAuditionError("Voice audition text is invalid")
    return selected


def _load_candidate_registry(
    plan: VoicePlan,
    candidate: VoiceCandidate,
    *,
    registry_cache: BoundedCache[tuple[Path, str], CharacterVoiceRegistry]
    | None = None,
    preflight_cache: BoundedCache[str, bool] | None = None,
    cancellation: _Cancellation | None = None,
) -> CharacterVoiceRegistry:
    registry, _reference = _load_candidate(
        plan,
        candidate,
        registry_cache=registry_cache,
        preflight_cache=preflight_cache,
        cancellation=cancellation,
    )
    if plan.voice_manifest:
        snapshot = capture_authority_file(
            plan.voice_manifest, "character voice manifest"
        )
        _count_audition("files_examined")
        _count_audition("bytes_examined", len(snapshot.payload))
        if snapshot.sha256 != plan.voice_manifest_sha256:
            raise VoiceAuditionError("Character voices changed while they were read")
    return registry


def _load_manifest_registry(
    plan: VoicePlan,
    registry_cache: BoundedCache[tuple[Path, str], CharacterVoiceRegistry] | None,
) -> CharacterVoiceRegistry:
    if not plan.voice_manifest:
        return CharacterVoiceRegistry()
    snapshot = capture_authority_file(plan.voice_manifest, "character voice manifest")
    _count_audition("files_examined")
    _count_audition("bytes_examined", len(snapshot.payload))
    if snapshot.sha256 != plan.voice_manifest_sha256:
        raise VoiceAuditionError("Character voices changed after planning")
    key = (snapshot.path, snapshot.sha256)
    registry = registry_cache.get(key) if registry_cache is not None else None
    if registry is None:
        _count_audition("registry_builds")
        registry = CharacterVoiceRegistry.from_document(
            snapshot.json_document("character voice manifest"), snapshot.path
        )
        if registry_cache is not None:
            registry_cache.put(key, registry)
    return registry


def _load_candidate(
    plan: VoicePlan,
    candidate: VoiceCandidate,
    *,
    registry_cache: BoundedCache[tuple[Path, str], CharacterVoiceRegistry]
    | None = None,
    preflight_cache: BoundedCache[str, bool] | None = None,
    cancellation: _Cancellation | None = None,
) -> tuple[CharacterVoiceRegistry, VoiceAuditionReference | None]:
    try:
        registry = _load_manifest_registry(plan, registry_cache)
        voice = registry.resolve_source(candidate.source_id)
    except (AuthoringAuthorityError, OSError, VoiceManifestError, ValueError) as error:
        raise VoiceAuditionError(
            f"Unable to resolve the voice candidate: {error}"
        ) from error
    if voice is None:
        raise VoiceAuditionError("Narrator fallback is not a voice candidate")
    reference = None
    captured: list[tuple[Path, bytes]] = []
    try:
        checksums: list[str] = []
        for path in voice.references:
            if cancellation is not None:
                _raise_if_cancelled(cancellation)
            payload = read_voice_reference_bytes(voice, path)
            _count_audition("reference_reads")
            _count_audition("files_examined")
            _count_audition("bytes_examined", len(payload))
            captured.append((Path(path), payload))
            checksums.append(hashlib.sha256(payload).hexdigest())
            if reference is None:
                reference = VoiceAuditionReference(Path(path), payload)
        identity = VoiceCandidate(
            source_id=candidate.source_id,
            source_character=voice.source_character or voice.character,
            source_speaker=voice.speaker,
            reference_sha256s=tuple(checksums),
        )
    except (OSError, VoiceManifestError, ValueError) as error:
        raise VoiceAuditionError(
            f"Unable to read the voice candidate: {error}"
        ) from error
    if (
        identity.source_id != candidate.source_id
        or identity.source_character != candidate.source_character
        or identity.source_speaker != candidate.source_speaker
        or identity.reference_sha256s != candidate.reference_sha256s
    ):
        raise VoiceAuditionError("Voice candidate changed after planning")
    if preflight_cache is not None:
        _preflight_captured_references(
            captured, checksums, preflight_cache, cancellation
        )
    if candidate.reference_transcript:
        voice = replace(voice, reference_transcript=candidate.reference_transcript)
    return CharacterVoiceRegistry((voice,)), reference


def _preflight_captured_references(
    captured: list[tuple[Path, bytes]],
    checksums: list[str],
    cache: BoundedCache[str, bool],
    cancellation: _Cancellation | None,
) -> None:
    for index, ((path, payload), checksum) in enumerate(
        zip(captured, checksums, strict=True)
    ):
        if cancellation is not None:
            _raise_if_cancelled(cancellation)
        if cache.get(checksum):
            continue
        _count_audition("reference_preflights")
        _preflight_candidate_reference(payload, path, index, checksum)
        cache.put(checksum, True)


def preview_identity(
    candidate: VoiceCandidate,
    text: str,
    *,
    backend: str,
    model: str | None,
    language: str | None,
    profile: str,
) -> str:
    """Bind preview and advisory evidence to the exact synthesis inputs."""
    document = {
        "version": 2,
        "references": candidate.reference_sha256s,
        "speaker": candidate.source_speaker
        if not candidate.reference_sha256s or backend == "coqui-xtts"
        else None,
        "reference_transcript": candidate.reference_transcript,
        "text": text,
        "backend": backend,
        "model": model,
        "language": language,
        "profile": profile,
        "seed": None if backend == "pocket-tts" else 0,
    }
    from vntts.runtime_paths import (
        RUNTIME_ENVIRONMENT_VARIABLES,
        source_runtime_project,
    )

    runtime_project = source_runtime_project(backend)
    if runtime_project is not None:
        document["runtime_recipe"] = [
            sha256_file(runtime_project / name)
            for name in ("pyproject.toml", "uv.lock")
        ]
    runtime_variable = RUNTIME_ENVIRONMENT_VARIABLES.get(backend)
    document["runtime_override"] = (
        os.environ.get(runtime_variable, "") if runtime_variable else None
    )
    if backend == "qwen-tts":
        from vntts.qwen_backend import (
            QWEN_CUDA_MODEL,
            QWEN_CUDA_REVISION,
            QWEN_MODEL,
            QWEN_REVISION,
        )

        document["model"] = (
            (QWEN_CUDA_MODEL, QWEN_CUDA_REVISION)
            if sys.platform == "win32"
            else (QWEN_MODEL, QWEN_REVISION)
        )
    if backend == "moss-tts":
        from vntts.moss_cpp_backend import (
            NATIVE_GENERATION_CONTRACT,
            moss_cpp_requested,
        )

        if moss_cpp_requested(model):
            document["native_generation_contract"] = NATIVE_GENERATION_CONTRACT
            from vntts.moss_cpp_installation import configured_paths
            from vntts.speech_backend_runtime import _source_identity

            document["native_files"] = [
                _source_identity(path) for path in configured_paths(model)
            ]
            document["native_controls"] = {
                name: os.environ.get(name)
                for name in (
                    "VNTTS_MOSS_GPU_LAYERS",
                    "VNTTS_MOSS_AUX_CPU",
                    "VNTTS_MOSS_CONTEXT",
                    "VNTTS_MOSS_AUX_CPU_THREADS",
                    "VNTTS_MOSS_QUALIFY_ADAPTIVE",
                )
            }
        else:
            from vntts.speech_backend import default_moss_tts_model

            document["model"] = (
                model or os.environ.get("VNTTS_MOSS_MODEL") or default_moss_tts_model
            )
            model = str(document["model"])
            if Path(model).is_dir():
                cached: object = str(Path(model) / "config.json")
            else:
                from huggingface_hub import try_to_load_from_cache

                cached = try_to_load_from_cache(model, "config.json")
            if isinstance(cached, str) and Path(cached).is_file():
                document["model_snapshot"] = (
                    str(Path(cached).parent),
                    sha256_file(cached),
                )
    return canonical_document_sha256(document)


def _file_stamps(paths: list[Path]) -> tuple[tuple[object, ...], ...]:
    stamps = []
    for path in paths:
        stat = path.stat(follow_symlinks=False)
        if not S_ISREG(stat.st_mode) or path.is_junction():
            raise VoiceAuditionError("Voice input is no longer a regular file")
        stamps.append(
            (
                str(path),
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        )
    return tuple(stamps)


def _backend_config(plan: VoicePlan) -> BackendConfig:
    return BackendConfig(
        plan.synthesis_backend,
        plan.synthesis_model,
        plan.synthesis_language,
        plan.synthesis_profile,
        plan.pocket_voice_cloning,
        plan.xtts_terms_accepted,
    )


def _native_moss_preview(plan: VoicePlan) -> bool:
    if plan.synthesis_backend != "moss-tts":
        return False
    from vntts.moss_cpp_backend import moss_cpp_requested

    return bool(moss_cpp_requested(plan.synthesis_model))


def _preview_seed(plan: VoicePlan, failed_attempts: int) -> int | None:
    if plan.synthesis_backend == "pocket-tts":
        return None
    if plan.synthesis_backend != "moss-tts" or failed_attempts == 0:
        return 0
    # Native maps request seed zero to one, so retrying with one repeats it.
    return failed_attempts + (1 if _native_moss_preview(plan) else 0)


def _preview_manifest_path(target: Path) -> Path:
    return target.with_suffix(".json")


def _write_preview_manifest(
    target: Path, identity: str, seed: int | None, audio_sha256: str
) -> None:
    manifest = _preview_manifest_path(target)
    with atomic_output_path(manifest) as staging:
        staging.write_text(
            json.dumps(
                {
                    "audio_sha256": audio_sha256,
                    "identity": identity,
                    "seed": seed,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )


def _cached_preview_metadata(
    target: Path, identity: str, plan: VoicePlan
) -> tuple[int | None, str | None]:
    manifest = _preview_manifest_path(target)
    if not manifest.exists():
        # Existing successful previews predate the sidecar and were all seed zero.
        return (None if plan.synthesis_backend == "pocket-tts" else 0), None
    if manifest.is_symlink():
        raise VoiceAuditionError(
            "Cached voice preview manifest must not be a symbolic link"
        )
    try:
        with open_regular_binary(manifest) as source:
            payload = source.read(1025)
        if len(payload) > 1024:
            raise ValueError("preview manifest is too large")
        document = decode_json(payload)
        if not isinstance(document, dict):
            raise ValueError("preview manifest must be an object")
        seed = document["seed"]
        audio_sha256 = file_sha256(target, error_type=OSError)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise VoiceAuditionError(
            f"Cached voice preview manifest is invalid: {error}"
        ) from error
    if (
        document.get("identity") != identity
        or not is_lowercase_sha256(document.get("audio_sha256"))
        or document["audio_sha256"] != audio_sha256
        or (seed is not None and (type(seed) is not int or not 0 <= seed < 2**64))
        or (seed is None) != (plan.synthesis_backend == "pocket-tts")
    ):
        raise VoiceAuditionError(
            "Cached voice preview manifest does not match its input"
        )
    return seed, audio_sha256


def _preflight_candidate_reference(
    payload: bytes, path: Path, index: int, checksum: str
) -> None:
    try:
        report = analyze_reference_bytes(payload, path=path)
    except ValueError as error:
        raise VoiceAuditionRejected(
            f"Voice reference {index + 1} failed objective preflight: {error}"
        ) from error
    if report["sha256"] != checksum:
        raise VoiceAuditionError("Voice reference changed during objective preflight")
    if report["objective_preflight"] != "pass":
        reasons = ", ".join(report["rejection_reasons"])
        raise VoiceAuditionRejected(
            f"Voice reference {index + 1} failed objective preflight: {reasons}"
        )


@measure_audition_phase("preview-quality")
def _inspect_preview(path: Path | BinaryIO, text: str) -> AudioQuality:
    audio_quality: AudioQuality | None = None
    try:
        audio_quality, samples = inspect_generated_wav_with_samples(path)
        speech_quality = inspect_generated_speech_samples(
            samples,
            sample_rate=audio_quality.sample_rate,
            duration_seconds=audio_quality.sample_count / audio_quality.sample_rate,
            text=text,
        )
    except SpeechSilenceValidationError as error:
        _record_preview_quality(
            "rejected", "speech-silence", audio_quality, error.quality
        )
        raise VoiceAuditionRejected(
            f"Generated voice preview failed objective preflight: {error}"
        ) from error
    except BulkGenerationError as error:
        _record_preview_quality(
            "rejected",
            "wav-validation" if audio_quality is None else "invalid-wav",
            audio_quality,
        )
        raise VoiceAuditionRejected(
            f"Generated voice preview failed objective preflight: {error}"
        ) from error
    _record_preview_quality("accepted", "accepted", audio_quality, speech_quality)
    return audio_quality


def _record_preview_quality(
    outcome: str,
    reason: str,
    audio_quality: AudioQuality | None,
    speech_quality: SpeechQuality | None = None,
) -> None:
    from vntts.support import native_speech_context, record_native_speech

    if native_speech_context.get() is None:
        return
    quality = asdict(audio_quality) if audio_quality is not None else {}
    if speech_quality is not None:
        quality.update(asdict(speech_quality))
    record_native_speech(
        operation="preview-quality",
        outcome=outcome,
        reason=reason,
        quality=quality,
        thresholds={
            "leading_silence_seconds": MAX_LEADING_SILENCE_SECONDS,
            "trailing_silence_seconds": MAX_TRAILING_SILENCE_SECONDS,
            "internal_silence_seconds": MAX_INTERNAL_SILENCE_SECONDS,
            "silence_ratio": MAX_SILENCE_RATIO,
        },
    )


def _cached_preview(
    target: Path,
    identity: str,
    plan: VoicePlan,
    group: VoiceGroup,
    candidate: VoiceCandidate,
    text: str,
    *,
    reused: bool = True,
    seed: int | None,
    expected_audio_sha256: str | None = None,
) -> VoiceAuditionPreview:
    if target.is_symlink():
        raise VoiceAuditionError("Cached voice preview must not be a symbolic link")
    try:
        payload = read_regular_bytes(target)
        audio_sha256 = hashlib.sha256(payload).hexdigest()
        if expected_audio_sha256 is not None and audio_sha256 != expected_audio_sha256:
            raise VoiceAuditionError("Voice preview changed while it was validated")
        info = _inspect_preview(BytesIO(payload), text)
        if file_sha256(target, error_type=OSError) != audio_sha256:
            raise VoiceAuditionError("Voice preview changed while it was validated")
    except (OSError, ValueError, VoiceAuditionError) as error:
        raise VoiceAuditionError(f"Cached voice preview is invalid: {error}") from error
    if reused and plan.synthesis_backend == "moss-tts":
        from vntts.moss_cpp_backend import moss_cpp_requested
        from vntts.support import record_native_speech

        if moss_cpp_requested(plan.synthesis_model):
            record_native_speech(
                operation="cached-preview",
                outcome="complete",
                cache="preview-file",
                reference="not-used",
                audio_s=info.duration_seconds,
                gen_s=None,
                decode_s=None,
            )
    return VoiceAuditionPreview(
        identity=identity,
        group_id=group.group_id,
        candidate_source_id=candidate.source_id,
        text=text,
        backend=plan.synthesis_backend,
        model=plan.synthesis_model,
        generation_profile=plan.synthesis_profile,
        seed=seed,
        path=target,
        audio_sha256=audio_sha256,
        sample_rate=info.sample_rate,
        duration_seconds=info.duration_seconds,
        reused=reused,
    )


def _mono_pcm(value: object) -> NDArray[np.float32]:
    samples: NDArray[np.float32] = np.asarray(value, dtype=np.float32)
    if samples.ndim == 2 and samples.shape[1] in {1, 2}:
        samples = (
            samples[:, 0]
            if samples.shape[1] == 1
            else samples.mean(axis=1, dtype=np.float32)
        )
    if samples.ndim != 1 or not np.isfinite(samples).all():
        raise VoiceAuditionError("Voice preview PCM must be finite mono audio")
    return samples


def _staging_path(target: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.stem}-",
        suffix=".wav",
        dir=target.parent,
    )
    try:
        os.close(descriptor)
    except BaseException as error:
        attempt_cleanup(
            lambda: Path(name).unlink(missing_ok=True),
            description="Voice preview stage acquisition cleanup",
            primary_error=error,
        )
        raise
    return Path(name)


def _raise_if_cancelled(cancellation: _Cancellation) -> None:
    if cancellation.is_set():
        raise VoiceAuditionCancelled("Voice audition generation was cancelled")


__all__ = [
    "COMPARISON_PREVIEW_TEXT",
    "VoiceAuditionCancelled",
    "VoiceAuditionError",
    "VoiceAuditionIncomplete",
    "VoiceAuditionRejected",
    "VoiceAuditionPreview",
    "VoiceAuditionPreviewService",
    "preview_identity",
]
