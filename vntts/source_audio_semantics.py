"""Validation and projection of checksum-bound source-audio evidence."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Literal, TypeAlias, TypedDict, cast

from durable_file import atomic_write_json
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    load_story_index_document,
    write_story_index_document,
)

from vntts.document_identity import (
    canonical_document_sha256,
    is_lowercase_sha256,
    sha256_file,
)
from vntts.json_types import decode_json, has_schema_version
from vntts.json_types import is_json_object as _is_json_document
from vntts.path_safety import read_regular_bytes

SEMANTIC_EVIDENCE_SCHEMA = "r1999.source-audio-semantic-evidence"
SEMANTIC_EVIDENCE_VERSION = 1
SEMANTIC_EVIDENCE_METHOD = "local-asr-exact-normalized-transcript"
SEMANTIC_EVIDENCE_PREPROCESSING = "mono-pcm16-resample-poly-16000-v1"
WORD_PATTERN = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", flags=re.UNICODE)

JsonDocument: TypeAlias = dict[str, object]


class SourceAudioSemanticEvidenceEntry(TypedDict):
    entry_id: str
    locale: str
    media_sha256: str
    displayed_text_sha256: str
    normalized_displayed_text_sha256: str
    observed_transcript: str
    normalized_observed_text_sha256: str
    verdict: Literal["full", "partial"]
    reason: Literal["exact-normalized-asr-transcript", "asr-transcript-mismatch"]
    method: Literal["local-asr-exact-normalized-transcript"]
    model_sha256: str
    source_line_ids: list[str]


class SourceAudioSemanticEvidence(TypedDict):
    evidence_id: str
    generated_at: str
    model: JsonDocument
    entries: list[SourceAudioSemanticEvidenceEntry]


class SourceAudioSemanticEvidenceError(RuntimeError):
    """Semantic source-audio evidence is missing, stale or malformed."""


def _semantic_document_sha256(document: object) -> str:
    try:
        return canonical_document_sha256(document)
    except (TypeError, ValueError) as error:
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence contains invalid JSON values"
        ) from error


def normalize_semantic_text(text: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(
        token.casefold().replace("’", "'") for token in WORD_PATTERN.findall(normalized)
    )


def semantic_text_sha256(text: object) -> str:
    return hashlib.sha256(normalize_semantic_text(text).encode("utf-8")).hexdigest()


def load_source_audio_semantic_evidence(
    path: str | Path,
    story_index_path: str | Path | StoryIndexDocument | None = None,
    *,
    payload: bytes | None = None,
) -> SourceAudioSemanticEvidence:
    evidence_path = Path(path).expanduser().resolve()
    try:
        if payload is None:
            payload = read_regular_bytes(evidence_path)
        document = decode_json(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SourceAudioSemanticEvidenceError(
            f"Unable to read source-audio semantic evidence {evidence_path}: {error}"
        ) from error
    if story_index_path is None:
        return validate_source_audio_semantic_evidence(document)
    return validate_story_semantic_evidence(
        story_index_path,
        hashlib.sha256(payload).hexdigest(),
        document,
    )


def semantic_preprocessing_is_current(evidence: SourceAudioSemanticEvidence) -> bool:
    return evidence["model"].get("preprocessing") == SEMANTIC_EVIDENCE_PREPROCESSING


def _bound_semantic_evidence(
    document: StoryIndexDocument,
) -> SourceAudioSemanticEvidence | None:
    if "source_audio_semantics" not in document.metadata:
        if any(
            record.document.get("source_audio_semantic_evidence_entry_id") is not None
            or record.document.get("source_audio_semantic_evidence_id") is not None
            for record in document.records
        ):
            raise SourceAudioSemanticEvidenceError(
                "Story semantic annotations have no evidence binding"
            )
        return None
    return load_source_audio_semantic_evidence(
        document.path.parent / "source-audio-semantic-evidence.json", document
    )


def source_audio_semantics_need_migration(
    story: str | Path | StoryIndexDocument,
) -> bool:
    document = (
        story
        if isinstance(story, StoryIndexDocument)
        else load_story_index_document(story)
    )
    evidence = _bound_semantic_evidence(document)
    return evidence is not None and not semantic_preprocessing_is_current(evidence)


def _clear_semantic_verdict(record: JsonDocument) -> None:
    if record.get("source_audio_semantic_evidence_entry_id") is None:
        return
    record["source_audio_completeness"] = "unknown"
    for key in (
        "source_audio_completeness_reason",
        "source_audio_semantic_evidence_id",
        "source_audio_semantic_evidence_entry_id",
    ):
        record.pop(key, None)


def reset_stale_source_audio_semantics(
    document: StoryIndexDocument, destination: str | Path
) -> StoryIndexDocument:
    """Expire validated ASR verdicts in a private successor, retaining native timing."""
    source_sha256 = sha256_file(document.path)
    evidence_path = document.path.parent / "source-audio-semantic-evidence.json"
    evidence_payload = (
        read_regular_bytes(evidence_path)
        if "source_audio_semantics" in document.metadata
        else None
    )
    evidence = (
        load_source_audio_semantic_evidence(
            evidence_path, document, payload=evidence_payload
        )
        if evidence_payload is not None
        else _bound_semantic_evidence(document)
    )

    def assert_unchanged() -> None:
        if sha256_file(document.path) != source_sha256 or (
            evidence_payload is not None
            and read_regular_bytes(evidence_path) != evidence_payload
        ):
            raise SourceAudioSemanticEvidenceError(
                "Semantic migration source or evidence changed while read"
            )

    assert_unchanged()
    if evidence is None or semantic_preprocessing_is_current(evidence):
        return document
    destination = Path(destination).expanduser()
    if destination.is_symlink():
        raise SourceAudioSemanticEvidenceError(
            "Semantic migration destination must not be a symlink"
        )
    destination = destination.resolve()
    origins = [document.path, evidence_path]
    origin = document.metadata.get("selection_origin")
    if isinstance(origin, dict) and isinstance(origin.get("story_index"), str):
        origin_story = Path(origin["story_index"])
        origins.extend(
            [origin_story, origin_story.parent / "source-audio-semantic-evidence.json"]
        )
    if any(
        destination == source.resolve()
        or destination.exists()
        and source.exists()
        and destination.samefile(source)
        for source in origins
    ):
        raise SourceAudioSemanticEvidenceError(
            "Semantic migration must publish a new story-index destination"
        )
    metadata = deepcopy(document.metadata)
    metadata.pop("source_audio_semantics", None)
    records = [record.to_record() for record in document.records]
    for record in records:
        _clear_semantic_verdict(record)

    assert_unchanged()
    destination.parent.mkdir(parents=True, exist_ok=True)
    successor = write_story_index_document(destination, metadata, records)
    try:
        assert_unchanged()
    except OSError, SourceAudioSemanticEvidenceError:
        destination.unlink(missing_ok=True)
        raise
    return successor


def validate_source_audio_semantic_evidence(
    document: object,
) -> SourceAudioSemanticEvidence:
    if (
        not _is_json_document(document)
        or document.get("schema") != SEMANTIC_EVIDENCE_SCHEMA
        or not has_schema_version(document, SEMANTIC_EVIDENCE_VERSION)
    ):
        raise SourceAudioSemanticEvidenceError(
            "Unsupported source-audio semantic evidence schema"
        )
    locale = document.get("locale")
    model = document.get("model")
    entries = document.get("entries")
    if not isinstance(locale, str) or not locale.strip():
        raise SourceAudioSemanticEvidenceError("Semantic evidence locale is invalid")
    if (
        not _is_json_document(model)
        or model.get("kind") != "whisper"
        or model.get("decoding") != "deterministic_greedy_default"
    ):
        raise SourceAudioSemanticEvidenceError("Semantic evidence model is invalid")
    model_sha256 = _require_sha256(model.get("sha256"), "semantic evidence model")
    _require_sha256(
        document.get("source_story_index_sha256"),
        "semantic evidence source story index",
    )
    if not isinstance(entries, list) or not entries:
        raise SourceAudioSemanticEvidenceError("Semantic evidence entries are empty")
    keys = []
    for raw_entry in entries:
        entry = _validate_semantic_evidence_entry(raw_entry, locale, model_sha256)
        keys.append(
            (
                entry["locale"],
                entry["media_sha256"],
                entry["normalized_displayed_text_sha256"],
            )
        )
    if keys != sorted(set(keys)):
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence entries are duplicated or not canonical"
        )
    authority = {
        key: value
        for key, value in document.items()
        if key not in {"evidence_id", "generated_at"}
    }
    if document.get("evidence_id") != _semantic_document_sha256(authority):
        raise SourceAudioSemanticEvidenceError("Semantic evidence ID changed")
    if not isinstance(document.get("generated_at"), str):
        raise SourceAudioSemanticEvidenceError(
            "Semantic evidence generation timestamp is invalid"
        )
    return cast(SourceAudioSemanticEvidence, document)


def _validate_semantic_evidence_entry(
    entry: object, locale: str, model_sha256: str
) -> SourceAudioSemanticEvidenceEntry:
    if not _is_json_document(entry) or entry.get("locale") != locale:
        raise SourceAudioSemanticEvidenceError("Semantic evidence entry locale changed")
    _require_sha256(entry.get("media_sha256"), "semantic media")
    normalized_text_sha256 = _require_sha256(
        entry.get("normalized_displayed_text_sha256"),
        "semantic normalized displayed text",
    )
    _require_sha256(entry.get("displayed_text_sha256"), "semantic displayed text")
    if entry.get("model_sha256") != model_sha256:
        raise SourceAudioSemanticEvidenceError("Semantic entry model binding changed")
    observed = entry.get("observed_transcript")
    if not isinstance(observed, str) or not normalize_semantic_text(observed):
        raise SourceAudioSemanticEvidenceError("Semantic transcript is empty")
    observed_sha256 = semantic_text_sha256(observed)
    if entry.get("normalized_observed_text_sha256") != observed_sha256:
        raise SourceAudioSemanticEvidenceError("Semantic transcript hash changed")
    verdict = entry.get("verdict")
    expected_reason = (
        "exact-normalized-asr-transcript"
        if verdict == "full"
        else "asr-transcript-mismatch"
    )
    if verdict not in ("full", "partial") or entry.get("reason") != expected_reason:
        raise SourceAudioSemanticEvidenceError("Semantic verdict is invalid")
    expected_verdict = (
        "full" if observed_sha256 == normalized_text_sha256 else "partial"
    )
    if verdict != expected_verdict:
        raise SourceAudioSemanticEvidenceError(
            "Semantic verdict contradicts transcript"
        )
    if entry.get("method") != SEMANTIC_EVIDENCE_METHOD:
        raise SourceAudioSemanticEvidenceError("Semantic evidence method changed")
    source_line_ids = entry.get("source_line_ids")
    if (
        not isinstance(source_line_ids, list)
        or any(
            not isinstance(line_id, str) or not line_id for line_id in source_line_ids
        )
        or source_line_ids != sorted(set(source_line_ids))
    ):
        raise SourceAudioSemanticEvidenceError("Semantic source line IDs are invalid")
    expected_entry_id = _semantic_document_sha256(
        {
            key: value
            for key, value in entry.items()
            if key not in {"entry_id", "source_line_ids"}
        }
    )
    if entry.get("entry_id") != expected_entry_id:
        raise SourceAudioSemanticEvidenceError("Semantic evidence entry ID changed")
    return cast(SourceAudioSemanticEvidenceEntry, entry)


def validate_story_semantic_evidence(
    story_index: str | Path | StoryIndexDocument,
    evidence_sha256: str,
    evidence: object,
) -> SourceAudioSemanticEvidence:
    evidence = validate_source_audio_semantic_evidence(evidence)
    if isinstance(story_index, StoryIndexDocument):
        story = story_index
    else:
        try:
            story = load_story_index_document(story_index)
        except StoryIndexError as error:
            raise SourceAudioSemanticEvidenceError(str(error)) from error
    metadata = story.metadata.get("source_audio_semantics")
    if not isinstance(metadata, dict):
        raise SourceAudioSemanticEvidenceError(
            "Story index has no source-audio semantic evidence binding"
        )
    if (
        metadata.get("evidence_id") != evidence["evidence_id"]
        or metadata.get("evidence_sha256") != evidence_sha256
        or metadata.get("method") != SEMANTIC_EVIDENCE_METHOD
    ):
        raise SourceAudioSemanticEvidenceError(
            "Story source-audio semantic evidence binding changed"
        )
    entry_by_id = {entry["entry_id"]: entry for entry in evidence["entries"]}
    matched = 0
    for line in story.records:
        record = line.to_record()
        entry_id = record.get("source_audio_semantic_evidence_entry_id")
        if entry_id is None:
            continue
        entry = entry_by_id.get(entry_id) if isinstance(entry_id, str) else None
        if (
            record.get("source_audio_semantic_evidence_id") != evidence["evidence_id"]
            or entry is None
            or line.line_id not in entry["source_line_ids"]
            or entry["locale"] != story.metadata.get("language")
            or entry["media_sha256"] != record.get("source_audio_duration_media_sha256")
            or entry["displayed_text_sha256"] != record.get("text_sha256")
            or entry["normalized_displayed_text_sha256"]
            != semantic_text_sha256(record.get("text"))
            or entry["verdict"] != record.get("source_audio_completeness")
            or entry["reason"] != record.get("source_audio_completeness_reason")
        ):
            raise SourceAudioSemanticEvidenceError(
                f"Story semantic evidence changed for {record.get('line_id')!r}"
            )
        matched += 1
    applied_count = metadata.get("applied_count")
    if type(applied_count) is not int or matched != applied_count or matched <= 0:
        raise SourceAudioSemanticEvidenceError(
            "Story semantic evidence applied count changed"
        )
    return evidence


def project_source_audio_semantics(
    source_story: StoryIndexDocument,
    records: list[JsonDocument],
    staging: Path,
) -> tuple[dict[str, object], list[JsonDocument], Path | None]:
    metadata = source_story.metadata
    binding = metadata.get("source_audio_semantics")
    if not isinstance(binding, dict):
        return metadata, records, None
    source = source_story.path.parent / "source-audio-semantic-evidence.json"
    try:
        evidence = load_source_audio_semantic_evidence(source, source_story)
    except (OSError, SourceAudioSemanticEvidenceError, ValueError) as error:
        raise SourceAudioSemanticEvidenceError(
            f"Selected dialogue source-audio evidence is invalid: {error}"
        ) from error
    selected_line_ids, selected_entry_ids = _selected_semantic_ids(records)
    if not selected_entry_ids:
        projected_metadata = deepcopy(metadata)
        projected_metadata.pop("source_audio_semantics", None)
        return projected_metadata, records, None
    projected_entries = []
    for entry in evidence["entries"]:
        if entry.get("entry_id") not in selected_entry_ids:
            continue
        projected = deepcopy(entry)
        projected["source_line_ids"] = sorted(
            set(projected["source_line_ids"]) & selected_line_ids
        )
        if not projected["source_line_ids"]:
            raise SourceAudioSemanticEvidenceError(
                "Selected source-audio evidence lost its dialogue binding"
            )
        projected_entries.append(projected)
    if {entry["entry_id"] for entry in projected_entries} != selected_entry_ids:
        raise SourceAudioSemanticEvidenceError(
            "Selected dialogue source-audio evidence is incomplete"
        )
    projected_evidence = {
        key: deepcopy(value)
        for key, value in evidence.items()
        if key not in {"evidence_id", "generated_at", "entries"}
    }
    projected_evidence["entries"] = projected_entries
    projected_evidence["evidence_id"] = _semantic_document_sha256(projected_evidence)
    projected_evidence["generated_at"] = evidence["generated_at"]
    projected_records = deepcopy(records)
    for record in projected_records:
        if record.get("source_audio_semantic_evidence_entry_id") is not None:
            record["source_audio_semantic_evidence_id"] = projected_evidence[
                "evidence_id"
            ]
    destination = staging / "source-audio-semantic-evidence.json"
    atomic_write_json(destination, projected_evidence, sort_keys=True)
    projected_metadata = deepcopy(metadata)
    projected_metadata["source_audio_semantics"] = {
        "evidence_id": projected_evidence["evidence_id"],
        "evidence_sha256": sha256_file(destination),
        "method": binding["method"],
        "selected_chapters": sorted(
            {
                chapter
                for record in projected_records
                if isinstance(chapter := record.get("chapter"), str) and chapter
            }
        ),
        "applied_count": sum(
            record.get("source_audio_semantic_evidence_entry_id") is not None
            for record in projected_records
        ),
    }
    return projected_metadata, projected_records, destination


def _selected_semantic_ids(records: list[JsonDocument]) -> tuple[set[str], set[str]]:
    selected_line_ids: set[str] = set()
    selected_entry_ids: set[str] = set()
    for record in records:
        line_id = record.get("line_id")
        entry_id = record.get("source_audio_semantic_evidence_entry_id")
        if not isinstance(line_id, str) or not line_id:
            raise SourceAudioSemanticEvidenceError(
                "Selected dialogue line ID is invalid"
            )
        selected_line_ids.add(line_id)
        if entry_id is not None:
            if not isinstance(entry_id, str):
                raise SourceAudioSemanticEvidenceError(
                    "Selected dialogue semantic entry ID is invalid"
                )
            selected_entry_ids.add(entry_id)
    return selected_line_ids, selected_entry_ids


def _merge_cumulative_semantic_entry(
    record: JsonDocument,
    entry_id: str,
    owner_entries: dict[str, SourceAudioSemanticEvidenceEntry],
    entries: dict[str, SourceAudioSemanticEvidenceEntry],
) -> None:
    found = owner_entries.get(entry_id)
    if found is None or record["line_id"] not in found["source_line_ids"]:
        raise SourceAudioSemanticEvidenceError(
            "Cumulative dialogue semantic entry changed"
        )
    candidate = deepcopy(found)
    candidate["source_line_ids"] = []
    previous = entries.setdefault(candidate["entry_id"], candidate)
    if {key: value for key, value in previous.items() if key != "source_line_ids"} != {
        key: value for key, value in candidate.items() if key != "source_line_ids"
    }:
        raise SourceAudioSemanticEvidenceError(
            "Cumulative dialogue has conflicting semantic evidence"
        )
    previous["source_line_ids"].append(str(record["line_id"]))


def _collect_cumulative_semantic_entries(
    stories: tuple[StoryIndexDocument, ...], records: list[JsonDocument]
) -> tuple[
    dict[str, SourceAudioSemanticEvidenceEntry], SourceAudioSemanticEvidence | None
]:
    owners = {record.line_id: story for story in stories for record in story.records}
    evidence_by_owner = {}
    entries_by_owner = {}
    for story in stories:
        evidence = _bound_semantic_evidence(story)
        if evidence is not None:
            evidence_by_owner[id(story)] = evidence
            entries_by_owner[id(story)] = {
                entry["entry_id"]: entry
                for entry in evidence_by_owner[id(story)]["entries"]
            }
    entries: dict[str, SourceAudioSemanticEvidenceEntry] = {}
    template: SourceAudioSemanticEvidence | None = None
    for record in records:
        entry_id = record.get("source_audio_semantic_evidence_entry_id")
        if entry_id is None:
            continue
        if not isinstance(entry_id, str):
            raise SourceAudioSemanticEvidenceError(
                "Cumulative dialogue semantic entry ID is invalid"
            )
        owner = owners.get(str(record.get("line_id")))
        evidence = evidence_by_owner.get(id(owner))
        if evidence is None:
            raise SourceAudioSemanticEvidenceError(
                "Cumulative dialogue lost its semantic owner"
            )
        if not semantic_preprocessing_is_current(evidence):
            _clear_semantic_verdict(record)
            continue
        if template is not None and (
            evidence.get("model") != template.get("model")
            or evidence.get("locale") != template.get("locale")
        ):
            raise SourceAudioSemanticEvidenceError(
                "Cumulative source-audio evidence uses incompatible models or languages"
            )
        template = evidence
        _merge_cumulative_semantic_entry(
            record, entry_id, entries_by_owner[id(owner)], entries
        )
    return entries, template


def merge_source_audio_semantics(
    catalog: StoryIndexDocument,
    stories: tuple[StoryIndexDocument, ...],
    records: list[JsonDocument],
    staging: Path,
    *,
    expected_catalog_sha256: str | None = None,
) -> tuple[dict[str, object], list[JsonDocument], Path | None]:
    """Combine validated chapter verdicts, with the latest chapter owner winning."""
    catalog_sha256 = sha256_file(catalog.path)
    if (
        expected_catalog_sha256 is not None
        and catalog_sha256 != expected_catalog_sha256
    ):
        raise SourceAudioSemanticEvidenceError("Cumulative source catalog changed")
    metadata = deepcopy(catalog.metadata)
    records = deepcopy(records)
    metadata.pop("selection_origin", None)
    metadata.pop("source_audio_semantics", None)
    for story in stories:
        for key in ("source_audio_completion", "source_audio_timing"):
            if key in story.metadata:
                metadata[key] = deepcopy(story.metadata[key])
    entries, template = _collect_cumulative_semantic_entries(stories, records)
    if template is None:
        return metadata, records, None
    for entry in entries.values():
        entry["source_line_ids"] = sorted(set(entry["source_line_ids"]))
    merged = {
        key: deepcopy(value)
        for key, value in template.items()
        if key not in {"evidence_id", "generated_at", "entries"}
    }
    merged["entries"] = sorted(
        entries.values(),
        key=lambda entry: (
            entry["locale"],
            entry["media_sha256"],
            entry["normalized_displayed_text_sha256"],
        ),
    )
    merged["source_story_index_sha256"] = catalog_sha256
    merged["evidence_id"] = _semantic_document_sha256(merged)
    merged["generated_at"] = template["generated_at"]
    validate_source_audio_semantic_evidence(merged)
    if sha256_file(catalog.path) != catalog_sha256:
        raise SourceAudioSemanticEvidenceError(
            "Cumulative source catalog changed while read"
        )
    projected = deepcopy(records)
    for record in projected:
        if record.get("source_audio_semantic_evidence_entry_id") is not None:
            record["source_audio_semantic_evidence_id"] = merged["evidence_id"]
    destination = staging / "source-audio-semantic-evidence.json"
    atomic_write_json(destination, merged, sort_keys=True)
    metadata["source_audio_semantics"] = {
        "evidence_id": merged["evidence_id"],
        "evidence_sha256": sha256_file(destination),
        "method": SEMANTIC_EVIDENCE_METHOD,
        "selected_chapters": sorted({str(record["chapter"]) for record in projected}),
        "applied_count": sum(
            record.get("source_audio_semantic_evidence_entry_id") is not None
            for record in projected
        ),
    }
    return metadata, projected, destination


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not is_lowercase_sha256(value):
        raise SourceAudioSemanticEvidenceError(f"{label} SHA-256 is invalid")
    return value
