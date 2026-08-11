import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vntts.reverse1999_voice_import import find_game_audio_directory
from vntts.settings import get_local_data_directory
from vntts.wwise import WwiseBankError, inspect_bank

index_version = 1
default_output = (
    get_local_data_directory() / "reverse1999" / "english-bank-index.json"
)
npc_id_pattern = re.compile(r"npc[_-]?(\d{4,})", re.IGNORECASE)
chapter_pattern = re.compile(r"chapter[_-]?(\d+)", re.IGNORECASE)


class Reverse1999IndexError(RuntimeError):
    pass


def create_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Index installed Reverse: 1999 English Wwise banks for NPC voice "
            "discovery. Existing unchanged entries are reused."
        )
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
        help="JSON index path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reinspect every bank instead of reusing unchanged entries.",
    )
    return parser


def classify_bank(filename):
    stem = Path(filename).stem.casefold()
    tags = []
    if "npc" in stem:
        tags.append("npc")
    if "plotvoc" in stem or "story" in stem or chapter_pattern.search(stem):
        tags.append("story")
    if "activity" in stem:
        tags.append("activity")
    if "voc" in stem or "voice" in stem:
        tags.append("voice")

    tag_set = set(tags)
    if {"npc", "activity"} <= tag_set:
        category = "activity-npc"
    elif {"npc", "story"} <= tag_set:
        category = "story-npc"
    elif "npc" in tag_set:
        category = "npc"
    elif "voice" in tag_set:
        category = "voice"
    else:
        category = "other"
    return category, tags


def inspect_bank_entry(bank, root, *, inspector=inspect_bank):
    stat = bank.stat()
    relative_path = bank.relative_to(root).as_posix()
    category, tags = classify_bank(bank.name)
    entry = {
        "path": relative_path,
        "filename": bank.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "category": category,
        "tags": tags,
        "npc_ids": sorted(set(npc_id_pattern.findall(bank.stem))),
        "chapters": sorted(
            {int(value) for value in chapter_pattern.findall(bank.stem)}
        ),
    }
    try:
        summary = inspector(bank)
    except (OSError, WwiseBankError) as error:
        entry["error"] = str(error)
        return entry

    entry.update(
        {
            "bank_version": summary.bank_version,
            "sections": list(summary.sections),
            "media_count": summary.media_count,
            "embedded_media_bytes": summary.embedded_media_bytes,
            "hirc_object_count": summary.hirc_object_count,
        }
    )
    return entry


def load_reusable_entries(path, root):
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if (
        previous.get("version") != index_version
        or previous.get("game_audio_directory") != str(root)
    ):
        return {}
    return {
        entry["path"]: entry
        for entry in previous.get("banks", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def build_bank_index(
    game_audio_directory,
    *,
    output=None,
    force=False,
    inspector=inspect_bank,
    progress=None,
):
    root = Path(game_audio_directory).expanduser().resolve()
    if not root.is_dir():
        raise Reverse1999IndexError(f"Game audio directory does not exist: {root}")
    banks = sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".bnk"
        ),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    if not banks:
        raise Reverse1999IndexError(f"No .bnk files found in {root}")

    output = Path(output or default_output).expanduser().resolve()
    reusable = {} if force else load_reusable_entries(output, root)
    entries = []
    reused_count = 0
    progress = progress or (lambda _current, _total, _bank, _reused: None)
    for current, bank in enumerate(banks, start=1):
        relative_path = bank.relative_to(root).as_posix()
        stat = bank.stat()
        previous = reusable.get(relative_path)
        reused = bool(
            previous
            and previous.get("size") == stat.st_size
            and previous.get("mtime_ns") == stat.st_mtime_ns
        )
        if reused:
            entry = previous
            reused_count += 1
        else:
            entry = inspect_bank_entry(bank, root, inspector=inspector)
        entries.append(entry)
        progress(current, len(banks), bank, reused)

    categories = Counter(entry["category"] for entry in entries)
    npc_banks = defaultdict(list)
    for entry in entries:
        for npc_id in entry["npc_ids"]:
            npc_banks[npc_id].append(entry["path"])
    index = {
        "version": index_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "game_audio_directory": str(root),
        "bank_count": len(entries),
        "reused_count": reused_count,
        "error_count": sum("error" in entry for entry in entries),
        "categories": dict(sorted(categories.items())),
        "npc_banks": dict(sorted(npc_banks.items())),
        "banks": entries,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return index, output


def main(arguments=None):
    arguments = create_parser().parse_args(arguments)
    game_audio_directory = arguments.game_audio_directory or find_game_audio_directory()
    if game_audio_directory is None:
        print(
            "Unable to find Reverse: 1999 game audio; pass --game-audio-directory",
            file=sys.stderr,
        )
        return 1

    def progress(current, total, bank, reused):
        if current == total or current % 100 == 0:
            action = "Reused" if reused else "Indexed"
            print(f"{action} {current}/{total}: {bank.name}")

    try:
        index, output = build_bank_index(
            game_audio_directory,
            output=arguments.output,
            force=arguments.force,
            progress=progress,
        )
    except Reverse1999IndexError as error:
        print(error, file=sys.stderr)
        return 1

    print(
        f"Indexed {index['bank_count']} banks ({index['reused_count']} reused, "
        f"{index['error_count']} errors) into {output}"
    )
    return 0
