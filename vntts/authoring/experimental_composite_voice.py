"""Publish an exact-bank composite as a comparison-only manifest voice."""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.authority import canonical_document_sha256
from vntts.authoring.publication import rename_directory_no_replace
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


@dataclass(frozen=True)
class ExperimentalCompositeVoiceResult:
    directory: Path
    created: bool
    bundle_id: str
    voice_character: str
    reference_sha256: str

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "created": self.created,
            "bundle_id": self.bundle_id,
            "voice_character": self.voice_character,
            "reference_sha256": self.reference_sha256,
        }


def publish_experimental_composite_voice_input(
    source_manifest,
    composite_directory,
    quality_review,
    voice_character,
    output_directory,
):
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

    authority = {
        "schema": EXPERIMENTAL_COMPOSITE_VOICE_SCHEMA,
        "schema_version": EXPERIMENTAL_COMPOSITE_VOICE_VERSION,
        "source_voice_manifest_sha256": source_manifest_sha256,
        "source_queue_voice_overrides_sha256": source_override_sha256,
        "voices": [
            {
                "voice_character": voice_character,
                "speaker": speaker,
                "reference": (
                    f"experimental-composites/{composite['reference_sha256']}"
                    "/reference.wav"
                ),
                **composite,
                "quality_decision": "needs_sample",
                "authority": (
                    "Comparison-only candidate. The needs_sample card is not "
                    "production authority and this record adds no queue route."
                ),
            }
        ],
        "authority": "experimental_only_no_queue_override_or_production_binding",
    }
    expected = {
        "source_manifest": source_manifest,
        "source_document": source_document,
        "source_voices": source_voices,
        "source_overrides": source_overrides,
        "authority": authority,
        "composite_directory": composite_directory,
        "quality_review": quality_review,
    }
    if output.exists() or output.is_symlink():
        bundle = _validate_experimental_composite_voice_input(output, expected)
        return _result(output, bundle, authority, created=False)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".experimental-composite-", dir=output.parent)
    ).resolve()
    try:
        inventory = []
        _copy_manifest_references(
            source_manifest.parent, source_voices, staging, inventory
        )
        reference_relative = Path(authority["voices"][0]["reference"])
        _copy_file(
            composite_directory / composite["composite_path"],
            staging / reference_relative,
            composite["reference_sha256"],
            inventory,
            staging,
        )
        _copy_file(
            composite_directory / "composite.json",
            staging / "authority/composite.json",
            composite["composite_ledger_sha256"],
            inventory,
            staging,
        )
        _copy_file(
            composite_directory / "evaluation.json",
            staging / "authority/evaluation.json",
            composite["composite_evaluation_sha256"],
            inventory,
            staging,
        )
        _copy_tree(
            quality_review.parent,
            staging / "authority/quality-review",
            inventory,
            staging,
        )

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
        staging = None
    except Exception:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        raise
    return _result(output, bundle, authority, created=True)


def _load_composite_authority(composite_directory, quality_review):
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
    clips = ledger.get("clips")
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
    variant_id = f"exact-bank-composite:{reference_sha256}"
    cards = [
        card for card in review["variants"] if card.get("variant_id") == variant_id
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


def _validate_experimental_composite_voice_input(directory, expected):
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
    inventory = bundle.get("inventory")
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
    if manifest.get(EXPERIMENTAL_COMPOSITE_VOICE_FIELD) != expected["authority"]:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite manifest authority changed"
        )
    if (
        queue_voice_overrides_sha256(overrides)
        != expected["authority"]["source_queue_voice_overrides_sha256"]
    ):
        raise ExperimentalCompositeVoiceError(
            "Experimental composite input changed queue routing"
        )
    experimental = [
        voice
        for voice in voices
        if voice.character == expected["authority"]["voices"][0]["voice_character"]
    ]
    if len(experimental) != 1:
        raise ExperimentalCompositeVoiceError(
            "Experimental composite voice is absent or ambiguous"
        )
    control = expected["authority"]["voices"][0]
    if (
        experimental[0].speaker != control["speaker"]
        or list(experimental[0].references) != [control["reference"]]
        or sha256_file(directory / control["reference"]) != control["reference_sha256"]
    ):
        raise ExperimentalCompositeVoiceError(
            "Experimental composite voice reference changed"
        )
    return bundle


def _copy_manifest_references(source_root, voices, destination, inventory):
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


def _copy_tree(source, destination, inventory, inventory_root):
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


def _copy_file(source, destination, expected_sha256, inventory, inventory_root):
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


def _result(directory, bundle, authority, *, created):
    voice = authority["voices"][0]
    return ExperimentalCompositeVoiceResult(
        Path(directory).resolve(),
        created,
        bundle["bundle_id"],
        voice["voice_character"],
        voice["reference_sha256"],
    )


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ExperimentalCompositeVoiceError(f"{label} must be non-empty text")
    return value.strip()


def _sha256(value, label):
    if not is_lowercase_sha256(value):
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
