"""Local, explicit narrator selection without changing existing character routes."""

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import load_voice_manifest

from vntts.application_directories import get_local_data_directory
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.pregeneration_voices import VoiceCandidate, VoiceGroup, VoicePlan
from vntts.reference_quality import analyze_reference_bytes
from vntts.voices import (
    CharacterVoiceRegistry,
    find_default_voice_manifest,
    normalize_character_name,
    pocket_tts_preset_voices,
    read_voice_reference_bytes,
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


def load_original_reference(manifest, source_id):
    """Inspect and play the same bytes; cloning suitability does not gate listening."""
    voice = CharacterVoiceRegistry.from_file(manifest).resolve_source(source_id)
    if voice is None or not voice.references:
        raise ValueError("The selected game reference is unavailable")
    path = voice.references[0]
    payload = read_voice_reference_bytes(voice, path)
    report = analyze_reference_bytes(payload, path=path)
    return OriginalReference(
        source_id=source_id,
        character=voice.source_character or voice.character,
        path=path,
        payload=payload,
        sha256=report["sha256"],
        duration_seconds=report["duration_seconds"],
        rejection_reasons=tuple(report["rejection_reasons"]),
    )


def narrator_preview_plan(settings, manifest, source_id, text):
    preset = isinstance(source_id, str) and source_id.startswith("preset:")
    if preset and (
        settings.speech_backend != "pocket-tts"
        or source_id.removeprefix("preset:") not in pocket_tts_preset_voices
    ):
        raise ValueError("Choose a supported Pocket built-in voice")
    registry = (
        CharacterVoiceRegistry()
        if preset
        else CharacterVoiceRegistry.from_file(manifest)
    )
    voice = registry.resolve_source(source_id)
    if voice is None or (not preset and not voice.references):
        raise ValueError("The selected game reference is unavailable")
    candidate = VoiceCandidate(
        source_id=source_id,
        source_character=voice.character,
        source_speaker=voice.speaker,
        reference_sha256s=tuple(sha256_file(path) for path in voice.references),
    )
    identity = (
        hashlib.sha256(source_id.encode()).hexdigest()
        if preset
        else sha256_file(manifest)
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
    settings,
    manifest,
    source_id,
    character,
    *,
    root=None,
    additional_manifest=None,
    target_character="Narrator",
):
    """Publish a new manifest snapshot; never rewrite the active pack or settings."""
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
    document = load_voice_manifest(base)[0] if base else {"version": 2, "voices": []}
    registry = (
        CharacterVoiceRegistry.from_file(base) if base else CharacterVoiceRegistry()
    )
    sources = {
        entry["character"]: registry.resolve_source(
            f"character:{normalize_character_name(entry['character'])}"
        )
        for entry in document["voices"]
    }
    evidence_base = base
    if additional_manifest:
        additional = load_voice_manifest(additional_manifest)[0]
        additional_registry = CharacterVoiceRegistry.from_file(additional_manifest)
        for entry in additional["voices"]:
            if entry["character"] not in sources:
                document["voices"].append(entry)
                sources[entry["character"]] = additional_registry.resolve_source(
                    f"character:{normalize_character_name(entry['character'])}"
                )
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
        staging = Path(tempfile.mkdtemp(prefix=".narrator-", dir=root))
        try:
            references = staging / "references"
            references.mkdir()
            for entry in document["voices"]:
                voice = sources[entry["character"]]
                copied = []
                for path, payload in zip(
                    voice.references, source_payloads[entry["character"]], strict=True
                ):
                    relative = (
                        f"references/{hashlib.sha256(payload).hexdigest()}{path.suffix}"
                    )
                    (staging / relative).write_bytes(payload)
                    copied.append(relative)
                entry["references"] = copied
            # Candidate evidence belongs to the original story and remains intact.
            evidence = document.get("vntts.player.voice_candidates")
            if evidence is not None:
                relative = PurePosixPath(evidence["candidate_report"])
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in str(relative)
                ):
                    raise ValueError("Unsafe candidate report path")
                source = Path(evidence_base).parent / relative
                source.resolve().relative_to(Path(evidence_base).parent.resolve())
                if (
                    source.is_symlink()
                    or sha256_file(source) != evidence["candidate_report_sha256"]
                ):
                    raise ValueError("Candidate report changed")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            copied = []
            for path, payload in zip(selected.references, payloads, strict=True):
                relative = (
                    f"references/{hashlib.sha256(payload).hexdigest()}{path.suffix}"
                )
                (staging / relative).write_bytes(payload)
                copied.append(relative)
            if not any(entry["character"] == name for entry in document["voices"]):
                document["voices"].append(
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
        finally:
            if staging.exists():
                shutil.rmtree(staging)
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
    return settings.updated(
        voice_manifest=str(output),
        **({"tts_speaker_wav": None} if narrator else {}),
        **{
            "voice_assignments" if narrator else "character_voice_defaults": assignments
        },
    )
