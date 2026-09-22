"""Publish an exact-bank composite as a comparison-only manifest voice."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestEntry,
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.reference_composite import (
    COMPOSITE_EVALUATION_SCHEMA,
    COMPOSITE_EVALUATION_VERSION,
    COMPOSITE_SCHEMA,
    COMPOSITE_VERSION,
)
from vntts.authoring.source_reference_bindings import (
    SourceReferenceBindingError,
    queue_voice_overrides_from_manifest,
    queue_voice_overrides_sha256,
)
from vntts.authoring.source_reference_quality_records import (
    SourceReferenceQualityError,
    load_source_reference_quality_review,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    safe_workspace_relative_path,
)
from vntts.document_identity import is_lowercase_sha256

EXPERIMENTAL_COMPOSITE_VOICE_FIELD = "vntts.authoring.experimental_composite_voices"
EXPERIMENTAL_COMPOSITE_VOICE_SCHEMA = "vntts.authoring-experimental-composite-voices"
EXPERIMENTAL_COMPOSITE_VOICE_VERSION = 1
EXPERIMENTAL_COMPOSITE_INPUT_SCHEMA = (
    "vntts.authoring-experimental-composite-voice-input"
)
EXPERIMENTAL_COMPOSITE_INPUT_VERSION = 1


class ExperimentalCompositeVoiceError(RuntimeError):
    """An experimental composite cannot be published without widening authority."""


class _CompositeAuthority(TypedDict):
    character: str
    portrait: str
    source_bank: str
    reference_sha256: str
    composite_path: str
    composite_ledger_sha256: str
    composite_evaluation_sha256: str
    quality_review_sha256: str


class _ExperimentalCompositeVoice(TypedDict):
    voice_character: str
    speaker: str
    reference: str
    character: str
    portrait: str
    source_bank: str
    reference_sha256: str
    composite_path: str
    composite_ledger_sha256: str
    composite_evaluation_sha256: str
    quality_review_sha256: str
    quality_decision: str
    authority: str


class _ExperimentalCompositeAuthority(TypedDict):
    schema: str
    schema_version: int
    source_voice_manifest_sha256: str
    source_queue_voice_overrides_sha256: str
    voices: list[_ExperimentalCompositeVoice]
    authority: str


class _ExperimentalCompositeVoiceInput(TypedDict):
    source_manifest: Path
    source_voices: Iterable[VoiceManifestEntry]
    authority: _ExperimentalCompositeAuthority
    composite_directory: Path
    quality_review: Path


@dataclass(frozen=True)
class ExperimentalCompositeVoiceResult:
    directory: Path
    created: bool
    bundle_id: str
    voice_character: str
    reference_sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "directory": str(self.directory),
            "created": self.created,
            "bundle_id": self.bundle_id,
            "voice_character": self.voice_character,
            "reference_sha256": self.reference_sha256,
        }


def publish_experimental_composite_voice_input(
    source_manifest: str | Path,
    composite_directory: str | Path,
    quality_review: str | Path,
    voice_character: object,
    output_directory: str | Path,
) -> ExperimentalCompositeVoiceResult:
    """Add one provenance-bound comparison voice without adding any route."""
    source_manifest = Path(source_manifest).expanduser().resolve()
    composite_directory = Path(composite_directory).expanduser().resolve()
    quality_review = Path(quality_review).expanduser().resolve()
    output = Path(output_directory).expanduser().resolve()
    voice_character = _text(voice_character, "Experimental voice character")

    try:
        source_payload = source_manifest.read_bytes()
        source_document = json.loads(source_payload.decode("utf-8"))
        _metadata, source_voices = load_voice_manifest(
            source_manifest, allow_legacy=False
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
    ) as error:
        raise ExperimentalCompositeVoiceError(str(error)) from error
    if EXPERIMENTAL_COMPOSITE_VOICE_FIELD in source_document:
        raise ExperimentalCompositeVoiceError(
            "Source manifest already contains experimental composite authority"
        )
    normalized = normalize_character_name(voice_character)
    if normalized in {
        normalize_character_name(voice.character) for voice in source_voices
    }:
        raise ExperimentalCompositeVoiceError(
            f"Experimental voice character already exists: {voice_character!r}"
        )

    composite = _load_composite_authority(composite_directory, quality_review)
    speaker = f"experimental-composite:{composite['reference_sha256']}"
    if speaker in {voice.speaker for voice in source_voices}:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite speaker identity already exists"
        )
    source_manifest_sha256 = sha256_file(source_manifest)
    try:
        source_overrides = queue_voice_overrides_from_manifest(
            source_document, voices=source_voices
        )
    except SourceReferenceBindingError as error:
        raise ExperimentalCompositeVoiceError(str(error)) from error
    source_override_sha256 = queue_voice_overrides_sha256(source_overrides)

    voice: _ExperimentalCompositeVoice = {
        "voice_character": voice_character,
        "speaker": speaker,
        "reference": (
            f"experimental-composites/{composite['reference_sha256']}/reference.wav"
        ),
        **composite,
        "quality_decision": "needs_sample",
        "authority": (
            "Comparison-only candidate. The needs_sample card is not "
            "production authority and this record adds no queue route."
        ),
    }
    authority: _ExperimentalCompositeAuthority = {
        "schema": EXPERIMENTAL_COMPOSITE_VOICE_SCHEMA,
        "schema_version": EXPERIMENTAL_COMPOSITE_VOICE_VERSION,
        "source_voice_manifest_sha256": source_manifest_sha256,
        "source_queue_voice_overrides_sha256": source_override_sha256,
        "voices": [voice],
        "authority": "experimental_only_no_queue_override_or_production_binding",
    }
    expected: _ExperimentalCompositeVoiceInput = {
        "source_manifest": source_manifest,
        "source_voices": source_voices,
        "authority": authority,
        "composite_directory": composite_directory,
        "quality_review": quality_review,
    }
    if output.exists() or output.is_symlink():
        bundle = _validate_experimental_composite_voice_input(output, expected)
        return _result(output, bundle, authority, created=False)

    output.parent.mkdir(parents=True, exist_ok=True)
    with staged_directory(output.parent, prefix=".experimental-composite-") as staging:
        inventory, reference_relative = _copy_experimental_artifacts(staging, expected)

        successor = copy.deepcopy(source_document)
        successor["voices"] = [
            *copy.deepcopy(source_document["voices"]),
            {
                "character": voice_character,
                "speaker": speaker,
                "references": [reference_relative.as_posix()],
            },
        ]
        successor[EXPERIMENTAL_COMPOSITE_VOICE_FIELD] = authority
        manifest_path = staging / "manifest.json"
        write_voice_manifest(manifest_path, successor)
        inventory.append(
            {"path": "manifest.json", "sha256": sha256_file(manifest_path)}
        )
        body = {
            "schema": EXPERIMENTAL_COMPOSITE_INPUT_SCHEMA,
            "schema_version": EXPERIMENTAL_COMPOSITE_INPUT_VERSION,
            "source_voice_manifest_sha256": source_manifest_sha256,
            "experimental_voice_character": voice_character,
            "experimental_reference_sha256": composite["reference_sha256"],
            "inventory": sorted(inventory, key=lambda value: value["path"]),
        }
        bundle = {**body, "bundle_id": canonical_document_sha256(body)}
        atomic_write_json(staging / "bundle.json", bundle, sort_keys=True)
        _validate_experimental_composite_voice_input(staging, expected)
        rename_directory_no_replace(staging, output)
    return _result(output, bundle, authority, created=True)


def _copy_experimental_artifacts(
    staging: Path, expected: _ExperimentalCompositeVoiceInput
) -> tuple[list[dict[str, str]], Path]:
    inventory: list[dict[str, str]] = []
    _copy_manifest_references(
        expected["source_manifest"].parent,
        expected["source_voices"],
        staging,
        inventory,
    )
    voice = expected["authority"]["voices"][0]
    reference_relative = Path(voice["reference"])
    composite_directory = expected["composite_directory"]
    for source, destination, digest in (
        (
            composite_directory / voice["composite_path"],
            staging / reference_relative,
            voice["reference_sha256"],
        ),
        (
            composite_directory / "composite.json",
            staging / "authority/composite.json",
            voice["composite_ledger_sha256"],
        ),
        (
            composite_directory / "evaluation.json",
            staging / "authority/evaluation.json",
            voice["composite_evaluation_sha256"],
        ),
    ):
        _copy_file(source, destination, digest, inventory, staging)
    _copy_tree(
        expected["quality_review"].parent,
        staging / "authority/quality-review",
        inventory,
        staging,
    )
    return inventory, reference_relative


def _load_composite_authority(
    composite_directory: Path, quality_review: Path
) -> _CompositeAuthority:
    ledger_path = composite_directory / "composite.json"
    evaluation_path = composite_directory / "evaluation.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
        review = load_source_reference_quality_review(quality_review)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        SourceReferenceQualityError,
    ) as error:
        raise ExperimentalCompositeVoiceError(str(error)) from error
    ledger_sha256 = sha256_file(ledger_path)
    evaluation_sha256 = sha256_file(evaluation_path)
    if (
        ledger.get("schema") != COMPOSITE_SCHEMA
        or ledger.get("schema_version") != COMPOSITE_VERSION
        or evaluation.get("schema") != COMPOSITE_EVALUATION_SCHEMA
        or evaluation.get("schema_version") != COMPOSITE_EVALUATION_VERSION
        or evaluation.get("source_composite_sha256") != ledger_sha256
        or review.get("source_reference_plan_sha256") != ledger_sha256
        or review.get("source_reference_evaluation_sha256") != evaluation_sha256
    ):
        raise ExperimentalCompositeVoiceError(
            "Composite ledger, evaluation and quality review identities differ"
        )
    record = ledger.get("composite")
    if not isinstance(record, dict):
        raise ExperimentalCompositeVoiceError("Composite WAV record is malformed")
    try:
        relative = safe_workspace_relative_path(record.get("path"), "Composite WAV")
        reference = contained_workspace_path(
            composite_directory, relative, "Composite WAV"
        )
    except AuthoringWorkbenchError as error:
        raise ExperimentalCompositeVoiceError(str(error)) from error
    reference_sha256 = _sha256(record.get("sha256"), "Composite WAV SHA-256")
    if (
        reference.is_symlink()
        or not reference.is_file()
        or sha256_file(reference) != reference_sha256
    ):
        raise ExperimentalCompositeVoiceError("Composite WAV changed")
    _validate_composite_clips(composite_directory, ledger.get("clips"))
    _exact_quality_card(review, ledger, reference_sha256)
    return {
        "character": ledger["character"],
        "portrait": ledger["portrait"],
        "source_bank": ledger["source_bank"],
        "reference_sha256": reference_sha256,
        "composite_path": relative.as_posix(),
        "composite_ledger_sha256": ledger_sha256,
        "composite_evaluation_sha256": evaluation_sha256,
        "quality_review_sha256": sha256_file(quality_review),
    }


def _validate_composite_clips(composite_directory: Path, clips: object) -> None:
    if not isinstance(clips, list) or len(clips) < 2:
        raise ExperimentalCompositeVoiceError("Composite clip inventory is invalid")
    for clip in clips:
        if not isinstance(clip, dict):
            raise ExperimentalCompositeVoiceError("Composite clip record is malformed")
        try:
            clip_relative = safe_workspace_relative_path(
                clip.get("reference"), "Composite clip"
            )
            clip_path = contained_workspace_path(
                composite_directory, clip_relative, "Composite clip"
            )
        except AuthoringWorkbenchError as error:
            raise ExperimentalCompositeVoiceError(str(error)) from error
        digest = _sha256(clip.get("reference_sha256"), "Composite clip SHA-256")
        if (
            clip_path.is_symlink()
            or not clip_path.is_file()
            or sha256_file(clip_path) != digest
        ):
            raise ExperimentalCompositeVoiceError("Composite clip changed")


def _exact_quality_card(
    review: Mapping[str, object], ledger: Mapping[str, object], reference_sha256: str
) -> None:
    variant_id = f"exact-bank-composite:{reference_sha256}"
    variants = review.get("variants")
    if not isinstance(variants, list):
        raise ExperimentalCompositeVoiceError("Quality review variants are malformed")
    cards = [
        card
        for card in variants
        if isinstance(card, dict) and card.get("variant_id") == variant_id
    ]
    if len(cards) != 1:
        raise ExperimentalCompositeVoiceError(
            "Quality review does not contain the exact composite"
        )
    card = cards[0]
    decision = card.get("decision")
    if not isinstance(decision, dict) or decision.get("decision") != "needs_sample":
        raise ExperimentalCompositeVoiceError(
            "Experimental composite requires an exact needs_sample quality decision"
        )
    if (
        card.get("reference_kind") != "exact_bank_composite"
        or card.get("character") != ledger.get("character")
        or card.get("portrait") != ledger.get("portrait")
        or card.get("source_bank") != ledger.get("source_bank")
        or card.get("reference", {}).get("audio_sha256") != reference_sha256
    ):
        raise ExperimentalCompositeVoiceError(
            "Quality review card differs from the composite ledger"
        )


def _validate_experimental_composite_voice_input(
    directory: str | Path, expected: _ExperimentalCompositeVoiceInput
) -> dict[str, object]:
    directory = Path(directory).resolve()
    try:
        bundle = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExperimentalCompositeVoiceError(str(error)) from error
    if (
        bundle.get("schema") != EXPERIMENTAL_COMPOSITE_INPUT_SCHEMA
        or bundle.get("schema_version") != EXPERIMENTAL_COMPOSITE_INPUT_VERSION
        or bundle.get("source_voice_manifest_sha256")
        != sha256_file(expected["source_manifest"])
        or bundle.get("experimental_voice_character")
        != expected["authority"]["voices"][0]["voice_character"]
        or bundle.get("experimental_reference_sha256")
        != expected["authority"]["voices"][0]["reference_sha256"]
        or bundle.get("bundle_id")
        != canonical_document_sha256(
            {key: value for key, value in bundle.items() if key != "bundle_id"}
        )
    ):
        raise ExperimentalCompositeVoiceError(
            "Experimental composite input identity is invalid"
        )
    _validate_bundle_inventory(directory, bundle.get("inventory"))
    _validate_bundle_manifest(directory, expected["authority"])
    return bundle


def _validate_bundle_inventory(directory: Path, inventory: object) -> None:
    if not isinstance(inventory, list) or not inventory:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite inventory is empty"
        )
    declared = set()
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ExperimentalCompositeVoiceError(
                "Experimental composite inventory is malformed"
            )
        try:
            relative = safe_workspace_relative_path(
                item["path"], "Experimental artifact"
            )
            path = contained_workspace_path(
                directory, relative, "Experimental artifact"
            )
        except AuthoringWorkbenchError as error:
            raise ExperimentalCompositeVoiceError(str(error)) from error
        if (
            path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != item["sha256"]
        ):
            raise ExperimentalCompositeVoiceError(
                "Experimental composite artifact changed"
            )
        if relative.as_posix() in declared:
            raise ExperimentalCompositeVoiceError(
                "Experimental composite inventory contains duplicate paths"
            )
        declared.add(relative.as_posix())
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "bundle.json"
    }
    if declared != actual:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite inventory is incomplete"
        )


def _validate_bundle_manifest(
    directory: Path, authority: _ExperimentalCompositeAuthority
) -> None:
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _metadata, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        overrides = queue_voice_overrides_from_manifest(manifest, voices=voices)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        VoiceManifestError,
        SourceReferenceBindingError,
    ) as error:
        raise ExperimentalCompositeVoiceError(str(error)) from error
    if manifest.get(EXPERIMENTAL_COMPOSITE_VOICE_FIELD) != authority:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite manifest authority changed"
        )
    if (
        queue_voice_overrides_sha256(overrides)
        != authority["source_queue_voice_overrides_sha256"]
    ):
        raise ExperimentalCompositeVoiceError(
            "Experimental composite input changed queue routing"
        )
    experimental = [
        voice
        for voice in voices
        if voice.character == authority["voices"][0]["voice_character"]
    ]
    if len(experimental) != 1:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite voice is absent or ambiguous"
        )
    control = authority["voices"][0]
    if (
        experimental[0].speaker != control["speaker"]
        or list(experimental[0].references) != [control["reference"]]
        or sha256_file(directory / control["reference"]) != control["reference_sha256"]
    ):
        raise ExperimentalCompositeVoiceError(
            "Experimental composite voice reference changed"
        )


def _copy_manifest_references(
    source_root: Path,
    voices: Iterable[VoiceManifestEntry],
    destination: Path,
    inventory: list[dict[str, str]],
) -> None:
    seen = set()
    for voice in voices:
        for value in voice.references:
            try:
                relative = safe_workspace_relative_path(value, "Source voice reference")
                source = contained_workspace_path(
                    source_root, relative, "Source voice reference"
                )
            except AuthoringWorkbenchError as error:
                raise ExperimentalCompositeVoiceError(str(error)) from error
            if source.is_symlink() or not source.is_file():
                raise ExperimentalCompositeVoiceError(
                    f"Source voice reference is unsafe: {value!r}"
                )
            if relative.as_posix() in seen:
                continue
            seen.add(relative.as_posix())
            _copy_file(
                source,
                destination / relative,
                sha256_file(source),
                inventory,
                destination,
            )


def _copy_tree(
    source: Path,
    destination: Path,
    inventory: list[dict[str, str]],
    inventory_root: Path,
) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ExperimentalCompositeVoiceError("Quality review root is unsafe")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ExperimentalCompositeVoiceError(
                "Quality review contains a symbolic link"
            )
        if path.is_file():
            _copy_file(
                path,
                destination / path.relative_to(source),
                sha256_file(path),
                inventory,
                inventory_root,
            )


def _copy_file(
    source: Path,
    destination: Path,
    expected_sha256: str,
    inventory: list[dict[str, str]],
    inventory_root: Path,
) -> None:
    if (
        source.is_symlink()
        or not source.is_file()
        or sha256_file(source) != expected_sha256
    ):
        raise ExperimentalCompositeVoiceError(f"Source artifact changed: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ExperimentalCompositeVoiceError(
            f"Experimental artifact path collides: {destination}"
        )
    shutil.copyfile(source, destination)
    if sha256_file(destination) != expected_sha256:
        raise ExperimentalCompositeVoiceError(
            f"Experimental artifact changed while copied: {source}"
        )
    inventory.append(
        {
            "path": destination.relative_to(inventory_root).as_posix(),
            "sha256": expected_sha256,
        }
    )


def _result(
    directory: str | Path,
    bundle: Mapping[str, object],
    authority: _ExperimentalCompositeAuthority,
    *,
    created: bool,
) -> ExperimentalCompositeVoiceResult:
    voice = authority["voices"][0]
    return ExperimentalCompositeVoiceResult(
        Path(directory).resolve(),
        created,
        _text(bundle.get("bundle_id"), "Experimental composite bundle ID"),
        voice["voice_character"],
        voice["reference_sha256"],
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperimentalCompositeVoiceError(f"{label} must be non-empty text")
    return value.strip()


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not is_lowercase_sha256(value):
        raise ExperimentalCompositeVoiceError(f"{label} is invalid")
    return value


__all__ = [
    "EXPERIMENTAL_COMPOSITE_INPUT_SCHEMA",
    "EXPERIMENTAL_COMPOSITE_INPUT_VERSION",
    "EXPERIMENTAL_COMPOSITE_VOICE_FIELD",
    "EXPERIMENTAL_COMPOSITE_VOICE_SCHEMA",
    "EXPERIMENTAL_COMPOSITE_VOICE_VERSION",
    "ExperimentalCompositeVoiceError",
    "ExperimentalCompositeVoiceResult",
    "publish_experimental_composite_voice_input",
]
