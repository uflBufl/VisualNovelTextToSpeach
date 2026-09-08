"""Read-only comparison of a voice default with verified prepared recordings."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import load_generated_audio_document
from vntts_artifacts.story_index import load_story_index_document

from vntts.game_pack import import_game_pack
from vntts.generated_audio import GeneratedAudioLibrary
from vntts.pregeneration_setup import PregenerationJobStore
from vntts.pregeneration_voices import (
    PregenerationVoiceError,
    VoicePlanStore,
    _raise_if_cancelled,
)
from vntts.voices import is_narrator, normalize_character_name


@dataclass(frozen=True)
class StoryVoiceImpact:
    selection_id: str
    title: str
    changed_line_ids: tuple[str, ...]
    matching: int = 0
    original: int = 0
    unknown: int = 0
    needs_choice: int = 0


def inspect_voice_default_impact(
    content, job_store, decisions, settings, proposed, role, *, cancellation=None
):
    """Use the real planner and saved decisions without changing durable work."""
    _raise_if_cancelled(cancellation)
    if sha256_file(content.story_index) != content.story_index_sha256:
        raise PregenerationVoiceError(
            "Story content changed. Refresh Stories and retry."
        )
    story = load_story_index_document(content.story_index)
    records = {record.line_id: record for record in story.records}
    saved = {}
    for job in job_store.jobs_for_content(content):
        _raise_if_cancelled(cancellation)
        for manifest in job_store.published_packs(job):
            for selection_id in job.selected_story_ids:
                saved.setdefault(selection_id, []).append(manifest)
    packs = {}

    def load_pack(path):
        _raise_if_cancelled(cancellation)
        path = Path(path).expanduser().resolve()
        if path not in packs:
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
            packs[path] = (
                {record.line_id: record for record in document.records},
                library,
            )
        return packs[path]

    selected = []
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
            pack_records, library = load_pack(candidate)
            if all(
                line_id in pack_records
                and pack_records[line_id].text_sha256 == records[line_id].text_sha256
                for line_id in selection.line_ids
            ):
                selected.append((selection, pack_records, library))
                break
    if not selected:
        return ()
    with TemporaryDirectory(prefix="vntts-voice-impact-") as temporary:
        preview_jobs = PregenerationJobStore(Path(temporary))
        job = preview_jobs.create_or_resume(
            content, tuple(selection.selection_id for selection, _, _ in selected)
        )
        planner = VoicePlanStore(preview_jobs, decisions=decisions)
        old_plan = planner.create(job, settings, cancellation=cancellation)
        new_plan = planner.create(job, proposed, cancellation=cancellation)
    old_groups = {
        line_id: group for group in old_plan.groups for line_id in group.line_ids
    }
    new_groups = {
        line_id: group for group in new_plan.groups for line_id in group.line_ids
    }
    narrator = is_narrator(role)
    role = normalize_character_name(role)
    results = []
    for selection, pack_records, library in selected:
        changed = []
        matching = original = unknown = needs_choice = 0
        for line_id in selection.line_ids:
            _raise_if_cancelled(cancellation)
            record = records[line_id]
            if (
                record.speakable
                and pack_records[line_id].source_audio_status == "available"
            ):
                original += 1
                continue
            group = new_groups.get(line_id)
            old_group = old_groups.get(line_id)
            if group is None or not (
                normalize_character_name(group.character) == role
                or narrator
                and any(
                    value is not None and value.route == "narrator"
                    for value in (old_group, group)
                )
            ):
                continue
            if group.route == "needs-audition":
                needs_choice += 1
                continue
            if library is None:
                continue
            entry = library.index.find(line_id, record.text_sha256, verify_file=False)
            if entry is None:
                continue
            prepared = library.find(line_id, record.text_sha256)
            if prepared is None:
                raise PregenerationVoiceError(
                    f"Saved audio is missing or damaged for {line_id}."
                )
            identity = prepared.recorded_voice
            if identity is None:
                unknown += 1
                continue
            # These engines synthesize from the first selected reference only.
            references = group.reference_sha256s[:1]
            speaker = group.source_speaker or (
                "alba"
                if proposed.speech_backend == "pocket-tts" and group.route == "narrator"
                else None
            )
            if (identity["speaker"], tuple(identity["reference_sha256s"])) == (
                speaker,
                references,
            ):
                matching += 1
            else:
                changed.append(line_id)
        results.append(
            StoryVoiceImpact(
                selection.selection_id,
                selection.title,
                tuple(changed),
                matching,
                original,
                unknown,
                needs_choice,
            )
        )
    return tuple(results)
