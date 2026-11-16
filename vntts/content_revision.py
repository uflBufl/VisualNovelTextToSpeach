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


@dataclass(frozen=True)
class ContentRevision:
    source_path: Path
    source_sha256: str
    _signature: SourceSignature
    _metadata: bytes
    _chapters: Mapping[str, tuple[tuple[int, int, str], ...]]
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
            chapters[record.chapter].append(
                (offset, len(raw), hashlib.sha256(raw).hexdigest())
            )
            offset += len(raw)
        return cls(
            path,
            hashlib.sha256(payload).hexdigest(),
            signature,
            rows[0],
            MappingProxyType({key: tuple(value) for key, value in chapters.items()}),
        )

    @classmethod
    def from_catalog(cls, catalog: DialogueCatalog) -> ContentRevision:
        """Reuse admitted SQL locations without retaining a corpus-sized map."""
        catalog.validate_current()
        return cls(
            catalog.source_path,
            catalog.source_sha256,
            catalog.signature,
            catalog.header,
            MappingProxyType({}),
            catalog,
        )

    def validate_current(self) -> None:
        if self._catalog is not None:
            self._catalog.validate_current()
        if source_signature(self.source_path) != self._signature:
            raise StoryIndexError("Content revision source changed")

    def read_chapter(self, chapter: str) -> StoryIndexDocument:
        """Read and verify only the chapter rows admitted in this revision."""
        if self._catalog is not None:
            self.validate_current()
            return self._catalog.records(self._catalog.line_ids(chapter))
        rows = []
        with open_regular_binary(self.source_path) as stream:
            # Windows descriptor ctime can differ from path stat ctime.
            if _signature(os.fstat(stream.fileno()))[:-1] != self._signature[:-1]:
                raise StoryIndexError("Content revision source changed")
            for offset, length, digest in self._chapters.get(chapter, ()):
                stream.seek(offset)
                raw = stream.read(length)
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise StoryIndexError("Content revision chapter row changed")
                rows.append(raw)
            if _signature(os.fstat(stream.fileno()))[:-1] != self._signature[:-1]:
                raise StoryIndexError("Content revision source changed")
        self.validate_current()
        metadata = json.loads(self._metadata)
        metadata.pop("selection_origin", None)
        metadata["line_count"] = len(rows)
        payload = json.dumps(metadata, ensure_ascii=False).encode() + b"\n"
        return load_story_index_snapshot(self.source_path, payload + b"".join(rows))
