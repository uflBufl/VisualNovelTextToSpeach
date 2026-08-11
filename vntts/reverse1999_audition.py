import json
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.reverse1999_config import default_output as default_dialogue_index
from vntts.reverse1999_index import default_output as default_bank_index
from vntts.settings import get_local_data_directory
from vntts.wwise import convert_audio, read_embedded_media

default_mapping_path = (
    get_local_data_directory() / "reverse1999" / "speaker-mappings.json"
)
default_audition_cache = get_local_data_directory() / "reverse1999" / "audition"


class Reverse1999AuditionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BankCandidate:
    path: str
    filename: str
    npc_ids: tuple[str, ...]
    media_ids: tuple[int, ...]
    score: int


def load_index(path, label):
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise Reverse1999AuditionError(
            f"{label} does not exist: {path}"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise Reverse1999AuditionError(f"Unable to read {label}: {error}") from error
    if not isinstance(document, dict):
        raise Reverse1999AuditionError(f"{label} must contain a JSON object")
    return document


def chapter_tokens(chapter):
    digits = re.sub(r"\D", "", str(chapter))
    if len(digits) < 2:
        return ()
    major, minor = digits[0], digits[1]
    return (f"{major}_{minor}", f"{major}-{minor}", f"plot{major}{minor}")


def filter_dialogue(dialogue, *, query="", chapter=None, speaker_id=None):
    query = query.strip().casefold()
    filtered = []
    for row in dialogue:
        if chapter and str(row.get("chapter")) != str(chapter):
            continue
        if speaker_id and str(row.get("speaker_id")) != str(speaker_id):
            continue
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("speaker_name", "speaker_id", "text", "language_key")
        ).casefold()
        if query and query not in haystack:
            continue
        filtered.append(row)
    return filtered


def _media_ids(entry):
    media = set()
    for event in entry.get("events", []):
        media.update(
            value for value in event.get("media_ids", []) if isinstance(value, int)
        )
    return tuple(sorted(media))


def candidate_banks(bank_index, *, chapter=None, speaker_id=None, limit=80):
    tokens = chapter_tokens(chapter) if chapter else ()
    candidates = []
    for entry in bank_index.get("banks", []):
        if not isinstance(entry, dict) or entry.get("error"):
            continue
        filename = str(entry.get("filename", ""))
        folded = filename.casefold()
        npc_ids = tuple(str(value) for value in entry.get("npc_ids", []))
        score = 0
        exact_speaker = bool(speaker_id and str(speaker_id) in npc_ids)
        chapter_match = bool(tokens and any(token in folded for token in tokens))
        if exact_speaker:
            score += 100
        if chapter_match:
            score += 40
        if (speaker_id or tokens) and not (exact_speaker or chapter_match):
            continue
        if "story" in folded or "plotvoc" in folded or "activityvoc" in folded:
            score += 10
        if "npc" in folded:
            score += 5
        if score == 0:
            continue
        candidates.append(
            BankCandidate(
                path=str(entry.get("path", "")),
                filename=filename,
                npc_ids=npc_ids,
                media_ids=_media_ids(entry),
                score=score,
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.filename.casefold()))
    return candidates[:limit]


def prepare_audition_clip(
    bank,
    media_id,
    *,
    decoder="vgmstream-cli",
    cache_directory=default_audition_cache,
):
    bank = Path(bank).expanduser().resolve()
    cache_directory = Path(cache_directory).expanduser().resolve()
    output = cache_directory / bank.stem / f"{media_id}.wav"
    if output.is_file():
        return output
    selected = next(
        (item for item in read_embedded_media(bank) if item.media_id == media_id), None
    )
    if selected is None:
        raise Reverse1999AuditionError(
            f"Media {media_id} does not exist in {bank.name}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="vntts-audition-") as temporary_directory:
        source = Path(temporary_directory) / f"{media_id}.wem"
        source.write_bytes(selected.data)
        convert_audio(source, output, decoder=decoder, overwrite=True)
    return output


def save_speaker_mapping(
    display_name,
    npc_id,
    bank,
    chapter,
    *,
    path=default_mapping_path,
):
    display_name = display_name.strip()
    npc_id = str(npc_id).strip()
    bank = str(bank).strip()
    if not display_name or not npc_id or not bank:
        raise Reverse1999AuditionError(
            "A speaker name, NPC ID, and voice bank are required"
        )
    path = Path(path).expanduser().resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = {"version": 1, "mappings": []}
    except (OSError, json.JSONDecodeError) as error:
        raise Reverse1999AuditionError(f"Unable to read speaker mappings: {error}")
    mappings = document.get("mappings")
    if document.get("version") != 1 or not isinstance(mappings, list):
        raise Reverse1999AuditionError("Speaker mapping file has an unsupported format")
    normalized = display_name.casefold()
    mappings[:] = [
        item
        for item in mappings
        if str(item.get("display_name", "")).casefold() != normalized
    ]
    mappings.append(
        {
            "display_name": display_name,
            "npc_id": npc_id,
            "bank": Path(bank).name,
            "chapter": str(chapter),
        }
    )
    mappings.sort(key=lambda item: item["display_name"].casefold())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def load_audition_data(
    dialogue_index=default_dialogue_index, bank_index=default_bank_index
):
    return (
        load_index(dialogue_index, "Dialogue index"),
        load_index(bank_index, "Bank index"),
    )
