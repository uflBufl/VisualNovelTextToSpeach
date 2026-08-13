import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from vntts.file_integrity import sha256_file

project_root = Path(__file__).resolve().parents[1]
default_catalog_path = project_root / "data" / "reverse1999-npc-catalog.json"
sha256_pattern = re.compile(r"[0-9a-f]{64}")


class Reverse1999CatalogError(RuntimeError):
    pass


def normalize_name(value):
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


@dataclass(frozen=True)
class ApprovedReference:
    bank: str
    media_id: int
    source_sha256: str
    reference: str
    reference_sha256: str


@dataclass(frozen=True)
class Reverse1999Npc:
    npc_id: str
    display_name: str
    aliases: tuple[str, ...]
    language: str
    game_versions: tuple[str, ...]
    banks: tuple[str, ...]
    approved_references: tuple[ApprovedReference, ...]


class Reverse1999NpcCatalog:
    def __init__(self, version, game, npcs):
        self.version = version
        self.game = game
        self.npcs = tuple(npcs)
        self._by_id = {npc.npc_id: npc for npc in self.npcs}
        self._by_name = {}
        for npc in self.npcs:
            for name in (npc.display_name, *npc.aliases):
                normalized = normalize_name(name)
                previous = self._by_name.get(normalized)
                if previous is not None and previous != npc:
                    raise Reverse1999CatalogError(
                        f"NPC name or alias {name!r} is used more than once"
                    )
                self._by_name[normalized] = npc

    def resolve(self, name):
        return self._by_name.get(normalize_name(name))

    def get(self, npc_id):
        return self._by_id.get(str(npc_id))

    def validate_reference_files(self, root=project_root / "data"):
        root = Path(root).expanduser().resolve()
        for npc in self.npcs:
            for approved in npc.approved_references:
                reference = root / approved.reference
                if not reference.is_file():
                    raise Reverse1999CatalogError(
                        f"Approved reference does not exist: {reference}"
                    )
                checksum = sha256_file(reference)
                if checksum != approved.reference_sha256:
                    raise Reverse1999CatalogError(
                        f"Approved reference checksum does not match: {reference}"
                    )
        return True

    @classmethod
    def load(cls, path=default_catalog_path):
        path = Path(path).expanduser().resolve()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise Reverse1999CatalogError(
                f"NPC catalog does not exist: {path}"
            ) from error
        except (OSError, json.JSONDecodeError) as error:
            raise Reverse1999CatalogError(
                f"Unable to read NPC catalog {path}: {error}"
            ) from error
        return cls.from_dict(document)

    @classmethod
    def from_dict(cls, document):
        if not isinstance(document, dict):
            raise Reverse1999CatalogError("NPC catalog must be a JSON object")
        version = document.get("version")
        game = document.get("game")
        entries = document.get("npcs")
        if not isinstance(version, int) or version <= 0:
            raise Reverse1999CatalogError("NPC catalog requires a positive version")
        if not isinstance(game, str) or not game.strip():
            raise Reverse1999CatalogError("NPC catalog requires a game name")
        if not isinstance(entries, list):
            raise Reverse1999CatalogError("NPC catalog requires an NPC list")

        npcs = [cls._parse_npc(entry, index) for index, entry in enumerate(entries)]
        npc_ids = [npc.npc_id for npc in npcs]
        if len(npc_ids) != len(set(npc_ids)):
            raise Reverse1999CatalogError("NPC IDs must be unique")
        return cls(version, game.strip(), npcs)

    @staticmethod
    def _parse_npc(entry, index):
        if not isinstance(entry, dict):
            raise Reverse1999CatalogError(f"NPC entry {index} must be an object")
        npc_id = entry.get("id")
        display_name = entry.get("display_name")
        aliases = entry.get("aliases", [])
        language = entry.get("language")
        game_versions = entry.get("game_versions")
        banks = entry.get("banks")
        references = entry.get("approved_references", [])
        if not isinstance(npc_id, str) or not npc_id.strip():
            raise Reverse1999CatalogError(f"NPC entry {index} requires an ID")
        if not isinstance(display_name, str) or not display_name.strip():
            raise Reverse1999CatalogError(f"NPC entry {index} requires a display name")
        for label, values in (
            ("aliases", aliases),
            ("game versions", game_versions),
            ("banks", banks),
        ):
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value.strip() for value in values
            ):
                raise Reverse1999CatalogError(
                    f"NPC entry {index} {label} must be non-empty strings"
                )
        if not isinstance(language, str) or not language.strip():
            raise Reverse1999CatalogError(f"NPC entry {index} requires a language")
        if not game_versions:
            raise Reverse1999CatalogError(f"NPC entry {index} requires a game version")
        if not banks:
            raise Reverse1999CatalogError(
                f"NPC entry {index} requires at least one bank"
            )
        if not isinstance(references, list):
            raise Reverse1999CatalogError(
                f"NPC entry {index} approved references must be a list"
            )
        approved_references = tuple(
            Reverse1999NpcCatalog._parse_reference(reference, index, banks)
            for reference in references
        )
        return Reverse1999Npc(
            npc_id=npc_id.strip(),
            display_name=display_name.strip(),
            aliases=tuple(alias.strip() for alias in aliases),
            language=language.strip(),
            game_versions=tuple(version.strip() for version in game_versions),
            banks=tuple(bank.strip() for bank in banks),
            approved_references=approved_references,
        )

    @staticmethod
    def _parse_reference(entry, npc_index, banks):
        if not isinstance(entry, dict):
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} approved reference must be an object"
            )
        bank = entry.get("bank")
        media_id = entry.get("media_id")
        source_sha256 = entry.get("source_sha256")
        reference = entry.get("reference")
        reference_sha256 = entry.get("reference_sha256")
        if bank not in banks:
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} reference bank is not in its bank list"
            )
        if not isinstance(media_id, int) or media_id <= 0:
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} reference requires a media ID"
            )
        for label, value in (
            ("source checksum", source_sha256),
            ("reference checksum", reference_sha256),
        ):
            if not isinstance(value, str) or not sha256_pattern.fullmatch(value):
                raise Reverse1999CatalogError(
                    f"NPC entry {npc_index} reference has an invalid {label}"
                )
        if not isinstance(reference, str) or not reference.strip():
            raise Reverse1999CatalogError(
                f"NPC entry {npc_index} reference requires a path"
            )
        return ApprovedReference(
            bank=bank,
            media_id=media_id,
            source_sha256=source_sha256,
            reference=reference.strip(),
            reference_sha256=reference_sha256,
        )


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Validate locally provisioned Reverse: 1999 voice references against "
            "the versioned catalog metadata."
        )
    )
    parser.add_argument("--catalog", type=Path, default=default_catalog_path)
    parser.add_argument(
        "--reference-root",
        type=Path,
        default=project_root / "data",
        help="Directory used as the base for catalog reference paths.",
    )
    return parser


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    try:
        catalog = Reverse1999NpcCatalog.load(arguments.catalog)
        catalog.validate_reference_files(arguments.reference_root)
    except Reverse1999CatalogError as error:
        print(error, file=sys.stderr)
        return 1
    reference_count = sum(len(npc.approved_references) for npc in catalog.npcs)
    suffix = "" if reference_count == 1 else "s"
    print(f"Validated {reference_count} approved reference{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
