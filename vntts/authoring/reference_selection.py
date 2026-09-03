"""Immutable objective comparison and explicit voice-reference selection."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    normalize_character_name,
    validate_voice_manifest,
)

from vntts.authoring.workspace_foundation import contained_regular_file
from vntts.reference_quality import analyze_reference_bytes

REFERENCE_SELECTION_SCHEMA_VERSION = 1
REFERENCE_SELECTION_EXTENSION = "vntts.authoring.reference_selection"


class ReferenceSelectionError(ValueError):
    """A voice-reference comparison or selection is unsafe."""


@dataclass(frozen=True)
class ReferenceSelectionResult:
    output: Path
    character: str
    selected_reference_number: int
    selected_reference_sha256: str
    source_manifest_sha256: str

    def to_dict(self):
        return {
            "output": str(self.output),
            "character": self.character,
            "selected_reference_number": self.selected_reference_number,
            "selected_reference_sha256": self.selected_reference_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
        }


def inspect_voice_reference_candidates(manifest_path, character):
    """Return objective metrics over one read-once manifest/reference snapshot."""
    snapshot = _capture_manifest(manifest_path, character)
    return copy.deepcopy(snapshot["report"])


def select_voice_reference(
    manifest_path,
    character,
    reference_number,
    output_path,
):
    """Publish a new no-overwrite manifest with one explicit first reference."""
    if (
        not isinstance(reference_number, int)
        or isinstance(reference_number, bool)
        or reference_number < 1
    ):
        raise ReferenceSelectionError("Reference number must be a positive integer")
    snapshot = _capture_manifest(manifest_path, character)
    candidates = snapshot["candidates"]
    if reference_number > len(candidates):
        raise ReferenceSelectionError(
            f"Reference number {reference_number} exceeds {len(candidates)} candidates"
        )
    source_path = snapshot["manifest_path"]
    output = Path(output_path).expanduser().resolve()
    if output == source_path:
        raise ReferenceSelectionError(
            "Reference selection must publish a new manifest path"
        )
    if output.exists() or output.is_symlink():
        raise ReferenceSelectionError(f"Reference-selection output exists: {output}")
    selected = candidates[reference_number - 1]
    ordered = [selected["relative"]] + [
        value["relative"]
        for index, value in enumerate(candidates)
        if index != reference_number - 1
    ]
    document = copy.deepcopy(snapshot["document"])
    if REFERENCE_SELECTION_EXTENSION in document:
        raise ReferenceSelectionError(
            f"Source manifest already defines {REFERENCE_SELECTION_EXTENSION!r}"
        )
    target = document["voices"][snapshot["entry_index"]]
    target.pop("reference", None)
    target["references"] = ordered
    document["version"] = 2
    document[REFERENCE_SELECTION_EXTENSION] = {
        "schema_version": REFERENCE_SELECTION_SCHEMA_VERSION,
        "source_manifest_sha256": snapshot["manifest_sha256"],
        "character": snapshot["character"],
        "selected_reference": selected["relative"],
        "selected_reference_sha256": selected["sha256"],
        "candidate_references": [
            {"path": value["relative"], "sha256": value["sha256"]}
            for value in candidates
        ],
        "manual_review_required": True,
    }
    try:
        validate_voice_manifest(document, allow_legacy=False)
    except VoiceManifestError as error:
        raise ReferenceSelectionError(str(error)) from error
    _assert_snapshot_unchanged(snapshot)
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_bytes_no_replace(output, payload)
    return ReferenceSelectionResult(
        output,
        snapshot["character"],
        reference_number,
        selected["sha256"],
        snapshot["manifest_sha256"],
    )


def validate_reference_selection_provenance(manifest_path, document):
    """Validate an optional selection extension against current reference bytes."""
    provenance = document.get(REFERENCE_SELECTION_EXTENSION)
    if provenance is None:
        return
    expected_fields = {
        "schema_version",
        "source_manifest_sha256",
        "character",
        "selected_reference",
        "selected_reference_sha256",
        "candidate_references",
        "manual_review_required",
    }
    if (
        not isinstance(provenance, dict)
        or set(provenance) != expected_fields
        or provenance.get("schema_version") != REFERENCE_SELECTION_SCHEMA_VERSION
        or provenance.get("manual_review_required") is not True
    ):
        raise ReferenceSelectionError(
            "Voice reference-selection provenance is malformed"
        )
    for field in ("source_manifest_sha256", "selected_reference_sha256"):
        value = provenance.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ReferenceSelectionError(
                f"Voice reference-selection {field} is malformed"
            )
    try:
        entries = validate_voice_manifest(document, allow_legacy=False)
    except VoiceManifestError as error:
        raise ReferenceSelectionError(str(error)) from error
    character = provenance.get("character")
    matching = [
        entry
        for entry in entries
        if normalize_character_name(entry.character)
        == normalize_character_name(character)
    ]
    if len(matching) != 1:
        raise ReferenceSelectionError(
            "Voice reference-selection character is absent or ambiguous"
        )
    entry = matching[0]
    selected = provenance.get("selected_reference")
    if not entry.references or entry.references[0] != selected:
        raise ReferenceSelectionError(
            "Selected voice reference is not first in the manifest"
        )
    candidates = provenance.get("candidate_references")
    if not isinstance(candidates, list) or not candidates:
        raise ReferenceSelectionError(
            "Voice reference-selection candidate inventory is malformed"
        )
    candidate_paths = []
    hashes = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"path", "sha256"}:
            raise ReferenceSelectionError(
                "Voice reference-selection candidate is malformed"
            )
        relative = candidate.get("path")
        path = _contained_reference(Path(manifest_path).resolve().parent, relative)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != candidate.get("sha256"):
            raise ReferenceSelectionError(
                f"Voice reference-selection candidate changed: {relative}"
            )
        candidate_paths.append(relative)
        hashes[relative] = digest
    if len(candidate_paths) != len(set(candidate_paths)) or set(candidate_paths) != set(
        entry.references
    ):
        raise ReferenceSelectionError(
            "Voice reference-selection candidate inventory conflicts with the manifest"
        )
    if hashes.get(selected) != provenance.get("selected_reference_sha256"):
        raise ReferenceSelectionError(
            "Selected voice reference checksum conflicts with its candidate"
        )


def _capture_manifest(manifest_path, character):
    manifest_path = Path(manifest_path).expanduser().resolve()
    try:
        payload = manifest_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
        entries = validate_voice_manifest(document)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
    ) as error:
        raise ReferenceSelectionError(
            f"Unable to read voice manifest {manifest_path}: {error}"
        ) from error
    target_name = normalize_character_name(character)
    matches = [
        index
        for index, entry in enumerate(entries)
        if target_name
        in {
            normalize_character_name(entry.character),
            *(normalize_character_name(value) for value in entry.aliases),
        }
    ]
    if len(matches) != 1:
        raise ReferenceSelectionError(
            f"Voice manifest must resolve exactly one character for {character!r}"
        )
    index = matches[0]
    entry = entries[index]
    if not entry.references:
        raise ReferenceSelectionError(
            f"Voice character {entry.character!r} has no reference candidates"
        )
    candidates = []
    seen = set()
    for relative in entry.references:
        if relative in seen:
            raise ReferenceSelectionError(
                f"Voice character {entry.character!r} repeats reference {relative!r}"
            )
        seen.add(relative)
        path = _contained_reference(manifest_path.parent, relative)
        try:
            reference_payload = path.read_bytes()
            analysis = analyze_reference_bytes(reference_payload, path=path)
        except (OSError, ValueError) as error:
            raise ReferenceSelectionError(str(error)) from error
        candidates.append(
            {
                "relative": relative,
                "path": path,
                "sha256": hashlib.sha256(reference_payload).hexdigest(),
                "analysis": analysis,
            }
        )
    ranking = sorted(
        range(len(candidates)),
        key=lambda candidate_index: (
            candidates[candidate_index]["analysis"]["objective_preflight"] != "pass",
            candidates[candidate_index]["analysis"]["clipping_fraction"],
            candidates[candidate_index]["analysis"]["leading_silence_seconds"]
            + candidates[candidate_index]["analysis"]["trailing_silence_seconds"],
            candidates[candidate_index]["analysis"]["inactive_window_fraction"],
            candidate_index,
        ),
    )
    report = {
        "schema": "vntts.authoring-reference-candidates",
        "schema_version": 1,
        "manifest": str(manifest_path),
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        "character": entry.character,
        "references": [
            {
                "reference_number": candidate_index + 1,
                "path": value["relative"],
                **{
                    key: item
                    for key, item in value["analysis"].items()
                    if key != "path"
                },
            }
            for candidate_index, value in enumerate(candidates)
        ],
        "objective_ranking": [value + 1 for value in ranking],
        "manual_review_required": [
            "speaker similarity",
            "music or background contamination",
            "spoken content and pronunciation",
        ],
    }
    return {
        "manifest_path": manifest_path,
        "manifest_payload": payload,
        "manifest_sha256": report["manifest_sha256"],
        "document": document,
        "entry_index": index,
        "character": entry.character,
        "candidates": candidates,
        "report": report,
    }


def _contained_reference(root, relative):
    return contained_regular_file(
        root, relative, "voice reference", error_type=ReferenceSelectionError
    )


def _assert_snapshot_unchanged(snapshot):
    path = snapshot["manifest_path"]
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot["manifest_sha256"]:
            raise ReferenceSelectionError("Voice manifest changed during selection")
        for candidate in snapshot["candidates"]:
            current = _contained_reference(path.parent, candidate["relative"])
            if (
                current != candidate["path"]
                or hashlib.sha256(current.read_bytes()).hexdigest()
                != candidate["sha256"]
            ):
                raise ReferenceSelectionError(
                    f"Voice reference changed during selection: {candidate['relative']}"
                )
    except OSError as error:
        raise ReferenceSelectionError(
            f"Voice selection input changed during selection: {error}"
        ) from error


def _write_bytes_no_replace(path, payload):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReferenceSelectionError(
            f"Unable to create reference-selection output directory {path.parent}: {error}"
        ) from error
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as destination:
            temporary = Path(destination.name)
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise ReferenceSelectionError(
            f"Reference-selection output exists: {path}"
        ) from error
    except OSError as error:
        raise ReferenceSelectionError(
            f"Unable to publish reference-selection manifest {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
