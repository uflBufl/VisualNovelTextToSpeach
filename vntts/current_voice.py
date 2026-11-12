"""Match archived recordings against the current voice-library routing."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vntts_artifacts.voice_manifest import (
    normalize_character_name,
    validate_voice_manifest,
)

from vntts.document_identity import is_lowercase_sha256
from vntts.generated_audio import GeneratedAudioEntryLike, recorded_voice_identity
from vntts.json_types import decode_json
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_voices import resolve_pregeneration_settings
from vntts.settings import AppSettings
from vntts.voice_library import VoiceLibrary, VoiceLibraryReadSnapshot
from vntts.voices import pocket_tts_preset_voices, synthesis_character_for_line


def generation_controls_match(
    document: Mapping[str, object], *, backend: str, model: str | None, profile: str
) -> bool:
    """Compare controls published with the recording, independently of narrator routing."""
    return (
        document.get("provider") == backend
        and document.get("generation_profile") == profile
        and (model is None or document.get("model") == model)
    )


@dataclass(frozen=True)
class RecordedReferenceSources:
    """Original source hashes named by a checksum-verified preparation manifest."""

    voices: Mapping[str, tuple[str, str, tuple[str, ...]]]

    @classmethod
    def capture(cls, path: Path, expected_sha256: str) -> RecordedReferenceSources:
        payload = read_regular_bytes(path)
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("Archived voice manifest changed")
        document = decode_json(payload)
        voices = validate_voice_manifest(document)
        assert isinstance(document, dict)
        identities = {}
        for raw, voice in zip(document["voices"], voices, strict=True):
            references = tuple(PurePosixPath(value) for value in voice.references)
            if not references or any(
                value.parent != PurePosixPath("references")
                or value.suffix != ".wav"
                or not is_lowercase_sha256(value.stem)
                for value in references
            ):
                continue
            source = raw.get("vntts.source_character", voice.character)
            if not isinstance(source, str):
                continue
            identities[normalize_character_name(voice.character)] = (
                source,
                voice.speaker,
                (references[0].stem,),
            )
        return cls(identities)

    def for_entry(self, entry: GeneratedAudioEntryLike) -> tuple[str, ...] | None:
        recorded = recorded_voice_identity(entry)
        document = getattr(entry, "document", entry)
        if recorded is None or not isinstance(document, dict):
            return None
        character = document.get("voice_character")
        if not isinstance(character, str):
            return None
        identity = self.voices.get(normalize_character_name(character))
        if (
            identity is None
            or len(recorded["reference_sha256s"]) != 1
            or identity[0] != recorded["source_character"]
            or identity[1] != recorded["speaker"]
        ):
            return None
        return identity[2]


@dataclass(frozen=True)
class CurrentVoiceIdentity:
    reference_sha256s: tuple[str, ...]
    speaker: str | None = None


@dataclass
class CurrentVoiceSnapshot:
    """One metadata-only routing projection shared by an entire archive scan."""

    library: VoiceLibraryReadSnapshot
    settings: AppSettings
    _identities: dict[str, CurrentVoiceIdentity | None] = field(default_factory=dict)

    @classmethod
    def capture(
        cls,
        library: VoiceLibrary,
        settings: AppSettings,
        previous: CurrentVoiceSnapshot | None = None,
    ) -> CurrentVoiceSnapshot:
        settings = resolve_pregeneration_settings(settings)
        snapshot = library.read_snapshot(previous.library if previous else None)
        if previous and snapshot is previous.library and settings == previous.settings:
            return previous
        return cls(snapshot, settings)

    def identity(
        self, role: str, *, variant_key: str | None = None
    ) -> CurrentVoiceIdentity | None:
        """Resolve only CURRENT bindings; historical variants never supply a voice."""
        key = normalize_character_name(role)
        if key in self._identities:
            return self._identities[key]
        binding = self.library.binding(role, variant_key=variant_key)
        if key != "narrator" and (binding is None or binding.route == "narrator"):
            identity = self.identity("Narrator")
        elif binding is not None and binding.route == "voice":
            if binding.source_sha256s:
                # Cloning backends synthesize from the first reference in the pool.
                identity = CurrentVoiceIdentity(binding.source_sha256s[:1])
            elif binding.source_id and binding.source_id.startswith("preset:"):
                speaker = binding.source_id.removeprefix("preset:")
                identity = (
                    CurrentVoiceIdentity((), speaker)
                    if speaker in pocket_tts_preset_voices
                    else None
                )
            else:
                identity = None
        elif (
            binding is None
            and key == "narrator"
            and self.settings.speech_backend == "pocket-tts"
        ):
            speaker = next(
                (
                    value
                    for value in (
                        self.settings.narrator_speaker,
                        self.settings.tts_speaker,
                        "alba",
                    )
                    if value in pocket_tts_preset_voices
                ),
                "alba",
            )
            identity = CurrentVoiceIdentity((), speaker)
        else:
            identity = None
        self._identities[key] = identity
        return identity

    def matches(
        self,
        entry: GeneratedAudioEntryLike,
        *,
        role: str | None = None,
        variant_key: str | None = None,
        reference_source_sha256s: tuple[str, ...] | None = None,
    ) -> bool:
        """Reject old or unverifiable voices without opening recording files."""
        recorded = recorded_voice_identity(entry)
        document = getattr(entry, "document", entry)
        if recorded is None or not isinstance(document, dict):
            return False
        if not generation_controls_match(
            document,
            backend=self.settings.speech_backend,
            model=self.settings.tts_model,
            profile=self.settings.tts_profile,
        ):
            return False
        if role is None:
            requested = document.get("requested_voice_character")
            speaker = document.get("speaker")
            voice = document.get("voice_character")
            if not isinstance(speaker, str):
                speaker = None
            if not isinstance(requested, str):
                requested = speaker or (voice if isinstance(voice, str) else None)
            role = synthesis_character_for_line(speaker, requested)
        current = self.identity(role, variant_key=variant_key)
        if current is None:
            return False
        references = (
            reference_source_sha256s
            if reference_source_sha256s is not None
            else tuple(recorded["reference_sha256s"])
        )
        if references != current.reference_sha256s:
            return False
        if not current.reference_sha256s and recorded["speaker"] != current.speaker:
            return False
        return True
