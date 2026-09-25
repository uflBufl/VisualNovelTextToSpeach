"""Ahead-of-time generated audio with verified live-TTS fallback."""

from __future__ import annotations

import hashlib
import io
import json
import wave
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock, RLock
from time import monotonic
from typing import Protocol, TypeAlias, TypedDict, TypeGuard

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts.audio import Pcm16MonoWavError
from vntts_artifacts.generated_audio import (
    GeneratedAudioDocument,
    GeneratedAudioEntry,
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
    GeneratedAudioRecord,
    load_generated_audio_document,
)

from vntts.audio_cache import BoundedCache
from vntts.audio_output import (
    PlaybackAudioOutput,
    match_output_sample_rate,
    resolve_audio_output,
    write_pcm_chunks,
)
from vntts.chapter_voice_preload import ChapterDialogue
from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256
from vntts.playback import (
    PlaybackOutcome as PlaybackOutcome,
)
from vntts.playback import (
    PlaybackStatus as PlaybackStatus,
)
from vntts.playback import (
    PreparedPlayback,
)
from vntts.settings import audio_source_policies
from vntts.speech_backend_contract import SpeechBackend
from vntts.speech_backend_runtime import validate_speed, validate_volume
from vntts.voices import is_unattributed_speaker, synthesis_character

LIVE_FALLBACK_REASONS = frozenset(
    {
        "offline_fallback_exhausted",
        "reference_unavailable_after_audit",
        "generated_audio_rejected",
        "generation_hypotheses_exhausted",
        "automatic_recovery_exhausted",
    }
)
SOURCE_AUDIO_COMPLETION_MARGIN_SECONDS = 0.35


@dataclass(frozen=True)
class PreparedGeneratedAudio:
    line_id: str
    text_sha256: str
    samples: NDArray[np.float32]
    sample_rate: int
    narrator_fallback_role: str | None = None
    provider: str | None = None
    model: str | None = None
    voice_character: str | None = None
    recorded_voice: dict[str, object] | None = None


@dataclass(frozen=True)
class PreparedSourceAudioPassThrough:
    """A line whose original audio is already being played by the game."""

    line_id: str
    text_sha256: str
    source_audio_id: str | None = None
    completion_seconds: float | None = None
    completion_source: str | None = None


@dataclass(frozen=True)
class AudioRouteTrace:
    generation: int | None
    effective_source: str
    match_result: str
    fallback_reason: str | None
    voice_reference_id: str | None
    line_id: str | None
    artifact_preflight_state: str
    chunk_id: str | None = None
    chunk_ordinal: int | None = None
    chunk_characters: int | None = None

    def message(self) -> str:
        values = (
            ("generation", self.generation),
            ("source", self.effective_source),
            ("line", self.line_id),
            ("match", self.match_result),
            ("fallback", self.fallback_reason),
            ("voice-reference", self.voice_reference_id),
            ("artifact-preflight", self.artifact_preflight_state),
            ("chunk", self.chunk_id),
            ("chunk-ordinal", self.chunk_ordinal),
            ("chunk-characters", self.chunk_characters),
        )
        return "Audio route: " + "; ".join(
            f"{key}={value if value is not None else 'none'}" for key, value in values
        )

    def support_details(self) -> AudioRouteDetails:
        return {
            "effective_source": self.effective_source,
            "match_result": self.match_result,
            "fallback_reason": self.fallback_reason,
            "voice_reference_id": self.voice_reference_id,
            "line_id": self.line_id,
            "artifact_preflight_state": self.artifact_preflight_state,
            "chunk_id": self.chunk_id,
            "chunk_ordinal": self.chunk_ordinal,
            "chunk_characters": self.chunk_characters,
        }

    def support_fields(self) -> AudioRouteSupportFields:
        return {"generation": self.generation, **self.support_details()}


@dataclass(frozen=True)
class SourceAudioRoute:
    prepared: PreparedSourceAudioPassThrough
    trace: AudioRouteTrace
    synthesis_ms: float = 0.0
    first_audio_ms: float | None = None
    cache_source: str | None = None


@dataclass(frozen=True)
class GeneratedAudioRoute:
    prepared: PreparedGeneratedAudio
    trace: AudioRouteTrace
    synthesis_ms: float = 0.0
    first_audio_ms: float | None = 0.0
    cache_source: str | None = "generated-audio"
    source_audio_lead_seconds: float = 0.0


@dataclass(frozen=True)
class PendingGeneratedAudioRoute:
    line_id: str
    text_sha256: str
    text: str
    trace: AudioRouteTrace
    synthesis_ms: float = 0.0
    first_audio_ms: float | None = None
    cache_source: str | None = "generation-in-progress"


@dataclass(frozen=True)
class LiveFallbackDecision:
    schema: str
    schema_version: int
    reason: str
    provider: str
    model: str
    generation_profile: str
    queue_id: str
    line_id: str
    text_sha256: str
    speaker: str
    requested_voice_character: str
    previous_result_sha256: str | None
    decided_at: str
    decision_sha256: str
    evidence: dict[str, object] | None = None


@dataclass(frozen=True)
class LiveFallbackRoute:
    prepared: PreparedPlayback
    decision: LiveFallbackDecision
    trace: AudioRouteTrace
    synthesis_ms: float | None
    first_audio_ms: float | None
    cache_source: str | None = None
    source_audio_lead_seconds: float = 0.0


@dataclass(frozen=True)
class LiveTTSRoute:
    prepared: PreparedPlayback
    trace: AudioRouteTrace
    synthesis_ms: float | None
    first_audio_ms: float | None
    cache_source: str | None = None
    source_audio_lead_seconds: float = 0.0


@dataclass(frozen=True)
class AudioEventOmissionDecision:
    schema: str
    schema_version: int
    reason: str
    queue_id: str
    line_id: str
    text_sha256: str
    speaker: str
    plan_sha256: str
    spoken_text_sha256: str
    decided_at: str
    authority: dict[str, object]
    decision_sha256: str


@dataclass(frozen=True)
class AudioEventOmissionRoute:
    decision: AudioEventOmissionDecision
    trace: AudioRouteTrace
    synthesis_ms: float = 0.0
    first_audio_ms: float | None = None
    cache_source: str | None = "audio-event-omission"


RouteDecision = (
    SourceAudioRoute
    | GeneratedAudioRoute
    | PendingGeneratedAudioRoute
    | LiveFallbackRoute
    | LiveTTSRoute
    | AudioEventOmissionRoute
)


class AudioRouteDetails(TypedDict):
    effective_source: str
    match_result: str
    fallback_reason: str | None
    voice_reference_id: str | None
    line_id: str | None
    artifact_preflight_state: str
    chunk_id: str | None
    chunk_ordinal: int | None
    chunk_characters: int | None


class AudioRouteSupportFields(AudioRouteDetails):
    generation: int | None


GeneratedAudioEntryLike: TypeAlias = GeneratedAudioEntry | GeneratedAudioRecord
GeneratedAudioSource: TypeAlias = GeneratedAudioDocument | GeneratedAudioIndex
PlaybackGuard: TypeAlias = Callable[[], bool] | None


class _LineResolver(Protocol):
    def resolve_exact(self, character: str, text: str) -> ChapterDialogue | None: ...

    def line_for_id(self, line_id: str) -> ChapterDialogue | None: ...


class _ResultLineResolver(_LineResolver, Protocol):
    def resolve_exact_with_result(
        self, character: str, text: str
    ) -> tuple[ChapterDialogue | None, str]: ...


def _has_result_resolver(resolver: _LineResolver) -> TypeGuard[_ResultLineResolver]:
    return callable(getattr(resolver, "resolve_exact_with_result", None))


def recorded_voice_identity(
    entry: GeneratedAudioEntryLike,
) -> dict[str, object] | None:
    """Read historical identity only when it is bound to this recording and route."""
    document = getattr(entry, "document", entry)
    if not isinstance(document, dict):
        return None
    identity = document.get("vntts.recorded_voice")
    if (
        not isinstance(identity, dict)
        or type(identity.get("schema_version")) is not int
        or identity["schema_version"] != 1
    ):
        return None
    for field in ("audio_sha256", "synthesis_provenance_sha256"):
        if not is_lowercase_sha256(identity.get(field)) or identity[
            field
        ] != document.get(field):
            return None
    for field in ("provider", "model", "voice_character"):
        if (
            not isinstance(identity.get(field), str)
            or not identity[field].strip()
            or identity[field] != document.get(field)
        ):
            return None
    if any(
        not isinstance(identity.get(field), str) or not identity[field].strip()
        for field in ("source_character", "speaker")
    ):
        return None
    references = identity.get("reference_sha256s")
    if not isinstance(references, list) or any(
        not is_lowercase_sha256(value) for value in references
    ):
        return None
    return {**identity, "reference_sha256s": list(references)}


def _validate_generated_audio_paths(index: GeneratedAudioSource) -> None:
    manifest_path = getattr(index, "manifest_path", None) or getattr(index, "path")
    root = manifest_path.parent.resolve()
    for entry in index.entries:
        try:
            entry.audio.resolve().relative_to(root)
        except ValueError as error:
            raise GeneratedAudioManifestError(
                "Generated audio must stay within the manifest directory"
            ) from error


class GeneratedAudioLibrary:
    def __init__(
        self,
        index: GeneratedAudioSource,
        *,
        warn: Callable[[str], object] | None = None,
        cache_size: int = 32,
    ) -> None:
        self.warn = warn or (lambda _message: None)
        self.cache: BoundedCache[
            tuple[str, str, str], tuple[NDArray[np.float32], int]
        ] = BoundedCache(cache_size)
        self.warned_entries: set[tuple[str, str]] = set()
        self.reload_lock = Lock()
        self.failed_reload_signature: tuple[int, int, int] | None = None
        self.manifest_path = (
            getattr(index, "manifest_path", None) or getattr(index, "path")
        ).resolve()
        self.manifest_signature = _manifest_signature(self.manifest_path)
        self.progress_state_signature: tuple[int, int, int] | None = None
        self.progress_active: dict[str, object] | None = None
        self._apply_index(index)

    def _apply_index(self, index: GeneratedAudioDocument | GeneratedAudioIndex) -> None:
        _validate_generated_audio_paths(index)
        live_fallbacks = _live_fallback_index(index.metadata)
        audio_event_omissions = _audio_event_omission_index(index.metadata)
        generated_identities = {
            (entry.line_id, entry.text_sha256) for entry in index.entries
        }
        if generated_identities.intersection(audio_event_omissions):
            raise GeneratedAudioManifestError(
                "Generated audio conflicts with an audio-event omission"
            )
        narrator_fallback_roles = {
            (entry.line_id, entry.text_sha256): role
            for entry in index.entries
            if (role := _narrator_fallback_role(entry)) is not None
        }
        self.index = index
        self.runtime_progress = index.metadata.get("vntts.runtime.progress") is True
        self.live_fallbacks = live_fallbacks
        self.audio_event_omissions = audio_event_omissions
        self.narrator_fallback_roles = narrator_fallback_roles

    def _reload_if_changed(self) -> None:
        signature = _manifest_signature(self.manifest_path)
        if signature in {
            None,
            self.manifest_signature,
            self.failed_reload_signature,
        }:
            return
        with self.reload_lock:
            signature = _manifest_signature(self.manifest_path)
            if signature in {
                None,
                self.manifest_signature,
                self.failed_reload_signature,
            }:
                return
            try:
                index = load_generated_audio_document(self.manifest_path)
                if _manifest_signature(self.manifest_path) != signature:
                    return
                self._apply_index(index)
            except (GeneratedAudioManifestError, OSError, ValueError) as error:
                self.failed_reload_signature = signature
                self.warn(f"Generated audio update ignored: {error}")
                return
            self.manifest_signature = signature
            self.failed_reload_signature = None
            self.warned_entries.clear()

    @classmethod
    def load_optional(
        cls,
        path: str | Path | None,
        *,
        warn: Callable[[str], object] | None = None,
        cache_size: int = 32,
    ) -> GeneratedAudioLibrary | None:
        if not path:
            return None
        try:
            index = load_generated_audio_document(path)
            return cls(index, warn=warn, cache_size=cache_size)
        except (GeneratedAudioManifestError, ValueError) as error:
            if warn is not None:
                warn(f"Generated audio disabled: {error}")
            return None

    def find(self, line_id: str, text_sha256: str) -> PreparedGeneratedAudio | None:
        prepared, _state = self.find_with_preflight(line_id, text_sha256)
        return prepared

    def find_with_preflight(
        self, line_id: str, text_sha256: str
    ) -> tuple[PreparedGeneratedAudio | None, str]:
        self._reload_if_changed()
        with self.reload_lock:
            index = self.index
            narrator_fallback_roles = self.narrator_fallback_roles
        entry = index.find(line_id, text_sha256, verify_file=False)
        if entry is None:
            return None, "generated-audio-entry-not-found"
        narrator_fallback_role = narrator_fallback_roles.get(
            (entry.line_id, entry.text_sha256)
        )
        try:
            payload = entry.audio.read_bytes()
        except OSError:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None, "generated-audio-entry-missing"
        if hashlib.sha256(payload).hexdigest() != entry.audio_sha256:
            self._warn_once(
                entry, f"Generated audio is missing or modified: {entry.audio}"
            )
            return None, "generated-audio-checksum-failed"
        cache_key = (
            entry.line_id,
            entry.text_sha256,
            entry.audio_sha256,
        )
        cached = self.cache.get(cache_key)
        if cached is None:
            try:
                samples, sample_rate = _read_pcm16_mono_wav_bytes(payload)
            except Pcm16MonoWavError as error:
                self._warn_once(
                    entry, f"Generated audio is invalid: {entry.audio}: {error}"
                )
                return None, "generated-audio-invalid-wav"
            self.cache.put(cache_key, (samples, sample_rate))
        else:
            samples, sample_rate = cached
        if sample_rate != entry.sample_rate or len(samples) != entry.sample_count:
            self._warn_once(
                entry,
                f"Generated audio metadata does not match the WAV file: {entry.audio}",
            )
            return None, "generated-audio-metadata-mismatch"
        prepared = PreparedGeneratedAudio(
            line_id=entry.line_id,
            text_sha256=entry.text_sha256,
            samples=samples,
            sample_rate=sample_rate,
            narrator_fallback_role=narrator_fallback_role,
            provider=getattr(entry, "provider", None),
            model=getattr(entry, "model", None),
            voice_character=getattr(entry, "voice_character", None),
            recorded_voice=recorded_voice_identity(entry),
        )
        return prepared, "generated-audio-entry-verified"

    def find_live_fallback(
        self, line_id: str, text_sha256: str
    ) -> LiveFallbackDecision | None:
        self._reload_if_changed()
        return self.live_fallbacks.get((line_id, text_sha256))

    def find_audio_event_omission(
        self, line_id: str, text_sha256: str
    ) -> AudioEventOmissionDecision | None:
        self._reload_if_changed()
        return self.audio_event_omissions.get((line_id, text_sha256))

    def progress_description(self, line_id: str, text_sha256: str) -> str | None:
        if not self.runtime_progress:
            return None
        state_path = self.manifest_path.parent / "generation-state.json"
        signature = _manifest_signature(state_path)
        if signature != self.progress_state_signature:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except OSError, UnicodeError, json.JSONDecodeError:
                return None
            self.progress_state_signature = signature
            self.progress_active = (
                state.get("active") if isinstance(state, dict) else None
            )
        active = self.progress_active
        if not isinstance(active, dict):
            return "Preparing this line - choosing the next safe attempt."
        if (active.get("line_id"), active.get("text_sha256")) != (
            line_id,
            text_sha256,
        ):
            return "Preparing this line next; finishing the active dialogue first."
        attempt, limit = active.get("attempt"), active.get("attempt_limit")
        if all(type(value) is int and value > 0 for value in (attempt, limit)):
            return f"Preparing this line - attempt {attempt} of {limit}."
        return "Preparing this line now."

    def _warn_once(self, entry: GeneratedAudioEntryLike, message: str) -> None:
        identity = entry.line_id, entry.text_sha256
        if identity in self.warned_entries:
            return
        self.warned_entries.add(identity)
        self.warn(message)


def _manifest_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


def _narrator_fallback_role(entry: GeneratedAudioEntryLike) -> str | None:
    document = getattr(entry, "document", None)
    if not isinstance(document, dict):
        return None
    speaker = document.get("speaker")
    requested = document.get("requested_voice_character")
    effective = document.get("voice_character")
    fallback = document.get("synthesis_fallback")
    if is_unattributed_speaker(speaker):
        if requested == "Narrator" and effective == "Narrator" and fallback is None:
            return "Unknown"
        raise GeneratedAudioManifestError(
            "Unattributed generated audio has inconsistent Narrator provenance"
        )
    if fallback is None:
        return None
    expected_fields = {
        "schema_version",
        "kind",
        "policy",
        "source_voice_character",
        "synthesis_voice_character",
        "narrator_character",
    }
    if not isinstance(fallback, dict) or set(fallback) != expected_fields:
        raise GeneratedAudioManifestError(
            "Generated audio Narrator fallback provenance is malformed"
        )
    source = fallback.get("source_voice_character")
    policy = fallback.get("policy")
    if (
        fallback.get("schema_version") != 1
        or fallback.get("kind") != "missing_voice_to_narrator"
        or not isinstance(source, str)
        or not source.strip()
        or requested != source
        or fallback.get("synthesis_voice_character") != "Narrator"
        or effective != "Narrator"
        or not isinstance(fallback.get("narrator_character"), str)
        or not fallback["narrator_character"].strip()
        or not isinstance(policy, dict)
        or policy.get("schema_version") != 1
        or policy.get("mode") != "narrator_roles"
        or not isinstance(policy.get("roles"), list)
        or source not in policy["roles"]
    ):
        raise GeneratedAudioManifestError(
            "Generated audio Narrator fallback provenance is inconsistent"
        )
    return source.strip()


class GeneratedAudioFallbackBackend:
    """Pass through source audio, prefer local generations, then use live TTS."""

    def __init__(
        self,
        live_backend: SpeechBackend,
        library: GeneratedAudioLibrary | None,
        line_resolver: _LineResolver,
        *,
        volume: object = 1.0,
        speed: object = 1.0,
        audio_output: PlaybackAudioOutput | None = None,
        playback_latency: object = "low",
        clock: Callable[[], float] = monotonic,
        audio_source_policy: str = "prefer-generated",
    ) -> None:
        self.live_backend = live_backend
        self.library = library
        self.line_resolver = line_resolver
        self.audio_output = resolve_audio_output(audio_output)
        self.playback_latency = playback_latency
        self.clock = clock
        if audio_source_policy not in audio_source_policies:
            raise ValueError(f"Unknown audio source policy: {audio_source_policy}")
        self.audio_source_policy = audio_source_policy
        prefix = "generated-audio" if library is not None else "story-audio"
        self.name = f"{prefix}+{live_backend.name}"
        self.capabilities = live_backend.capabilities
        self.generated_preflight_lock = Lock()
        self.generated_reservations: BoundedCache[
            tuple[str, str], PreparedGeneratedAudio
        ] = BoundedCache(32)
        self.live_mode_active = False
        self.volume = 1.0
        self.speed = 1.0
        self.voice_override: Callable[[str], bool] | None = None
        self.progress_wait_status: Callable[[str], object] = lambda _message: None
        self.progress_wait_request: Callable[[str, str], object] = (
            lambda _line_id, _text_sha256: None
        )
        self.progress_line_observed: Callable[[str, str], object] = (
            lambda _line_id, _text_sha256: None
        )
        self.set_volume(volume, delegate=False)
        self.set_speed(speed, delegate=False)
        self.playback_owner = AudioRoutePlaybackOwner(self)

    def will_use_source_audio(self, character: str, text: str) -> bool:
        """Return whether live playback for this exact line stays in the game."""
        if not self.live_mode_active:
            return False
        return self._will_use_source_audio(character, text)

    def will_use_source_audio_in_live_mode(self, character: str, text: str) -> bool:
        """Read the future live route without advancing story-match authority."""
        current_match = getattr(self.line_resolver, "current_match", None)
        try:
            return self._will_use_source_audio(character, text)
        finally:
            if hasattr(self.line_resolver, "current_match"):
                setattr(self.line_resolver, "current_match", current_match)

    def has_resolved_route_in_live_mode(self, character: str, text: str) -> bool:
        """Return whether an exact line has a non-generic authorized live route."""
        current_match = getattr(self.line_resolver, "current_match", None)
        try:
            if self.voice_override is not None and self.voice_override(character):
                return False
            line = self.line_resolver.resolve_exact(character, text)
            if line is None or not line.line_id or not line.text_sha256:
                return False
            if self._will_use_source_audio(character, text):
                return True
            if self.library is None:
                return False
            if (
                self.library.find_audio_event_omission(
                    line.line_id,
                    line.text_sha256,
                )
                is not None
            ):
                return True
            if (
                self.audio_source_policy in {"prefer-generated", "prefer-game-audio"}
                and self.speed == 1.0
            ):
                prepared, _state = self.library.find_with_preflight(
                    line.line_id,
                    line.text_sha256,
                )
                if prepared is not None:
                    return True
            if self.library.runtime_progress:
                return True
            live_fallback = self.library.find_live_fallback(
                line.line_id,
                line.text_sha256,
            )
            if live_fallback is None:
                return False
            try:
                _validate_live_fallback_backend(self.live_backend, live_fallback)
            except ValueError:
                return False
            return True
        finally:
            if hasattr(self.line_resolver, "current_match"):
                setattr(self.line_resolver, "current_match", current_match)

    def _will_use_source_audio(self, character: str, text: str) -> bool:
        if self.audio_source_policy != "prefer-game-audio":
            return False
        if self.voice_override is not None and self.voice_override(character):
            return False
        line = self.line_resolver.resolve_exact(character, text)
        return bool(
            line is not None
            and line.line_id
            and line.source_audio_status == "available"
            and line.source_audio_authoritative
            and line.source_audio_completeness == "full"
            and line.source_audio_duration_seconds is not None
        )

    def has_generated_line(self, line: ChapterDialogue) -> bool:
        """Reserve a verified generation for safe early-prefix expansion."""
        if (
            self.library is None
            or self.speed != 1.0
            or not line.line_id
            or not line.text_sha256
        ):
            return False
        reservation_key = (line.line_id, line.text_sha256)
        with self.generated_preflight_lock:
            if self.generated_reservations.get(reservation_key) is not None:
                return True
            prepared, _state = self.library.find_with_preflight(
                line.line_id, line.text_sha256
            )
            if prepared is None:
                return False
            self.generated_reservations.put(reservation_key, prepared)
        return True

    def reserve_generated_line_for_early_playback(self, line: ChapterDialogue) -> bool:
        """Reserve only an exact line whose effective early route is generated.

        Prefix/cursor playback must not use the presence of a generated WAV to
        bypass an original-audio route or a manual live-voice override. The
        ordinary route builder remains authoritative once the line is queued.
        """
        if (
            self.audio_source_policy not in {"prefer-generated", "prefer-game-audio"}
            or self.speed != 1.0
            or (self.voice_override is not None and self.voice_override(line.speaker))
            or (
                self.audio_source_policy == "prefer-game-audio"
                and line.source_audio_status == "available"
                and line.source_audio_authoritative
            )
        ):
            return False
        return self.has_generated_line(line)

    def prepare_route(
        self, character: str, text: str, *, line_id: str | None = None
    ) -> RouteDecision:
        voice_overridden = self.voice_override is not None and self.voice_override(
            character
        )
        if line_id is not None and not voice_overridden:
            line = self.line_resolver.line_for_id(line_id)
            if line is not None and (line.speaker, line.text) == (character, text):
                match_result = "exact"
            else:
                line, match_result = None, "line-id-mismatch"
        else:
            line, match_result = self._resolve_line(character, text, voice_overridden)
        if (
            line is not None
            and line.line_id
            and line.text_sha256
            and self.library is not None
            and self.library.runtime_progress
        ):
            self.progress_line_observed(line.line_id, line.text_sha256)
        omission_line = line
        if omission_line is None and voice_overridden:
            omission_line = self._resolve_without_advancing(character, text)
        fallback_reasons: list[str] = []
        artifact_preflight_state = "not-applicable"
        if voice_overridden:
            fallback_reasons.append("manual-voice-override")
            artifact_preflight_state = "skipped-manual-voice-override"
        elif match_result != "exact":
            fallback_reasons.append(f"story-line-{match_result}")
        source_audio_completion = (
            float(line.source_audio_duration_seconds)
            if line is not None and line.source_audio_duration_seconds is not None
            else None
        )
        source_audio_wait = (
            source_audio_completion + SOURCE_AUDIO_COMPLETION_MARGIN_SECONDS
            if source_audio_completion is not None
            else None
        )
        source_audio_completeness = (
            line.source_audio_completeness if line is not None else "unknown"
        )
        source_audio_partial = bool(
            line is not None
            and self.audio_source_policy == "prefer-game-audio"
            and line.source_audio_status == "available"
            and line.source_audio_authoritative
            and source_audio_completion is not None
            and source_audio_completeness == "partial"
        )
        source_audio_full = bool(
            line is not None
            and line.source_audio_status == "available"
            and line.source_audio_authoritative
            and source_audio_completion is not None
            and source_audio_completeness == "full"
        )
        if (
            line is not None
            and line.line_id
            and line.text_sha256
            and self.live_mode_active
            and self.audio_source_policy == "prefer-game-audio"
            and source_audio_full
        ):
            trace = AudioRouteTrace(
                None,
                "game",
                match_result,
                None,
                None,
                line.line_id,
                "source-audio-declared-available",
            )
            return SourceAudioRoute(
                PreparedSourceAudioPassThrough(
                    line.line_id,
                    line.text_sha256,
                    line.source_audio_id,
                    (
                        source_audio_wait
                        if source_audio_completeness == "full"
                        else None
                    ),
                    (
                        "story-index+conservative-postroll"
                        if source_audio_completion is not None
                        and source_audio_completeness == "full"
                        else None
                    ),
                ),
                trace,
            )
        if self.audio_source_policy == "prefer-game-audio" and line is not None:
            if source_audio_partial:
                fallback_reasons.append("source-audio-partial-cue")
                artifact_preflight_state = "source-audio-partial-cue"
            elif line.source_audio_status == "available":
                fallback_reasons.append("source-audio-authority-unavailable")
                artifact_preflight_state = "source-audio-authority-unavailable"
            else:
                source_status = line.source_audio_status
                fallback_reasons.append(f"source-audio-{source_status}")
                artifact_preflight_state = f"source-audio-{source_status}"
        omission = (
            None
            if (
                omission_line is None
                or not omission_line.line_id
                or not omission_line.text_sha256
                or self.library is None
            )
            else self.library.find_audio_event_omission(
                omission_line.line_id, omission_line.text_sha256
            )
        )
        if omission is not None:
            trace = AudioRouteTrace(
                None,
                "audio-event-omission",
                "exact",
                f"authorized:{omission.reason}",
                None,
                omission.line_id,
                "audio-event-omission-authorized",
            )
            return AudioEventOmissionRoute(omission, trace)
        if (
            line is not None
            and line.line_id
            and line.text_sha256
            and self.audio_source_policy in {"prefer-generated", "prefer-game-audio"}
            and self.library is not None
            and self.speed == 1.0
        ):
            reservation_key = (line.line_id, line.text_sha256)
            with self.generated_preflight_lock:
                prepared = self.generated_reservations.get(reservation_key)
                if prepared is not None:
                    artifact_preflight_state = "generated-audio-entry-reserved"
                else:
                    prepared, artifact_preflight_state = (
                        self.library.find_with_preflight(
                            line.line_id,
                            line.text_sha256,
                        )
                    )
            if prepared is not None:
                trace = AudioRouteTrace(
                    None,
                    "generated",
                    match_result,
                    ";".join(fallback_reasons) or None,
                    None,
                    line.line_id,
                    artifact_preflight_state,
                )
                generated_route = GeneratedAudioRoute(prepared, trace)
                return (
                    replace(
                        generated_route,
                        source_audio_lead_seconds=source_audio_wait or 0.0,
                    )
                    if source_audio_partial
                    else generated_route
                )
            fallback_reasons.append(artifact_preflight_state)
        elif line is not None and self.audio_source_policy in {
            "prefer-generated",
            "prefer-game-audio",
        }:
            if self.library is None:
                artifact_preflight_state = "generated-audio-library-not-configured"
            elif self.speed != 1.0:
                artifact_preflight_state = "generated-audio-skipped-nondefault-speed"
            fallback_reasons.append(artifact_preflight_state)
        live_fallback = (
            None
            if (
                line is None
                or not line.line_id
                or not line.text_sha256
                or self.library is None
            )
            else self.library.find_live_fallback(line.line_id, line.text_sha256)
        )
        if (
            line is not None
            and line.line_id
            and line.text_sha256
            and self.library is not None
            and self.library.runtime_progress
            and not voice_overridden
            and live_fallback is None
        ):
            return PendingGeneratedAudioRoute(
                line.line_id,
                line.text_sha256,
                text,
                AudioRouteTrace(
                    None,
                    "waiting-for-generation",
                    match_result,
                    ";".join(dict.fromkeys(fallback_reasons)) or None,
                    None,
                    line.line_id,
                    "generation-in-progress",
                ),
            )
        line_id = line.line_id if line is not None else None
        if live_fallback is not None:
            return self._live_fallback_route(
                live_fallback,
                text=text,
                trace=AudioRouteTrace(
                    None,
                    "live-fallback",
                    match_result,
                    ";".join(dict.fromkeys(fallback_reasons)) or None,
                    None,
                    line_id,
                    artifact_preflight_state,
                ),
                source_audio_lead_seconds=(
                    source_audio_wait or 0.0 if source_audio_partial else 0.0
                ),
            )
        live_prepared = self.live_backend.prepare_playback(
            synthesis_character(character), text
        )
        effective_source = live_prepared.audio_source
        trace = AudioRouteTrace(
            None,
            effective_source,
            match_result,
            ";".join(dict.fromkeys(fallback_reasons)) or None,
            None,
            line_id,
            artifact_preflight_state,
        )
        live_route = LiveTTSRoute(
            live_prepared,
            trace,
            live_prepared.synthesis_ms,
            live_prepared.first_audio_ms,
            live_prepared.cache_source,
        )
        return (
            replace(live_route, source_audio_lead_seconds=source_audio_wait or 0.0)
            if source_audio_partial
            else live_route
        )

    def _live_fallback_route(
        self,
        decision: LiveFallbackDecision,
        *,
        text: str,
        trace: AudioRouteTrace,
        source_audio_lead_seconds: float = 0.0,
    ) -> LiveFallbackRoute:
        _validate_live_fallback_backend(self.live_backend, decision)
        if decision.schema_version == 6:
            evidence = decision.evidence
            spoken_text = evidence.get("spoken_text") if evidence is not None else None
            if not isinstance(spoken_text, str):
                raise ValueError("Generated-audio event projection text is missing")
            text = spoken_text
        prepared = self.live_backend.prepare_playback(
            decision.requested_voice_character, text
        )
        return LiveFallbackRoute(
            prepared,
            decision,
            replace(
                trace,
                effective_source="live-fallback",
                fallback_reason=";".join(
                    part
                    for part in (trace.fallback_reason, f"authorized:{decision.reason}")
                    if part
                ),
                artifact_preflight_state="live-fallback-authorized",
            ),
            prepared.synthesis_ms,
            prepared.first_audio_ms,
            prepared.cache_source,
            source_audio_lead_seconds,
        )

    def _resolve_line(
        self, character: str, text: str, voice_overridden: bool
    ) -> tuple[ChapterDialogue | None, str]:
        if voice_overridden:
            return None, "skipped"
        if _has_result_resolver(self.line_resolver):
            return self.line_resolver.resolve_exact_with_result(character, text)
        line = self.line_resolver.resolve_exact(character, text)
        return line, "exact" if line is not None else "no-match"

    def _resolve_without_advancing(
        self, character: str, text: str
    ) -> ChapterDialogue | None:
        current_match = getattr(self.line_resolver, "current_match", None)
        try:
            if _has_result_resolver(self.line_resolver):
                line, _result = self.line_resolver.resolve_exact_with_result(
                    character, text
                )
                return line
            return self.line_resolver.resolve_exact(character, text)
        finally:
            if hasattr(self.line_resolver, "current_match"):
                setattr(self.line_resolver, "current_match", current_match)

    def play_route(
        self, route: RouteDecision, *, playback_guard: PlaybackGuard = None
    ) -> PlaybackOutcome:
        return self.playback_owner.play_route(route, playback_guard=playback_guard)

    def resolved_pending_route(
        self, route: PendingGeneratedAudioRoute
    ) -> GeneratedAudioRoute | LiveFallbackRoute | None:
        if self.library is None:
            return None
        prepared, _state = self.library.find_with_preflight(
            route.line_id, route.text_sha256
        )
        if prepared is not None:
            return GeneratedAudioRoute(
                prepared,
                replace(
                    route.trace,
                    effective_source="generated",
                    artifact_preflight_state="generated-audio-entry-verified",
                ),
            )
        live_fallback = self.library.find_live_fallback(
            route.line_id, route.text_sha256
        )
        if live_fallback is None:
            return None
        return self._live_fallback_route(
            live_fallback, text=route.text, trace=route.trace
        )

    def prime(self, character: str) -> object:
        prime = getattr(self.live_backend, "prime", None)
        return prime(character) if callable(prime) else False

    def set_live_mode_active(self, active: object) -> object:
        self.live_mode_active = bool(active)
        configure = getattr(self.live_backend, "set_live_mode_active", None)
        return configure(active) if callable(configure) else self.live_mode_active

    def set_volume(self, volume: object, *, delegate: bool = True) -> float:
        self.volume = validate_volume(volume)
        configure = getattr(self.live_backend, "set_volume", None)
        if delegate and callable(configure):
            configure(self.volume)
        return self.volume

    def set_speed(self, speed: object, *, delegate: bool = True) -> float:
        self.speed = validate_speed(speed)
        configure = getattr(self.live_backend, "set_speed", None)
        if delegate and callable(configure):
            configure(self.speed)
        return self.speed

    def stop(self) -> bool:
        return self.playback_owner.stop()


class AudioRoutePlaybackOwner:
    """Own the one active output route; selection only returns frozen decisions."""

    def __init__(self, router: GeneratedAudioFallbackBackend) -> None:
        self.router = router
        self.playback_lock = RLock()
        self.source_audio_completion_stop = Event()
        self.generated_audio_stop = Event()
        self.progress_wait_stop = Event()
        self.active_generated_stream: object | None = None
        self.active_playback_source: str | None = None
        self.playback_active = False

    def play_route(
        self, route: RouteDecision, *, playback_guard: PlaybackGuard = None
    ) -> PlaybackOutcome:
        with self.playback_lock:
            return self._dispatch_route(route, playback_guard=playback_guard)

    def _dispatch_route(
        self, route: RouteDecision, *, playback_guard: PlaybackGuard = None
    ) -> PlaybackOutcome:
        """Play one immutable route and return metrics bound to that route."""
        if isinstance(route, SourceAudioRoute):
            return self._play_source_route(route, playback_guard)
        if isinstance(route, GeneratedAudioRoute):
            return self._play_generated_route(route, playback_guard)
        if isinstance(route, PendingGeneratedAudioRoute):
            return self._play_pending_generated_route(route, playback_guard)
        if isinstance(route, LiveFallbackRoute):
            return self._play_live_route(route, playback_guard)
        if isinstance(route, LiveTTSRoute):
            return self._play_live_route(route, playback_guard)
        if isinstance(route, AudioEventOmissionRoute):
            status = (
                PlaybackStatus.COMPLETED
                if playback_guard is None or playback_guard()
                else PlaybackStatus.INTERRUPTED
            )
            return _route_outcome(route, status, 0.0)
        raise TypeError(f"Unsupported audio route: {type(route).__name__}")

    def _play_pending_generated_route(
        self,
        route: PendingGeneratedAudioRoute,
        playback_guard: Callable[[], bool] | None,
    ) -> PlaybackOutcome:
        if self.router.library is None:
            raise RuntimeError("Generated-audio progress route requires a library")
        self.progress_wait_stop.clear()
        self.router.progress_wait_request(route.line_id, route.text_sha256)
        last_status = (
            self.router.library.progress_description(route.line_id, route.text_sha256)
            or "Waiting for offline preparation to finish the current dialogue..."
        )
        self.router.progress_wait_status(last_status)
        started = self.router.clock()
        self.playback_active = True
        self.active_playback_source = "preparing"
        try:
            while playback_guard is None or playback_guard():
                resolved = self.router.resolved_pending_route(route)
                if resolved is not None:
                    self.router.progress_wait_status(
                        (
                            "Prepared audio is ready; continuing reading."
                            if isinstance(resolved, GeneratedAudioRoute)
                            else "Live fallback is ready; continuing reading."
                        )
                    )
                    return self._dispatch_route(resolved, playback_guard=playback_guard)
                status = self.router.library.progress_description(
                    route.line_id, route.text_sha256
                )
                if status is not None and status != last_status:
                    last_status = status
                    self.router.progress_wait_status(status)
                if self.progress_wait_stop.wait(0.25):
                    break
            return _route_outcome(
                route,
                PlaybackStatus.INTERRUPTED,
                (self.router.clock() - started) * 1000,
            )
        finally:
            self.playback_active = False
            self.active_playback_source = None

    def _play_source_route(
        self, route: SourceAudioRoute, playback_guard: PlaybackGuard
    ) -> PlaybackOutcome:
        prepared = route.prepared
        if playback_guard is not None and not playback_guard():
            return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
        if prepared.completion_seconds is None:
            return _route_outcome(route, PlaybackStatus.PASSTHROUGH_UNOBSERVED, None)
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
            started = self.router.clock()
            self.source_audio_completion_stop.clear()
            try:
                self.playback_active = True
                self.active_playback_source = "game"
                interrupted = self.source_audio_completion_stop.wait(
                    prepared.completion_seconds
                )
                playable = playback_guard is None or bool(playback_guard())
                status = (
                    PlaybackStatus.INTERRUPTED
                    if interrupted or not playable
                    else PlaybackStatus.COMPLETED
                )
                return _route_outcome(
                    route,
                    status,
                    (self.router.clock() - started) * 1000,
                )
            finally:
                self.playback_active = False
                self.active_playback_source = None

    def _play_generated_route(
        self, route: GeneratedAudioRoute, playback_guard: PlaybackGuard
    ) -> PlaybackOutcome:
        if playback_guard is not None and not playback_guard():
            return _route_outcome(
                route,
                PlaybackStatus.INTERRUPTED,
                None,
            )
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return _route_outcome(
                    route,
                    PlaybackStatus.INTERRUPTED,
                    None,
                )
            started = self.router.clock()
            self.generated_audio_stop.clear()
            try:
                self.playback_active = True
                if not self._wait_for_source_audio_lead(route, playback_guard):
                    return _route_outcome(
                        route,
                        PlaybackStatus.INTERRUPTED,
                        (self.router.clock() - started) * 1000,
                    )
                self.active_playback_source = "generated"
                samples = (
                    np.asarray(route.prepared.samples, dtype=np.float32)
                    * self.router.volume
                )
                samples, sample_rate = match_output_sample_rate(
                    self.router.audio_output,
                    samples,
                    route.prepared.sample_rate,
                )
                sample_count = int(len(samples))
                expected_playback_ms = sample_count * 1000 / sample_rate
                if self.generated_audio_stop.is_set() or (
                    playback_guard is not None and not playback_guard()
                ):
                    return _route_outcome(
                        route,
                        PlaybackStatus.INTERRUPTED,
                        (self.router.clock() - started) * 1000,
                        source_sample_rate=route.prepared.sample_rate,
                        playback_sample_rate=sample_rate,
                        sample_count=sample_count,
                        expected_playback_ms=expected_playback_ms,
                    )
                stream_factory = getattr(self.router.audio_output, "OutputStream", None)
                if callable(stream_factory):
                    with stream_factory(
                        samplerate=sample_rate,
                        channels=1,
                        dtype="float32",
                        latency=self.router.playback_latency,
                    ) as stream:
                        self.active_generated_stream = stream
                        completed, underflowed = write_pcm_chunks(
                            stream,
                            samples,
                            sample_rate,
                            lambda: (
                                self.generated_audio_stop.is_set()
                                or (playback_guard is not None and not playback_guard())
                            ),
                        )
                else:
                    self.router.audio_output.play(
                        samples,
                        sample_rate,
                        latency=self.router.playback_latency,
                    )
                    status = self.router.audio_output.wait()
                    underflowed = bool(getattr(status, "output_underflow", False))
                    completed = not self.generated_audio_stop.is_set()
                playable = playback_guard is None or bool(playback_guard())
                playback_status = (
                    PlaybackStatus.INTERRUPTED
                    if not completed or not playable
                    else PlaybackStatus.COMPLETED
                )
                return _route_outcome(
                    route,
                    playback_status,
                    (self.router.clock() - started) * 1000,
                    underflowed=underflowed,
                    first_audio_ms=route.first_audio_ms,
                    source_sample_rate=route.prepared.sample_rate,
                    playback_sample_rate=sample_rate,
                    sample_count=sample_count,
                    expected_playback_ms=expected_playback_ms,
                )
            except Exception as error:
                return _route_outcome(
                    route,
                    PlaybackStatus.FAILED,
                    (self.router.clock() - started) * 1000,
                    first_audio_ms=route.first_audio_ms,
                    error=str(error),
                )
            finally:
                self.active_generated_stream = None
                self.playback_active = False
                self.active_playback_source = None

    def _play_live_route(
        self,
        route: LiveFallbackRoute | LiveTTSRoute,
        playback_guard: PlaybackGuard,
    ) -> PlaybackOutcome:
        if playback_guard is not None and not playback_guard():
            return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
        lead_ms = 0.0
        if route.source_audio_lead_seconds > 0:
            lead_started = self.router.clock()
            self.playback_active = True
            try:
                if not self._wait_for_source_audio_lead(route, playback_guard):
                    return _route_outcome(
                        route,
                        PlaybackStatus.INTERRUPTED,
                        (self.router.clock() - lead_started) * 1000,
                    )
            finally:
                self.playback_active = False
                self.active_playback_source = None
            lead_ms = (self.router.clock() - lead_started) * 1000
        self.playback_active = True
        self.active_playback_source = "live"
        try:
            outcome = self.router.live_backend.play_prepared(
                route.prepared,
                playback_guard=playback_guard,
            )
        except Exception as error:
            return _route_outcome(
                route,
                PlaybackStatus.FAILED,
                None,
                first_audio_ms=route.first_audio_ms,
                error=str(error),
            )
        finally:
            self.playback_active = False
            self.active_playback_source = None
        return replace(
            outcome,
            audio_source=route.trace.effective_source,
            playback_ms=(
                None if outcome.playback_ms is None else outcome.playback_ms + lead_ms
            ),
            first_audio_ms=(
                None
                if outcome.first_audio_ms is None
                else outcome.first_audio_ms + lead_ms
            ),
        )

    def _wait_for_source_audio_lead(
        self,
        route: GeneratedAudioRoute | LiveFallbackRoute | LiveTTSRoute,
        playback_guard: PlaybackGuard,
    ) -> bool:
        seconds = float(getattr(route, "source_audio_lead_seconds", 0.0) or 0.0)
        if seconds <= 0:
            return playback_guard is None or bool(playback_guard())
        self.active_playback_source = "game"
        self.source_audio_completion_stop.clear()
        interrupted = self.source_audio_completion_stop.wait(seconds)
        return not interrupted and (playback_guard is None or bool(playback_guard()))

    def stop(self) -> bool:
        was_playing = self.playback_active
        self.progress_wait_stop.set()
        if self.active_playback_source == "game":
            self.source_audio_completion_stop.set()
        elif self.active_playback_source == "generated":
            self.generated_audio_stop.set()
        return bool(self.router.live_backend.stop()) or was_playing


def _live_fallback_index(
    metadata: dict[str, object],
) -> dict[tuple[str, str], LiveFallbackDecision]:
    value = metadata.get("vntts.authoring.live_fallback")
    if value is None:
        return {}
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "mode", "entries"}
        or value.get("schema_version") != 1
        or value.get("mode") != "explicit"
        or not isinstance(value.get("entries"), list)
    ):
        raise ValueError("Generated-audio live fallback ledger is malformed")
    common_fields = {
        "schema",
        "schema_version",
        "reason",
        "provider",
        "model",
        "generation_profile",
        "queue_id",
        "line_id",
        "text_sha256",
        "speaker",
        "requested_voice_character",
        "previous_result_sha256",
        "decided_at",
        "decision_sha256",
    }
    indexed: dict[tuple[str, str], LiveFallbackDecision] = {}
    for raw in value["entries"]:
        version = raw.get("schema_version") if isinstance(raw, dict) else None
        fields = common_fields | (
            {"evidence"} if version in {2, 3, 4, 5, 6, 7, 8} else set()
        )
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("Generated-audio live fallback entry is malformed")
        for field in fields - {
            "schema_version",
            "text_sha256",
            "previous_result_sha256",
            "decision_sha256",
            "evidence",
        }:
            if not isinstance(raw[field], str) or not raw[field].strip():
                raise ValueError(
                    "Generated-audio live fallback text fields must be non-empty"
                )
        if raw["schema"] != "vntts.authoring-live-fallback-decision" or version not in {
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
        }:
            raise ValueError("Generated-audio live fallback schema is unsupported")
        for field in ("text_sha256", "decision_sha256"):
            value_hash = raw[field]
            if (
                not isinstance(value_hash, str)
                or len(value_hash) != 64
                or any(character not in "0123456789abcdef" for character in value_hash)
            ):
                raise ValueError(
                    "Generated-audio live fallback hashes must be lowercase SHA-256"
                )
        previous = raw["previous_result_sha256"]
        if previous is not None and (
            not isinstance(previous, str)
            or len(previous) != 64
            or any(character not in "0123456789abcdef" for character in previous)
        ):
            raise ValueError(
                "Generated-audio previous-result hash must be lowercase SHA-256"
            )
        if (
            raw["reason"] not in LIVE_FALLBACK_REASONS
            or raw["provider"] != "pocket-tts"
            or raw["model"] != "pocket-tts"
            or raw["generation_profile"] != "default"
        ):
            raise ValueError("Generated-audio live fallback policy is unsupported")
        if version == 4:
            if raw["reason"] != "reference_unavailable_after_audit":
                raise ValueError(
                    "Generated-audio missing-voice fallback reason is unsupported"
                )
            _validate_missing_voice_live_fallback_evidence(
                raw["evidence"],
                raw["queue_id"],
                raw["requested_voice_character"],
            )
        elif version == 5:
            if raw["reason"] != "generation_hypotheses_exhausted":
                raise ValueError(
                    "Generated-audio known-role fallback reason is unsupported"
                )
            _validate_known_role_live_fallback_evidence(
                raw["evidence"],
                raw["queue_id"],
                raw["speaker"],
                raw["requested_voice_character"],
            )
        elif version == 6:
            if raw["reason"] != "generated_audio_rejected":
                raise ValueError(
                    "Generated-audio event projection fallback reason is unsupported"
                )
            _validate_audio_event_projection_fallback_evidence(
                raw["evidence"],
                raw["queue_id"],
                raw["speaker"],
                raw["requested_voice_character"],
                raw["previous_result_sha256"],
            )
        elif version == 7:
            if raw["reason"] != "generated_audio_rejected":
                raise ValueError(
                    "Generated-audio reviewed rejection reason is unsupported"
                )
            _validate_reviewed_rejection_fallback_evidence(
                raw["evidence"],
                raw["queue_id"],
                raw["speaker"],
                raw["requested_voice_character"],
                raw["previous_result_sha256"],
            )
        elif version == 8:
            if raw["reason"] != "automatic_recovery_exhausted":
                raise ValueError(
                    "Generated-audio automatic recovery reason is unsupported"
                )
            _validate_automatic_recovery_fallback_evidence(
                raw["evidence"],
                raw["queue_id"],
                raw["previous_result_sha256"],
            )
        elif version in {2, 3}:
            if raw["reason"] != "generation_hypotheses_exhausted":
                raise ValueError(
                    "Generated-audio evidence fallback reason is unsupported"
                )
            _validate_live_fallback_evidence(
                raw["evidence"], raw["previous_result_sha256"]
            )
        elif raw["reason"] == "generation_hypotheses_exhausted":
            raise ValueError("Generated-audio live fallback evidence is missing")
        decision_document = {
            key: value for key, value in raw.items() if key != "decision_sha256"
        }
        decision_sha256 = hashlib.sha256(
            json.dumps(
                decision_document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if decision_sha256 != raw["decision_sha256"]:
            raise ValueError("Generated-audio live fallback decision hash changed")
        decision = LiveFallbackDecision(
            schema=str(raw["schema"]),
            schema_version=int(version),
            reason=str(raw["reason"]),
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            generation_profile=str(raw["generation_profile"]),
            queue_id=str(raw["queue_id"]),
            line_id=str(raw["line_id"]),
            text_sha256=str(raw["text_sha256"]),
            speaker=str(raw["speaker"]),
            requested_voice_character=str(raw["requested_voice_character"]),
            previous_result_sha256=(str(previous) if previous is not None else None),
            decided_at=str(raw["decided_at"]),
            decision_sha256=str(raw["decision_sha256"]),
            evidence=(
                raw["evidence"]
                if version != 1 and isinstance(raw["evidence"], dict)
                else None
            ),
        )
        identity = decision.line_id, decision.text_sha256
        if identity in indexed:
            raise ValueError("Generated-audio live fallback identity is duplicated")
        indexed[identity] = decision
    return indexed


def _validate_automatic_recovery_fallback_evidence(
    evidence: object,
    queue_id: str,
    previous_result_sha256: str | None,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "queue_sha256",
        "queue_id",
        "base_result_sha256",
        "base_result",
        "recovery_action",
        "failure_kind",
    }
    base_result = evidence.get("base_result") if isinstance(evidence, dict) else None
    failure = base_result.get("failure") if isinstance(base_result, dict) else None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema")
        != "vntts.self-service-automatic-recovery-live-fallback-evidence"
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("base_result_sha256") != previous_result_sha256
        or not is_lowercase_sha256(evidence.get("queue_sha256"))
        or not is_lowercase_sha256(evidence.get("base_result_sha256"))
        or not isinstance(base_result, dict)
        or base_result.get("status") != "failed"
        or not (
            base_result.get("provider") == "pocket-tts"
            and base_result.get("model") == "pocket-tts"
            and base_result.get("generation_profile") == "default"
            or base_result.get("provider") == "moss-tts"
            and isinstance(failure, dict)
            and failure.get("kind") in {"missed_eos_audio_limit", "speech_silence"}
        )
        or not isinstance(failure, dict)
        or failure.get("kind") != evidence.get("failure_kind")
        or failure.get("kind") in {"cancelled", "interrupted"}
        or evidence.get("recovery_action")
        not in {
            "bounded_seed_retry",
            "offline_fallback_backend",
            "inline_pause_marker_comparison",
            "reference_comparison",
            "reference_discovery",
            "backend_diagnosis",
            "provenance_recovery_or_regeneration",
        }
    ):
        raise ValueError("Generated-audio automatic recovery evidence is malformed")
    base_result_sha256 = hashlib.sha256(
        json.dumps(
            base_result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if base_result_sha256 != evidence["base_result_sha256"]:
        raise ValueError("Generated-audio automatic recovery evidence changed")


def _audio_event_omission_index(
    metadata: dict[str, object],
) -> dict[tuple[str, str], AudioEventOmissionDecision]:
    value = metadata.get("vntts.authoring.audio_event_omission")
    if value is None:
        return {}
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "mode", "entries"}
        or value.get("schema_version") != 1
        or value.get("mode") != "explicit"
        or not isinstance(value.get("entries"), list)
    ):
        raise ValueError("Generated-audio audio-event omission ledger is malformed")
    fields = {
        "schema",
        "schema_version",
        "reason",
        "queue_id",
        "line_id",
        "text_sha256",
        "speaker",
        "plan_sha256",
        "spoken_text_sha256",
        "decided_at",
        "authority",
        "decision_sha256",
    }
    authority_fields = {
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
    }
    indexed: dict[tuple[str, str], AudioEventOmissionDecision] = {}
    for raw in value["entries"]:
        authority = raw.get("authority") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or set(raw) != fields
            or raw.get("schema") != "vntts.authoring-audio-event-omission"
            or raw.get("schema_version") != 1
            or raw.get("reason") != "no_validated_source_or_supported_generator"
            or not isinstance(authority, dict)
            or set(authority) != authority_fields
        ):
            raise ValueError("Generated-audio audio-event omission entry is malformed")
        if any(
            not isinstance(raw.get(field), str) or not raw[field].strip()
            for field in ("queue_id", "line_id", "speaker", "decided_at")
        ) or (
            not isinstance(authority.get("base_workspace_id"), str)
            or not authority["base_workspace_id"].strip()
        ):
            raise ValueError(
                "Generated-audio audio-event omission text fields are malformed"
            )
        if any(
            not is_lowercase_sha256(raw.get(field))
            for field in (
                "text_sha256",
                "plan_sha256",
                "spoken_text_sha256",
                "decision_sha256",
            )
        ) or any(
            not is_lowercase_sha256(authority.get(field))
            for field in (
                "batch_id",
                "base_workspace_sha256",
                "base_state_sha256",
                "queue_sha256",
            )
        ):
            raise ValueError(
                "Generated-audio audio-event omission hashes are malformed"
            )
        decision_document = {
            key: field_value
            for key, field_value in raw.items()
            if key != "decision_sha256"
        }
        if canonical_document_sha256(decision_document) != raw["decision_sha256"]:
            raise ValueError(
                "Generated-audio audio-event omission decision checksum changed"
            )
        decision = AudioEventOmissionDecision(**raw)
        identity = decision.line_id, decision.text_sha256
        if identity in indexed:
            raise ValueError("Generated-audio audio-event omission is duplicated")
        indexed[identity] = decision
    return indexed


def _validate_live_fallback_evidence(
    evidence: object, previous_result_sha256: str | None
) -> None:
    if isinstance(evidence, dict) and evidence.get("schema_version") == 2:
        return _validate_render_review_fallback_evidence(
            evidence, previous_result_sha256
        )
    fields = {
        "schema",
        "schema_version",
        "queue_sha256",
        "base_result_sha256",
        "hypotheses",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != "vntts.authoring-live-fallback-evidence"
        or evidence.get("schema_version") != 1
        or evidence.get("base_result_sha256") != previous_result_sha256
        or not is_lowercase_sha256(evidence.get("queue_sha256"))
        or not is_lowercase_sha256(evidence.get("base_result_sha256"))
    ):
        raise ValueError("Generated-audio live fallback evidence is malformed")
    hypotheses = evidence.get("hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses:
        raise ValueError("Generated-audio live fallback evidence is empty")
    order = []
    hypothesis_fields = {
        "workspace_id",
        "workspace_sha256",
        "state_sha256",
        "queue_sha256",
        "result_sha256",
        "strategy",
        "result",
    }
    for hypothesis in hypotheses:
        if (
            not isinstance(hypothesis, dict)
            or set(hypothesis) != hypothesis_fields
            or not isinstance(hypothesis.get("workspace_id"), str)
            or not hypothesis["workspace_id"].startswith("resume-")
            or hypothesis.get("strategy") != "sentence_boundary_segmentation"
            or hypothesis.get("queue_sha256") != evidence["queue_sha256"]
            or any(
                not is_lowercase_sha256(hypothesis.get(field))
                for field in ("workspace_sha256", "state_sha256", "result_sha256")
            )
            or not isinstance(hypothesis.get("result"), dict)
            or canonical_document_sha256(hypothesis["result"])
            != hypothesis["result_sha256"]
        ):
            raise ValueError(
                "Generated-audio live fallback evidence hypothesis is malformed"
            )
        result = hypothesis["result"]
        repair = result.get("failure_repair")
        carry = result.get("carry_forward")
        if (
            result.get("status") != "failed"
            or not isinstance(result.get("failure"), dict)
            or not isinstance(repair, dict)
            or repair.get("strategy") != hypothesis["strategy"]
            or not isinstance(carry, dict)
            or carry.get("source_item_sha256") != evidence["base_result_sha256"]
        ):
            raise ValueError(
                "Generated-audio live fallback evidence result is inconsistent"
            )
        order.append((hypothesis["workspace_id"], hypothesis["result_sha256"]))
    if order != sorted(order) or len(order) != len(set(order)):
        raise ValueError(
            "Generated-audio live fallback evidence hypotheses are not canonical"
        )


def _validate_missing_voice_live_fallback_evidence(
    evidence: object, queue_id: str, requested_voice_character: str
) -> None:
    fields = {
        "schema",
        "schema_version",
        "authority_bundle_id",
        "authority_bundle_sha256",
        "authority_decision_id",
        "authority_decision_sha256",
        "plan_id",
        "source_workspace_id",
        "source_workspace_sha256",
        "cohort_id",
        "queue_id",
        "decision_origin",
        "requested_voice_character",
        "configured_narrator_character",
        "batch_id",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema")
        != "vntts.authoring-missing-voice-live-fallback-evidence"
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("requested_voice_character") != requested_voice_character
        or evidence.get("decision_origin") != "automatic_no_complete_candidate"
    ):
        raise ValueError("Generated-audio missing-voice fallback evidence is malformed")
    for field in (
        "authority_bundle_id",
        "authority_bundle_sha256",
        "authority_decision_id",
        "authority_decision_sha256",
        "plan_id",
        "source_workspace_sha256",
        "cohort_id",
        "batch_id",
    ):
        if not is_lowercase_sha256(evidence.get(field)):
            raise ValueError("Generated-audio missing-voice fallback hash is malformed")
    for field in (
        "source_workspace_id",
        "queue_id",
        "requested_voice_character",
        "configured_narrator_character",
    ):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError("Generated-audio missing-voice fallback text is malformed")


def _validate_known_role_live_fallback_evidence(
    evidence: object,
    queue_id: str,
    source_character: str,
    synthesis_character: str,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "queue_id",
        "voice_manifest_sha256",
        "route_binding_sha256",
        "queue_voice_overrides_sha256",
        "source_character",
        "synthesis_character",
        "evidence_workspace_id",
        "evidence_workspace_sha256",
        "evidence_state_sha256",
        "evidence_item_sha256",
        "evidence_item",
    }
    item = evidence.get("evidence_item") if isinstance(evidence, dict) else None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != "vntts.authoring-known-role-live-fallback-evidence"
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("source_character") != source_character
        or evidence.get("synthesis_character") != synthesis_character
        or not isinstance(item, dict)
        or item.get("status") != "failed"
        or canonical_document_sha256(item) != evidence.get("evidence_item_sha256")
    ):
        raise ValueError("Generated-audio known-role fallback evidence is malformed")
    for field in (
        "batch_id",
        "voice_manifest_sha256",
        "route_binding_sha256",
        "queue_voice_overrides_sha256",
        "evidence_workspace_sha256",
        "evidence_state_sha256",
        "evidence_item_sha256",
    ):
        if not is_lowercase_sha256(evidence.get(field)):
            raise ValueError("Generated-audio known-role fallback hash is malformed")
    for field in (
        "source_character",
        "synthesis_character",
        "evidence_workspace_id",
    ):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError("Generated-audio known-role fallback text is malformed")


def _validate_audio_event_projection_fallback_evidence(
    evidence: object,
    queue_id: str,
    source_character: str,
    synthesis_character: str,
    previous_result_sha256: str | None,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "queue_id",
        "base_result_sha256",
        "base_result",
        "plan_sha256",
        "spoken_text",
        "spoken_text_sha256",
        "source_character",
        "synthesis_character",
    }
    base_result = evidence.get("base_result") if isinstance(evidence, dict) else None
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema")
        != "vntts.authoring-audio-event-projection-live-fallback-evidence"
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("source_character") != source_character
        or evidence.get("synthesis_character") != synthesis_character
        or synthesis_character != "Narrator"
        or evidence.get("base_result_sha256") != previous_result_sha256
        or not isinstance(base_result, dict)
        or base_result.get("status") != "generated"
        or base_result.get("review_status") != "rejected"
        or isinstance(base_result.get("live_fallback"), dict)
        or canonical_document_sha256(base_result) != evidence.get("base_result_sha256")
    ):
        raise ValueError(
            "Generated-audio event projection fallback evidence is malformed"
        )
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "base_result_sha256",
        "plan_sha256",
        "spoken_text_sha256",
    ):
        if not is_lowercase_sha256(evidence.get(field)):
            raise ValueError(
                "Generated-audio event projection fallback hash is malformed"
            )
    for field in (
        "base_workspace_id",
        "spoken_text",
        "source_character",
        "synthesis_character",
    ):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(
                "Generated-audio event projection fallback text is malformed"
            )
    if (
        hashlib.sha256(evidence["spoken_text"].encode("utf-8")).hexdigest()
        != evidence["spoken_text_sha256"]
    ):
        raise ValueError("Generated-audio event spoken projection changed")


def _validate_reviewed_rejection_fallback_evidence(
    evidence: object,
    queue_id: str,
    source_character: str,
    synthesis_character: str,
    previous_result_sha256: str | None,
) -> None:
    fields = {
        "schema",
        "schema_version",
        "batch_id",
        "base_workspace_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "queue_id",
        "base_result_sha256",
        "base_result",
        "source_character",
        "synthesis_character",
        "route_source",
        "route_reference_sha256s",
    }
    base_result = evidence.get("base_result") if isinstance(evidence, dict) else None
    references = (
        evidence.get("route_reference_sha256s") if isinstance(evidence, dict) else None
    )
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema")
        != "vntts.authoring-reviewed-rejection-live-fallback-evidence"
        or evidence.get("schema_version") != 1
        or evidence.get("queue_id") != queue_id
        or evidence.get("source_character") != source_character
        or evidence.get("synthesis_character") != synthesis_character
        or evidence.get("base_result_sha256") != previous_result_sha256
        or evidence.get("route_source") not in {"config_rebase", "voice_manifest"}
        or not isinstance(base_result, dict)
        or base_result.get("status") != "generated"
        or base_result.get("review_status") != "rejected"
        or isinstance(base_result.get("live_fallback"), dict)
        or canonical_document_sha256(base_result) != evidence.get("base_result_sha256")
        or not isinstance(references, list)
        or not references
        or references != sorted(set(references))
    ):
        raise ValueError(
            "Generated-audio reviewed-rejection fallback evidence is malformed"
        )
    for field in (
        "batch_id",
        "base_workspace_sha256",
        "base_state_sha256",
        "queue_sha256",
        "voice_manifest_sha256",
        "base_result_sha256",
    ):
        if not is_lowercase_sha256(evidence.get(field)):
            raise ValueError(
                "Generated-audio reviewed-rejection fallback hash is malformed"
            )
    for field in ("base_workspace_id", "source_character", "synthesis_character"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(
                "Generated-audio reviewed-rejection fallback text is malformed"
            )
    if any(not is_lowercase_sha256(digest) for digest in references):
        raise ValueError(
            "Generated-audio reviewed-rejection reference hash is malformed"
        )
    if evidence["route_source"] == "config_rebase":
        rebase = base_result.get("config_rebase")
        if (
            not isinstance(rebase, dict)
            or rebase.get("target_route_status") != "active"
            or rebase.get("target_effective_character") != synthesis_character
            or sorted(set(rebase.get("target_reference_sha256s", []))) != references
        ):
            raise ValueError("Generated-audio reviewed-rejection config route changed")
    elif base_result.get("voice_character") != synthesis_character:
        raise ValueError("Generated-audio reviewed-rejection manifest route changed")


def _validate_render_review_fallback_evidence(
    evidence: object, previous_result_sha256: str | None
) -> None:
    fields = {
        "schema",
        "schema_version",
        "queue_sha256",
        "base_result_sha256",
        "hypotheses",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != fields
        or evidence.get("schema") != "vntts.authoring-live-fallback-evidence"
        or evidence.get("schema_version") != 2
        or evidence.get("base_result_sha256") != previous_result_sha256
        or not is_lowercase_sha256(evidence.get("queue_sha256"))
        or not is_lowercase_sha256(evidence.get("base_result_sha256"))
    ):
        raise ValueError("Generated-audio live fallback review evidence is malformed")
    hypotheses = evidence.get("hypotheses")
    if not isinstance(hypotheses, list) or not hypotheses:
        raise ValueError("Generated-audio live fallback review evidence is empty")
    hypothesis_fields = {
        "kind",
        "review_id",
        "review_sha256",
        "review_document_sha256",
        "decision_sha256",
        "decision_document_sha256",
        "comparison_sha256",
        "arm_report_sha256",
        "reference_sha256",
        "result_sha256",
        "decision",
        "review",
        "decision_document",
    }
    order = []
    for hypothesis in hypotheses:
        if (
            not isinstance(hypothesis, dict)
            or set(hypothesis) != hypothesis_fields
            or hypothesis.get("kind") != "render_hypothesis_review"
            or hypothesis.get("decision") != "need_different"
            or not isinstance(hypothesis.get("review"), dict)
            or not isinstance(hypothesis.get("decision_document"), dict)
            or any(
                not is_lowercase_sha256(hypothesis.get(field))
                for field in hypothesis_fields
                - {"kind", "decision", "review", "decision_document"}
            )
        ):
            raise ValueError(
                "Generated-audio live fallback render-review hypothesis is malformed"
            )
        review = hypothesis["review"]
        decision = hypothesis["decision_document"]
        if (
            canonical_document_sha256(review) != hypothesis["review_document_sha256"]
            or canonical_document_sha256(decision)
            != hypothesis["decision_document_sha256"]
            or review.get("review_id") != hypothesis["review_id"]
            or review.get("comparison_sha256") != hypothesis["comparison_sha256"]
            or review.get("arm_report_sha256") != hypothesis["arm_report_sha256"]
            or review.get("reference_sha256") != hypothesis["reference_sha256"]
            or review.get("result_sha256") != hypothesis["result_sha256"]
            or decision.get("schema") != "vntts.authoring-render-hypothesis-decision"
            or decision.get("schema_version") != 1
            or decision.get("review_id") != hypothesis["review_id"]
            or decision.get("review_sha256") != hypothesis["review_sha256"]
            or decision.get("reference_sha256") != hypothesis["reference_sha256"]
            or decision.get("result_sha256") != hypothesis["result_sha256"]
            or decision.get("decision") != hypothesis["decision"]
        ):
            raise ValueError(
                "Generated-audio live fallback render-review authority changed"
            )
        order.append((hypothesis["kind"], hypothesis["review_id"]))
    if order != sorted(order) or len(order) != len(set(order)):
        raise ValueError(
            "Generated-audio live fallback render-review hypotheses are not canonical"
        )


def _validate_live_fallback_backend(
    backend: SpeechBackend, decision: LiveFallbackDecision
) -> None:
    provider = getattr(backend, "name", None)
    model = (
        getattr(backend, "model_identity", None)
        or getattr(backend, "model_name", None)
        or provider
    )
    profile = getattr(backend, "generation_profile", None)
    if (
        provider != decision.provider
        or str(model) != decision.model
        or profile != decision.generation_profile
    ):
        raise ValueError(
            "Configured live backend differs from the authorized fallback decision"
        )


def _read_pcm16_mono_wav_bytes(
    payload: bytes,
) -> tuple[NDArray[np.float32], int]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getnchannels() != 1:
                raise Pcm16MonoWavError("WAV must be mono")
            if source.getsampwidth() != 2:
                raise Pcm16MonoWavError("WAV must contain 16-bit PCM")
            if source.getcomptype() != "NONE":
                raise Pcm16MonoWavError("WAV must contain uncompressed PCM")
            sample_rate = source.getframerate()
            sample_count = source.getnframes()
            if sample_rate <= 0:
                raise Pcm16MonoWavError("WAV sample rate must be positive")
            frames = source.readframes(sample_count)
    except (EOFError, wave.Error) as error:
        raise Pcm16MonoWavError(str(error)) from error
    if len(frames) != sample_count * 2:
        raise Pcm16MonoWavError("WAV frame data is truncated")
    pcm: NDArray[np.int16] = np.frombuffer(frames, dtype="<i2")
    samples = np.asarray(pcm, dtype=np.float32) / 32768.0
    return samples, sample_rate


def _route_outcome(
    route: RouteDecision,
    status: PlaybackStatus,
    playback_ms: float | None,
    *,
    underflowed: bool = False,
    generation_limited: bool = False,
    first_audio_ms: float | None = None,
    error: str | None = None,
    source_sample_rate: int | None = None,
    playback_sample_rate: int | None = None,
    sample_count: int | None = None,
    expected_playback_ms: float | None = None,
) -> PlaybackOutcome:
    return PlaybackOutcome(
        status,
        playback_ms,
        underflowed=underflowed,
        generation_limited=generation_limited,
        first_audio_ms=first_audio_ms,
        error=error,
        synthesis_ms=route.synthesis_ms,
        cache_source=route.cache_source,
        audio_source=route.trace.effective_source,
        source_sample_rate=source_sample_rate,
        playback_sample_rate=playback_sample_rate,
        sample_count=sample_count,
        expected_playback_ms=expected_playback_ms,
    )
