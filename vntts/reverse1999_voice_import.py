import argparse
import json
import sys
import unicodedata
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.wwise import (
    AudioConversionError,
    WwiseBankError,
    convert_audio,
    read_embedded_media,
    resolve_decoder,
)

project_root = Path(__file__).resolve().parents[1]
default_output = project_root / "data" / "reverse1999-voices"
known_story_banks = {
    "kamuta": "activitystory_yuzhou2_7_yishi_npc520301_voc.bnk",
}


class GameVoiceImportError(RuntimeError):
    pass


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Import clean Reverse: 1999 story voice clips from a locally "
            "installed game's Wwise bank into a VNTTS voice manifest."
        )
    )
    parser.add_argument("character", help="Speaker name shown in game dialogue.")
    parser.add_argument(
        "--bank",
        type=Path,
        help=(
            "Explicit English .bnk file. Required for characters not yet in "
            "the built-in story-bank map."
        ),
    )
    parser.add_argument(
        "--game-audio-directory",
        type=Path,
        help="Directory containing the installed game's English .bnk files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help="Existing or new VNTTS voice-pack directory.",
    )
    parser.add_argument(
        "--references",
        type=int,
        default=3,
        help="Maximum clean clips to import from the bank.",
    )
    parser.add_argument(
        "--decoder",
        default="vgmstream-cli",
        help="Path or command name for vgmstream-cli.",
    )
    return parser


def normalize_name(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def slugify(value):
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    return (
        "-".join(
            part
            for part in "".join(
                character if character.isalnum() else " " for character in ascii_value
            )
            .casefold()
            .split()
        )
        or "character"
    )


def find_game_audio_directory(home=None):
    home = Path.home() if home is None else Path(home)
    containers = home / "Library" / "Containers"
    candidates = containers.glob("*/Data/Documents/ResLib/iOS/audios/iOS/en")
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*.bnk")):
            return candidate.resolve()
    return None


def resolve_bank(character, bank=None, game_audio_directory=None):
    if bank is not None:
        bank = Path(bank).expanduser().resolve()
    else:
        filename = known_story_banks.get(normalize_name(character))
        if filename is None:
            raise GameVoiceImportError(
                f"No known story bank for {character!r}; pass --bank explicitly"
            )
        game_audio_directory = game_audio_directory or find_game_audio_directory()
        if game_audio_directory is None:
            raise GameVoiceImportError(
                "Unable to find Reverse: 1999 game audio; pass --game-audio-directory"
            )
        bank = Path(game_audio_directory).expanduser().resolve() / filename

    if not bank.is_file():
        raise GameVoiceImportError(f"Voice bank does not exist: {bank}")
    return bank


def decode_references(bank, output_directory, character, reference_count, decoder):
    if reference_count <= 0:
        raise GameVoiceImportError("--references must be positive")
    media = read_embedded_media(bank)
    selected = sorted(media, key=lambda entry: entry.size, reverse=True)[
        :reference_count
    ]
    if not selected:
        raise GameVoiceImportError(f"Voice bank contains no embedded media: {bank}")

    references_directory = output_directory / "references"
    references_directory.mkdir(parents=True, exist_ok=True)
    slug = slugify(character)
    decoded = []
    with TemporaryDirectory(prefix="vntts-game-voice-") as temporary_directory:
        temporary_directory = Path(temporary_directory)
        for index, item in enumerate(selected, start=1):
            source = temporary_directory / f"{item.media_id}.wem"
            source.write_bytes(item.data)
            output = references_directory / f"{slug}-game-{index:02d}.wav"
            convert_audio(
                source,
                output,
                decoder=decoder,
                overwrite=True,
            )
            decoded.append(output)
    return decoded


def update_manifest(output_directory, character, references, source_bank):
    manifest_path = output_directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {"version": 2, "reference_count": len(references), "voices": []}
    except json.JSONDecodeError as error:
        raise GameVoiceImportError(f"Invalid voice manifest: {error}") from error

    voices = manifest.get("voices")
    if not isinstance(voices, list):
        raise GameVoiceImportError("Voice manifest must contain a voices list")
    normalized_character = normalize_name(character)
    voices = [
        voice
        for voice in voices
        if normalize_name(str(voice.get("character", ""))) != normalized_character
    ]
    voices.append(
        {
            "character": character.strip(),
            "speaker": f"reverse-1999-{slugify(character)}-game-v1",
            "references": [
                reference.relative_to(output_directory).as_posix()
                for reference in references
            ],
            "aliases": [],
            "sources": [f"local-game-bank:{source_bank.name}"],
        }
    )
    voices.sort(key=lambda voice: voice["character"].casefold())
    manifest["version"] = 2
    manifest["voices"] = voices
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        bank = resolve_bank(
            arguments.character,
            arguments.bank,
            arguments.game_audio_directory,
        )
        decoder = resolve_decoder(arguments.decoder)
        output_directory = arguments.output.expanduser().resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        references = decode_references(
            bank,
            output_directory,
            arguments.character,
            arguments.references,
            decoder,
        )
        manifest = update_manifest(
            output_directory,
            arguments.character,
            references,
            bank,
        )
    except (GameVoiceImportError, WwiseBankError, AudioConversionError) as error:
        print(error, file=sys.stderr)
        return 1

    print(
        f"Imported {len(references)} clean references for {arguments.character} "
        f"into {manifest}"
    )
    return 0
