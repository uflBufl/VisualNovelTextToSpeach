"""Strict parsing of captured story bytes with their original source identity."""

from dataclasses import replace
from pathlib import Path

from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    load_story_index_document,
)

from vntts.cleanup import temporary_directory


def load_story_index_snapshot(path: str | Path, payload: bytes) -> StoryIndexDocument:
    """Keep the path-only artifact parser isolated from changes to the source."""
    source = Path(path).expanduser().resolve()
    with temporary_directory(prefix="vntts-story-snapshot-") as temporary:
        snapshot = (Path(temporary) / "story-index.jsonl").resolve()
        snapshot.write_bytes(payload)
        try:
            document = load_story_index_document(snapshot)
        except StoryIndexError as error:
            raise StoryIndexError(
                str(error).replace(str(snapshot), str(source))
            ) from error.__cause__
    return replace(document, path=source)
