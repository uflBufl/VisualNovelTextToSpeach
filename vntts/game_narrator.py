"""Local, explicit narrator selection without changing existing character routes."""

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import load_voice_manifest

from vntts.application_directories import get_local_data_directory
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
from vntts.reference_quality import analyze_reference_bytes
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary
from vntts.voices import (
    CharacterVoice,
    CharacterVoiceRegistry,
    application_voice_library,
    find_default_voice_manifest,
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


def _voice_manifest_document(
    value: object,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if not isinstance(value, dict):
        raise ValueError("Voice manifest must be a JSON object")
    document = {key: item for key, item in value.items() if isinstance(key, str)}
    raw_voices = document.get("voices")
    if not isinstance(raw_voices, list):
        raise ValueError("Voice manifest must contain a voices list")
    voices: list[dict[str, object]] = []
    for item in raw_voices:
        if not isinstance(item, dict):
            raise ValueError("Voice manifest entries must be JSON objects")
        entry = {key: field for key, field in item.items() if isinstance(key, str)}
        _voice_character(entry)
        voices.append(entry)
    document["voices"] = voices
    return document, voices


def _voice_character(entry: dict[str, object]) -> str:
    character = entry.get("character")
    if not isinstance(character, str) or not character:
        raise ValueError("Voice manifest entry requires a character")
    return character


def _candidate_evidence(value: object) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Candidate evidence must be a JSON object")
    report = value.get("candidate_report")
    checksum = value.get("candidate_report_sha256")
    if not isinstance(report, str) or not isinstance(checksum, str):
        raise ValueError("Candidate evidence is incomplete")
    return report, checksum


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


def bind_game_narrator(
    settings: AppSettings,
    manifest: str | Path | None,
    source_id: str,
    character: str,
    *,
    root: str | Path | None = None,
    additional_manifest: str | Path | None = None,
    target_character: str = "Narrator",
) -> AppSettings:
    """Publish a new manifest snapshot; never rewrite the active pack or settings."""
    if manifest is None:
        raise ValueError("Choose an available game voice")
    target_character = target_character.strip()
    if not normalize_character_name(target_character):
        raise ValueError("Choose a narrator or character role")
    narrator = normalize_character_name(target_character) == "narrator"
    selected = CharacterVoiceRegistry.from_file(manifest).resolve_source(source_id)
    if selected is None or not selected.references:
        raise ValueError("Choose an available game voice")
    payloads = tuple(
        read_voice_reference_bytes(selected, p) for p in selected.references
    )
    for index, (path, payload) in enumerate(
        zip(selected.references, payloads, strict=True), 1
    ):
        try:
            report = analyze_reference_bytes(payload, path=path)
            if report["objective_preflight"] != "pass":
                raise ValueError(", ".join(report["rejection_reasons"]))
        except ValueError as error:
            raise ValueError(
                f"Cannot save {character}: reference {index} is unusable ({error}). "
                "Choose another spoken reference."
            ) from error
    base = settings.voice_manifest or find_default_voice_manifest()
    document, voices = _voice_manifest_document(
        load_voice_manifest(base)[0] if base else {"version": 2, "voices": []}
    )
    registry = (
        CharacterVoiceRegistry.from_file(base) if base else CharacterVoiceRegistry()
    )
    sources: dict[str, CharacterVoice] = {}
    for entry in voices:
        entry_character = _voice_character(entry)
        source = registry.resolve_source(
            f"character:{normalize_character_name(entry_character)}"
        )
        if source is None:
            raise ValueError("Voice manifest source is unavailable")
        sources[entry_character] = source
    evidence_base = base
    if additional_manifest:
        additional, additional_voices = _voice_manifest_document(
            load_voice_manifest(additional_manifest)[0]
        )
        additional_registry = CharacterVoiceRegistry.from_file(additional_manifest)
        for entry in additional_voices:
            entry_character = _voice_character(entry)
            if entry_character not in sources:
                source = additional_registry.resolve_source(
                    f"character:{normalize_character_name(entry_character)}"
                )
                if source is None:
                    raise ValueError("Voice manifest source is unavailable")
                voices.append(entry)
                sources[entry_character] = source
        if "vntts.player.voice_candidates" in additional:
            document["vntts.player.voice_candidates"] = additional[
                "vntts.player.voice_candidates"
            ]
            evidence_base = additional_manifest
    reference_id = hashlib.sha256(b"".join(payloads)).hexdigest()
    name = f"Game {'narrator' if narrator else 'voice'} {character} {reference_id[:12]}"
    selected_id = f"character:{normalize_character_name(name)}"
    original_base = next(
        (
            binding["base_manifest_sha256"]
            for key in ("vntts.game_narrator", "vntts.game_character_voices")
            if isinstance(binding := document.get(key), dict)
            and binding.get("base_manifest_sha256")
        ),
        sha256_file(base) if base else None,
    )
    if narrator:
        document["vntts.game_narrator"] = {
            "source_id": selected_id,
            "character": character,
            "base_manifest_sha256": original_base,
        }
    else:
        document["vntts.game_character_voices"] = {
            "base_manifest_sha256": original_base
        }
    source_payloads = {
        name: tuple(
            read_voice_reference_bytes(voice, path) for path in voice.references
        )
        for name, voice in sources.items()
    }
    identity = hashlib.sha256(
        json.dumps(
            [
                document,
                name,
                {
                    name: [hashlib.sha256(payload).hexdigest() for payload in values]
                    for name, values in source_payloads.items()
                },
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    root = Path(root or get_local_data_directory() / "voice-packs" / "game-narrators")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / identity
    output = destination / "manifest.json"
    if not output.is_file():
        with staged_directory(root, prefix=".narrator-") as staging:
            references = staging / "references"
            references.mkdir()
            for entry in voices:
                entry_character = _voice_character(entry)
                voice = sources[entry_character]
                copied = []
                for path, payload in zip(
                    voice.references, source_payloads[entry_character], strict=True
                ):
                    relative = (
                        f"references/{hashlib.sha256(payload).hexdigest()}{path.suffix}"
                    )
                    (staging / relative).write_bytes(payload)
                    copied.append(relative)
                entry["references"] = copied
            # Candidate evidence belongs to the original story and remains intact.
            evidence = _candidate_evidence(
                document.get("vntts.player.voice_candidates")
            )
            if evidence is not None:
                candidate_report, candidate_report_sha256 = evidence
                candidate_relative = PurePosixPath(candidate_report)
                if (
                    candidate_relative.is_absolute()
                    or ".." in candidate_relative.parts
                    or "\\" in str(candidate_relative)
                ):
                    raise ValueError("Unsafe candidate report path")
                if evidence_base is None:
                    raise ValueError("Candidate evidence has no manifest")
                candidate_source = Path(evidence_base).parent / candidate_relative
                candidate_source.resolve().relative_to(
                    Path(evidence_base).parent.resolve()
                )
                if (
                    candidate_source.is_symlink()
                    or sha256_file(candidate_source) != candidate_report_sha256
                ):
                    raise ValueError("Candidate report changed")
                target = staging / candidate_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate_source, target)
            copied = []
            for path, payload in zip(selected.references, payloads, strict=True):
                relative = (
                    f"references/{hashlib.sha256(payload).hexdigest()}{path.suffix}"
                )
                (staging / relative).write_bytes(payload)
                copied.append(relative)
            if not any(_voice_character(entry) == name for entry in voices):
                voices.append(
                    {
                        "character": name,
                        "speaker": selected.speaker,
                        "aliases": [],
                        "references": copied,
                        "vntts.source_character": character,
                    }
                )
            atomic_write_json(staging / "manifest.json", document)
            CharacterVoiceRegistry.from_file(staging / "manifest.json")
            try:
                rename_directory_no_replace(staging, destination)
            except AtomicPublicationError:
                if not output.is_file():
                    raise
    saved_registry = CharacterVoiceRegistry.from_file(output)
    for character_name, expected in source_payloads.items():
        saved_voice = saved_registry.resolve_source(
            f"character:{normalize_character_name(character_name)}"
        )
        if (
            saved_voice is None
            or tuple(
                read_voice_reference_bytes(saved_voice, p)
                for p in saved_voice.references
            )
            != expected
        ):
            raise ValueError("Saved character reference changed")
    bound = saved_registry.resolve_source(selected_id)
    if (
        bound is None
        or tuple(read_voice_reference_bytes(bound, p) for p in bound.references)
        != payloads
    ):
        raise ValueError("Saved narrator reference changed")
    assignments = {
        name: value
        for name, value in (
            settings.voice_assignments
            if narrator
            else settings.character_voice_defaults
        ).items()
        if normalize_character_name(name) != normalize_character_name(target_character)
    }
    assignments[target_character] = selected_id
    if narrator:
        return settings.updated(
            voice_manifest=str(output),
            tts_speaker_wav=None,
            voice_assignments=assignments,
        )
    return settings.updated(
        voice_manifest=str(output),
        character_voice_defaults=assignments,
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
        voice_assignments={},
        character_voice_defaults={},
        tts_speaker_wav=(
            None
            if normalize_character_name(target_character) == "narrator"
            else settings.tts_speaker_wav
        ),
    )
