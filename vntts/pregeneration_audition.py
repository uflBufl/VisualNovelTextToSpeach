"""Cancellable, checksum-bound voice auditions for self-service pregeneration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic

import numpy as np
from vntts_artifacts.atomic_io import atomic_output_path
from vntts_artifacts.audio import probe_pcm16_mono_wav, write_pcm16_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import VoiceManifestError

from vntts.application_directories import get_local_data_directory
from vntts.authoring.generation_lease import BulkGenerationError
from vntts.authoring.generation_manifest import inspect_generated_wav
from vntts.authoring.speech_quality import (
    MAX_INTERNAL_SILENCE_SECONDS,
    MAX_LEADING_SILENCE_SECONDS,
    MAX_SILENCE_RATIO,
    MAX_TRAILING_SILENCE_SECONDS,
    SpeechSilenceValidationError,
    inspect_generated_speech,
)
from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
from vntts.reference_quality import analyze_reference
from vntts.speech_backend_runtime import shutdown_speech_backend
from vntts.synthesis import SynthesisCachePolicy, SynthesisCompletion, SynthesisRequest
from vntts.tts_benchmark import create_backend
from vntts.voices import CharacterVoiceRegistry


class VoiceAuditionError(RuntimeError):
    """An exact player voice audition could not be rendered safely."""


class VoiceAuditionCancelled(VoiceAuditionError):
    """The player cancelled audition generation."""


class VoiceAuditionIncomplete(VoiceAuditionError):
    """The provider stopped without producing a complete audition."""


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


class VoiceAuditionPreviewService:
    """Render at most one persistent preview for each exact candidate input."""

    def __init__(self, root=None, *, backend_factory=create_backend):
        self.root = (
            Path(
                root or get_local_data_directory() / "pregeneration" / "voice-auditions"
            )
            .expanduser()
            .resolve()
        )
        self.backend_factory = backend_factory
        self._backend = None
        self._backend_config = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._failed_preview_attempts = {}
        self._preview_salt = secrets.token_bytes(32)

    @property
    def backend(self):
        """The currently loaded preview engine, for read-only UI status."""
        return self._backend

    def generate(
        self,
        plan,
        group,
        candidate_source_id,
        *,
        text=None,
        cancel_event=None,
        progress=None,
    ):
        """Generate or reuse the group's one representative candidate phrase."""
        with self._lock:
            if self._closed:
                raise VoiceAuditionError("Voice audition service is closed")
            self._cancel.clear()
            notify = progress or (lambda _message: None)
            cancellation = _CombinedCancellation(self._cancel, cancel_event)
            _raise_if_cancelled(cancellation)
            candidate = _validate_request(plan, group, candidate_source_id)
            preview_text = _preview_text(group, text)
            identity = _preview_identity(plan, group, candidate, preview_text)
            target = self.root / f"{identity}.wav"
            native_context = self._native_preview_context(plan, identity)
            if native_context is not None:
                from vntts.support import native_speech_context

                context_token = native_speech_context.set(native_context)
            else:
                context_token = None
            started = monotonic()
            seed = None
            outcome, reason, stage, cache_source = (
                "backend_failed",
                "backend-failed",
                "reference-preflight",
                None,
            )
            try:
                try:
                    _raise_if_cancelled(cancellation)
                    registry = _load_candidate_registry(plan, candidate)
                    _preflight_candidate_references(registry, candidate)
                    _raise_if_cancelled(cancellation)
                except VoiceAuditionCancelled:
                    raise
                except VoiceAuditionError:
                    outcome, reason = (
                        "reference_preflight_failed",
                        "reference-preflight-failed",
                    )
                    raise
                if target.exists():
                    notify("Checking the saved preview...")
                    stage = "cache"
                    try:
                        seed = _cached_preview_seed(target, identity, plan)
                        preview = _cached_preview(
                            target,
                            identity,
                            plan,
                            group,
                            candidate,
                            preview_text,
                            seed=seed,
                        )
                    except VoiceAuditionError:
                        outcome, reason = (
                            "cache_validation_failed",
                            "cache-validation-failed",
                        )
                        raise
                    outcome, reason, stage, cache_source = (
                        "success",
                        "accepted",
                        "cache",
                        "preview-file",
                    )
                    return preview

                self.root.mkdir(parents=True, exist_ok=True)
                backend_config = (
                    plan.synthesis_backend,
                    plan.synthesis_model,
                    plan.synthesis_profile,
                    plan.pocket_voice_cloning,
                )
                if self._backend is None or self._backend_config != backend_config:
                    stage = "startup"
                    notify(
                        "Starting the preview model. First use also loads its weights..."
                    )
                    self._backend = shutdown_speech_backend(self._backend)
                    self._backend_config = None
                    try:
                        self._backend = self.backend_factory(
                            plan.synthesis_backend,
                            registry,
                            self.root / "synthesis-cache",
                            model_name=plan.synthesis_model,
                            startup_cancellation=cancellation,
                            **(
                                {"startup_progress": progress}
                                if progress is not None
                                else {}
                            ),
                            allow_gated_model_access=plan.pocket_voice_cloning,
                        )
                    except Exception as error:
                        if cancellation.is_set():
                            raise VoiceAuditionCancelled(
                                "Voice audition generation was cancelled"
                            ) from error
                        raise VoiceAuditionError(
                            f"Unable to start the voice preview model: {error}"
                        ) from error
                    self._backend_config = backend_config
                else:
                    self._backend.registry = registry

                attempt = self._failed_preview_attempts.get(identity, 0)
                seed = _preview_seed(plan, attempt)
                request = SynthesisRequest(
                    voice=candidate.source_character,
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
                stage = "render"
                try:
                    notify(
                        f"Trying a new sample (attempt {attempt + 1}) with the loaded model..."
                        if attempt
                        else "Generating preview audio with the loaded model..."
                    )
                    result = self._backend.render(request).collect()
                except Exception as error:
                    if cancellation.is_set():
                        raise VoiceAuditionCancelled(
                            "Voice audition generation was cancelled"
                        ) from error
                    raise VoiceAuditionError(
                        f"Unable to generate the voice preview: {error}"
                    ) from error
                if (
                    cancellation.is_set()
                    or result.completion is SynthesisCompletion.CANCELLED
                ):
                    raise VoiceAuditionCancelled(
                        "Voice audition generation was cancelled"
                    )
                if result.completion is not SynthesisCompletion.COMPLETE:
                    self._record_failed_preview_attempt(plan, identity)
                    outcome, reason = "limited", "generation-limit"
                    raise VoiceAuditionIncomplete(
                        "The voice preview did not complete within its generation limits"
                    )
                if (
                    result.diagnostics.backend != plan.synthesis_backend
                    or result.diagnostics.generation_profile != plan.synthesis_profile
                    or result.diagnostics.seed != seed
                ):
                    raise VoiceAuditionError(
                        "Voice preview diagnostics differ from the planned controls"
                    )
                samples = _mono_pcm(result.pcm)
                sample_rate = int(result.sample_rate)
                if not len(samples) or sample_rate < 1:
                    raise VoiceAuditionError(
                        "Voice preview generation produced no audio"
                    )

                staging = _staging_path(target)
                try:
                    write_pcm16_wav(staging, samples, sample_rate)
                    stage = "quality"
                    notify("Checking generated audio for silence and other failures...")
                    try:
                        _inspect_preview(staging, preview_text)
                    except VoiceAuditionError as error:
                        if cancellation.is_set():
                            raise VoiceAuditionCancelled(
                                "Voice audition generation was cancelled"
                            ) from error
                        self._record_failed_preview_attempt(plan, identity)
                        outcome, reason = "quality_failed", "quality-rejected"
                        raise
                    _load_candidate_registry(plan, candidate)
                    _raise_if_cancelled(cancellation)
                    stage = "publish"
                    audio_sha256 = sha256_file(staging)
                    try:
                        _write_preview_manifest(target, identity, seed, audio_sha256)
                        os.replace(staging, target)
                    except Exception:
                        target.unlink(missing_ok=True)
                        _preview_manifest_path(target).unlink(missing_ok=True)
                        raise
                finally:
                    staging.unlink(missing_ok=True)
                preview = _cached_preview(
                    target,
                    identity,
                    plan,
                    group,
                    candidate,
                    preview_text,
                    reused=False,
                    seed=seed,
                )
                outcome, reason, cache_source = (
                    "success",
                    "accepted",
                    result.diagnostics.cache_source,
                )
                return preview
            except VoiceAuditionCancelled:
                outcome, reason = "cancelled", "cancelled"
                raise
            finally:
                try:
                    self._record_native_preview_outcome(
                        plan,
                        native_context,
                        seed=seed,
                        outcome=outcome,
                        reason=reason,
                        stage=stage,
                        cache_source=cache_source,
                        elapsed_ms=round((monotonic() - started) * 1000),
                    )
                finally:
                    if context_token is not None:
                        native_speech_context.reset(context_token)

    def cancel(self):
        self._cancel.set()

    def _record_failed_preview_attempt(self, plan, identity):
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

    def _native_preview_context(self, plan, identity):
        if not _native_moss_preview(plan):
            return None
        logical_key = hashlib.sha256(
            self._preview_salt + identity.encode("ascii")
        ).hexdigest()[:24]
        return {"logical_key": logical_key, "attempt_id": secrets.token_hex(12)}

    @staticmethod
    def _record_native_preview_outcome(
        plan,
        context,
        *,
        seed,
        outcome,
        reason,
        stage,
        cache_source,
        elapsed_ms,
    ):
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

    def reference_audio(self, plan, group, candidate_source_id):
        """Return one checksum-verified, playable original reference when present."""
        candidate = _validate_request(plan, group, candidate_source_id)
        registry = _load_candidate_registry(plan, candidate)
        try:
            voice = registry.resolve_source(candidate.source_id)
            if voice is None or not voice.references:
                return None
            reference = voice.references[0]
            if sha256_file(reference) != candidate.reference_sha256s[0]:
                raise VoiceAuditionError("Original voice anchor changed after planning")
            probe_pcm16_mono_wav(reference)
            return reference
        except (OSError, ValueError, VoiceManifestError) as error:
            raise VoiceAuditionError(
                f"Original voice anchor is not playable: {error}"
            ) from error

    def close(self):
        self.cancel()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._backend = shutdown_speech_backend(self._backend)
            self._backend_config = None


class _CombinedCancellation:
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)


def _validate_request(plan, group, candidate_source_id):
    if not isinstance(plan, VoicePlan) or not isinstance(group, VoiceGroup):
        raise VoiceAuditionError("Voice audition inputs are invalid")
    if group not in plan.groups:
        raise VoiceAuditionError("Voice audition group is not part of this plan")
    if group.route != "needs-audition":
        raise VoiceAuditionError("Only an ambiguous voice group may be auditioned")
    candidates = tuple(
        candidate
        for candidate in group.candidates
        if candidate.source_id == candidate_source_id
    )
    if (
        not candidates
        and group.narrator_candidate is not None
        and group.narrator_candidate.source_id == candidate_source_id
    ):
        candidates = (*candidates, group.narrator_candidate)
    if len(candidates) != 1:
        raise VoiceAuditionError("Voice audition candidate is not uniquely available")
    return candidates[0]


def _preview_text(group, text):
    selected = group.sample_text if text is None else text
    if (
        not isinstance(selected, str)
        or not selected.strip()
        or selected not in {group.sample_text, group.alternate_sample_text}
    ):
        raise VoiceAuditionError("Voice audition text is invalid")
    return selected


def _load_candidate_registry(plan, candidate):
    manifest = (
        Path(plan.voice_manifest).expanduser().resolve()
        if plan.voice_manifest
        else None
    )
    if manifest is not None:
        try:
            before = sha256_file(manifest)
        except OSError as error:
            raise VoiceAuditionError(
                f"Unable to read character voices: {error}"
            ) from error
        if before != plan.voice_manifest_sha256:
            raise VoiceAuditionError("Character voices changed after planning")
    try:
        registry = (
            CharacterVoiceRegistry.from_file(manifest)
            if manifest is not None
            else CharacterVoiceRegistry()
        )
        voice = registry.resolve_source(candidate.source_id)
    except (OSError, VoiceManifestError, ValueError) as error:
        raise VoiceAuditionError(
            f"Unable to resolve the voice candidate: {error}"
        ) from error
    if voice is None:
        raise VoiceAuditionError("Narrator fallback is not a voice candidate")
    try:
        identity = VoiceCandidate(
            source_id=candidate.source_id,
            source_character=voice.character,
            source_speaker=voice.speaker,
            reference_sha256s=tuple(sha256_file(path) for path in voice.references),
        )
    except OSError as error:
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
    if manifest is not None and sha256_file(manifest) != plan.voice_manifest_sha256:
        raise VoiceAuditionError("Character voices changed while they were read")
    return CharacterVoiceRegistry((voice,))


def _preview_identity(plan, group, candidate, text):
    document = {
        "group_id": group.group_id,
        "decision_context_sha256": group.decision_context_sha256,
        "candidate": asdict(candidate),
        "text": text,
        "backend": plan.synthesis_backend,
        "model": plan.synthesis_model,
        "language": plan.synthesis_language,
        "profile": plan.synthesis_profile,
        "controls": plan.synthesis_controls_sha256,
        "seed": None if plan.synthesis_backend == "pocket-tts" else 0,
    }
    if plan.synthesis_backend == "moss-tts":
        from vntts.moss_cpp_backend import (
            NATIVE_GENERATION_CONTRACT,
            moss_cpp_requested,
        )

        if moss_cpp_requested(plan.synthesis_model):
            document["native_generation_contract"] = NATIVE_GENERATION_CONTRACT
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _native_moss_preview(plan):
    if plan.synthesis_backend != "moss-tts":
        return False
    from vntts.moss_cpp_backend import moss_cpp_requested

    return moss_cpp_requested(plan.synthesis_model)


def _preview_seed(plan, failed_attempts):
    if plan.synthesis_backend == "pocket-tts":
        return None
    if plan.synthesis_backend != "moss-tts" or failed_attempts == 0:
        return 0
    # Native maps request seed zero to one, so retrying with one repeats it.
    return failed_attempts + (1 if _native_moss_preview(plan) else 0)


def _preview_manifest_path(target):
    return target.with_suffix(".json")


def _write_preview_manifest(target, identity, seed, audio_sha256):
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


def _cached_preview_seed(target, identity, plan):
    manifest = _preview_manifest_path(target)
    if not manifest.exists():
        # Existing successful previews predate the sidecar and were all seed zero.
        return None if plan.synthesis_backend == "pocket-tts" else 0
    if manifest.is_symlink():
        raise VoiceAuditionError(
            "Cached voice preview manifest must not be a symbolic link"
        )
    try:
        if manifest.stat().st_size > 1024:
            raise ValueError("preview manifest is too large")
        payload = manifest.read_bytes()
        if len(payload) > 1024:
            raise ValueError("preview manifest is too large")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("preview manifest must be an object")
        seed = document["seed"]
        audio_sha256 = sha256_file(target)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise VoiceAuditionError(
            f"Cached voice preview manifest is invalid: {error}"
        ) from error
    if (
        document.get("identity") != identity
        or not isinstance(document.get("audio_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", document["audio_sha256"])
        or document["audio_sha256"] != audio_sha256
        or (seed is not None and (type(seed) is not int or not 0 <= seed < 2**64))
        or (seed is None) != (plan.synthesis_backend == "pocket-tts")
    ):
        raise VoiceAuditionError(
            "Cached voice preview manifest does not match its input"
        )
    return seed


def _preflight_candidate_references(registry, candidate):
    voice = registry.resolve_source(candidate.source_id)
    if voice is None:
        return
    for index, reference in enumerate(voice.references):
        try:
            report = analyze_reference(reference)
        except ValueError as error:
            raise VoiceAuditionError(
                f"Voice reference {index + 1} failed objective preflight: {error}"
            ) from error
        if report["sha256"] != candidate.reference_sha256s[index]:
            raise VoiceAuditionError(
                "Voice reference changed during objective preflight"
            )
        if report["objective_preflight"] != "pass":
            reasons = ", ".join(report["rejection_reasons"])
            raise VoiceAuditionError(
                f"Voice reference {index + 1} failed objective preflight: {reasons}"
            )


def _inspect_preview(path, text):
    audio_quality = None
    try:
        audio_quality = inspect_generated_wav(path)
        speech_quality = inspect_generated_speech(path, text=text)
    except SpeechSilenceValidationError as error:
        _record_preview_quality(
            "rejected", "speech-silence", audio_quality, error.quality
        )
        raise VoiceAuditionError(
            f"Generated voice preview failed objective preflight: {error}"
        ) from error
    except BulkGenerationError as error:
        _record_preview_quality(
            "rejected",
            "wav-validation" if audio_quality is None else "invalid-wav",
            audio_quality,
        )
        raise VoiceAuditionError(
            f"Generated voice preview failed objective preflight: {error}"
        ) from error
    _record_preview_quality("accepted", "accepted", audio_quality, speech_quality)
    return audio_quality


def _record_preview_quality(outcome, reason, audio_quality, speech_quality=None):
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
    target, identity, plan, group, candidate, text, *, reused=True, seed=Ellipsis
):
    if target.is_symlink():
        raise VoiceAuditionError("Cached voice preview must not be a symbolic link")
    try:
        info = _inspect_preview(target, text)
        audio_sha256 = sha256_file(target)
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
    if seed is Ellipsis:
        seed = _cached_preview_seed(target, identity, plan)
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


def _mono_pcm(value):
    samples = np.asarray(value, dtype=np.float32)
    if samples.ndim == 2 and samples.shape[1] in {1, 2}:
        samples = (
            samples[:, 0]
            if samples.shape[1] == 1
            else samples.mean(axis=1, dtype=np.float32)
        )
    if samples.ndim != 1 or not np.isfinite(samples).all():
        raise VoiceAuditionError("Voice preview PCM must be finite mono audio")
    return samples


def _staging_path(target):
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.stem}-",
        suffix=".wav",
        dir=target.parent,
    )
    os.close(descriptor)
    return Path(name)


def _raise_if_cancelled(cancellation):
    if cancellation.is_set():
        raise VoiceAuditionCancelled("Voice audition generation was cancelled")


__all__ = [
    "VoiceAuditionCancelled",
    "VoiceAuditionError",
    "VoiceAuditionIncomplete",
    "VoiceAuditionPreview",
    "VoiceAuditionPreviewService",
]
