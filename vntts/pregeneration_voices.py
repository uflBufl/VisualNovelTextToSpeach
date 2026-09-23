"""Checksum-bound voice routing for player-owned offline preparation jobs."""

from __future__ import annotations

import hashlib
import json
import math
import wave
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import perf_counter, process_time
from typing import Protocol, TypeAlias, TypedDict

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    StoryIndexRecord,
)
from vntts_artifacts.voice_generation_queue import (
    expected_voice_generation_queue_id,
    text_sha256,
)
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.source_reference_bindings import (
    SOURCE_REFERENCE_BINDINGS_FIELD,
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.chapter_voice_preload import (
    _has_authoritative_source_audio,
    _validated_source_audio_line_ids,
)
from vntts.pregeneration_setup import (
    PregenerationJob,
    PregenerationJobStore,
    load_verified_story_index_document,
)
from vntts.services.tts_engine import default_tts_profile, get_tts_profile
from vntts.settings import AppSettings
from vntts.support import record_background_operation
from vntts.versioned_json import read_versioned_json, write_versioned_json
from vntts.voice_library import VoiceLibrary, VoiceSelection
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    default_voice_choice_id,
    discover_voice_source,
    find_default_voice_manifest,
    is_narrator,
    pocket_tts_preset_voices,
    read_voice_reference_bytes,
    registry_with_voice_library,
    remember_voice_binding,
    synthesis_character_for_line,
    voice_binding_source_id,
)

JsonObject: TypeAlias = dict[str, object]
VariantEvidence: TypeAlias = tuple[str | None, str | None, str | None]
PortraitSnapshot: TypeAlias = tuple[str | None, str | None]
GroupValue: TypeAlias = tuple[
    StoryIndexRecord,
    str,
    VariantEvidence,
    str | None,
    str | None,
    str | None,
    str | None,
]


class SynthesisControls(TypedDict):
    backend: str
    model: str | None
    language: str | None
    profile: str
    pocket_voice_cloning: bool | None
    narrator_speaker: str | None
    narrator_reference: JsonObject | None


class CandidateIdentity(TypedDict):
    source_id: str
    character: str
    speaker: str
    references: list[str]


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


voice_plan_schema_version = 4
voice_decisions_schema_version = 1
PLAYER_VOICE_CANDIDATES_FIELD = "vntts.player.voice_candidates"
PLAYER_VOICE_CANDIDATES_SCHEMA = "vntts.player-voice-candidates"
PLAYER_VOICE_CANDIDATES_VERSION = 2
PLAYER_VOICE_CANDIDATES_VERSIONS = frozenset({1, PLAYER_VOICE_CANDIDATES_VERSION})
_CLEAR_WINNER_SCORE = 80
_CLEAR_WINNER_MARGIN = 20
_MAX_AUDITION_CANDIDATES = 3


class PregenerationVoiceError(RuntimeError):
    """Offline preparation cannot establish trustworthy voice routes."""


class PregenerationVoiceCancelled(PregenerationVoiceError):
    """The player cancelled voice planning before publication."""


def resolve_pregeneration_settings(settings: AppSettings) -> AppSettings:
    """Normalize profiles without replacing the user's selected speech engine."""
    backend = settings.speech_backend
    if backend == "pocket-tts":
        return settings.updated(tts_model=None, tts_profile="default")
    if backend == "moss-tts":
        from vntts.moss_cpp_backend import moss_cpp_requested

        model = settings.tts_model
        if moss_cpp_requested(model) and not str(model or "").lower().endswith(".gguf"):
            settings = settings.updated(tts_model=None)
    try:
        get_tts_profile(settings.tts_profile)
    except ValueError:
        return settings.updated(tts_profile=default_tts_profile)
    return settings


def pregeneration_narrator_source_id(
    settings: AppSettings, *, voice_library: VoiceLibrary | None = None
) -> str:
    """Return the narrator source that self-service generation will use."""
    source_id = _effective_assignment_source(
        settings, "Narrator", library=voice_library
    )
    if source_id is not None:
        return source_id
    if settings.speech_backend != "pocket-tts":
        return str(default_voice_choice_id)
    speaker = next(
        (
            value
            for value in (settings.narrator_speaker, settings.tts_speaker, "alba")
            if value in pocket_tts_preset_voices
        ),
        "alba",
    )
    return f"preset:{speaker}"


@dataclass(frozen=True)
class VoiceCandidate:
    """One immutable synthesis source that may be auditioned for a group."""

    source_id: str
    source_character: str
    source_speaker: str
    reference_sha256s: tuple[str, ...]
    match_score: int = 0
    recommendation: str = "Available character voice"
    portrait: str | None = None
    source_bank: str | None = None
    source_voice_ids: tuple[str, ...] = ()
    source_line_ids: tuple[str, ...] = ()
    reference_duration_seconds: float | None = None

    def to_document(self) -> JsonObject:
        value = asdict(self)
        for field in ("reference_sha256s", "source_voice_ids", "source_line_ids"):
            value[field] = list(value[field])
        return value


@dataclass(frozen=True)
class VoiceGroup:
    group_id: str
    character: str
    speakers: tuple[str, ...]
    portrait: str | None
    source_bank: str | None
    source_voice_id: str | None
    line_ids: tuple[str, ...]
    sample_text: str
    alternate_sample_text: str | None
    route: str
    source_id: str
    source_character: str | None
    source_speaker: str | None
    reference_sha256s: tuple[str, ...]
    decision_context_sha256: str
    control_sha256: str
    resolution: str
    candidates: tuple[VoiceCandidate, ...] = ()
    narrator_candidate: VoiceCandidate | None = None
    candidate_inventory: tuple[VoiceCandidate, ...] = ()
    anchor_source_id: str | None = None
    portrait_image: str | None = None
    portrait_image_sha256: str | None = None

    def to_document(self) -> JsonObject:
        value = asdict(self)
        for field in ("speakers", "line_ids", "reference_sha256s"):
            value[field] = list(value[field])
        value["candidates"] = [candidate.to_document() for candidate in self.candidates]
        value["narrator_candidate"] = (
            None
            if self.narrator_candidate is None
            else self.narrator_candidate.to_document()
        )
        value["candidate_inventory"] = [
            candidate.to_document() for candidate in self.candidate_inventory
        ]
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
    pocket_voice_cloning: bool
    synthesis_controls_sha256: str
    groups: tuple[VoiceGroup, ...]

    @property
    def generation_line_count(self) -> int:
        return sum(len(group.line_ids) for group in self.groups)

    @property
    def audition_count(self) -> int:
        return sum(group.route == "needs-audition" for group in self.groups)

    @property
    def narrator_fallback_count(self) -> int:
        return sum(
            group.resolution == "automatic-narrator-fallback" for group in self.groups
        )

    def to_document(self) -> JsonObject:
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
            "pocket_voice_cloning": self.pocket_voice_cloning,
            "synthesis_controls_sha256": self.synthesis_controls_sha256,
            "groups": [group.to_document() for group in self.groups],
        }


class VoiceDecisionStore:
    """Reuse explicit player choices only under identical evidence and controls."""

    def __init__(
        self,
        path: str | Path,
        *,
        voice_library: VoiceLibrary | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.voice_library: VoiceLibrary | None = voice_library
        self.clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )

    def choice_for(self, group_id: str, decision_context_sha256: str) -> str | None:
        decision = self._load().get(_decision_key(group_id, decision_context_sha256))
        if decision is None:
            return None
        source_id = decision.get("source_id")
        return source_id if isinstance(source_id, str) else None

    def remember(self, group: VoiceGroup, source_id: str) -> None:
        self.remember_many(((group, source_id),))

    def remember_many(self, selections: Iterable[tuple[VoiceGroup, str]]) -> None:
        selections = tuple(selections)
        if not selections:
            raise PregenerationVoiceError("At least one voice choice is required")
        decisions = self._load()
        decided_at = self.clock().astimezone(timezone.utc).isoformat()
        validated_selections = _validated_decision_selections(selections)
        library_selections = self._library_selections(validated_selections, decided_at)
        for group, source_id in validated_selections:
            decisions[_decision_key(group.group_id, group.decision_context_sha256)] = {
                "group_id": group.group_id,
                "decision_context_sha256": group.decision_context_sha256,
                "source_id": source_id,
                "decided_at": decided_at,
            }
        previous_bindings = (
            self.voice_library.bindings() if self.voice_library is not None else None
        )
        if self.voice_library is not None:
            self.voice_library.select_many(library_selections)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_versioned_json(
                self.path,
                voice_decisions_schema_version,
                {"decisions": decisions},
            )
        except Exception as error:
            if self.voice_library is not None and previous_bindings is not None:
                try:
                    self.voice_library.replace_bindings(previous_bindings)
                except Exception as rollback_error:
                    error.add_note(
                        "Unable to restore the previous voice bindings: "
                        f"{rollback_error}"
                    )
            raise

    def _library_selections(
        self,
        selections: Sequence[tuple[VoiceGroup, str]],
        decided_at: str,
    ) -> list[VoiceSelection]:
        if self.voice_library is None:
            return []
        return [
            _voice_library_selection(group, source_id, decided_at)
            for group, source_id in selections
        ]

    def _load(self) -> dict[str, JsonObject]:
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
            result: dict[str, JsonObject] = {}
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


def _validated_decision_selections(
    selections: Sequence[tuple[VoiceGroup, str]],
) -> list[tuple[VoiceGroup, str]]:
    observed_groups: set[str] = set()
    validated: list[tuple[VoiceGroup, str]] = []
    for group, source_id in selections:
        if group.group_id in observed_groups:
            raise PregenerationVoiceError("A voice group was selected more than once")
        observed_groups.add(group.group_id)
        source_id = _required_text(source_id, "voice source")
        if source_id not in _allowed_voice_sources(group):
            raise PregenerationVoiceError(
                "The selected voice is not part of this voice plan"
            )
        validated.append((group, source_id))
    return validated


def _allowed_voice_sources(group: VoiceGroup) -> set[str]:
    sources = {
        default_voice_choice_id,
        *(candidate.source_id for candidate in group.candidates),
        *(candidate.source_id for candidate in group.candidate_inventory),
    }
    if group.narrator_candidate is not None:
        sources.add(group.narrator_candidate.source_id)
    if group.source_id:
        sources.add(group.source_id)
    return sources


def _voice_library_selection(
    group: VoiceGroup, source_id: str, decided_at: str
) -> VoiceSelection:
    selected = next(
        (
            candidate
            for candidate in (*group.candidates, *group.candidate_inventory)
            if candidate.source_id == source_id
        ),
        None,
    )
    evidence: JsonObject = {"decision_context_sha256": group.decision_context_sha256}
    if selected is not None:
        evidence.update(
            {
                "source_id": selected.source_id,
                "source_character": selected.source_character,
                "speaker": selected.source_speaker,
            }
        )
    references = selected.reference_sha256s if selected is not None else ()
    return VoiceSelection(
        role=group.character,
        route="narrator" if source_id == default_voice_choice_id else "voice",
        source_sha256s=references,
        source_id=None
        if source_id == default_voice_choice_id or references
        else source_id,
        method="manual",
        evidence=evidence,
        algorithm="voice-audition-v1",
        timestamp=decided_at,
    )


class VoicePlanStore:
    def __init__(
        self,
        job_store: PregenerationJobStore,
        *,
        decisions: VoiceDecisionStore | None = None,
        voice_library: VoiceLibrary | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.job_store = job_store
        self.decisions = decisions
        self.voice_library: VoiceLibrary | None = voice_library
        self.clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )

    def create(
        self,
        job: PregenerationJob,
        settings: AppSettings,
        *,
        manifest_path: str | Path | None = None,
        cancellation: Cancellation | None = None,
        ignore_decisions: bool = False,
    ) -> VoicePlan:
        previous_bindings = (
            self.voice_library.bindings() if self.voice_library is not None else None
        )
        try:
            return self._create(
                job,
                settings,
                manifest_path=manifest_path,
                cancellation=cancellation,
                ignore_decisions=ignore_decisions,
            )
        except Exception as error:
            if self.voice_library is not None and previous_bindings is not None:
                try:
                    self.voice_library.replace_bindings(previous_bindings)
                except Exception as rollback_error:
                    error.add_note(
                        "Unable to restore the previous voice bindings: "
                        f"{rollback_error}"
                    )
            raise

    def _create(
        self,
        job: PregenerationJob,
        settings: AppSettings,
        *,
        manifest_path: str | Path | None = None,
        cancellation: Cancellation | None = None,
        ignore_decisions: bool = False,
    ) -> VoicePlan:
        _raise_if_cancelled(cancellation)
        phase_started, cpu_started = perf_counter(), process_time()
        document = _load_bound_story(job)
        source_completion = document.metadata.get("source_audio_completion")
        authoritative_source_lines = _validated_source_audio_line_ids(
            job.story_index, document
        )
        _record_plan_phase(
            "story",
            phase_started,
            cpu_started,
            files_examined=1,
            bytes_examined=_file_size(job.story_index),
        )
        selected_line_ids = set(job.selected_line_ids)
        records: dict[str, StoryIndexRecord] = {
            record.line_id: record
            for record in document.records
            if record.line_id in selected_line_ids
        }
        if set(records) != selected_line_ids:
            raise PregenerationVoiceError(
                "Selected dialogue changed after offline preparation was planned"
            )
        _raise_if_cancelled(cancellation)
        phase_started, cpu_started = perf_counter(), process_time()
        manifest_path = _selected_manifest(settings, manifest_path)
        registry, manifest_sha256, manifest_document = _load_registry(manifest_path)
        registry = self._prepared_voice_library(
            registry,
            records.values(),
            settings,
            source_completion,
            authoritative_source_lines,
            ignore_decisions,
        )
        queue_bindings = _manifest_queue_bindings(manifest_document, registry)
        candidate_variants = _manifest_candidate_variants(
            manifest_document,
            registry,
            manifest_path,
            job.story_index_sha256,
        )
        if self.voice_library is not None:
            manifest_path = _materialize_voice_catalog(self.job_store, job, registry)
            manifest_sha256 = sha256_file(manifest_path)
        reference_files, reference_bytes = _voice_reference_stats(registry)
        _record_plan_phase(
            "voice-inventory",
            phase_started,
            cpu_started,
            files_examined=reference_files + (1 if manifest_path else 0),
            bytes_examined=reference_bytes + _file_size(manifest_path),
        )
        controls = _synthesis_controls(settings)
        saved_groups = (
            self._saved_independent_groups(controls) if not ignore_decisions else ()
        )
        controls_sha256 = _digest(controls)
        portrait_snapshots: dict[str, PortraitSnapshot] = {}
        grouped: dict[str, list[GroupValue]] = {}
        assignment_cache: dict[str, str | None] = {}
        for line_id in job.selected_line_ids:
            record = records[line_id]
            if not record.speakable or _has_authoritative_source_audio(
                record, source_completion, authoritative_source_lines
            ):
                continue
            character = synthesis_character_for_line(
                record.speaker, record.voice_character
            )
            evidence = _variant_evidence(record)
            line_source = _bound_source_for_record(record, queue_bindings)
            if character not in assignment_cache:
                assignment_cache[character] = _effective_assignment_source(
                    settings,
                    character,
                    library=self.voice_library,
                )
            bound_source = assignment_cache[character] or line_source
            portrait_image, portrait_image_sha256 = _portrait_snapshot(
                Path(job.story_index).expanduser().resolve().parent,
                evidence[0],
                portrait_snapshots,
            )
            identity = [
                normalize_character_name(character),
                line_source,
            ]
            group_id = _digest(identity)
            grouped.setdefault(group_id, []).append(
                (
                    record,
                    character,
                    evidence,
                    None,
                    bound_source,
                    portrait_image,
                    portrait_image_sha256,
                )
            )

        phase_started, cpu_started = perf_counter(), process_time()
        groups: list[VoiceGroup] = []
        for group_id, values in grouped.items():
            groups.append(
                self._resolve_group(
                    group_id,
                    values,
                    settings,
                    registry,
                    candidate_variants,
                    controls,
                    ignore_decisions,
                    saved_groups,
                )
            )
            if self.voice_library is not None:
                registry = registry_with_voice_library(registry, self.voice_library)
        _record_plan_phase(
            "routing",
            phase_started,
            cpu_started,
            files_examined=reference_files,
            bytes_examined=reference_bytes,
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
            pocket_voice_cloning=bool(controls["pocket_voice_cloning"]),
            synthesis_controls_sha256=controls_sha256,
            groups=tuple(groups),
        )
        return self._persist_plan(job, plan)

    def _persist_plan(self, job: PregenerationJob, plan: VoicePlan) -> VoicePlan:
        phase_started, cpu_started = perf_counter(), process_time()
        path = self.path_for(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_versioned_json(path, voice_plan_schema_version, plan.to_document())
        _record_plan_phase(
            "write",
            phase_started,
            cpu_started,
            files_examined=1,
            bytes_examined=_file_size(path),
        )
        return plan

    def _prepared_voice_library(
        self,
        registry: CharacterVoiceRegistry,
        records: Iterable[StoryIndexRecord],
        settings: AppSettings,
        source_completion: str | None,
        authoritative_source_lines: frozenset[str],
        ignore_decisions: bool,
    ) -> CharacterVoiceRegistry:
        if self.voice_library is None:
            return registry
        if ignore_decisions:
            reset_roles = {
                normalize_character_name(
                    synthesis_character_for_line(record.speaker, record.voice_character)
                )
                for record in records
                if record.speakable
                and not _has_authoritative_source_audio(
                    record, source_completion, authoritative_source_lines
                )
            }
            for binding in self.voice_library.bindings():
                if (
                    not is_narrator(binding.role)
                    and normalize_character_name(binding.role) in reset_roles
                ):
                    self.voice_library.clear(
                        binding.role, variant_key=binding.variant_key
                    )
        for binding in self.voice_library.bindings():
            if (
                binding.route == "narrator"
                and binding.provenance.get("method") == "automatic"
            ):
                self.voice_library.clear(binding.role, variant_key=binding.variant_key)
        return registry_with_voice_library(registry, self.voice_library)

    def path_for(self, job: PregenerationJob) -> Path:
        return Path(self.job_store.path_for(job.job_id)).parent / "voice-plan.json"

    def _saved_independent_groups(
        self, controls: SynthesisControls
    ) -> tuple[JsonObject, ...]:
        if self.decisions is None:
            return ()
        groups: list[JsonObject] = []
        for path in self.job_store.root.glob("*/voice-plan.json"):
            try:
                plan = read_versioned_json(
                    path,
                    schema_version=voice_plan_schema_version,
                    document_name="offline voice plan",
                )
            except OSError, ValueError:
                continue
            if (
                plan.get("synthesis_backend") != controls["backend"]
                or plan.get("synthesis_model") != controls["model"]
                or plan.get("synthesis_language") != controls["language"]
                or plan.get("synthesis_profile") != controls["profile"]
                or plan.get("pocket_voice_cloning")
                != bool(controls["pocket_voice_cloning"])
            ):
                continue
            plan_groups = plan.get("groups")
            if not isinstance(plan_groups, list):
                continue
            groups.extend(
                group
                for group in plan_groups
                if isinstance(group, dict) and group.get("route") == "voice"
            )
        return tuple(groups)

    def _resolve_group(
        self,
        group_id: str,
        values: Sequence[GroupValue],
        settings: AppSettings,
        registry: CharacterVoiceRegistry,
        candidate_variants: Sequence[JsonObject],
        controls: SynthesisControls,
        ignore_decisions: bool,
        saved_groups: Sequence[JsonObject],
    ) -> VoiceGroup:
        records = tuple(value[0] for value in values)
        character = values[0][1]
        portrait, source_bank, source_voice_id = values[0][2]
        variant_key = values[0][3]
        if any(value[2][1] != source_bank for value in values):
            source_bank = None
        if any(value[2][2] != source_voice_id for value in values):
            source_voice_id = None
        bound_source = values[0][4]
        portrait_value = next((value for value in values if value[5]), values[0])
        portrait = portrait_value[2][0]
        portrait_image, portrait_image_sha256 = portrait_value[5:7]
        speakers = tuple(dict.fromkeys(record.speaker for record in records))
        assignment_source = _effective_assignment_source(
            settings,
            character,
            library=self.voice_library,
            variant_key=variant_key,
        )
        candidate_inventory = _candidate_inventory(
            character,
            bound_source,
            settings,
            registry,
            candidate_variants,
            assignment_source=assignment_source,
        )
        if self.voice_library is not None:
            for available in candidate_inventory:
                discover_voice_source(
                    self.voice_library,
                    registry,
                    character,
                    available.source_id,
                    variant_key=variant_key,
                    method="automatic",
                    evidence={"recommendation": available.recommendation},
                    algorithm="voice-plan-v1",
                )
        eligible_candidates = _eligible_candidates(candidate_inventory)
        narrator_candidate = _narrator_candidate(
            settings,
            registry,
            assignment_source=_effective_assignment_source(
                settings, "Narrator", library=self.voice_library
            ),
        )
        decision_context_sha256 = _digest(
            {
                "group_id": group_id,
                "controls": controls,
                "candidates": [
                    _candidate_decision_identity(candidate)
                    for candidate in eligible_candidates
                ],
                "candidate_inventory": [
                    _candidate_decision_identity(candidate)
                    for candidate in candidate_inventory
                ],
                "narrator": (
                    None
                    if narrator_candidate is None
                    else narrator_candidate.to_document()
                ),
            }
        )
        prior_source = (
            self.decisions.choice_for(group_id, decision_context_sha256)
            if self.voice_library is None
            and self.decisions is not None
            and not ignore_decisions
            else None
        )
        if prior_source is None and self.voice_library is None and eligible_candidates:
            candidate_identities = [
                _candidate_decision_identity(value) for value in eligible_candidates
            ]
            for previous in reversed(saved_groups):
                previous_candidates = previous.get("candidates", ())
                if (
                    previous.get("group_id") != group_id
                    or not isinstance(previous_candidates, list)
                    or not previous_candidates
                    or not all(isinstance(value, dict) for value in previous_candidates)
                ):
                    continue
                if [
                    {key: value.get(key) for key in candidate_identities[0]}
                    for value in previous_candidates
                ] != candidate_identities:
                    continue
                context = previous.get("decision_context_sha256")
                if not isinstance(context, str) or not _is_sha256(context):
                    continue
                decisions = self.decisions
                if decisions is None:
                    continue
                source = decisions.choice_for(group_id, context)
                if (
                    source is not None
                    and source != default_voice_choice_id
                    and source in {value.source_id for value in eligible_candidates}
                ):
                    # An explicit independent voice choice does not depend on the
                    # narrator. Reuse its original evidence key across narrator edits.
                    prior_source = source
                    decision_context_sha256 = context
                    break
        if prior_source is not None:
            if prior_source == default_voice_choice_id:
                selected = (
                    _candidate_from_source(narrator_candidate.source_id, registry)
                    if narrator_candidate is not None
                    else None
                )
            else:
                selected = _candidate_from_source(prior_source, registry)
            if selected is None and prior_source != default_voice_choice_id:
                raise PregenerationVoiceError(
                    f"Saved voice choice is no longer available for {character!r}"
                )
            route = "narrator" if prior_source == default_voice_choice_id else "voice"
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
            candidate = (
                _candidate_from_source(candidate_inventory[0].source_id, registry)
                if candidate_inventory
                else None
            )
        elif assignment_source == default_voice_choice_id:
            route = "narrator"
            resolution = "saved-voice-assignment"
            source_id = default_voice_choice_id
            candidate = (
                _candidate_from_source(narrator_candidate.source_id, registry)
                if narrator_candidate is not None
                else None
            )
        elif candidate_inventory:
            selected_candidate = candidate_inventory[0]
            source_id = selected_candidate.source_id
            candidate = _candidate_from_source(source_id, registry)
            if _requires_audition(eligible_candidates, records):
                route = "needs-audition"
                resolution = "ambiguous-voice-evidence"
                if narrator_candidate is not None:
                    source_id = narrator_candidate.source_id
                    candidate = _candidate_from_source(source_id, registry)
            else:
                route = "voice"
                if assignment_source:
                    resolution = "saved-voice-assignment"
                elif bound_source == source_id:
                    resolution = "exact-source-voice-binding"
                elif len(records) == 1 and len(eligible_candidates) > 1:
                    resolution = "automatic-incidental-role"
                else:
                    resolution = "known-character-voice"
        else:
            route = "narrator"
            resolution = "automatic-narrator-fallback"
            source_id = default_voice_choice_id
            candidate = (
                _candidate_from_source(narrator_candidate.source_id, registry)
                if narrator_candidate is not None
                else None
            )
        if self.voice_library is not None and assignment_source is None:
            if route == "voice":
                remember_voice_binding(
                    self.voice_library,
                    registry,
                    character,
                    source_id,
                    variant_key=variant_key,
                    method="automatic",
                    evidence={"resolution": resolution},
                    algorithm="voice-plan-v1",
                    only_if_unbound=True,
                )
            elif route == "narrator" and is_narrator(character):
                if narrator_candidate is not None:
                    remember_voice_binding(
                        self.voice_library,
                        registry,
                        character,
                        narrator_candidate.source_id,
                        variant_key=variant_key,
                        method="automatic",
                        evidence={"resolution": resolution},
                        algorithm="voice-plan-v1",
                        only_if_unbound=True,
                    )
        selected_identity = _candidate_identity(
            (source_id, candidate) if candidate is not None else None
        )
        candidates = eligible_candidates
        if route == "narrator":
            candidates = ()
        anchor_source_id = (
            candidates[0].source_id
            if route == "needs-audition"
            and candidates
            and candidates[0].match_score >= 100
            else None
        )
        sample_text, alternate_sample_text = _sample_texts(records)
        return VoiceGroup(
            group_id=group_id,
            character=character,
            speakers=speakers,
            portrait=portrait,
            source_bank=source_bank,
            source_voice_id=source_voice_id,
            line_ids=tuple(record.line_id for record in records),
            sample_text=sample_text,
            alternate_sample_text=alternate_sample_text,
            route=route,
            source_id=source_id,
            source_character=(
                candidate.source_character or candidate.character
                if candidate is not None
                else None
            ),
            source_speaker=candidate.speaker if candidate is not None else None,
            reference_sha256s=(
                tuple(selected_identity["references"])
                if selected_identity is not None
                else ()
            ),
            decision_context_sha256=decision_context_sha256,
            control_sha256=_digest(
                {"controls": controls, "selected": selected_identity}
            ),
            resolution=resolution,
            candidates=candidates,
            narrator_candidate=narrator_candidate,
            candidate_inventory=candidate_inventory,
            anchor_source_id=anchor_source_id,
            portrait_image=portrait_image,
            portrait_image_sha256=portrait_image_sha256,
        )


def _load_bound_story(job: PregenerationJob) -> StoryIndexDocument:
    path = Path(job.story_index).expanduser().resolve()
    try:
        document = load_verified_story_index_document(path, job.story_index_sha256)
    except PregenerationVoiceError:
        raise
    except (OSError, StoryIndexError, ValueError) as error:
        raise PregenerationVoiceError(
            f"Unable to read selected dialogue: {error}"
        ) from error
    return document


def _selected_manifest(
    settings: AppSettings, manifest_path: str | Path | None
) -> Path | None:
    value = manifest_path or settings.voice_manifest or find_default_voice_manifest()
    return Path(value).expanduser().resolve() if value else None


def _json_object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise PregenerationVoiceError(f"{label} must be an object")
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise PregenerationVoiceError(f"{label} keys must be text")
        result[key] = item
    return result


def _load_registry(
    manifest_path: Path | None,
) -> tuple[CharacterVoiceRegistry, str | None, JsonObject]:
    if manifest_path is None:
        return CharacterVoiceRegistry(), None, {}
    try:
        before = sha256_file(manifest_path)
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        manifest_document = _json_object(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            "character voice manifest",
        )
        after = sha256_file(manifest_path)
    except (json.JSONDecodeError, OSError, VoiceManifestError, ValueError) as error:
        raise PregenerationVoiceError(
            f"Unable to read character voices: {error}"
        ) from error
    if before != after:
        raise PregenerationVoiceError("Character voices changed while they were read")
    return registry, before, manifest_document


def _materialize_voice_catalog(
    job_store: PregenerationJobStore,
    job: PregenerationJob,
    registry: CharacterVoiceRegistry,
) -> Path:
    voices: list[JsonObject] = []
    payloads: dict[str, bytes] = {}
    identity: list[tuple[str, str, list[str]]] = []
    for voice in sorted(
        registry.unique_voices(), key=lambda value: value.character.casefold()
    ):
        references: list[str] = []
        checksums: list[str] = []
        for reference in voice.references:
            payload = read_voice_reference_bytes(voice, reference)
            checksum = hashlib.sha256(payload).hexdigest()
            suffix = reference.suffix or ".wav"
            relative = f"references/{checksum}{suffix}"
            payloads.setdefault(relative, payload)
            references.append(relative)
            checksums.append(checksum)
        entry: JsonObject = {
            "character": voice.character,
            "speaker": voice.speaker,
            "aliases": [],
            "references": references,
        }
        if voice.source_character:
            entry["vntts.source_character"] = voice.source_character
        voices.append(entry)
        identity.append((voice.character, voice.speaker, checksums))
    digest = _digest(identity)
    root = Path(job_store.path_for(job.job_id)).parent
    destination = root / f"voice-catalog-{digest[:16]}"
    manifest = destination / "manifest.json"
    if manifest.is_file():
        return Path(manifest)
    root.mkdir(parents=True, exist_ok=True)
    with staged_directory(root, prefix=".voice-catalog-") as staging:
        for relative, payload in payloads.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        write_voice_manifest(
            staging / "manifest.json", {"version": 2, "voices": voices}
        )
        CharacterVoiceRegistry.from_file(staging / "manifest.json")
        try:
            rename_directory_no_replace(staging, destination)
        except AtomicPublicationError:
            if not manifest.is_file():
                raise
    return Path(manifest)


def _candidate_for(
    character: str,
    settings: AppSettings,
    registry: CharacterVoiceRegistry,
    *,
    assignment_source: str | None = None,
) -> tuple[str, CharacterVoice] | None:
    source_id = assignment_source
    if source_id is None:
        source_id = _effective_assignment_source(settings, character)
    if source_id == default_voice_choice_id:
        return None
    if source_id:
        candidate = _candidate_from_source(source_id, registry)
        return (source_id, candidate) if candidate is not None else None
    voice = registry.resolve(character)
    if voice is None or not _usable_voice(voice):
        return None
    return f"character:{normalize_character_name(voice.character)}", voice


def _candidate_inventory(
    character: str,
    bound_source: str | None,
    settings: AppSettings,
    registry: CharacterVoiceRegistry,
    candidate_variants: Sequence[JsonObject],
    *,
    assignment_source: str | None = None,
) -> tuple[VoiceCandidate, ...]:
    assignment = assignment_source
    candidates: dict[str, VoiceCandidate] = {}
    candidate_ranks: dict[str, tuple[float, float]] = {}

    def add(
        source_id: str,
        score: int,
        recommendation: str,
        variant: Mapping[str, object] | None = None,
    ) -> None:
        voice = _candidate_from_source(source_id, registry)
        if voice is None:
            return
        variant = variant or {}
        candidate = _ranked_candidate(
            source_id,
            voice,
            score,
            recommendation,
            variant=variant,
        )
        previous = candidates.get(source_id)
        quality = variant.get("quality_score")
        duration = variant.get("duration_seconds")
        rank = (
            -(
                quality
                if isinstance(quality, int) and not isinstance(quality, bool)
                else -1
            ),
            (
                abs(float(duration) - 4.0)
                if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                else float("inf")
            ),
        )
        duplicate = next(
            (
                existing
                for existing, value in candidates.items()
                if candidate.reference_sha256s
                and value.reference_sha256s == candidate.reference_sha256s
            ),
            None,
        )
        if duplicate is not None and duplicate != source_id:
            if candidates[duplicate].match_score >= candidate.match_score:
                return
            del candidates[duplicate]
            del candidate_ranks[duplicate]
        if (
            previous is None
            or candidate.match_score > previous.match_score
            or candidate.match_score == previous.match_score
            and rank < candidate_ranks[source_id]
        ):
            candidates[source_id] = candidate
            candidate_ranks[source_id] = rank

    if assignment and assignment != default_voice_choice_id:
        add(assignment, 120, "Your saved voice assignment")

    if _public_pocket_mode(settings):
        return tuple(candidates.values())

    if bound_source:
        add(bound_source, 120, "Exact voice binding for this dialogue")

    exact = _candidate_for(
        character,
        settings,
        registry,
        assignment_source=assignment,
    )
    if exact is not None:
        add(exact[0], 90, "Exact character name or known alias")

    target = normalize_character_name(character)
    for variant in candidate_variants:
        if (
            not isinstance(variant, dict)
            or normalize_character_name(variant.get("character", "")) != target
        ):
            continue
        voice_character = variant.get("voice_character")
        if not isinstance(voice_character, str) or not voice_character.strip():
            continue
        source_id = f"character:{normalize_character_name(voice_character)}"
        add(source_id, 90, "Reviewed voice for this character", variant)

    return tuple(
        sorted(
            candidates.values(),
            key=lambda value: (
                -value.match_score,
                *candidate_ranks[value.source_id],
                value.source_character.casefold(),
                value.source_id,
            ),
        )
    )


def _narrator_candidate(
    settings: AppSettings,
    registry: CharacterVoiceRegistry,
    *,
    assignment_source: str | None = None,
) -> VoiceCandidate | None:
    selected = _candidate_for(
        "Narrator",
        settings,
        registry,
        assignment_source=assignment_source,
    )
    if selected is not None:
        return _ranked_candidate(
            selected[0],
            selected[1],
            120,
            "Configured narrator voice",
        )
    if settings.speech_backend != "pocket-tts":
        voice = registry.resolve("Narrator")
        if voice is None or not _usable_voice(voice):
            return None
        return _ranked_candidate(
            f"character:{normalize_character_name(voice.character)}",
            voice,
            120,
            "Configured narrator voice",
        )
    source_id = pregeneration_narrator_source_id(settings)
    voice = _candidate_from_source(source_id, registry)
    if voice is None:
        return None
    return _ranked_candidate(
        source_id,
        voice,
        120,
        "Configured narrator voice",
    )


def _effective_assignment_source(
    settings: AppSettings,
    character: str,
    *,
    library: VoiceLibrary | None = None,
    variant_key: str | None = None,
) -> str | None:
    if library is not None:
        binding = library.binding(character, variant_key=variant_key)
        if binding is None and variant_key is not None:
            binding = library.binding(character)
        if (
            binding is not None
            and binding.route == "narrator"
            and binding.provenance.get("method") == "automatic"
        ):
            binding = None
        source_id = voice_binding_source_id(binding) if binding is not None else None
        if (
            source_id
            and source_id != default_voice_choice_id
            and _public_pocket_mode(settings)
            and not source_id.startswith("preset:")
        ):
            raise PregenerationVoiceError(
                f"The saved game voice for {character!r} requires Pocket voice "
                "cloning access. Accept the model terms in Voices or choose an "
                "available engine."
            )
        return source_id
    return None


def _public_pocket_mode(settings: AppSettings) -> bool:
    return bool(
        settings.speech_backend == "pocket-tts"
        and not settings.pocket_gated_model_accepted
    )


def _text_values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise PregenerationVoiceError("Voice candidate text values are invalid")
    return tuple(value)


def _ranked_candidate(
    source_id: str,
    voice: CharacterVoice,
    score: int,
    recommendation: str,
    *,
    variant: Mapping[str, object] | None = None,
) -> VoiceCandidate:
    variant = variant or {}
    return VoiceCandidate(
        source_id=source_id,
        source_character=voice.source_character or voice.character,
        source_speaker=voice.speaker,
        reference_sha256s=tuple(sha256_file(path) for path in voice.references),
        match_score=score,
        recommendation=recommendation,
        portrait=_optional_variant(variant.get("portrait")),
        source_bank=_optional_variant(variant.get("source_bank")),
        source_voice_ids=_text_values(variant.get("source_voice_ids")),
        source_line_ids=_text_values(variant.get("source_line_ids")),
        reference_duration_seconds=_reference_duration_seconds(voice.references),
    )


def _reference_duration_seconds(references: Sequence[Path]) -> float | None:
    total = 0.0
    try:
        for reference in references:
            with wave.open(str(reference), "rb") as audio:
                total += audio.getnframes() / audio.getframerate()
    except EOFError, OSError, ValueError, wave.Error, ZeroDivisionError:
        return None
    return round(total, 3) if references else None


def _voice_reference_stats(registry: CharacterVoiceRegistry) -> tuple[int, int]:
    references = {
        path.resolve()
        for voice in registry.unique_voices()
        for path in voice.references
    }
    return len(references), sum(_file_size(path) for path in references)


def _file_size(path: str | Path | None) -> int:
    if path is None:
        return 0
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def _record_plan_phase(
    name: str, started: float, cpu_started: float, **details: object
) -> None:
    record_background_operation(
        f"pregeneration-voice-plan-{name}",
        (perf_counter() - started) * 1000,
        "complete",
        cpu_ms=(process_time() - cpu_started) * 1000,
        **details,
    )


def _candidate_decision_identity(candidate: VoiceCandidate) -> JsonObject:
    return {
        "source_id": candidate.source_id,
        "source_character": candidate.source_character,
        "source_speaker": candidate.source_speaker,
        "reference_sha256s": list(candidate.reference_sha256s),
        "match_score": candidate.match_score,
    }


def _requires_audition(
    candidates: Sequence[VoiceCandidate], records: Sequence[StoryIndexRecord]
) -> bool:
    if len(candidates) < 2 or len(records) <= 1:
        return False
    first, second = candidates[:2]
    return not (
        first.match_score >= _CLEAR_WINNER_SCORE
        and first.match_score - second.match_score >= _CLEAR_WINNER_MARGIN
    )


def _eligible_candidates(
    candidates: tuple[VoiceCandidate, ...],
) -> tuple[VoiceCandidate, ...]:
    if len(candidates) < 2:
        return candidates
    best_score = candidates[0].match_score
    eligible = tuple(
        candidate
        for candidate in candidates
        if best_score - candidate.match_score < _CLEAR_WINNER_MARGIN
    )
    return (eligible or candidates[:1])[:_MAX_AUDITION_CANDIDATES]


def _manifest_queue_bindings(
    manifest_document: JsonObject, registry: CharacterVoiceRegistry
) -> dict[str, str]:
    if (
        not manifest_document
        or SOURCE_REFERENCE_BINDINGS_FIELD not in manifest_document
    ):
        return {}
    try:
        bindings = queue_voice_overrides_from_manifest(
            manifest_document,
            voices=registry.unique_voices(),
        )
        if not isinstance(bindings, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in bindings.items()
        ):
            raise PregenerationVoiceError("Character voice bindings are invalid")
        return {str(key): str(value) for key, value in bindings.items()}
    except SourceReferenceBindingError as error:
        raise PregenerationVoiceError(
            f"Character voice evidence is invalid: {error}"
        ) from error


def _validate_player_voice_variant(variant: object, index: int, version: int) -> None:
    fields = {
        "variant_id",
        "character",
        "portrait",
        "source_bank",
        "source_voice_ids",
        "voice_character",
        "reference_sha256",
        "source_line_ids",
        "source_event_ids",
        "duration_seconds",
        "quality_score",
    }
    if version >= 2:
        fields.add("portrait_image_sha256")
    if (
        not isinstance(variant, dict)
        or not fields <= set(variant)
        or set(variant) - fields - {"candidate_origin"}
    ):
        raise PregenerationVoiceError(f"Player voice candidate {index} is malformed")
    character = variant.get("character")
    portrait = variant.get("portrait")
    source_bank = variant.get("source_bank")
    voice_character = variant.get("voice_character")
    source_event_ids = variant.get("source_event_ids")
    duration = variant.get("duration_seconds")
    quality = variant.get("quality_score")
    checks = (
        ("variant_id", _is_sha256(variant.get("variant_id"))),
        ("character", isinstance(character, str) and bool(character.strip())),
        (
            "portrait",
            portrait is None or isinstance(portrait, str) and bool(portrait.strip()),
        ),
        ("source_bank", isinstance(source_bank, str) and bool(source_bank.strip())),
        (
            "voice_character",
            isinstance(voice_character, str) and bool(voice_character.strip()),
        ),
        ("reference_sha256", _is_sha256(variant.get("reference_sha256"))),
        (
            "source_voice_ids",
            _canonical_texts(variant.get("source_voice_ids"), allow_empty=True),
        ),
        (
            "source_line_ids",
            _canonical_texts(variant.get("source_line_ids"), allow_empty=True),
        ),
        (
            "source_event_ids",
            _canonical_nonnegative_ints(source_event_ids),
        ),
        (
            "duration_seconds",
            not isinstance(duration, bool)
            and isinstance(duration, (int, float))
            and math.isfinite(duration)
            and duration > 0,
        ),
        (
            "quality_score",
            not isinstance(quality, bool)
            and isinstance(quality, int)
            and 0 <= quality <= 100,
        ),
        (
            "portrait_image_sha256",
            version < 2
            or variant.get("portrait_image_sha256") is None
            or _is_sha256(variant["portrait_image_sha256"]),
        ),
        (
            "candidate_origin",
            variant.get("candidate_origin")
            in {None, "exact_bank_unrouted_media", "story_line_route"},
        ),
    )
    invalid = next((field for field, valid in checks if not valid), None)
    if invalid is not None:
        raise PregenerationVoiceError(
            f"Player voice candidate {index} has invalid {invalid}"
        )


def _record_rejected_player_voice_candidate(reason: str) -> None:
    from vntts.support import record_game_import

    record_game_import(
        "voice-candidate-validation",
        outcome="rejected",
        reason=str(reason),
    )


def _manifest_candidate_variants(
    manifest_document: JsonObject,
    registry: CharacterVoiceRegistry,
    manifest_path: Path | None,
    story_index_sha256: str,
) -> tuple[JsonObject, ...]:
    bindings = manifest_document.get(SOURCE_REFERENCE_BINDINGS_FIELD, {})
    variants = list(
        bindings.get("selected_variants", ()) if isinstance(bindings, dict) else ()
    )
    player = manifest_document.get(PLAYER_VOICE_CANDIDATES_FIELD)
    if player is None:
        return tuple(variants)
    expected_fields = {
        "schema",
        "schema_version",
        "story_index_sha256",
        "candidate_report",
        "candidate_report_sha256",
        "variants",
    }
    if (
        not isinstance(player, dict)
        or set(player) != expected_fields
        or player.get("schema") != PLAYER_VOICE_CANDIDATES_SCHEMA
        or player.get("schema_version") not in PLAYER_VOICE_CANDIDATES_VERSIONS
        or player.get("story_index_sha256") != story_index_sha256
        or manifest_path is None
    ):
        raise PregenerationVoiceError("Player voice candidate evidence is invalid")
    report_relative = player.get("candidate_report")
    if (
        not isinstance(report_relative, str)
        or not report_relative.strip()
        or "\\" in report_relative
    ):
        raise PregenerationVoiceError("Player voice candidate report path is invalid")
    relative = PurePosixPath(report_relative)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise PregenerationVoiceError("Player voice candidate report path is unsafe")
    report = (manifest_path.parent / Path(*relative.parts)).resolve()
    try:
        report.relative_to(manifest_path.parent.resolve())
    except ValueError as error:
        raise PregenerationVoiceError(
            "Player voice candidate report escapes its manifest"
        ) from error
    if (
        report.is_symlink()
        or not report.is_file()
        or not _is_sha256(player.get("candidate_report_sha256"))
        or sha256_file(report) != player["candidate_report_sha256"]
    ):
        raise PregenerationVoiceError("Player voice candidate report changed")
    values = player.get("variants")
    if not isinstance(values, list) or not values:
        raise PregenerationVoiceError("Player voice candidate inventory is empty")
    seen = set()
    version = player["schema_version"]
    for index, variant in enumerate(values):
        try:
            _validate_player_voice_variant(variant, index, version)
        except PregenerationVoiceError as error:
            _record_rejected_player_voice_candidate(str(error))
            continue
        variant_id = variant["variant_id"]
        if variant_id in seen:
            _record_rejected_player_voice_candidate(
                f"Player voice candidate {index} duplicates an earlier candidate"
            )
            continue
        voice_character = variant["voice_character"]
        reference_sha256 = variant["reference_sha256"]
        source_id = f"character:{normalize_character_name(voice_character)}"
        voice = _candidate_from_source(source_id, registry)
        if voice is None or tuple(sha256_file(path) for path in voice.references) != (
            reference_sha256,
        ):
            _record_rejected_player_voice_candidate(
                f"Player voice candidate {index} reference is invalid"
            )
            continue
        seen.add(variant_id)
        variants.append(dict(variant))
    return tuple(variants)


def validated_player_voice_candidates(
    manifest_path: Path,
) -> tuple[CharacterVoiceRegistry, tuple[JsonObject, ...]]:
    """Read the same checksum-validated candidates used by story preparation."""
    registry, _digest, document = _load_registry(manifest_path)
    player = _json_object(
        document.get(PLAYER_VOICE_CANDIDATES_FIELD), "player voice candidates"
    )
    story_sha256 = player.get("story_index_sha256")
    if not isinstance(story_sha256, str) or not _is_sha256(story_sha256):
        raise PregenerationVoiceError(
            "Player voice candidate story checksum is invalid"
        )
    return registry, _manifest_candidate_variants(
        document, registry, manifest_path, story_sha256
    )


def _canonical_texts(values: object, *, allow_empty: bool = False) -> bool:
    if (
        not isinstance(values, list)
        or (not values and not allow_empty)
        or any(not isinstance(value, str) or not value.strip() for value in values)
    ):
        return False
    return values == sorted(set(values), key=str.casefold)


def _canonical_nonnegative_ints(values: object) -> bool:
    return (
        isinstance(values, list)
        and all(
            not isinstance(value, bool) and isinstance(value, int) and value >= 0
            for value in values
        )
        and values == sorted(set(values))
    )


def _bound_source_for_record(
    record: StoryIndexRecord, bindings: Mapping[str, str]
) -> str | None:
    if not bindings:
        return None
    queue_id = expected_voice_generation_queue_id(
        record.line_id,
        text_sha256(record.text),
    )
    voice_character = bindings.get(queue_id)
    if not voice_character:
        return None
    return f"character:{normalize_character_name(voice_character)}"


def _candidate_from_source(
    source_id: str, registry: CharacterVoiceRegistry
) -> CharacterVoice | None:
    try:
        voice = registry.resolve_source(source_id)
    except VoiceManifestError as error:
        raise PregenerationVoiceError(str(error)) from error
    return voice if voice is not None and _usable_voice(voice) else None


def _usable_voice(voice: CharacterVoice) -> bool:
    if voice.reference_root is None and not voice.references:
        return True
    return bool(voice.references) and all(path.is_file() for path in voice.references)


def _candidate_identity(
    candidate: tuple[str, CharacterVoice] | None,
) -> CandidateIdentity | None:
    if candidate is None:
        return None
    source_id, voice = candidate
    return {
        "source_id": source_id,
        "character": voice.character,
        "speaker": voice.speaker,
        "references": [sha256_file(path) for path in voice.references],
    }


def _variant_evidence(record: StoryIndexRecord) -> VariantEvidence:
    return (
        _optional_variant(record.producer_fields.get("portrait")),
        _optional_variant(record.producer_fields.get("source_bank")),
        _optional_variant(record.producer_fields.get("source_voice_id")),
    )


def _portrait_snapshot(
    content_root: str | Path,
    portrait: str | None,
    cache: dict[str, PortraitSnapshot],
) -> PortraitSnapshot:
    if portrait is None:
        return None, None
    text = str(portrait).strip()
    if not text or "\\" in text or Path(text).name != text or text in {".", ".."}:
        return None, None
    cached = cache.get(text)
    if cached is not None:
        return cached
    root = Path(content_root).resolve()
    names = (text,) if Path(text).suffix else (text, f"{text}.png")
    result: PortraitSnapshot = (None, None)
    for name in names:
        candidate = root / "portraits" / name
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        before = sha256_file(resolved)
        if sha256_file(resolved) != before:
            raise PregenerationVoiceError(
                f"Character portrait changed while it was read: {text}"
            )
        result = str(resolved), before
        break
    cache[text] = result
    return result


def _optional_variant(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return _digest(value)


def _sample_texts(
    records: Sequence[StoryIndexRecord],
) -> tuple[str, str | None]:
    eligible = list(
        dict.fromkeys(record.text.strip() for record in records if record.text.strip())
    )
    if not eligible:
        return "Voice preview.", None
    ranked = sorted(eligible, key=lambda value: (abs(len(value) - 90), len(value)))
    return ranked[0], ranked[1] if len(ranked) > 1 else None


def _synthesis_controls(settings: AppSettings) -> SynthesisControls:
    return {
        "backend": settings.speech_backend,
        "model": settings.tts_model,
        "language": settings.tts_language,
        "profile": (
            "default"
            if settings.speech_backend == "pocket-tts"
            else settings.tts_profile
        ),
        "pocket_voice_cloning": (
            settings.pocket_gated_model_accepted
            if settings.speech_backend == "pocket-tts"
            else None
        ),
        "narrator_speaker": settings.narrator_speaker,
        "narrator_reference": _path_identity(settings.tts_speaker_wav),
    }


def _path_identity(value: str | Path | None) -> JsonObject | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return {
        "path": str(path),
        "sha256": sha256_file(path) if path.is_file() else None,
    }


def _decision_key(group_id: str, decision_context_sha256: str) -> str:
    if not _is_sha256(group_id) or not _is_sha256(decision_context_sha256):
        raise PregenerationVoiceError("Voice decision identity is invalid")
    return _digest([group_id, decision_context_sha256])


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _raise_if_cancelled(cancellation: Cancellation | None) -> None:
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
    "pregeneration_narrator_source_id",
    "resolve_pregeneration_settings",
]
