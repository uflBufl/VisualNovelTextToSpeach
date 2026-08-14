from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)


@dataclass(frozen=True)
class CharacterVoice:
    character: str
    speaker: str
    reference: Path | None = None
    aliases: tuple[str, ...] = ()
    references: tuple[Path, ...] = ()

    def __post_init__(self):
        references = self.references
        if not references and self.reference is not None:
            references = (self.reference,)
        if references and self.reference is None:
            object.__setattr__(self, "reference", references[0])
        object.__setattr__(self, "references", tuple(references))


class CharacterVoiceRegistry:
    def __init__(self, voices=()):
        self.voices = {}
        for voice in voices:
            self._add_name(voice.character, voice)
            for alias in voice.aliases:
                self._add_name(alias, voice)

    @classmethod
    def from_file(cls, manifest_path):
        manifest_path = Path(manifest_path).expanduser().resolve()
        _manifest, entries = load_voice_manifest(manifest_path)
        voices = [
            CharacterVoice(
                character=entry.character,
                speaker=entry.speaker,
                reference=(manifest_path.parent / entry.references[0]).resolve()
                if entry.references
                else None,
                aliases=entry.aliases,
                references=tuple(
                    (manifest_path.parent / reference).resolve()
                    for reference in entry.references
                ),
            )
            for entry in entries
        ]
        return cls(voices)

    def resolve(self, character):
        return self.voices.get(normalize_character_name(character))

    def resolve_closest(self, character, *, minimum_similarity=0.78):
        normalized_name = normalize_character_name(character)
        exact_voice = self.voices.get(normalized_name)
        if exact_voice is not None:
            return exact_voice
        if len(normalized_name) < 3:
            return None

        best_similarity = 0.0
        best_voice = None
        for configured_name, voice in self.voices.items():
            if len(configured_name) < 3:
                continue
            similarity = SequenceMatcher(
                None,
                normalized_name,
                configured_name,
            ).ratio()
            if similarity > best_similarity:
                best_similarity = similarity
                best_voice = voice
        return best_voice if best_similarity >= minimum_similarity else None

    def _add_name(self, name, voice):
        normalized_name = normalize_character_name(name)
        existing_voice = self.voices.get(normalized_name)
        if existing_voice is not None and existing_voice != voice:
            raise VoiceManifestError(f"Duplicate voice name or alias: {name!r}")
        self.voices[normalized_name] = voice


def find_default_voice_manifest(project_root=None):
    project_root = (
        Path(__file__).resolve().parents[1]
        if project_root is None
        else Path(project_root).expanduser().resolve()
    )
    manifest_path = project_root / "data" / "voice-packs" / "default" / "manifest.json"
    if not manifest_path.is_file():
        return None

    try:
        registry = CharacterVoiceRegistry.from_file(manifest_path)
    except VoiceManifestError:
        return None

    voices = tuple({id(voice): voice for voice in registry.voices.values()}.values())
    if not voices or any(
        not reference.is_file() for voice in voices for reference in voice.references
    ):
        return None
    return manifest_path.resolve()


class CharacterVoiceRouter:
    def __init__(self, tts, registry=None, *, narrator_speaker=None):
        self.tts = tts
        self.registry = registry or CharacterVoiceRegistry()
        self.narrator_speaker = narrator_speaker

    def speak(self, character, text, *, playback_guard=None):
        arguments = self._speech_arguments(character)
        if playback_guard is not None:
            arguments["playback_guard"] = playback_guard
        return self.tts.speak(text, **arguments)

    def synthesize(self, character, text):
        return self.tts.synthesize(text, **self._speech_arguments(character))

    def play(self, audio, *, playback_guard=None):
        return self.tts.play(audio, playback_guard=playback_guard)

    def warm_up(self, *, progress=None, text="Voice ready."):
        progress = progress or (lambda _current, _total, _character: None)
        voices = sorted(
            {id(voice): voice for voice in self.registry.voices.values()}.values(),
            key=lambda voice: voice.character.casefold(),
        )
        characters = ["Narrator", *(voice.character for voice in voices)]
        for current, character in enumerate(characters, start=1):
            progress(current, len(characters), character)
            self.tts.synthesize(text, **self._speech_arguments(character))
        return len(characters)

    def _speech_arguments(self, character):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            return {"speaker": self.narrator_speaker}

        speaker_wav = None
        if not self.tts.has_speaker(voice.speaker):
            if not voice.references:
                raise VoiceManifestError(
                    f"Voice {voice.character!r} is not cached and has no references"
                )
            missing_references = [
                reference for reference in voice.references if not reference.is_file()
            ]
            if missing_references:
                raise VoiceManifestError(
                    f"Voice reference does not exist: {missing_references[0]}"
                )
            speaker_wav = [str(reference) for reference in voice.references]
            if len(speaker_wav) == 1:
                speaker_wav = speaker_wav[0]

        return {
            "speaker": voice.speaker,
            "speaker_wav": speaker_wav,
        }


def is_narrator(character):
    return normalize_character_name(character) in {"", "narrator"}
