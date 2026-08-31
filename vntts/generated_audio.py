"""Ahead-of-time generated audio with verified live-TTS fallback."""

from __future__ import annotations

import hashlib
import io
import json
import wave
from dataclasses import dataclass, replace
from threading import Event, Lock
from time import monotonic

import numpy as np
from vntts_artifacts.audio import Pcm16MonoWavError
from vntts_artifacts.generated_audio import (
    GeneratedAudioManifestError,
    load_generated_audio_document,
)

from vntts.playback import PlaybackOutcome, PlaybackStatus
from vntts.services.tts_engine import match_output_sample_rate
from vntts.settings import audio_source_policies
from vntts.speech_backend_runtime import BoundedCache, validate_speed, validate_volume
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
    samples: np.ndarray
    sample_rate: int
    narrator_fallback_role: str | None = None


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

    def message(self):
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

    def support_fields(self):
        return {
            "generation": self.generation,
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
    evidence: dict | None = None


@dataclass(frozen=True)
class LiveFallbackRoute:
    prepared: object
    decision: LiveFallbackDecision
    trace: AudioRouteTrace
    synthesis_ms: float | None
    first_audio_ms: float | None
    cache_source: str | None = None
    source_audio_lead_seconds: float = 0.0


@dataclass(frozen=True)
class LiveTTSRoute:
    prepared: object
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
    authority: dict
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
    | LiveFallbackRoute
    | LiveTTSRoute
    | AudioEventOmissionRoute
)


def _validate_generated_audio_paths(index):
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
    def __init__(self, index, *, warn=None, cache_size=32):
        _validate_generated_audio_paths(index)
        self.index = index
        self.warn = warn or (lambda _message: None)
        self.cache = BoundedCache(cache_size)
        self.warned_entries = set()
        self.live_fallbacks = _live_fallback_index(index.metadata)
        self.audio_event_omissions = _audio_event_omission_index(index.metadata)
        generated_identities = {
            (entry.line_id, entry.text_sha256) for entry in index.entries
        }
        if generated_identities.intersection(self.audio_event_omissions):
            raise GeneratedAudioManifestError(
                "Generated audio conflicts with an audio-event omission"
            )
        self.narrator_fallback_roles = {
            (entry.line_id, entry.text_sha256): role
            for entry in index.entries
            if (role := _narrator_fallback_role(entry)) is not None
        }

    @classmethod
    def load_optional(cls, path, *, warn=None, cache_size=32):
        if not path:
            return None
        try:
            index = load_generated_audio_document(path)
            return cls(index, warn=warn, cache_size=cache_size)
        except (GeneratedAudioManifestError, ValueError) as error:
            if warn is not None:
                warn(f"Generated audio disabled: {error}")
            return None

    def find(self, line_id, text_sha256):
        prepared, _state = self.find_with_preflight(line_id, text_sha256)
        return prepared

    def find_with_preflight(self, line_id, text_sha256):
        entry = self.index.find(line_id, text_sha256, verify_file=False)
        if entry is None:
            return None, "generated-audio-entry-not-found"
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
        if cached is not None:
            return cached, "generated-audio-entry-verified"
        try:
            samples, sample_rate = _read_pcm16_mono_wav_bytes(payload)
        except Pcm16MonoWavError as error:
            self._warn_once(
                entry, f"Generated audio is invalid: {entry.audio}: {error}"
            )
            return None, "generated-audio-invalid-wav"
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
            narrator_fallback_role=self.narrator_fallback_roles.get(
                (entry.line_id, entry.text_sha256)
            ),
        )
        self.cache.put(cache_key, prepared)
        return prepared, "generated-audio-entry-verified"

    def find_live_fallback(self, line_id, text_sha256):
        return self.live_fallbacks.get((line_id, text_sha256))

    def find_audio_event_omission(self, line_id, text_sha256):
        return self.audio_event_omissions.get((line_id, text_sha256))

    def _warn_once(self, entry, message):
        identity = entry.line_id, entry.text_sha256
        if identity in self.warned_entries:
            return
        self.warned_entries.add(identity)
        self.warn(message)


def _narrator_fallback_role(entry):
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
        live_backend,
        library,
        line_resolver,
        *,
        volume=1.0,
        speed=1.0,
        audio_output=None,
        playback_latency="low",
        clock=monotonic,
        audio_source_policy="prefer-generated",
        require_source_audio_completion=False,
    ):
        if audio_output is None:
            import sounddevice

            audio_output = sounddevice
        self.live_backend = live_backend
        self.library = library
        self.line_resolver = line_resolver
        self.audio_output = audio_output
        self.playback_latency = playback_latency
        self.clock = clock
        if audio_source_policy not in audio_source_policies:
            raise ValueError(f"Unknown audio source policy: {audio_source_policy}")
        self.audio_source_policy = audio_source_policy
        self.require_source_audio_completion = bool(require_source_audio_completion)
        prefix = "generated-audio" if library is not None else "story-audio"
        self.name = f"{prefix}+{live_backend.name}"
        self.capabilities = live_backend.capabilities
        self.playback_lock = Lock()
        self.generated_preflight_lock = Lock()
        self.source_audio_completion_stop = Event()
        self.generated_audio_stop = Event()
        self.generated_reservations = BoundedCache(32)
        self.active_generated_stream = None
        self.active_playback_source = None
        self.playback_active = False
        self.live_mode_active = False
        self.voice_override = None
        self.set_volume(volume, delegate=False)
        self.set_speed(speed, delegate=False)

    def will_use_source_audio(self, character, text):
        """Return whether live playback for this exact line stays in the game."""
        if not self.live_mode_active:
            return False
        return self._will_use_source_audio(character, text)

    def will_use_source_audio_in_live_mode(self, character, text):
        """Read the future live route without advancing story-match authority."""
        current_match = getattr(self.line_resolver, "current_match", None)
        try:
            return self._will_use_source_audio(character, text)
        finally:
            if hasattr(self.line_resolver, "current_match"):
                self.line_resolver.current_match = current_match

    def has_resolved_route_in_live_mode(self, character, text):
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
                self.line_resolver.current_match = current_match

    def _will_use_source_audio(self, character, text):
        if self.audio_source_policy != "prefer-game-audio":
            return False
        if self.voice_override is not None and self.voice_override(character):
            return False
        line = self.line_resolver.resolve_exact(character, text)
        return bool(
            line is not None
            and line.line_id
            and getattr(line, "source_audio_status", "unknown") == "available"
            and getattr(line, "source_audio_completeness", "unknown") != "partial"
            and (
                not self.require_source_audio_completion
                or getattr(line, "source_audio_duration_seconds", None) is not None
            )
        )

    def has_generated_line(self, line):
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

    def reserve_generated_line_for_early_playback(self, line):
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
                and getattr(line, "source_audio_status", "unknown") == "available"
            )
        ):
            return False
        return self.has_generated_line(line)

    def prepare_route(self, character, text, *, line_id=None):
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
        omission_line = line
        if omission_line is None and voice_overridden:
            omission_line = self._resolve_without_advancing(character, text)
        fallback_reasons = []
        artifact_preflight_state = "not-applicable"
        if voice_overridden:
            fallback_reasons.append("manual-voice-override")
            artifact_preflight_state = "skipped-manual-voice-override"
        elif match_result != "exact":
            fallback_reasons.append(f"story-line-{match_result}")
        source_audio_completion = (
            getattr(line, "source_audio_duration_seconds", None)
            if line is not None
            else None
        )
        source_audio_wait = (
            source_audio_completion + SOURCE_AUDIO_COMPLETION_MARGIN_SECONDS
            if source_audio_completion is not None
            else None
        )
        source_audio_completeness = (
            getattr(line, "source_audio_completeness", "unknown")
            if line is not None
            else "unknown"
        )
        source_audio_partial = bool(
            line is not None
            and self.audio_source_policy == "prefer-game-audio"
            and getattr(line, "source_audio_status", "unknown") == "available"
            and source_audio_completion is not None
            and source_audio_completeness == "partial"
        )
        source_audio_missing_completion = bool(
            self.require_source_audio_completion
            and line is not None
            and getattr(line, "source_audio_status", "unknown") == "available"
            and source_audio_completion is None
        )
        if (
            line is not None
            and line.line_id
            and self.live_mode_active
            and self.audio_source_policy == "prefer-game-audio"
            and getattr(line, "source_audio_status", "unknown") == "available"
            and not source_audio_missing_completion
            and not source_audio_partial
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
                    getattr(line, "source_audio_id", None),
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
            elif source_audio_missing_completion:
                fallback_reasons.append("source-audio-completion-unavailable")
                artifact_preflight_state = "source-audio-completion-unavailable"
            else:
                source_status = getattr(line, "source_audio_status", "unknown")
                fallback_reasons.append(f"source-audio-{source_status}")
                artifact_preflight_state = f"source-audio-{source_status}"
        omission = (
            None
            if omission_line is None or self.library is None
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
                route = GeneratedAudioRoute(prepared, trace)
                return (
                    replace(route, source_audio_lead_seconds=source_audio_wait)
                    if source_audio_partial
                    else route
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
            if line is None or self.library is None
            else self.library.find_live_fallback(line.line_id, line.text_sha256)
        )
        if live_fallback is not None:
            _validate_live_fallback_backend(self.live_backend, live_fallback)
        prepared = self.live_backend.prepare_playback(
            (
                live_fallback.requested_voice_character
                if live_fallback is not None
                else synthesis_character(character)
            ),
            (
                live_fallback.evidence["spoken_text"]
                if live_fallback is not None and live_fallback.schema_version == 6
                else text
            ),
        )
        effective_source = prepared.audio_source
        line_id = line.line_id if line is not None else None
        trace = AudioRouteTrace(
            None,
            effective_source,
            match_result,
            ";".join(dict.fromkeys(fallback_reasons)) or None,
            None,
            line_id,
            artifact_preflight_state,
        )
        if live_fallback is not None:
            trace = AudioRouteTrace(
                None,
                "live-fallback",
                match_result,
                ";".join(
                    dict.fromkeys(
                        [*fallback_reasons, f"authorized:{live_fallback.reason}"]
                    )
                ),
                None,
                line_id,
                "live-fallback-authorized",
            )
            route = LiveFallbackRoute(
                prepared,
                live_fallback,
                trace,
                prepared.synthesis_ms,
                prepared.first_audio_ms,
                prepared.cache_source,
            )
            return (
                replace(route, source_audio_lead_seconds=source_audio_wait)
                if source_audio_partial
                else route
            )
        route = LiveTTSRoute(
            prepared,
            trace,
            prepared.synthesis_ms,
            prepared.first_audio_ms,
            prepared.cache_source,
        )
        return (
            replace(route, source_audio_lead_seconds=source_audio_wait)
            if source_audio_partial
            else route
        )

    def _resolve_line(self, character, text, voice_overridden):
        if voice_overridden:
            return None, "skipped"
        resolve = getattr(self.line_resolver, "resolve_exact_with_result", None)
        if callable(resolve):
            return resolve(character, text)
        line = self.line_resolver.resolve_exact(character, text)
        return line, "exact" if line is not None else "no-match"

    def _resolve_without_advancing(self, character, text):
        current_match = getattr(self.line_resolver, "current_match", None)
        try:
            resolve = getattr(self.line_resolver, "resolve_exact_with_result", None)
            if callable(resolve):
                line, _result = resolve(character, text)
                return line
            return self.line_resolver.resolve_exact(character, text)
        finally:
            if hasattr(self.line_resolver, "current_match"):
                self.line_resolver.current_match = current_match

    def play_route(self, route, *, playback_guard=None):
        """Play one immutable route and return metrics bound to that route."""
        if isinstance(route, SourceAudioRoute):
            return self._play_source_route(route, playback_guard)
        if isinstance(route, GeneratedAudioRoute):
            return self._play_generated_route(route, playback_guard)
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

    def _play_source_route(self, route, playback_guard):
        prepared = route.prepared
        if playback_guard is not None and not playback_guard():
            return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
        if prepared.completion_seconds is None:
            return _route_outcome(route, PlaybackStatus.PASSTHROUGH_UNOBSERVED, None)
        with self.playback_lock:
            if playback_guard is not None and not playback_guard():
                return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
            started = self.clock()
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
                    (self.clock() - started) * 1000,
                )
            finally:
                self.playback_active = False
                self.active_playback_source = None

    def _play_generated_route(self, route, playback_guard):
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
            started = self.clock()
            self.generated_audio_stop.clear()
            try:
                self.playback_active = True
                if not self._wait_for_source_audio_lead(route, playback_guard):
                    return _route_outcome(
                        route,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - started) * 1000,
                    )
                self.active_playback_source = "generated"
                samples = (
                    np.asarray(route.prepared.samples, dtype=np.float32) * self.volume
                )
                samples, sample_rate = match_output_sample_rate(
                    self.audio_output,
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
                        (self.clock() - started) * 1000,
                        source_sample_rate=route.prepared.sample_rate,
                        playback_sample_rate=sample_rate,
                        sample_count=sample_count,
                        expected_playback_ms=expected_playback_ms,
                    )
                stream_factory = getattr(self.audio_output, "OutputStream", None)
                if callable(stream_factory):
                    with stream_factory(
                        samplerate=sample_rate,
                        channels=1,
                        dtype="float32",
                        latency=self.playback_latency,
                    ) as stream:
                        self.active_generated_stream = stream
                        underflowed = bool(stream.write(samples.reshape(-1, 1)))
                else:
                    self.audio_output.play(
                        samples,
                        sample_rate,
                        latency=self.playback_latency,
                    )
                    status = self.audio_output.wait()
                    underflowed = bool(getattr(status, "output_underflow", False))
                playable = playback_guard is None or bool(playback_guard())
                playback_status = (
                    PlaybackStatus.INTERRUPTED
                    if self.generated_audio_stop.is_set() or not playable
                    else PlaybackStatus.COMPLETED
                )
                return _route_outcome(
                    route,
                    playback_status,
                    (self.clock() - started) * 1000,
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
                    (self.clock() - started) * 1000,
                    first_audio_ms=route.first_audio_ms,
                    error=str(error),
                )
            finally:
                self.active_generated_stream = None
                self.playback_active = False
                self.active_playback_source = None

    def _play_live_route(self, route, playback_guard):
        if playback_guard is not None and not playback_guard():
            return _route_outcome(route, PlaybackStatus.INTERRUPTED, None)
        lead_ms = 0.0
        if route.source_audio_lead_seconds > 0:
            lead_started = self.clock()
            self.playback_active = True
            try:
                if not self._wait_for_source_audio_lead(route, playback_guard):
                    return _route_outcome(
                        route,
                        PlaybackStatus.INTERRUPTED,
                        (self.clock() - lead_started) * 1000,
                    )
            finally:
                self.playback_active = False
                self.active_playback_source = None
            lead_ms = (self.clock() - lead_started) * 1000
        try:
            outcome = self.live_backend.play_prepared(
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

    def _wait_for_source_audio_lead(self, route, playback_guard):
        seconds = float(getattr(route, "source_audio_lead_seconds", 0.0) or 0.0)
        if seconds <= 0:
            return playback_guard is None or bool(playback_guard())
        self.active_playback_source = "game"
        self.source_audio_completion_stop.clear()
        interrupted = self.source_audio_completion_stop.wait(seconds)
        return not interrupted and (playback_guard is None or bool(playback_guard()))

    def prime(self, character):
        prime = getattr(self.live_backend, "prime", None)
        return prime(character) if callable(prime) else False

    def set_live_mode_active(self, active):
        self.live_mode_active = bool(active)
        configure = getattr(self.live_backend, "set_live_mode_active", None)
        return configure(active) if callable(configure) else self.live_mode_active

    def set_volume(self, volume, *, delegate=True):
        self.volume = validate_volume(volume)
        configure = getattr(self.live_backend, "set_volume", None)
        if delegate and callable(configure):
            configure(self.volume)
        return self.volume

    def set_speed(self, speed, *, delegate=True):
        self.speed = validate_speed(speed)
        configure = getattr(self.live_backend, "set_speed", None)
        if delegate and callable(configure):
            configure(self.speed)
        return self.speed

    def stop(self):
        was_playing = self.playback_active
        if self.active_playback_source == "game":
            self.source_audio_completion_stop.set()
        elif self.active_playback_source == "generated":
            self.generated_audio_stop.set()
            stream = self.active_generated_stream
            if stream is not None:
                abort = getattr(stream, "abort", None)
                if callable(abort):
                    abort()
                else:
                    stream.stop()
            else:
                self.audio_output.stop()
        return bool(self.live_backend.stop()) or was_playing


def _live_fallback_index(metadata):
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
    indexed = {}
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
            **raw, **({"evidence": None} if version == 1 else {})
        )
        identity = decision.line_id, decision.text_sha256
        if identity in indexed:
            raise ValueError("Generated-audio live fallback identity is duplicated")
        indexed[identity] = decision
    return indexed


def _validate_automatic_recovery_fallback_evidence(
    evidence,
    queue_id,
    previous_result_sha256,
):
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
        or not _lowercase_sha256(evidence.get("queue_sha256"))
        or not _lowercase_sha256(evidence.get("base_result_sha256"))
        or not isinstance(base_result, dict)
        or base_result.get("status") != "failed"
        or base_result.get("provider") != "pocket-tts"
        or base_result.get("model") != "pocket-tts"
        or base_result.get("generation_profile") != "default"
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


def _audio_event_omission_index(metadata):
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
    indexed = {}
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
            not _lowercase_sha256(raw.get(field))
            for field in (
                "text_sha256",
                "plan_sha256",
                "spoken_text_sha256",
                "decision_sha256",
            )
        ) or any(
            not _lowercase_sha256(authority.get(field))
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
        if _canonical_sha256(decision_document) != raw["decision_sha256"]:
            raise ValueError(
                "Generated-audio audio-event omission decision checksum changed"
            )
        decision = AudioEventOmissionDecision(**raw)
        identity = decision.line_id, decision.text_sha256
        if identity in indexed:
            raise ValueError("Generated-audio audio-event omission is duplicated")
        indexed[identity] = decision
    return indexed


def _validate_live_fallback_evidence(evidence, previous_result_sha256):
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
        or not _lowercase_sha256(evidence.get("queue_sha256"))
        or not _lowercase_sha256(evidence.get("base_result_sha256"))
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
                not _lowercase_sha256(hypothesis.get(field))
                for field in ("workspace_sha256", "state_sha256", "result_sha256")
            )
            or not isinstance(hypothesis.get("result"), dict)
            or _canonical_sha256(hypothesis["result"]) != hypothesis["result_sha256"]
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
    evidence, queue_id, requested_voice_character
):
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
        if not _lowercase_sha256(evidence.get(field)):
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
    evidence, queue_id, source_character, synthesis_character
):
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
        or _canonical_sha256(item) != evidence.get("evidence_item_sha256")
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
        if not _lowercase_sha256(evidence.get(field)):
            raise ValueError("Generated-audio known-role fallback hash is malformed")
    for field in (
        "source_character",
        "synthesis_character",
        "evidence_workspace_id",
    ):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError("Generated-audio known-role fallback text is malformed")


def _validate_audio_event_projection_fallback_evidence(
    evidence,
    queue_id,
    source_character,
    synthesis_character,
    previous_result_sha256,
):
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
        or _canonical_sha256(base_result) != evidence.get("base_result_sha256")
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
        if not _lowercase_sha256(evidence.get(field)):
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
    evidence,
    queue_id,
    source_character,
    synthesis_character,
    previous_result_sha256,
):
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
        or _canonical_sha256(base_result) != evidence.get("base_result_sha256")
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
        if not _lowercase_sha256(evidence.get(field)):
            raise ValueError(
                "Generated-audio reviewed-rejection fallback hash is malformed"
            )
    for field in ("base_workspace_id", "source_character", "synthesis_character"):
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(
                "Generated-audio reviewed-rejection fallback text is malformed"
            )
    if any(not _lowercase_sha256(digest) for digest in references):
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


def _validate_render_review_fallback_evidence(evidence, previous_result_sha256):
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
        or not _lowercase_sha256(evidence.get("queue_sha256"))
        or not _lowercase_sha256(evidence.get("base_result_sha256"))
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
                not _lowercase_sha256(hypothesis.get(field))
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
            _canonical_sha256(review) != hypothesis["review_document_sha256"]
            or _canonical_sha256(decision) != hypothesis["decision_document_sha256"]
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


def _lowercase_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _validate_live_fallback_backend(backend, decision):
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


def _read_pcm16_mono_wav_bytes(payload):
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
    pcm = np.frombuffer(frames, dtype="<i2")
    samples = np.asarray(pcm, dtype=np.float32) / 32768.0
    return samples, sample_rate


def _numeric_metric(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _route_outcome(
    route,
    status,
    playback_ms,
    *,
    underflowed=False,
    generation_limited=False,
    first_audio_ms=None,
    error=None,
    source_sample_rate=None,
    playback_sample_rate=None,
    sample_count=None,
    expected_playback_ms=None,
):
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
