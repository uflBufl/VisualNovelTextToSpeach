"""Checksum-bound voice routing for player-owned offline preparation jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_generation_queue import (
    expected_voice_generation_queue_id,
    text_sha256,
)
from vntts_artifacts.voice_manifest import VoiceManifestError, normalize_character_name

from vntts.authoring.source_reference_bindings import (
    SOURCE_REFERENCE_BINDINGS_FIELD,
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
)
from vntts.versioned_json import read_versioned_json, write_versioned_json
from vntts.voices import (
    CharacterVoiceRegistry,
    default_voice_choice_id,
    find_default_voice_manifest,
    find_voice_assignment,
    is_narrator,
    pocket_tts_preset_voices,
    synthesis_character_for_line,
)

voice_plan_schema_version = 3
voice_decisions_schema_version = 1
PLAYER_VOICE_CANDIDATES_FIELD = "vntts.player.voice_candidates"
PLAYER_VOICE_CANDIDATES_SCHEMA = "vntts.player-voice-candidates"
PLAYER_VOICE_CANDIDATES_VERSION = 2
PLAYER_VOICE_CANDIDATES_VERSIONS = frozenset({1, PLAYER_VOICE_CANDIDATES_VERSION})
_CLEAR_WINNER_SCORE = 80
_CLEAR_WINNER_MARGIN = 20


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
    match_score: int = 0
    recommendation: str = "Available character voice"
    portrait: str | None = None
    source_bank: str | None = None
    source_voice_ids: tuple[str, ...] = ()
    source_line_ids: tuple[str, ...] = ()

    def to_document(self):
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
    age: str | None
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

    def to_document(self):
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
        self.remember_many(((group, source_id),))

    def remember_many(self, selections):
        selections = tuple(selections)
        if not selections:
            raise PregenerationVoiceError("At least one voice choice is required")
        decisions = self._load()
        decided_at = self.clock().astimezone(timezone.utc).isoformat()
        observed_groups = set()
        for group, source_id in selections:
            if group.route != "needs-audition":
                raise PregenerationVoiceError(
                    "Only an unresolved voice audition can create a player decision"
                )
            if group.group_id in observed_groups:
                raise PregenerationVoiceError(
                    "A voice group was selected more than once"
                )
            observed_groups.add(group.group_id)
            source_id = _required_text(source_id, "voice source")
            allowed_sources = {
                default_voice_choice_id,
                *(candidate.source_id for candidate in group.candidates),
            }
            if source_id not in allowed_sources:
                raise PregenerationVoiceError(
                    "The selected voice is not a candidate for this audition"
                )
            decisions[_decision_key(group.group_id, group.decision_context_sha256)] = {
                "group_id": group.group_id,
                "decision_context_sha256": group.decision_context_sha256,
                "source_id": source_id,
                "decided_at": decided_at,
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

    def create(
        self,
        job,
        settings,
        *,
        manifest_path=None,
        cancellation=None,
        ignore_decisions=False,
    ):
        _raise_if_cancelled(cancellation)
        document = _load_bound_story(job)
        _raise_if_cancelled(cancellation)
        manifest_path = _selected_manifest(settings, manifest_path)
        registry, manifest_sha256, manifest_document = _load_registry(manifest_path)
        queue_bindings = _manifest_queue_bindings(manifest_document, registry)
        candidate_variants = _manifest_candidate_variants(
            manifest_document,
            registry,
            manifest_path,
            job.story_index_sha256,
            Path(job.story_index).resolve().parent,
        )
        controls = _synthesis_controls(settings)
        controls_sha256 = _digest(controls)
        records = {
            record.line_id: record
            for record in document.records
            if record.line_id in set(job.selected_line_ids)
        }
        portrait_snapshots = {}
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
            bound_source = _bound_source_for_record(record, queue_bindings)
            portrait_image, portrait_image_sha256 = _portrait_snapshot(
                Path(job.story_index).expanduser().resolve().parent,
                evidence[0],
                portrait_snapshots,
            )
            identity = [
                normalize_character_name(character),
                *evidence,
                bound_source,
                portrait_image_sha256,
            ]
            group_id = _digest(identity)
            grouped.setdefault(group_id, []).append(
                (
                    record,
                    character,
                    evidence,
                    bound_source,
                    portrait_image,
                    portrait_image_sha256,
                )
            )

        groups = tuple(
            self._resolve_group(
                group_id,
                values,
                settings,
                registry,
                candidate_variants,
                controls,
                ignore_decisions,
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

    def _resolve_group(
        self,
        group_id,
        values,
        settings,
        registry,
        candidate_variants,
        controls,
        ignore_decisions,
    ):
        records = tuple(value[0] for value in values)
        character = values[0][1]
        portrait, age, source_bank, source_voice_id = values[0][2]
        bound_source = values[0][3]
        portrait_image, portrait_image_sha256 = values[0][4:6]
        speakers = tuple(dict.fromkeys(record.speaker for record in records))
        assignment_source = find_voice_assignment(settings.voice_assignments, character)
        candidate_inventory = _candidate_inventory(
            character,
            records,
            portrait,
            source_bank,
            source_voice_id,
            bound_source,
            settings,
            registry,
            candidate_variants,
        )
        eligible_candidates = _eligible_candidates(candidate_inventory)
        narrator_candidate = _narrator_candidate(settings, registry)
        decision_context_sha256 = _digest(
            {
                "group_id": group_id,
                "controls": controls,
                "candidates": [
                    _candidate_decision_identity(candidate)
                    for candidate in eligible_candidates
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
            if self.decisions is not None and not ignore_decisions
            else None
        )
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
            age=age,
            source_bank=source_bank,
            source_voice_id=source_voice_id,
            line_ids=tuple(record.line_id for record in records),
            sample_text=sample_text,
            alternate_sample_text=alternate_sample_text,
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
            narrator_candidate=narrator_candidate,
            candidate_inventory=candidate_inventory,
            anchor_source_id=anchor_source_id,
            portrait_image=portrait_image,
            portrait_image_sha256=portrait_image_sha256,
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
        return CharacterVoiceRegistry(), None, {}
    try:
        before = sha256_file(manifest_path)
        registry = CharacterVoiceRegistry.from_file(manifest_path)
        manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
        after = sha256_file(manifest_path)
    except (json.JSONDecodeError, OSError, VoiceManifestError, ValueError) as error:
        raise PregenerationVoiceError(
            f"Unable to read character voices: {error}"
        ) from error
    if before != after:
        raise PregenerationVoiceError("Character voices changed while they were read")
    return registry, before, manifest_document


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


def _candidate_inventory(
    character,
    records,
    portrait,
    source_bank,
    source_voice_id,
    bound_source,
    settings,
    registry,
    candidate_variants,
):
    assignment = find_voice_assignment(settings.voice_assignments, character)
    if assignment and assignment != default_voice_choice_id:
        voice = _candidate_from_source(assignment, registry)
        return (
            (
                _ranked_candidate(
                    assignment,
                    voice,
                    120,
                    "Your saved voice assignment",
                ),
            )
            if voice is not None
            else ()
        )

    candidates = {}

    def add(source_id, score, recommendation, variant=None):
        voice = _candidate_from_source(source_id, registry)
        if voice is None:
            return
        candidate = _ranked_candidate(
            source_id,
            voice,
            score,
            recommendation,
            variant=variant,
        )
        previous = candidates.get(source_id)
        if previous is None or candidate.match_score > previous.match_score:
            candidates[source_id] = candidate

    if bound_source:
        add(bound_source, 120, "Exact voice binding for this dialogue")

    exact = _candidate_for(character, settings, registry)
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
        variant_portrait = _optional_variant(variant.get("portrait"))
        variant_bank = _optional_variant(variant.get("source_bank"))
        variant_voice_ids = variant.get("source_voice_ids", ())
        voice = _candidate_from_source(source_id, registry)
        if voice is None:
            continue
        if source_voice_id and any(
            _same_identity(source_voice_id, value) for value in variant_voice_ids
        ):
            score = 110
            reason = "Same original game voice ID"
        elif (
            portrait
            and source_bank
            and (
                portrait == variant_portrait
                and _same_identity(source_bank, variant_bank)
            )
        ):
            score = 100
            reason = "Same character portrait and original voice bank"
        elif portrait and portrait == variant_portrait:
            score = 75
            reason = "Same character portrait"
        elif source_bank and _same_identity(source_bank, variant_bank):
            score = 65
            reason = "Same original voice bank"
        else:
            score = 45
            reason = "Reviewed voice from another variant of this character"
        add(source_id, score, reason, variant)

    return tuple(
        sorted(
            candidates.values(),
            key=lambda value: (
                -value.match_score,
                value.source_character.casefold(),
                value.source_id,
            ),
        )
    )


def _narrator_candidate(settings, registry):
    selected = _candidate_for("Narrator", settings, registry)
    if selected is not None:
        return _ranked_candidate(
            selected[0],
            selected[1],
            120,
            "Configured narrator voice",
        )
    if settings.speech_backend != "pocket-tts":
        return None
    speaker = next(
        (
            value
            for value in (settings.narrator_speaker, settings.tts_speaker, "alba")
            if value in pocket_tts_preset_voices
        ),
        "alba",
    )
    source_id = f"preset:{speaker}"
    voice = _candidate_from_source(source_id, registry)
    return _ranked_candidate(
        source_id,
        voice,
        120,
        "Configured narrator voice",
    )


def _ranked_candidate(source_id, voice, score, recommendation, *, variant=None):
    identity = _candidate_identity((source_id, voice))
    variant = variant or {}
    return VoiceCandidate(
        source_id=source_id,
        source_character=voice.character,
        source_speaker=voice.speaker,
        reference_sha256s=tuple(identity["references"]),
        match_score=score,
        recommendation=recommendation,
        portrait=_optional_variant(variant.get("portrait")),
        source_bank=_optional_variant(variant.get("source_bank")),
        source_voice_ids=tuple(variant.get("source_voice_ids", ())),
        source_line_ids=tuple(variant.get("source_line_ids", ())),
    )


def _candidate_decision_identity(candidate):
    return {
        "source_id": candidate.source_id,
        "source_character": candidate.source_character,
        "source_speaker": candidate.source_speaker,
        "reference_sha256s": list(candidate.reference_sha256s),
        "match_score": candidate.match_score,
    }


def _requires_audition(candidates, records):
    if len(candidates) < 2 or len(records) <= 1:
        return False
    first, second = candidates[:2]
    return not (
        first.match_score >= _CLEAR_WINNER_SCORE
        and first.match_score - second.match_score >= _CLEAR_WINNER_MARGIN
    )


def _eligible_candidates(candidates):
    if len(candidates) < 2:
        return candidates
    best_score = candidates[0].match_score
    eligible = tuple(
        candidate
        for candidate in candidates
        if best_score - candidate.match_score < _CLEAR_WINNER_MARGIN
    )
    return eligible or candidates[:1]


def _manifest_queue_bindings(manifest_document, registry):
    if (
        not manifest_document
        or SOURCE_REFERENCE_BINDINGS_FIELD not in manifest_document
    ):
        return {}
    try:
        return queue_voice_overrides_from_manifest(
            manifest_document,
            voices=registry.unique_voices(),
        )
    except SourceReferenceBindingError as error:
        raise PregenerationVoiceError(
            f"Character voice evidence is invalid: {error}"
        ) from error


def _manifest_candidate_variants(
    manifest_document,
    registry,
    manifest_path,
    story_index_sha256,
    content_root,
):
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
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
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
        if not isinstance(variant, dict) or set(variant) != fields:
            raise PregenerationVoiceError(
                f"Player voice candidate {index} is malformed"
            )
        variant_id = variant.get("variant_id")
        character = variant.get("character")
        source_bank = variant.get("source_bank")
        voice_character = variant.get("voice_character")
        reference_sha256 = variant.get("reference_sha256")
        source_voice_ids = variant.get("source_voice_ids")
        source_line_ids = variant.get("source_line_ids")
        source_event_ids = variant.get("source_event_ids")
        portrait = variant.get("portrait")
        duration = variant.get("duration_seconds")
        quality = variant.get("quality_score")
        portrait_image_sha256 = variant.get("portrait_image_sha256")
        if (
            not _is_sha256(variant_id)
            or variant_id in seen
            or not isinstance(character, str)
            or not character.strip()
            or portrait is not None
            and (not isinstance(portrait, str) or not portrait.strip())
            or not isinstance(source_bank, str)
            or not source_bank.strip()
            or not isinstance(voice_character, str)
            or not voice_character.strip()
            or not _is_sha256(reference_sha256)
            or not _canonical_texts(source_voice_ids)
            or not _canonical_texts(source_line_ids)
            or not isinstance(source_event_ids, list)
            or source_event_ids != sorted(set(source_event_ids))
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in source_event_ids
            )
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration <= 0
            or isinstance(quality, bool)
            or not isinstance(quality, int)
            or not 0 <= quality <= 100
            or version >= 2
            and portrait_image_sha256 is not None
            and not _is_sha256(portrait_image_sha256)
        ):
            raise PregenerationVoiceError(
                f"Player voice candidate {index} evidence is invalid"
            )
        if version >= 2:
            _portrait_path, actual_portrait_sha256 = _portrait_snapshot(
                content_root,
                portrait,
                {},
            )
            if portrait_image_sha256 != actual_portrait_sha256:
                raise PregenerationVoiceError(
                    f"Player voice candidate {index} portrait changed"
                )
        source_id = f"character:{normalize_character_name(voice_character)}"
        voice = _candidate_from_source(source_id, registry)
        if voice is None or tuple(sha256_file(path) for path in voice.references) != (
            reference_sha256,
        ):
            raise PregenerationVoiceError(
                f"Player voice candidate {index} reference is invalid"
            )
        seen.add(variant_id)
        variants.append(dict(variant))
    return tuple(variants)


def _canonical_texts(values):
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value.strip() for value in values)
    ):
        return False
    return values == sorted(set(values), key=str.casefold)


def _bound_source_for_record(record, bindings):
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


def _same_identity(first, second):
    return bool(first and second) and normalize_character_name(
        str(first)
    ) == normalize_character_name(str(second))


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


def _variant_evidence(record):
    return tuple(
        _optional_variant(record.producer_fields.get(field))
        for field in ("portrait", "age", "source_bank", "source_voice_id")
    )


def _portrait_snapshot(content_root, portrait, cache):
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
    result = (None, None)
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


def _optional_variant(value):
    if value is None:
        return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        return text or None
    return _digest(value)


def _sample_texts(records):
    eligible = list(
        dict.fromkeys(record.text.strip() for record in records if record.text.strip())
    )
    if not eligible:
        return "Voice preview.", None
    ranked = sorted(eligible, key=lambda value: (abs(len(value) - 90), len(value)))
    return ranked[0], ranked[1] if len(ranked) > 1 else None


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
