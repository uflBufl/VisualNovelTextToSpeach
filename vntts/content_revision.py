"""Admitted story identity and bounded reads of its original chapter rows."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vntts.story_catalog import DialogueCatalog

from vntts_artifacts.story_index import StoryIndexDocument, StoryIndexError

from vntts.path_safety import open_regular_binary
from vntts.story_index_snapshot import load_story_index_snapshot

SourceSignature = tuple[int, int, int, int, int, int]


def _signature(info: os.stat_result) -> SourceSignature:
    if not stat.S_ISREG(info.st_mode):
        raise StoryIndexError("Content revision source is not a regular file")
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def source_signature(path: Path) -> SourceSignature:
    """Capture before reading the payload admitted as a content revision."""
    return _signature(path.stat(follow_symlinks=False))


def reusable_file_signature(path: Path) -> SourceSignature | None:
    """Identify unchanged regular files on systems with a fine-grained POSIX ctime."""
    if os.name != "posix":
        return None
    info = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_ino
        or info.st_ctime_ns % 1_000_000 == 0
    ):
        return None
    return _signature(info)


@dataclass(frozen=True)
class ContentRevision:
    source_path: Path
    source_sha256: str
    _metadata: bytes
    _chapters: Mapping[str, tuple[tuple[int, int], ...]]
    _catalog: DialogueCatalog | None = None

    @classmethod
    def capture(
        cls,
        path: Path,
        payload: bytes,
        document: StoryIndexDocument,
        *,
        signature: SourceSignature,
    ) -> ContentRevision:
        """Retain locations from an already parsed, admitted payload, not records."""
        path = Path(path).expanduser().absolute()
        rows = payload.splitlines(keepends=True)
        if (
            path.resolve() != document.path.resolve()
            or len(payload) != signature[3]
            or len(rows) != len(document.records) + 1
            or source_signature(path) != signature
        ):
            raise StoryIndexError("Content revision source changed during admission")
        chapters = defaultdict(list)
        offset = len(rows[0])
        for record, raw in zip(document.records, rows[1:], strict=True):
            chapters[record.chapter].append((offset, len(raw)))
            offset += len(raw)
        return cls(
            path,
            hashlib.sha256(payload).hexdigest(),
            rows[0],
            MappingProxyType({key: tuple(value) for key, value in chapters.items()}),
        )

    @classmethod
    def from_catalog(cls, catalog: DialogueCatalog) -> ContentRevision:
        """Reuse admitted SQL locations without retaining a corpus-sized map."""
        catalog.ensure_open()
        return cls(
            catalog.source_path,
            catalog.source_sha256,
            catalog.header,
            MappingProxyType({}),
            catalog,
        )

    def read_chapter(self, chapter: str) -> StoryIndexDocument:
        """Read admitted chapter locations; source replacement requires a new revision."""
        if self._catalog is not None:
            return self._catalog.records(self._catalog.line_ids(chapter))
        rows = []
        with open_regular_binary(self.source_path) as stream:
            for offset, length in self._chapters.get(chapter, ()):
                stream.seek(offset)
                rows.append(stream.read(length))
        metadata = json.loads(self._metadata)
        metadata.pop("selection_origin", None)
        metadata["line_count"] = len(rows)
        payload = json.dumps(metadata, ensure_ascii=False).encode() + b"\n"
        return load_story_index_snapshot(self.source_path, payload + b"".join(rows))
