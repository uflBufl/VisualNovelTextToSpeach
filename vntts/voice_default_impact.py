"""Read-only comparison of a voice default with verified prepared recordings."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import load_generated_audio_document
from vntts_artifacts.story_index import StoryIndexRecord, load_story_index_document

from vntts.chapter_voice_preload import _validated_source_audio_line_ids
from vntts.game_pack import import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_setup import (
    GameContent,
    PregenerationJobStore,
)
from vntts.pregeneration_voices import (
    PregenerationVoiceError,
    VoiceDecisionStore,
    VoiceGroup,
    VoicePlanStore,
    _raise_if_cancelled,
)
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import is_narrator, normalize_character_name


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class _CompatiblePack:
    selection_id: str
    title: str
    line_ids: tuple[str, ...]
    records: dict[str, StoryIndexRecord]
    library: GeneratedAudioLibrary | None
    authoritative_source_line_ids: frozenset[str]


SavedVoiceStatus = Literal["not-saved", "unknown", "matching", "changed"]


@dataclass(frozen=True)
class StoryVoiceImpact:
    selection_id: str
    title: str
    changed_line_ids: tuple[str, ...]
    matching: int = 0
    original: int = 0
    unknown: int = 0
    needs_choice: int = 0


def _verified_records(content: GameContent) -> dict[str, StoryIndexRecord]:
    story = load_story_index_document(content.story_index)
    return {record.line_id: record for record in story.records}


def _saved_packs(
    content: GameContent,
    job_store: PregenerationJobStore,
    cancellation: Cancellation | None,
) -> dict[str, list[Path]]:
    saved: dict[str, list[Path]] = {}
    for job in job_store.jobs_for_content(content):
        _raise_if_cancelled(cancellation)
        for manifest in job_store.published_packs(job):
            for selection_id in job.selected_story_ids:
                saved.setdefault(selection_id, []).append(manifest)
    return saved


def _load_pack(
    path: Path,
    cache: dict[
        Path,
        tuple[
            dict[str, StoryIndexRecord],
            GeneratedAudioLibrary | None,
            frozenset[str],
        ],
    ],
    cancellation: Cancellation | None,
) -> tuple[dict[str, StoryIndexRecord], GeneratedAudioLibrary | None, frozenset[str]]:
    _raise_if_cancelled(cancellation)
    path = path.expanduser().resolve()
    if path not in cache:
        imported = import_game_pack(path)
        _raise_if_cancelled(cancellation)
        document = load_story_index_document(imported.story_index)
        library = (
            GeneratedAudioLibrary(
                load_generated_audio_document(imported.generated_audio_manifest),
                cache_size=1,
            )
            if imported.generated_audio_manifest
            else None
        )
        cache[path] = (
            {record.line_id: record for record in document.records},
            library,
            _validated_source_audio_line_ids(imported.story_index, document),
        )
    return cache[path]


def _compatible_packs(
    content: GameContent,
    job_store: PregenerationJobStore,
    settings: AppSettings,
    records: dict[str, StoryIndexRecord],
    cancellation: Cancellation | None,
) -> tuple[_CompatiblePack, ...]:
    saved = _saved_packs(content, job_store, cancellation)
    cache: dict[
        Path,
        tuple[
            dict[str, StoryIndexRecord],
            GeneratedAudioLibrary | None,
            frozenset[str],
        ],
    ] = {}
    selected: list[_CompatiblePack] = []
    for selection in content.selections:
        _raise_if_cancelled(cancellation)
        candidates = sorted(
            set(saved.get(selection.selection_id, ())),
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
            reverse=True,
        )
        if settings.game_pack:
            candidates.insert(0, Path(settings.game_pack))
        for candidate in candidates:
            pack_records, library, authoritative_source_line_ids = _load_pack(
                candidate, cache, cancellation
            )
            if all(
                line_id in pack_records
                and pack_records[line_id].text_sha256 == records[line_id].text_sha256
                for line_id in selection.line_ids
            ):
                selected.append(
                    _CompatiblePack(
                        selection.selection_id,
                        selection.title,
                        selection.line_ids,
                        pack_records,
                        library,
                        authoritative_source_line_ids,
                    )
                )
                break
    return tuple(selected)


def _impact_for_pack(
    pack: _CompatiblePack,
    records: dict[str, StoryIndexRecord],
    old_groups: dict[str, VoiceGroup],
    new_groups: dict[str, VoiceGroup],
    proposed: AppSettings,
    role: str,
    narrator: bool,
    cancellation: Cancellation | None,
) -> StoryVoiceImpact:
    changed: list[str] = []
    matching = original = unknown = needs_choice = 0
    for line_id in pack.line_ids:
        _raise_if_cancelled(cancellation)
        record = records[line_id]
        if record.speakable and line_id in pack.authoritative_source_line_ids:
            original += 1
            continue
        group = new_groups.get(line_id)
        old_group = old_groups.get(line_id)
        if group is None:
            continue
        if not _matches_role(group, old_group, role, narrator):
            continue
        if group.route == "needs-audition":
            needs_choice += 1
            continue
        saved_voice = _saved_voice_status(
            pack.library,
            line_id,
            record.text_sha256,
            group,
            proposed,
        )
        if saved_voice == "unknown":
            unknown += 1
        elif saved_voice == "matching":
            matching += 1
        elif saved_voice == "changed":
            changed.append(line_id)
    return StoryVoiceImpact(
        pack.selection_id,
        pack.title,
        tuple(changed),
        matching,
        original,
        unknown,
        needs_choice,
    )


def _matches_role(
    group: VoiceGroup,
    old_group: VoiceGroup | None,
    role: str,
    narrator: bool,
) -> bool:
    return normalize_character_name(group.character) == role or (
        narrator
        and any(
            value is not None and value.route == "narrator"
            for value in (old_group, group)
        )
    )


def _saved_voice_status(
    library: GeneratedAudioLibrary | None,
    line_id: str,
    text_sha256: str,
    group: VoiceGroup,
    proposed: AppSettings,
) -> SavedVoiceStatus:
    if (
        library is None
        or library.index.find(line_id, text_sha256, verify_file=False) is None
    ):
        return "not-saved"
    prepared = library.find(line_id, text_sha256)
    if prepared is None:
        raise PregenerationVoiceError(
            f"Saved audio is missing or damaged for {line_id}."
        )
    identity = prepared.recorded_voice
    if identity is None:
        return "unknown"
    references = group.reference_sha256s[:1]
    speaker = group.source_speaker or (
        "alba"
        if proposed.speech_backend == "pocket-tts" and group.route == "narrator"
        else None
    )
    return (
        "matching"
        if (identity["speaker"], tuple(identity["reference_sha256s"]))
        == (speaker, references)
        else "changed"
    )


def inspect_voice_default_impact(
    content: GameContent,
    job_store: PregenerationJobStore,
    decisions: VoiceDecisionStore,
    settings: AppSettings,
    role: str,
    *,
    current_voice_library: VoiceLibrary,
    proposed_voice_library: VoiceLibrary,
    cancellation: Cancellation | None = None,
) -> tuple[StoryVoiceImpact, ...]:
    """Use the real planner and saved decisions without changing durable work."""
    _raise_if_cancelled(cancellation)
    if sha256_file(content.story_index) != content.story_index_sha256:
        raise PregenerationVoiceError(
            "Story content changed. Refresh Stories and retry."
        )
    records = _verified_records(content)
    selected = _compatible_packs(content, job_store, settings, records, cancellation)
    if not selected:
        return ()
    with TemporaryDirectory(prefix="vntts-voice-impact-") as temporary:
        root = Path(temporary)
        preview_jobs = PregenerationJobStore(root / "jobs")
        job = preview_jobs.create_or_resume(
            content, tuple(pack.selection_id for pack in selected)
        )
        old_library = current_voice_library.copy_to(root / "current-voices")
        new_library = proposed_voice_library.copy_to(root / "proposed-voices")
        old_plan = VoicePlanStore(
            preview_jobs, decisions=decisions, voice_library=old_library
        ).create(job, settings, cancellation=cancellation)
        new_plan = VoicePlanStore(
            preview_jobs, decisions=decisions, voice_library=new_library
        ).create(job, settings, cancellation=cancellation)
    old_groups = {
        line_id: group for group in old_plan.groups for line_id in group.line_ids
    }
    new_groups = {
        line_id: group for group in new_plan.groups for line_id in group.line_ids
    }
    narrator = is_narrator(role)
    role = normalize_character_name(role)
    return tuple(
        _impact_for_pack(
            pack,
            records,
            old_groups,
            new_groups,
            settings,
            role,
            narrator,
            cancellation,
        )
        for pack in selected
    )
