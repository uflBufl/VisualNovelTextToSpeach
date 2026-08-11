import json
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


class VoiceManifestError(ValueError):
    pass


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
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise VoiceManifestError(
                f"Unable to read voice manifest {manifest_path}: {error}"
            ) from error

        if not isinstance(manifest, dict):
            raise VoiceManifestError("Voice manifest must be a JSON object")
        entries = manifest.get("voices")
        if not isinstance(entries, list):
            raise VoiceManifestError("Voice manifest must contain a voices list")

        voices = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise VoiceManifestError(f"Voice entry {index} must be an object")
            character = entry.get("character")
            speaker = entry.get("speaker")
            if not isinstance(character, str) or not character.strip():
                raise VoiceManifestError(
                    f"Voice entry {index} requires a character name"
                )
            if not isinstance(speaker, str) or not speaker.strip():
                raise VoiceManifestError(f"Voice entry {index} requires a speaker ID")

            legacy_reference = entry.get("reference")
            manifest_references = entry.get("references")
            if legacy_reference is not None and manifest_references is not None:
                raise VoiceManifestError(
                    f"Voice entry {index} cannot contain reference and references"
                )
            if manifest_references is None:
                manifest_references = (
                    [] if legacy_reference is None else [legacy_reference]
                )
            if not isinstance(manifest_references, list) or not all(
                isinstance(reference, str) and reference.strip()
                for reference in manifest_references
            ):
                raise VoiceManifestError(
                    f"Voice entry {index} references must be non-empty strings"
                )

            references = tuple(
                (manifest_path.parent / reference).resolve()
                for reference in manifest_references
            )

            aliases = entry.get("aliases", [])
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) and alias.strip() for alias in aliases
            ):
                raise VoiceManifestError(
                    f"Voice entry {index} aliases must be non-empty strings"
                )

            voices.append(
                CharacterVoice(
                    character=character.strip(),
                    speaker=speaker.strip(),
                    reference=references[0] if references else None,
                    aliases=tuple(aliases),
                    references=references,
                )
            )
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
    manifest_path = project_root / "data" / "reverse1999-voices" / "manifest.json"
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


def normalize_character_name(character):
    normalized = unicodedata.normalize("NFKC", character or "").casefold()
    return "".join(value for value in normalized if value.isalnum())


def is_narrator(character):
    return normalize_character_name(character) in {"", "narrator"}
