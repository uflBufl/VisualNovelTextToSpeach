import json
from dataclasses import dataclass
from pathlib import Path

story_index_schema = "vntts.story-index"
story_index_schema_version = 1


class StoryIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoryIndexLine:
    line_id: str
    chapter: str
    sequence: int
    speaker: str
    text: str
    kind: str


def load_story_index(path):
    path = Path(path).expanduser().resolve()
    try:
        stream = path.open(encoding="utf-8")
    except OSError as error:
        raise StoryIndexError(f"Unable to open story index {path}: {error}") from error

    with stream:
        try:
            metadata = json.loads(next(stream))
        except StopIteration as error:
            raise StoryIndexError(f"Story index is empty: {path}") from error
        except json.JSONDecodeError as error:
            raise StoryIndexError(
                f"Invalid story-index metadata in {path}: {error}"
            ) from error
        if not isinstance(metadata, dict):
            raise StoryIndexError("Story-index metadata must be an object")
        if metadata.get("record_type") != "metadata":
            raise StoryIndexError("Story index must begin with a metadata record")
        if metadata.get("schema") != story_index_schema:
            raise StoryIndexError(
                f"Unsupported story-index schema: {metadata.get('schema')!r}"
            )
        if metadata.get("schema_version") != story_index_schema_version:
            raise StoryIndexError(
                f"Unsupported story-index schema version: {metadata.get('schema_version')!r}"
            )

        lines = []
        for row_number, row in enumerate(stream, start=2):
            try:
                record = json.loads(row)
                if not isinstance(record, dict) or record.get("record_type") != "line":
                    raise ValueError("expected a line record")
                line_id = _required_text(record, "line_id")
                chapter = _required_text(record, "chapter")
                speaker = _required_text(record, "speaker")
                text = _required_text(record, "text")
                sequence = int(record["sequence"])
                kind = str(record.get("kind") or "dialogue").strip()
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise StoryIndexError(
                    f"Invalid story-index record at {path}:{row_number}: {error}"
                ) from error
            lines.append(
                StoryIndexLine(line_id, chapter, sequence, speaker, text, kind)
            )

    declared_count = metadata.get("line_count")
    if isinstance(declared_count, int) and declared_count != len(lines):
        raise StoryIndexError(
            f"Story-index line count mismatch: metadata says {declared_count}, read {len(lines)}"
        )
    return metadata, tuple(lines)


def _required_text(record, name):
    value = record[name]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()
