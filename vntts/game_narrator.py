"""Local, explicit narrator selection without changing existing character routes."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.file_integrity import sha256_file

from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
from vntts.reference_quality import analyze_reference_bytes
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import (
    CharacterVoiceRegistry,
    application_voice_library,
    normalize_character_name,
    pocket_tts_preset_voices,
    read_voice_reference_bytes,
    remember_voice_binding,
)


@dataclass(frozen=True)
class OriginalReference:
    source_id: str
    character: str
    path: Path
    payload: bytes
    sha256: str
    duration_seconds: float
    rejection_reasons: tuple[str, ...]


def load_original_reference(
    manifest: str | Path | None, source_id: str
) -> OriginalReference:
    """Inspect and play the same bytes; cloning suitability does not gate listening."""
    if manifest is None:
        raise ValueError("The selected game reference is unavailable")
    voice = CharacterVoiceRegistry.from_file(manifest).resolve_source(source_id)
    if voice is None or not voice.references:
        raise ValueError("The selected game reference is unavailable")
    path = voice.references[0]
    payload = read_voice_reference_bytes(voice, path)
    report = analyze_reference_bytes(payload, path=path)
    from vntts.support import record_game_import

    record_game_import(
        "original-reference",
        path=path,
        reference_bytes=len(payload),
        duration_seconds=report["duration_seconds"],
        reference_sha256=report["sha256"],
        reason=report["rejection_reasons"],
    )
    return OriginalReference(
        source_id=source_id,
        character=voice.source_character or voice.character,
        path=path,
        payload=payload,
        sha256=report["sha256"],
        duration_seconds=report["duration_seconds"],
        rejection_reasons=tuple(report["rejection_reasons"]),
    )


def narrator_preview_plan(
    settings: AppSettings,
    manifest: str | Path | None,
    source_id: str,
    text: str,
) -> VoicePlan:
    preset = isinstance(source_id, str) and source_id.startswith("preset:")
    if preset and (
        settings.speech_backend != "pocket-tts"
        or source_id.removeprefix("preset:") not in pocket_tts_preset_voices
    ):
        raise ValueError("Choose a supported Pocket built-in voice")
    if preset:
        registry = CharacterVoiceRegistry()
        identity = hashlib.sha256(source_id.encode()).hexdigest()
    else:
        if manifest is None:
            raise ValueError("The selected game reference is unavailable")
        registry = CharacterVoiceRegistry.from_file(manifest)
        identity = sha256_file(manifest)
    voice = registry.resolve_source(source_id)
    if voice is None or (not preset and not voice.references):
        raise ValueError("The selected game reference is unavailable")
    candidate = VoiceCandidate(
        source_id=source_id,
        source_character=voice.character,
        source_speaker=voice.speaker,
        reference_sha256s=tuple(sha256_file(path) for path in voice.references),
    )
    controls = hashlib.sha256(
        repr(
            (
                settings.speech_backend,
                settings.tts_model,
                settings.tts_profile,
                settings.pocket_gated_model_accepted,
            )
        ).encode()
    ).hexdigest()
    group = VoiceGroup(
        group_id=identity,
        character="Narrator",
        speakers=("Narrator",),
        portrait=None,
        age=None,
        source_bank=None,
        source_voice_id=None,
        line_ids=(),
        sample_text=text,
        alternate_sample_text=None,
        route="needs-audition",
        source_id=source_id,
        source_character=voice.character,
        source_speaker=voice.speaker,
        reference_sha256s=candidate.reference_sha256s,
        decision_context_sha256=identity,
        control_sha256=controls,
        resolution="player-narrator-choice",
        candidates=(candidate,),
    )
    return VoicePlan(
        job_id="game-narrator",
        created_at="",
        story_index_sha256=identity,
        voice_manifest=None if preset else str(manifest),
        voice_manifest_sha256=None if preset else identity,
        synthesis_backend=settings.speech_backend,
        synthesis_model=settings.tts_model,
        synthesis_language=settings.tts_language,
        synthesis_profile=settings.tts_profile,
        pocket_voice_cloning=settings.pocket_gated_model_accepted and not preset,
        synthesis_controls_sha256=controls,
        groups=(group,),
    )


def bind_voice_library_selection(
    settings: AppSettings,
    manifest: str | Path | None,
    source_id: str,
    character: str,
    *,
    root: str | Path | None = None,
    additional_manifest: str | Path | None = None,
    target_character: str = "Narrator",
) -> AppSettings:
    """Save a role once in the authoritative library, not another manifest."""
    del additional_manifest
    if manifest is None:
        raise ValueError("Choose an available game voice")
    target_character = target_character.strip()
    if not normalize_character_name(target_character):
        raise ValueError("Choose a narrator or character role")
    registry = CharacterVoiceRegistry.from_file(manifest)
    selected = registry.resolve_source(source_id)
    if selected is None or not selected.references:
        raise ValueError("Choose an available game voice")
    for index, path in enumerate(selected.references, 1):
        try:
            report = analyze_reference_bytes(
                read_voice_reference_bytes(selected, path), path=path
            )
            if report["objective_preflight"] != "pass":
                raise ValueError(", ".join(report["rejection_reasons"]))
        except ValueError as error:
            raise ValueError(
                f"Cannot save {character}: reference {index} is unusable ({error}). "
                "Choose another spoken reference."
            ) from error
    library = VoiceLibrary(root) if root is not None else application_voice_library()
    remember_voice_binding(
        library,
        registry,
        target_character,
        source_id,
        method="manual",
        evidence={"selected_character": character},
        algorithm="voice-picker-v1",
    )
    return settings.updated(
        voice_manifest=settings.voice_manifest or str(manifest),
        tts_speaker_wav=(
            None
            if normalize_character_name(target_character) == "narrator"
            else settings.tts_speaker_wav
        ),
    )
