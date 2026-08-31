import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
)

default_voice_choice_id = "default"
pocket_tts_preset_voices = (
    "alba",
    "anna",
    "azelma",
    "bill_boerst",
    "caro_davy",
    "charles",
    "cosette",
    "eponine",
    "estelle",
    "eve",
    "fantine",
    "george",
    "giovanni",
    "jane",
    "javert",
    "jean",
    "juergen",
    "lola",
    "marius",
    "mary",
    "michael",
    "paul",
    "peter_yearsley",
    "rafael",
    "stuart_bell",
    "vera",
)


@dataclass(frozen=True)
class VoiceChoice:
    id: str
    label: str
    description: str = ""


def find_voice_assignment(assignments, character):
    target = normalize_character_name(synthesis_character(character))
    return next(
        (
            source_id
            for configured_character, source_id in assignments.items()
            if normalize_character_name(configured_character) == target
        ),
        None,
    )


@dataclass(frozen=True)
class CharacterVoice:
    character: str
    speaker: str
    reference: Path | None = None
    aliases: tuple[str, ...] = ()
    references: tuple[Path, ...] = ()
    reference_root: Path | None = None

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
        self.assignments = {}
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
                reference=_contained_manifest_reference(
                    manifest_path, entry.references[0]
                )
                if entry.references
                else None,
                aliases=entry.aliases,
                references=tuple(
                    _contained_manifest_reference(manifest_path, reference)
                    for reference in entry.references
                ),
                reference_root=manifest_path.parent.resolve(),
            )
            for entry in entries
        ]
        return cls(voices)

    def resolve(self, character):
        normalized_name = normalize_character_name(synthesis_character(character))
        if normalized_name in self.assignments:
            voice = self.assignments[normalized_name]
        else:
            voice = self.voices.get(normalized_name)
        if voice is not None:
            _validate_voice_reference_ownership(voice)
        return voice

    def resolve_source(self, source_id):
        if source_id == default_voice_choice_id:
            return None
        source_type, separator, value = (source_id or "").partition(":")
        if not separator or not value:
            raise VoiceManifestError(f"Invalid voice choice: {source_id!r}")
        if source_type == "preset":
            return CharacterVoice(value, value)
        if source_type == "character":
            voice = self.voices.get(normalize_character_name(value))
            if voice is None:
                raise VoiceManifestError(
                    f"The selected voice is no longer available: {value!r}"
                )
            _validate_voice_reference_ownership(voice)
            return voice
        raise VoiceManifestError(f"Unknown voice choice: {source_id!r}")

    def set_assignment(self, character, source_id):
        normalized_name = normalize_character_name(character)
        if not normalized_name:
            raise VoiceManifestError("Character name is required")
        self.assignments[normalized_name] = self.resolve_source(source_id)

    def apply_assignments(
        self,
        assignments,
        *,
        warn=None,
        preset_validator=None,
    ):
        warn = warn or (lambda _message: None)
        for character, source_id in assignments.items():
            if (
                source_id.startswith("preset:")
                and preset_validator is not None
                and not preset_validator(source_id.removeprefix("preset:"))
            ):
                warn(f"Voice choice {source_id!r} is not available for {character!r}")
                continue
            try:
                self.set_assignment(character, source_id)
            except VoiceManifestError as error:
                warn(str(error))

    def unique_voices(self):
        return tuple({id(voice): voice for voice in self.voices.values()}.values())

    def choices(self):
        return tuple(
            VoiceChoice(
                f"character:{normalize_character_name(voice.character)}",
                voice.character,
                "Imported character voice",
            )
            for voice in sorted(
                self.unique_voices(), key=lambda item: item.character.casefold()
            )
        )

    def resolve_closest(self, character, *, minimum_similarity=0.78):
        normalized_name = normalize_character_name(synthesis_character(character))
        if normalized_name in self.assignments:
            voice = self.assignments[normalized_name]
            _validate_voice_reference_ownership(voice)
            return voice
        exact_voice = self.voices.get(normalized_name)
        if exact_voice is not None:
            _validate_voice_reference_ownership(exact_voice)
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
        if best_similarity < minimum_similarity:
            return None
        _validate_voice_reference_ownership(best_voice)
        return best_voice

    def _add_name(self, name, voice):
        normalized_name = normalize_character_name(name)
        existing_voice = self.voices.get(normalized_name)
        if existing_voice is not None and existing_voice != voice:
            raise VoiceManifestError(f"Duplicate voice name or alias: {name!r}")
        self.voices[normalized_name] = voice


def _contained_manifest_reference(manifest_path, reference):
    if not isinstance(reference, str) or not reference.strip() or "\\" in reference:
        raise VoiceManifestError("Voice reference must be a safe POSIX-relative path")
    relative = PurePosixPath(reference.strip())
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise VoiceManifestError("Voice reference must be a safe POSIX-relative path")
    root = Path(manifest_path).parent.resolve()
    unresolved = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise VoiceManifestError("Voice reference must not use symlinks")
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise VoiceManifestError(
            "Voice reference must stay within the manifest directory"
        ) from error
    return resolved


def _validate_voice_reference_ownership(voice):
    root = voice.reference_root
    if root is None:
        return
    root = Path(root).resolve()
    for reference in voice.references:
        lexical = Path(reference)
        try:
            relative = lexical.relative_to(root)
        except ValueError as error:
            raise VoiceManifestError(
                "Voice reference must stay within the manifest directory"
            ) from error
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise VoiceManifestError("Voice reference must not use symlinks")
        try:
            lexical.resolve().relative_to(root)
        except ValueError as error:
            raise VoiceManifestError(
                "Voice reference must stay within the manifest directory"
            ) from error


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
    def __init__(
        self,
        tts,
        registry=None,
        *,
        narrator_speaker=None,
        narrator_voice=None,
        force_reference_audio=False,
    ):
        self.tts = tts
        self.registry = registry or CharacterVoiceRegistry()
        self.narrator_speaker = narrator_speaker
        self.narrator_voice = narrator_voice
        self.force_reference_audio = bool(force_reference_audio)

    def speak(self, character, text, *, playback_guard=None):
        with self._speech_arguments(character) as arguments:
            if playback_guard is not None:
                arguments["playback_guard"] = playback_guard
            return self.tts.speak(text, **arguments)

    def synthesize(
        self,
        character,
        text,
        *,
        synthesis_options=None,
        cache_policy="use",
        cancellation=None,
    ):
        with self._speech_arguments(character) as arguments:
            return self.tts.synthesize(
                text,
                synthesis_options=synthesis_options,
                cache_policy=cache_policy,
                cancellation=cancellation,
                **arguments,
            )

    def prepare_playback(
        self,
        character,
        text,
        *,
        synthesis_options=None,
        cache_policy="use",
        cancellation=None,
    ):
        with self._speech_arguments(character) as arguments:
            return self.tts.prepare_synthesis(
                text,
                synthesis_options=synthesis_options,
                cache_policy=cache_policy,
                cancellation=cancellation,
                **arguments,
            )

    def play(self, audio, *, playback_guard=None):
        return self.tts.play(audio, playback_guard=playback_guard)

    def play_prepared(self, prepared, *, playback_guard=None):
        return self.tts.play_prepared(prepared, playback_guard=playback_guard)

    def warm_up(self, *, progress=None, text="Voice ready."):
        progress = progress or (lambda _current, _total, _character: None)
        voices = sorted(
            {id(voice): voice for voice in self.registry.voices.values()}.values(),
            key=lambda voice: voice.character.casefold(),
        )
        characters = ["Narrator", *(voice.character for voice in voices)]
        for current, character in enumerate(characters, start=1):
            progress(current, len(characters), character)
            with self._speech_arguments(character) as arguments:
                self.tts.synthesize(text, **arguments)
        return len(characters)

    @contextmanager
    def _speech_arguments(self, character):
        voice = self.registry.resolve(character)
        if is_narrator(character) or voice is None:
            voice = self.narrator_voice
        if voice is None:
            yield {"speaker": self.narrator_speaker}
            return

        needs_reference = self.force_reference_audio or not self.tts.has_speaker(
            voice.speaker
        )
        if not needs_reference:
            yield {"speaker": voice.speaker, "speaker_wav": None}
            return
        if not voice.references:
            raise VoiceManifestError(
                f"Voice {voice.character!r} is not cached and has no references"
            )
        with _immutable_voice_reference_snapshots(voice) as references:
            speaker_wav = [str(reference) for reference in references]
            yield {
                "speaker": None if self.force_reference_audio else voice.speaker,
                "speaker_wav": speaker_wav[0] if len(speaker_wav) == 1 else speaker_wav,
            }


@contextmanager
def _immutable_voice_reference_snapshots(voice):
    """Give a backend private bytes instead of a mutable manifest pathname."""
    if voice.reference_root is None:
        missing = [
            reference for reference in voice.references if not reference.is_file()
        ]
        if missing:
            raise VoiceManifestError(f"Voice reference does not exist: {missing[0]}")
        yield voice.references
        return

    payloads = [
        _read_owned_voice_reference(voice.reference_root, reference)
        for reference in voice.references
    ]
    with TemporaryDirectory(prefix="vntts-voice-reference-") as directory:
        snapshots = []
        for index, (reference, payload) in enumerate(zip(voice.references, payloads)):
            suffix = reference.suffix if reference.suffix else ".wav"
            destination = Path(directory) / f"reference-{index + 1}{suffix}"
            destination.write_bytes(payload)
            snapshots.append(destination)
        yield tuple(snapshots)


def _read_owned_voice_reference(root, reference):
    root = Path(root).resolve()
    reference = Path(reference)
    try:
        reference.relative_to(root)
    except ValueError as error:
        raise VoiceManifestError(
            "Voice reference must stay within the manifest directory"
        ) from error
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(reference, flags)
    except FileNotFoundError as error:
        raise VoiceManifestError(
            f"Voice reference does not exist: {reference}"
        ) from error
    except OSError as error:
        raise VoiceManifestError(
            f"Voice reference could not be opened without following links: {reference}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise VoiceManifestError("Voice reference must be a regular file")
        _validate_voice_reference_ownership(
            CharacterVoice(
                character="snapshot",
                speaker="snapshot",
                references=(reference,),
                reference_root=root,
            )
        )
        try:
            current = os.stat(reference, follow_symlinks=False)
        except OSError as error:
            raise VoiceManifestError(
                f"Voice reference changed while it was opened: {reference}"
            ) from error
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise VoiceManifestError("Voice reference changed while it was opened")
        chunks = []
        while True:
            try:
                chunk = os.read(descriptor, 1024 * 1024)
            except OSError as error:
                raise VoiceManifestError(
                    f"Voice reference became unreadable: {reference}"
                ) from error
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            finished.st_dev,
            finished.st_ino,
            finished.st_size,
            finished.st_mtime_ns,
        ):
            raise VoiceManifestError("Voice reference changed while it was read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_voice_reference_bytes(voice, reference):
    """Read one declared reference without following or racing path links."""

    def canonical_parent_path(value):
        value = Path(value).expanduser().absolute()
        return value.parent.resolve() / value.name

    reference = canonical_parent_path(reference)
    declared = tuple(canonical_parent_path(value) for value in voice.references)
    if reference not in declared:
        raise VoiceManifestError("Voice reference is not declared by this voice")
    root = voice.reference_root
    if root is None:
        root = reference.parent
    return _read_owned_voice_reference(root, reference)


def synthesis_character(character):
    """Return the voice identity used for live and authoring synthesis."""
    original = str(character or "Narrator").strip() or "Narrator"
    return "Narrator" if is_unattributed_speaker(original) else original


def is_unattributed_speaker(character):
    return str(character or "").strip() == "???"


def synthesis_character_for_line(speaker, voice_character=None):
    """Resolve a line voice while giving the exact `???` speaker priority."""
    if is_unattributed_speaker(speaker):
        return "Narrator"
    return synthesis_character(voice_character or speaker)


def is_narrator(character):
    return normalize_character_name(synthesis_character(character)) == "narrator"
