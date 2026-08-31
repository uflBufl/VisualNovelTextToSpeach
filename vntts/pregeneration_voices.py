"""Checksum-bound voice routing for player-owned offline preparation jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import VoiceManifestError, normalize_character_name

from vntts.versioned_json import read_versioned_json, write_versioned_json
from vntts.voices import (
    CharacterVoiceRegistry,
    default_voice_choice_id,
    find_default_voice_manifest,
    find_voice_assignment,
    is_narrator,
    synthesis_character_for_line,
)

voice_plan_schema_version = 1
voice_decisions_schema_version = 1


class PregenerationVoiceError(RuntimeError):
    """Offline preparation cannot establish trustworthy voice routes."""


class PregenerationVoiceCancelled(PregenerationVoiceError):
    """The player cancelled voice planning before publication."""


@dataclass(frozen=True)
class VoiceCandidate:
    """One immutable synthesis source that may be auditioned for a group."""

    source_id: str
    source_character: str
    source_speaker: str
    reference_sha256s: tuple[str, ...]

    def to_document(self):
        value = asdict(self)
        value["reference_sha256s"] = list(self.reference_sha256s)
        return value


@dataclass(frozen=True)
class VoiceGroup:
    group_id: str
    character: str
    speakers: tuple[str, ...]
    portrait: str | None
    age: str | None
    source_bank: str | None
    source_voice_id: str | None
    line_ids: tuple[str, ...]
    sample_text: str
    route: str
    source_id: str
    source_character: str | None
    source_speaker: str | None
    reference_sha256s: tuple[str, ...]
    decision_context_sha256: str
    control_sha256: str
    resolution: str
    candidates: tuple[VoiceCandidate, ...] = ()

    def to_document(self):
        value = asdict(self)
        for field in ("speakers", "line_ids", "reference_sha256s"):
            value[field] = list(value[field])
        value["candidates"] = [candidate.to_document() for candidate in self.candidates]
        return value


@dataclass(frozen=True)
class VoicePlan:
    job_id: str
    created_at: str
    story_index_sha256: str
    voice_manifest: str | None
    voice_manifest_sha256: str | None
    synthesis_backend: str
    synthesis_model: str | None
    synthesis_language: str | None
    synthesis_profile: str
    synthesis_controls_sha256: str
    groups: tuple[VoiceGroup, ...]

    @property
    def generation_line_count(self):
        return sum(len(group.line_ids) for group in self.groups)

    @property
    def audition_count(self):
        return sum(group.route == "needs-audition" for group in self.groups)

    @property
    def narrator_fallback_count(self):
        return sum(
            group.resolution == "automatic-narrator-fallback" for group in self.groups
        )

    def to_document(self):
        return {
            "job_id": self.job_id,
            "created_at": self.created_at,
            "story_index_sha256": self.story_index_sha256,
            "voice_manifest": self.voice_manifest,
            "voice_manifest_sha256": self.voice_manifest_sha256,
            "synthesis_backend": self.synthesis_backend,
            "synthesis_model": self.synthesis_model,
            "synthesis_language": self.synthesis_language,
            "synthesis_profile": self.synthesis_profile,
            "synthesis_controls_sha256": self.synthesis_controls_sha256,
            "groups": [group.to_document() for group in self.groups],
        }


class VoiceDecisionStore:
    """Reuse explicit player choices only under identical evidence and controls."""

    def __init__(self, path, *, clock=None):
        self.path = Path(path).expanduser()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def choice_for(self, group_id, decision_context_sha256):
        return (
            self._load()
            .get(_decision_key(group_id, decision_context_sha256), {})
            .get("source_id")
        )

    def remember(self, group, source_id):
        if group.route != "needs-audition":
            raise PregenerationVoiceError(
                "Only an unresolved voice audition can create a player decision"
            )
        source_id = _required_text(source_id, "voice source")
        decisions = self._load()
        decisions[_decision_key(group.group_id, group.decision_context_sha256)] = {
            "group_id": group.group_id,
            "decision_context_sha256": group.decision_context_sha256,
            "source_id": source_id,
            "decided_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_versioned_json(
            self.path,
            voice_decisions_schema_version,
            {"decisions": decisions},
        )

    def _load(self):
        if not self.path.is_file():
            return {}
        try:
            document = read_versioned_json(
                self.path,
                schema_version=voice_decisions_schema_version,
                document_name="offline voice decisions",
            )
            values = document.get("decisions")
            if not isinstance(values, dict):
                raise ValueError("decisions must be an object")
            result = {}
            for key, value in values.items():
                if (
                    not _is_sha256(key)
                    or not isinstance(value, dict)
                    or value.get("group_id") is None
                    or not _is_sha256(value.get("decision_context_sha256"))
                ):
                    raise ValueError("decision entry is invalid")
                _required_text(value.get("source_id"), "voice source")
                _required_text(value.get("decided_at"), "decision timestamp")
                if key != _decision_key(
                    value["group_id"], value["decision_context_sha256"]
                ):
                    raise ValueError("decision identity changed")
                result[key] = dict(value)
            return result
        except (OSError, TypeError, ValueError) as error:
            raise PregenerationVoiceError(
                f"Unable to read saved voice decisions: {error}"
            ) from error


class VoicePlanStore:
    def __init__(self, job_store, *, decisions=None, clock=None):
        self.job_store = job_store
        self.decisions = decisions
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create(self, job, settings, *, manifest_path=None, cancellation=None):
        _raise_if_cancelled(cancellation)
        document = _load_bound_story(job)
        _raise_if_cancelled(cancellation)
        manifest_path = _selected_manifest(settings, manifest_path)
        registry, manifest_sha256 = _load_registry(manifest_path)
        controls = _synthesis_controls(settings)
        controls_sha256 = _digest(controls)
        records = {
            record.line_id: record
            for record in document.records
            if record.line_id in set(job.selected_line_ids)
        }
        if set(records) != set(job.selected_line_ids):
            raise PregenerationVoiceError(
                "Selected dialogue changed after offline preparation was planned"
            )
        grouped = {}
        for line_id in job.selected_line_ids:
            record = records[line_id]
            if not record.speakable or record.source_audio_status == "available":
                continue
            character = synthesis_character_for_line(
                record.speaker, record.voice_character
            )
            evidence = _variant_evidence(record)
            identity = [normalize_character_name(character), *evidence]
            group_id = _digest(identity)
            grouped.setdefault(group_id, []).append((record, character, evidence))

        groups = tuple(
            self._resolve_group(
                group_id,
                values,
                settings,
                registry,
                controls,
            )
            for group_id, values in grouped.items()
        )
        _raise_if_cancelled(cancellation)
        plan = VoicePlan(
            job_id=job.job_id,
            created_at=self.clock().astimezone(timezone.utc).isoformat(),
            story_index_sha256=job.story_index_sha256,
            voice_manifest=str(manifest_path) if manifest_path else None,
            voice_manifest_sha256=manifest_sha256,
            synthesis_backend=settings.speech_backend,
            synthesis_model=settings.tts_model,
            synthesis_language=settings.tts_language,
            synthesis_profile=controls["profile"],
            synthesis_controls_sha256=controls_sha256,
            groups=groups,
        )
        path = self.path_for(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_versioned_json(path, voice_plan_schema_version, plan.to_document())
        return plan

    def path_for(self, job):
        return self.job_store.path_for(job.job_id).parent / "voice-plan.json"

    def _resolve_group(self, group_id, values, settings, registry, controls):
        records = tuple(value[0] for value in values)
        character = values[0][1]
        portrait, age, source_bank, source_voice_id = values[0][2]
        speakers = tuple(dict.fromkeys(record.speaker for record in records))
        assignment_source = find_voice_assignment(settings.voice_assignments, character)
        candidate = _candidate_for(character, settings, registry)
        narrator_candidate = _candidate_for("Narrator", settings, registry)
        candidate_identity = _candidate_identity(candidate)
        decision_context_sha256 = _digest(
            {
                "group_id": group_id,
                "controls": controls,
                "candidate": candidate_identity,
                "narrator": _candidate_identity(narrator_candidate),
            }
        )
        prior_source = (
            self.decisions.choice_for(group_id, decision_context_sha256)
            if self.decisions is not None
            else None
        )
        if prior_source is not None:
            if prior_source == default_voice_choice_id:
                selected = narrator_candidate[1] if narrator_candidate else None
            else:
                selected = _candidate_from_source(prior_source, registry)
            if selected is None and prior_source != default_voice_choice_id:
                raise PregenerationVoiceError(
                    f"Saved voice choice is no longer available for {character!r}"
                )
            route = "narrator" if selected is None else "voice"
            resolution = "saved-player-decision"
            source_id = prior_source
            candidate = selected
        elif is_narrator(character):
            route = "narrator"
            resolution = (
                "saved-narrator-assignment"
                if assignment_source is not None
                else "narrator-dialogue"
            )
            source_id = assignment_source or default_voice_choice_id
            candidate = candidate[1] if candidate is not None else None
        elif assignment_source == default_voice_choice_id:
            route = "narrator"
            resolution = "saved-voice-assignment"
            source_id = default_voice_choice_id
            candidate = narrator_candidate[1] if narrator_candidate else None
        elif candidate is not None:
            route = "voice"
            resolution = (
                "saved-voice-assignment"
                if assignment_source
                else "known-character-voice"
            )
            source_id, candidate = candidate
        else:
            route = "narrator"
            resolution = "automatic-narrator-fallback"
            source_id = default_voice_choice_id
            candidate = narrator_candidate[1] if narrator_candidate else None
        selected_identity = _candidate_identity(
            (source_id, candidate) if candidate is not None else None
        )
        candidates = (
            (_voice_candidate(selected_identity),)
            if route == "voice" and selected_identity is not None
            else ()
        )
        return VoiceGroup(
            group_id=group_id,
            character=character,
            speakers=speakers,
            portrait=portrait,
            age=age,
            source_bank=source_bank,
            source_voice_id=source_voice_id,
            line_ids=tuple(record.line_id for record in records),
            sample_text=_sample_text(records),
            route=route,
            source_id=source_id,
            source_character=candidate.character if candidate is not None else None,
            source_speaker=candidate.speaker if candidate is not None else None,
            reference_sha256s=tuple((selected_identity or {}).get("references", ())),
            decision_context_sha256=decision_context_sha256,
            control_sha256=_digest(
                {"controls": controls, "selected": selected_identity}
            ),
            resolution=resolution,
            candidates=candidates,
        )


def _load_bound_story(job):
    path = Path(job.story_index).expanduser().resolve()
    try:
        before = sha256_file(path)
        if before != job.story_index_sha256:
            raise PregenerationVoiceError(
                "Selected dialogue changed after offline preparation was planned"
            )
        document = load_story_index_document(path)
        after = sha256_file(path)
    except PregenerationVoiceError:
        raise
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationVoiceError(
            f"Unable to read selected dialogue: {error}"
        ) from error
    if before != after or before != job.story_index_sha256:
        raise PregenerationVoiceError(
            "Selected dialogue changed after offline preparation was planned"
        )
    return document


def _selected_manifest(settings, manifest_path):
    value = manifest_path or settings.voice_manifest or find_default_voice_manifest()
    return Path(value).expanduser().resolve() if value else None


def _load_registry(manifest_path):
    if manifest_path is None:
        return CharacterVoiceRegistry(), None
    try:
        before = sha256_file(manifest_path)
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        after = sha256_file(manifest_path)
    except (OSError, VoiceManifestError, ValueError) as error:
        raise PregenerationVoiceError(
            f"Unable to read character voices: {error}"
        ) from error
    if before != after:
        raise PregenerationVoiceError("Character voices changed while they were read")
    return registry, before


def _candidate_for(character, settings, registry):
    source_id = find_voice_assignment(settings.voice_assignments, character)
    if source_id == default_voice_choice_id:
        return None
    if source_id:
        candidate = _candidate_from_source(source_id, registry)
        return (source_id, candidate) if candidate is not None else None
    voice = registry.resolve(character)
    if voice is None or not _usable_voice(voice):
        return None
    return f"character:{normalize_character_name(voice.character)}", voice


def _candidate_from_source(source_id, registry):
    try:
        voice = registry.resolve_source(source_id)
    except VoiceManifestError as error:
        raise PregenerationVoiceError(str(error)) from error
    return voice if voice is not None and _usable_voice(voice) else None


def _usable_voice(voice):
    if voice.reference_root is None and not voice.references:
        return True
    return bool(voice.references) and all(path.is_file() for path in voice.references)


def _candidate_identity(candidate):
    if candidate is None:
        return None
    source_id, voice = candidate
    return {
        "source_id": source_id,
        "character": voice.character,
        "speaker": voice.speaker,
        "references": [sha256_file(path) for path in voice.references],
    }


def _voice_candidate(identity):
    return VoiceCandidate(
        source_id=identity["source_id"],
        source_character=identity["character"],
        source_speaker=identity["speaker"],
        reference_sha256s=tuple(identity["references"]),
    )


def _variant_evidence(record):
    return tuple(
        _optional_variant(record.producer_fields.get(field))
        for field in ("portrait", "age", "source_bank", "source_voice_id")
    )


def _optional_variant(value):
    if value is None:
        return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return _digest(value)


def _sample_text(records):
    eligible = [record.text.strip() for record in records if record.text.strip()]
    if not eligible:
        return "Voice preview."
    return min(eligible, key=lambda value: (abs(len(value) - 90), len(value)))


def _synthesis_controls(settings):
    return {
        "backend": settings.speech_backend,
        "model": settings.tts_model,
        "language": settings.tts_language,
        "profile": (
            "default"
            if settings.speech_backend == "pocket-tts"
            else settings.tts_profile
        ),
        "narrator_speaker": settings.narrator_speaker,
        "narrator_reference": _path_identity(settings.tts_speaker_wav),
    }


def _path_identity(value):
    if not value:
        return None
    path = Path(value).expanduser()
    return {
        "path": str(path),
        "sha256": sha256_file(path) if path.is_file() else None,
    }


def _decision_key(group_id, decision_context_sha256):
    if not _is_sha256(group_id) or not _is_sha256(decision_context_sha256):
        raise PregenerationVoiceError("Voice decision identity is invalid")
    return _digest([group_id, decision_context_sha256])


def _digest(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _raise_if_cancelled(cancellation):
    if cancellation is not None and cancellation.is_set():
        raise PregenerationVoiceCancelled("Offline voice matching was cancelled")


__all__ = [
    "PregenerationVoiceCancelled",
    "PregenerationVoiceError",
    "VoiceDecisionStore",
    "VoiceCandidate",
    "VoiceGroup",
    "VoicePlan",
    "VoicePlanStore",
]
