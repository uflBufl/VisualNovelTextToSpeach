"""Small story snapshots with membership witnesses in a verified source catalog.

Persistent witnesses are hints, not validation receipts. Each process admits the
complete source and its semantic evidence once; later reads still hash the source.
Only immutable metadata, row locations and original row hashes survive admission.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import cast

from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    write_story_index_document,
)

from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.document_identity import is_lowercase_sha256, sha256_file
from vntts.path_safety import open_regular_binary, read_regular_bytes
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
    project_source_audio_semantics,
)

_ORIGIN_SCHEMA = "vntts.selected-story-origin"
_EVIDENCE_NAME = "source-audio-semantic-evidence.json"
_ANNOTATION_FIELDS = frozenset(
    {
        "source_audio_duration_seconds",
        "source_audio_duration_media_id",
        "source_audio_duration_media_sha256",
        "source_audio_duration_sample_rate",
        "source_audio_duration_sample_count",
        "source_audio_duration_decoder",
        "source_audio_completeness",
        "source_audio_completeness_reason",
        "source_audio_semantic_evidence_id",
        "source_audio_semantic_evidence_entry_id",
    }
)
_PROJECTED_METADATA = frozenset(
    {
        "line_count",
        "selection_origin",
        "source_audio_completion",
        "source_audio_timing",
        "source_audio_semantics",
    }
)


@dataclass(frozen=True)
class _SourceReceipt:
    metadata: bytes
    rows: Mapping[str, tuple[int, int, str]]
    selections: tuple[tuple[str, tuple[str, ...]], ...]


_source_receipts: OrderedDict[tuple[str, str, str | None], _SourceReceipt] = (
    OrderedDict()
)
_receipt_lock = Lock()


def _ids(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise StoryIndexError(
            f"Selected story {label} must contain unique non-empty IDs"
        )
    return tuple(value)


def _origin(document: StoryIndexDocument) -> dict[str, object] | None:
    origin = document.metadata.get("selection_origin")
    if origin is None and "selection_origin" not in document.metadata:
        return None
    if (
        not isinstance(origin, dict)
        or set(origin)
        != {
            "schema",
            "schema_version",
            "story_index",
            "story_index_sha256",
            "selected_line_ids",
            "selected_story_ids",
            "records",
            "source_audio_semantic_evidence_sha256",
        }
        or origin.get("schema") != _ORIGIN_SCHEMA
        or type(origin.get("schema_version")) is not int
        or origin.get("schema_version") != 1
        or not is_lowercase_sha256(origin.get("story_index_sha256"))
    ):
        raise StoryIndexError("Selected story origin is invalid")
    path = origin.get("story_index")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or str(Path(path).resolve()) != path
    ):
        raise StoryIndexError(
            "Selected story origin path must be canonical and absolute"
        )
    evidence = origin.get("source_audio_semantic_evidence_sha256")
    if evidence is not None and not is_lowercase_sha256(evidence):
        raise StoryIndexError("Selected story origin evidence checksum is invalid")
    _ids(origin.get("selected_line_ids"), "line selection")
    _ids(origin.get("selected_story_ids"), "story selection")
    witnesses = origin.get("records")
    if not isinstance(witnesses, list) or any(
        not isinstance(row, dict)
        or set(row) != {"line_id", "offset", "length"}
        or not isinstance(row["line_id"], str)
        or type(row["offset"]) is not int
        or row["offset"] < 0
        or type(row["length"]) is not int
        or row["length"] <= 0
        for row in witnesses
    ):
        raise StoryIndexError("Selected story origin row witnesses are invalid")
    return origin


def selected_story_origin(document: StoryIndexDocument) -> tuple[Path, str] | None:
    """Return the ultimate catalog identity, leaving admission to the loader."""
    origin = _origin(document)
    if origin is None:
        return None
    return Path(str(origin["story_index"])), str(origin["story_index_sha256"])


def _evidence_payload(source: Path) -> bytes | None:
    try:
        return read_regular_bytes(source.with_name(_EVIDENCE_NAME))
    except FileNotFoundError:
        return None


def _digest(payload: bytes | None) -> str | None:
    return None if payload is None else hashlib.sha256(payload).hexdigest()


def _changed(message: str) -> None:
    from vntts.pregeneration_setup import StoryContentChanged

    raise StoryIndexError(message) from StoryContentChanged(message)


def _assert_current(
    source: Path, expected_sha256: str, evidence_sha256: str | None
) -> None:
    if sha256_file(source) != expected_sha256:
        _changed("Selected story origin checksum changed while read")
    if _digest(_evidence_payload(source)) != evidence_sha256:
        _changed("Selected story origin evidence checksum changed while read")


def _admit_source(
    source: Path,
    expected_sha256: str,
    evidence_sha256: str | None,
    *,
    captured_payload: bytes | None = None,
) -> tuple[bytes, _SourceReceipt, StoryIndexDocument | None]:
    from vntts.story_index_snapshot import load_story_index_snapshot

    payload = (
        read_regular_bytes(source) if captured_payload is None else captured_payload
    )
    if _digest(payload) != expected_sha256:
        _changed("Selected story origin checksum changed")
    evidence_payload = _evidence_payload(source)
    if _digest(evidence_payload) != evidence_sha256:
        _changed("Selected story origin evidence checksum changed")
    try:
        metadata = json.loads(payload.partition(b"\n")[0])
    except (UnicodeError, ValueError) as error:
        raise StoryIndexError(
            f"Selected story origin metadata is invalid: {error}"
        ) from error
    if isinstance(metadata, dict) and "selection_origin" in metadata:
        raise StoryIndexError(
            "Selected story origin must identify the complete catalog"
        )
    receipt = _cached_receipt(source, expected_sha256, evidence_sha256)
    document = None
    if receipt is None:
        document = load_story_index_snapshot(source, payload)
        _validate_source_semantics(source, document, evidence_payload)
        receipt = _remember_source_receipt(
            document, payload, expected_sha256, evidence_sha256
        )
    return payload, receipt, document


def remember_parsed_story_source(
    document: StoryIndexDocument, payload: bytes, expected_sha256: str
) -> None:
    """Seal a fresh strict parse before its mutable document escapes the loader."""
    if "selection_origin" in document.metadata:
        return
    try:
        evidence_payload = _evidence_payload(document.path)
    except OSError:
        if sha256_file(document.path) != expected_sha256:
            _changed("Selected story origin checksum changed while read")
        return
    evidence_sha256 = _digest(evidence_payload)
    if _cached_receipt(document.path, expected_sha256, evidence_sha256) is not None:
        _assert_current(document.path, expected_sha256, evidence_sha256)
        return
    try:
        _validate_source_semantics(document.path, document, evidence_payload)
    except StoryIndexError:
        # Portable inspection may lack evidence; later selection still requires it.
        _assert_current(document.path, expected_sha256, evidence_sha256)
        return
    _remember_source_receipt(document, payload, expected_sha256, evidence_sha256)


def _remember_source_receipt(
    document: StoryIndexDocument,
    payload: bytes,
    expected_sha256: str,
    evidence_sha256: str | None,
) -> _SourceReceipt:
    receipt = _build_receipt(document, payload)
    _assert_current(document.path, expected_sha256, evidence_sha256)
    key = (str(document.path), expected_sha256, evidence_sha256)
    with _receipt_lock:
        _source_receipts[key] = receipt
        _source_receipts.move_to_end(key)
        if len(_source_receipts) > 8:
            _source_receipts.popitem(last=False)
    return receipt


def _cached_receipt(
    source: Path, checksum: str, evidence_checksum: str | None
) -> _SourceReceipt | None:
    key = (str(source), checksum, evidence_checksum)
    with _receipt_lock:
        receipt = _source_receipts.get(key)
        if receipt is not None:
            _source_receipts.move_to_end(key)
    return receipt


def _validate_source_semantics(
    source: Path, document: StoryIndexDocument, evidence: bytes | None
) -> None:
    if "source_audio_semantics" not in document.metadata:
        return
    try:
        load_source_audio_semantic_evidence(
            source.with_name(_EVIDENCE_NAME),
            document,
            payload=evidence if evidence is not None else b"",
        )
    except SourceAudioSemanticEvidenceError as error:
        raise StoryIndexError(f"Selected story origin is invalid: {error}") from error


def _build_receipt(document: StoryIndexDocument, payload: bytes) -> _SourceReceipt:
    # Picker grouping proves completeness, including split stages and legacy chapters.
    from vntts.pregeneration_setup import _story_selections

    selections = tuple(
        (selection.selection_id, tuple(selection.line_ids))
        for selection in _story_selections(document)
    )
    raw_rows = payload.splitlines(keepends=True)
    if len(raw_rows) != len(document.records) + 1:
        raise StoryIndexError("Selected story origin row boundaries changed")
    offset = len(raw_rows[0])
    rows = {}
    for line, raw in zip(document.records, raw_rows[1:], strict=True):
        rows[line.line_id] = (offset, len(raw), hashlib.sha256(raw).hexdigest())
        offset += len(raw)
    return _SourceReceipt(raw_rows[0], MappingProxyType(rows), selections)


def _selected_rows(
    receipt: _SourceReceipt,
    line_ids: tuple[str, ...],
    story_ids: tuple[str, ...],
) -> tuple[tuple[str, int, int], ...]:
    selections = dict(receipt.selections)
    if any(story_id not in selections for story_id in story_ids):
        raise StoryIndexError("Selected story contains unknown story IDs")
    selected = set(story_ids)
    expected = tuple(
        line_id
        for story_id, lines in receipt.selections
        if story_id in selected
        for line_id in lines
    )
    if line_ids != expected:
        raise StoryIndexError(
            "Selected story line IDs do not match the complete story selection"
        )
    return tuple(
        (line_id, receipt.rows[line_id][0], receipt.rows[line_id][1])
        for line_id in line_ids
    )


def _immutable_json(value: dict[str, object], omitted: frozenset[str]) -> str:
    # JSON serialization also distinguishes true from 1 in producer fields.
    return json.dumps(
        {key: item for key, item in value.items() if key not in omitted},
        sort_keys=True,
        allow_nan=False,
    )


def _record_digest(record: dict[str, object]) -> str:
    return hashlib.sha256(
        _immutable_json(record, _ANNOTATION_FIELDS).encode()
    ).hexdigest()


def _validate_records(
    document: StoryIndexDocument, origin: dict[str, object], receipt: _SourceReceipt
) -> None:
    line_ids = _ids(origin["selected_line_ids"], "line selection")
    story_ids = _ids(origin["selected_story_ids"], "story selection")
    rows = _selected_rows(receipt, line_ids, story_ids)
    witnesses = [
        {"line_id": line_id, "offset": offset, "length": length}
        for line_id, offset, length in rows
    ]
    if origin["records"] != witnesses:
        raise StoryIndexError("Selected story origin row witnesses changed")
    if tuple(record.line_id for record in document.records) != line_ids:
        raise StoryIndexError(
            "Selected story contains missing, extra or reordered records"
        )
    if _immutable_json(document.metadata, _PROJECTED_METADATA) != _immutable_json(
        json.loads(receipt.metadata), _PROJECTED_METADATA
    ):
        raise StoryIndexError("Selected story origin metadata changed")
    # Compare only selected records; raw hashes bind each read to full admission.
    with open_regular_binary(Path(str(origin["story_index"]))) as source:
        for record in document.records:
            offset, length, expected_sha256 = receipt.rows[record.line_id]
            source.seek(offset)
            raw = source.read(length)
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                _changed("Selected story origin checksum changed while reading a row")
            if _record_digest(record.to_record()) != _record_digest(json.loads(raw)):
                raise StoryIndexError(
                    f"Selected story origin record changed: {record.line_id!r}"
                )


def _validate_selected_semantics(document: StoryIndexDocument) -> None:
    if "source_audio_semantics" in document.metadata:
        selected_evidence = _evidence_payload(document.path)
        load_source_audio_semantic_evidence(
            document.path.with_name(_EVIDENCE_NAME),
            document,
            payload=selected_evidence if selected_evidence is not None else b"",
        )
        if _digest(_evidence_payload(document.path)) != _digest(selected_evidence):
            raise StoryIndexError("Selected story evidence changed while read")
    elif any(
        record.document.get("source_audio_semantic_evidence_entry_id") is not None
        for record in document.records
    ):
        raise StoryIndexError(
            "Selected story semantic annotations have no evidence binding"
        )


def validate_selected_story_origin(document: StoryIndexDocument) -> None:
    """Admit every selected record against current captured origin bytes."""
    origin = _origin(document)
    if origin is None:
        return
    source = Path(str(origin["story_index"]))
    expected_sha256 = str(origin["story_index_sha256"])
    evidence_sha256 = cast(str | None, origin["source_audio_semantic_evidence_sha256"])
    try:
        receipt = _cached_receipt(source, expected_sha256, evidence_sha256)
        if receipt is None:
            _, receipt, _ = _admit_source(source, expected_sha256, evidence_sha256)
        _validate_records(document, origin, receipt)
        _validate_selected_semantics(document)
        _assert_current(source, expected_sha256, evidence_sha256)
    except (OSError, SourceAudioSemanticEvidenceError) as error:
        raise StoryIndexError(f"Selected story origin is invalid: {error}") from error


def create_selected_story_snapshot(
    source: Path,
    expected_sha256: str,
    selected_line_ids: tuple[str, ...],
    selected_story_ids: tuple[str, ...],
    directory: Path,
) -> Path:
    """Publish or reuse a strict subset without granting authority to disk hints."""
    from vntts.story_index_snapshot import load_story_index_snapshot

    source = Path(source).expanduser().resolve()
    directory = Path(directory).expanduser().resolve()
    line_ids = _ids(selected_line_ids, "line selection")
    story_ids = _ids(selected_story_ids, "story selection")
    with open_regular_binary(source) as stream:
        header = json.loads(stream.readline())
    if isinstance(header, dict) and "selection_origin" in header:
        captured = read_regular_bytes(source)
        if _digest(captured) != expected_sha256:
            _changed("Selected story source checksum changed")
        document = load_story_index_snapshot(source, captured)
        _require_scope(document, line_ids, story_ids)
        if sha256_file(source) != expected_sha256:
            _changed("Selected story source checksum changed while read")
        return source
    destination = directory / "story-index.jsonl"
    if directory.exists():
        reused = load_story_index_snapshot(destination, read_regular_bytes(destination))
        _require_scope(reused, line_ids, story_ids, source, expected_sha256)
        return destination
    evidence_sha256 = _digest(_evidence_payload(source))
    captured = read_regular_bytes(source)
    payload, receipt, document = _admit_source(
        source, expected_sha256, evidence_sha256, captured_payload=captured
    )
    rows = _selected_rows(receipt, line_ids, story_ids)
    raw_records = [
        json.loads(payload[offset : offset + length]) for _, offset, length in rows
    ]
    if document is None:
        document = _selected_source_view(source, receipt, raw_records, payload, rows)
    _publish_snapshot(
        source,
        expected_sha256,
        evidence_sha256,
        line_ids,
        story_ids,
        rows,
        document,
        raw_records,
        directory,
    )
    return destination


def _require_scope(
    document: StoryIndexDocument,
    line_ids: tuple[str, ...],
    story_ids: tuple[str, ...],
    source: Path | None = None,
    checksum: str | None = None,
) -> None:
    origin = _origin(document)
    if origin is None:
        raise StoryIndexError("Selected story snapshot scope changed")
    if (
        _ids(origin["selected_line_ids"], "line selection") != line_ids
        or _ids(origin["selected_story_ids"], "story selection") != story_ids
    ):
        raise StoryIndexError("Selected story snapshot scope changed")
    if source is not None and (origin["story_index"], origin["story_index_sha256"]) != (
        str(source),
        checksum,
    ):
        raise StoryIndexError("Selected story cached snapshot scope changed")


def _selected_source_view(
    source: Path,
    receipt: _SourceReceipt,
    records: list[dict[str, object]],
    payload: bytes,
    rows: tuple[tuple[str, int, int], ...],
) -> StoryIndexDocument:
    from vntts.story_index_snapshot import load_story_index_snapshot

    # Full admission covers unselected annotations; this view only projects evidence.
    metadata = json.loads(receipt.metadata)
    metadata["line_count"] = len(records)
    if "source_audio_semantics" in metadata:
        count = sum(
            record.get("source_audio_semantic_evidence_entry_id") is not None
            for record in records
        )
        if count:
            metadata["source_audio_semantics"]["applied_count"] = count
        else:
            metadata.pop("source_audio_semantics")
    selected_payload = (
        json.dumps(metadata, ensure_ascii=False) + "\n"
    ).encode() + b"".join(
        payload[offset : offset + length] for _, offset, length in rows
    )
    return load_story_index_snapshot(source, selected_payload)


def _publish_snapshot(
    source: Path,
    expected_sha256: str,
    evidence_sha256: str | None,
    line_ids: tuple[str, ...],
    story_ids: tuple[str, ...],
    rows: tuple[tuple[str, int, int], ...],
    document: StoryIndexDocument,
    raw_records: list[dict[str, object]],
    directory: Path,
) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(directory.parent, prefix=".selected-story-") as staging:
        try:
            metadata, records, _ = project_source_audio_semantics(
                document, raw_records, staging
            )
        except SourceAudioSemanticEvidenceError as error:
            raise StoryIndexError(
                f"Selected story evidence is invalid: {error}"
            ) from error
        metadata = deepcopy(metadata)
        metadata["selection_origin"] = {
            "schema": _ORIGIN_SCHEMA,
            "schema_version": 1,
            "story_index": str(source),
            "story_index_sha256": expected_sha256,
            "selected_line_ids": list(line_ids),
            "selected_story_ids": list(story_ids),
            "records": [
                {"line_id": line_id, "offset": offset, "length": length}
                for line_id, offset, length in rows
            ],
            "source_audio_semantic_evidence_sha256": evidence_sha256,
        }
        result = write_story_index_document(
            staging / "story-index.jsonl", metadata, records
        )
        validate_selected_story_origin(result)
        _assert_current(source, expected_sha256, evidence_sha256)
        rename_directory_no_replace(staging, directory)
