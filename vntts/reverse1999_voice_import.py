import argparse
import hashlib
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.atomic_io import atomic_write_json
from vntts.file_integrity import sha256_file
from vntts.reverse1999_aliases import aliases_for_character
from vntts.reverse1999_catalog import (
    Reverse1999CatalogError,
    Reverse1999NpcCatalog,
    default_catalog_path,
    normalize_name,
)
from vntts.settings import get_local_data_directory
from vntts.voice_reference_quality import trim_and_normalize_voice_reference
from vntts.wwise import (
    AudioConversionError,
    WwiseBankError,
    convert_audio,
    read_embedded_media,
    resolve_decoder,
)

project_root = Path(__file__).resolve().parents[1]
default_output = get_local_data_directory() / "voice-packs" / "reverse1999"


class GameVoiceImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportedReference:
    path: Path
    media_id: int
    source_sha256: str
    reference_sha256: str
    bank: str | None = None


def is_scene_audio_bank(bank):
    """Return whether a bank is a scene-audio container, not a speaker bank."""
    stem = Path(bank).stem.casefold()
    return stem.startswith("activityvoc_story_") or stem.startswith(
        "plotvoc_story_"
    )


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
        "--catalog",
        type=Path,
        default=default_catalog_path,
        help="Versioned Reverse: 1999 NPC catalog.",
    )
    parser.add_argument(
        "--references",
        type=int,
        default=3,
        help="Maximum clean clips to import from the bank.",
    )
    parser.add_argument(
        "--media-id",
        type=int,
        action="append",
        dest="media_ids",
        help=(
            "Reviewed embedded media ID to import. Repeat for multiple clips; "
            "when omitted, the largest clips are selected."
        ),
    )
    parser.add_argument(
        "--decoder",
        default="vgmstream-cli",
        help="Path or command name for vgmstream-cli.",
    )
    return parser


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


def resolve_bank(
    character,
    bank=None,
    game_audio_directory=None,
    catalog_path=default_catalog_path,
):
    if bank is not None:
        bank = Path(bank).expanduser().resolve()
    else:
        try:
            npc = Reverse1999NpcCatalog.load(catalog_path).resolve(character)
        except Reverse1999CatalogError as error:
            raise GameVoiceImportError(str(error)) from error
        if npc is None:
            raise GameVoiceImportError(
                f"No cataloged story bank for {character!r}; pass --bank explicitly"
            )
        filename = npc.banks[0]
        game_audio_directory = game_audio_directory or find_game_audio_directory()
        if game_audio_directory is None:
            raise GameVoiceImportError(
                "Unable to find Reverse: 1999 game audio; pass --game-audio-directory"
            )
        bank = Path(game_audio_directory).expanduser().resolve() / filename

    if not bank.is_file():
        raise GameVoiceImportError(f"Voice bank does not exist: {bank}")
    return bank


def decode_references(
    bank,
    output_directory,
    character,
    reference_count,
    decoder,
    *,
    media_ids=None,
):
    if reference_count <= 0:
        raise GameVoiceImportError("--references must be positive")
    if not media_ids and is_scene_audio_bank(bank):
        raise GameVoiceImportError(
            f"Scene-audio bank {Path(bank).name} may contain TV, radio, crowd, or "
            "unrelated voices; pass explicitly reviewed --media-id values"
        )
    media = read_embedded_media(bank)
    if media_ids:
        by_id = {entry.media_id: entry for entry in media}
        missing = [media_id for media_id in media_ids if media_id not in by_id]
        if missing:
            joined = ", ".join(str(media_id) for media_id in missing)
            raise GameVoiceImportError(
                f"Voice bank {bank.name} does not contain media ID(s): {joined}"
            )
        selected = [by_id[media_id] for media_id in dict.fromkeys(media_ids)]
    else:
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
            decoded_output = temporary_directory / f"{item.media_id}.wav"
            convert_audio(
                source,
                decoded_output,
                decoder=decoder,
                overwrite=True,
            )
            trim_and_normalize_voice_reference(decoded_output, output)
            decoded.append(
                ImportedReference(
                    path=output,
                    media_id=item.media_id,
                    source_sha256=hashlib.sha256(item.data).hexdigest(),
                    reference_sha256=sha256_file(output),
                    bank=bank.name,
                )
            )
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
    reference_paths = [
        reference.path if isinstance(reference, ImportedReference) else Path(reference)
        for reference in references
    ]
    entry = {
        "character": character.strip(),
        "speaker": f"reverse-1999-{slugify(character)}-game-v1",
        "references": [
            reference.relative_to(output_directory).as_posix()
            for reference in reference_paths
        ],
        "aliases": list(aliases_for_character(character)),
        "sources": [
            f"local-game-bank:{name}"
            for name in sorted(
                {
                    reference.bank or source_bank.name
                    for reference in references
                    if isinstance(reference, ImportedReference)
                }
                or {source_bank.name}
            )
        ],
    }
    imported = [
        reference
        for reference in references
        if isinstance(reference, ImportedReference)
    ]
    if imported:
        entry["reference_metadata"] = [
            {
                "bank": reference.bank or source_bank.name,
                "media_id": reference.media_id,
                "source_sha256": reference.source_sha256,
                "reference_sha256": reference.reference_sha256,
            }
            for reference in imported
        ]
    voices.append(entry)
    voices.sort(key=lambda voice: voice["character"].casefold())
    manifest["version"] = 2
    manifest["voices"] = voices
    atomic_write_json(manifest_path, manifest)
    return manifest_path


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        bank = resolve_bank(
            arguments.character,
            arguments.bank,
            arguments.game_audio_directory,
            arguments.catalog,
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
            media_ids=arguments.media_ids,
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
