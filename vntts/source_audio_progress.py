"""Validated, content-free progress from a source-audio publisher."""

from dataclasses import dataclass
from pathlib import Path

from vntts.json_types import decode_json
from vntts.path_safety import open_regular_text


@dataclass(frozen=True)
class SourceAudioProgress:
    stage: str
    processed: int
    total: int
    state: str


def read_source_audio_progress(path: Path) -> SourceAudioProgress | None:
    try:
        with open_regular_text(path, encoding="utf-8") as stream:
            payload = stream.read(4097)
        if len(payload) > 4096:
            return None
        document = decode_json(payload)
    except OSError, ValueError:
        return None
    if not isinstance(document, dict):
        return None
    processed, total = document.get("processed"), document.get("total")
    stage, state = document.get("stage"), document.get("state")
    if (
        document.get("schema") != "r1999.source-audio-progress"
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or stage not in ("duration", "semantics", "references", "portraits")
        or state not in ("analyzing", "publishing")
        or type(processed) is not int
        or type(total) is not int
        or not 0 <= processed <= total <= 2**31 - 1
        or state == "publishing"
        and stage != "portraits"
        and processed != total
    ):
        return None
    return SourceAudioProgress(str(stage), processed, total, str(state))
