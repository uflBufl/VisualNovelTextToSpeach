"""Checksum-bound, diagnostic-only speaker-identity evaluation."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path

import numpy as np
import soundfile as sf
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.speaker_identity_model import (
    IMPLEMENTATION_VERSION,
    managed_speaker_identity_status,
)
from vntts.voices import CharacterVoiceRegistry, read_voice_reference_bytes

INVENTORY_SCHEMA = "vntts.speaker-reference-inventory"
LABELS_SCHEMA = "vntts.speaker-identity-labels"
REPORT_SCHEMA = "vntts.speaker-identity-report"
SCHEMA_VERSION = 1
RELATIONSHIPS = frozenset(
    {"same-speaker", "different-speaker", "same-character/different-age"}
)
PARTITIONS = frozenset({"fit", "held-out"})


class SpeakerIdentityError(RuntimeError):
    """Speaker-identity evidence is missing, ambiguous, or inconsistent."""


def build_reference_inventory(manifest_path):
    """Capture every declared voice reference without changing its authority."""
    manifest_path = Path(manifest_path).expanduser().resolve()
    manifest_sha256 = sha256_file(manifest_path)
    try:
        registry = CharacterVoiceRegistry.from_file(manifest_path)
    except Exception as error:
        raise SpeakerIdentityError(f"Unable to load voice manifest: {error}") from error
    references = []
    for voice in sorted(
        registry.unique_voices(), key=lambda value: value.character.casefold()
    ):
        for reference in voice.references:
            try:
                payload = read_voice_reference_bytes(voice, reference)
                info = sf.info(io.BytesIO(payload))
            except Exception as error:
                raise SpeakerIdentityError(
                    f"Unable to inspect reference {reference}: {error}"
                ) from error
            digest = _sha256_bytes(payload)
            relative = reference.relative_to(manifest_path.parent).as_posix()
            identity = {
                "character": voice.character,
                "speaker": voice.speaker,
                "path": relative,
                "sha256": digest,
            }
            references.append(
                {
                    "reference_id": canonical_document_sha256(identity),
                    **identity,
                    "sample_rate_hz": info.samplerate,
                    "channels": info.channels,
                    "frames": info.frames,
                    "duration_seconds": info.duration,
                }
            )
    if not references:
        raise SpeakerIdentityError("Voice manifest has no references")
    body = {
        "schema": INVENTORY_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "voice_manifest": str(manifest_path),
        "voice_manifest_sha256": manifest_sha256,
        "reference_count": len(references),
        "references": references,
    }
    if sha256_file(manifest_path) != manifest_sha256:
        raise SpeakerIdentityError("Voice manifest changed while inventory was built")
    return {**body, "inventory_id": canonical_document_sha256(body)}


def write_reference_inventory(document, output):
    _validate_inventory_shape(document)
    return _write_no_replace(output, document, "speaker-reference inventory")


def load_reference_inventory(path):
    document = _load_json(path, "speaker-reference inventory")
    _validate_inventory_shape(document)
    rebuilt = build_reference_inventory(document["voice_manifest"])
    if rebuilt != document:
        raise SpeakerIdentityError(
            "Speaker-reference inventory no longer matches its voice manifest"
        )
    return document


def build_labelled_pairs(inventory, pairs):
    """Bind human labels to exact inventory references and non-overlapping splits."""
    _validate_inventory_shape(inventory)
    known = {item["reference_id"] for item in inventory["references"]}
    normalized = []
    seen = set()
    reference_partitions = {}
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise SpeakerIdentityError(f"Labelled pair {index} must be an object")
        left = _required_text(pair.get("left_reference_id"), f"pair {index} left")
        right = _required_text(pair.get("right_reference_id"), f"pair {index} right")
        if left == right:
            raise SpeakerIdentityError(f"Labelled pair {index} repeats one reference")
        if left not in known or right not in known:
            raise SpeakerIdentityError(
                f"Labelled pair {index} uses an unknown reference"
            )
        partition = pair.get("partition")
        relationship = pair.get("relationship")
        if partition not in PARTITIONS:
            raise SpeakerIdentityError(f"Labelled pair {index} has invalid partition")
        if relationship not in RELATIONSHIPS:
            raise SpeakerIdentityError(
                f"Labelled pair {index} has invalid relationship"
            )
        canonical_pair = tuple(sorted((left, right)))
        if canonical_pair in seen:
            raise SpeakerIdentityError(
                f"Labelled pair {index} duplicates or leaks across partitions"
            )
        for reference_id in canonical_pair:
            previous = reference_partitions.setdefault(reference_id, partition)
            if previous != partition:
                raise SpeakerIdentityError(
                    f"Labelled pair {index} leaks a reference across partitions"
                )
        seen.add(canonical_pair)
        normalized.append(
            {
                "left_reference_id": canonical_pair[0],
                "right_reference_id": canonical_pair[1],
                "partition": partition,
                "relationship": relationship,
            }
        )
    if not normalized:
        raise SpeakerIdentityError("At least one labelled pair is required")
    normalized.sort(
        key=lambda item: (
            item["partition"],
            item["left_reference_id"],
            item["right_reference_id"],
        )
    )
    body = {
        "schema": LABELS_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "inventory_id": inventory["inventory_id"],
        "pairs": normalized,
    }
    return {**body, "labels_id": canonical_document_sha256(body)}


def write_labelled_pairs(document, output):
    _validate_labels_shape(document)
    return _write_no_replace(output, document, "speaker-identity labels")


def load_labelled_pairs(path, inventory):
    document = _load_json(path, "speaker-identity labels")
    _validate_labels_shape(document)
    if document["inventory_id"] != inventory["inventory_id"]:
        raise SpeakerIdentityError("Labels belong to a different reference inventory")
    rebuilt = build_labelled_pairs(inventory, document["pairs"])
    if rebuilt != document:
        raise SpeakerIdentityError("Speaker-identity labels are inconsistent")
    return document


def build_speaker_identity_report(inventory, labels, embed, model):
    """Fit one safe threshold and report held-out evidence without applying it."""
    _validate_inventory_shape(inventory)
    _validate_labels_shape(labels)
    if labels["inventory_id"] != inventory["inventory_id"]:
        raise SpeakerIdentityError("Labels belong to a different reference inventory")
    by_id = {item["reference_id"]: item for item in inventory["references"]}
    required_ids = sorted(
        {
            pair[field]
            for pair in labels["pairs"]
            for field in ("left_reference_id", "right_reference_id")
        }
    )
    vectors = {}
    for reference_id in required_ids:
        item = by_id[reference_id]
        path = Path(inventory["voice_manifest"]).parent / item["path"]
        try:
            payload = capture_authority_file(
                path,
                f"speaker reference {item['path']}",
                root=Path(inventory["voice_manifest"]).parent,
            ).payload
        except AuthoringAuthorityError as error:
            raise SpeakerIdentityError(str(error)) from error
        if _sha256_bytes(payload) != item["sha256"]:
            raise SpeakerIdentityError(f"Reference changed: {item['path']}")
        vector = np.asarray(embed(payload), dtype=np.float64).reshape(-1)
        if not vector.size or not np.all(np.isfinite(vector)):
            raise SpeakerIdentityError(f"Invalid embedding for {item['path']}")
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0:
            raise SpeakerIdentityError(f"Zero embedding for {item['path']}")
        vectors[reference_id] = vector / norm
    results = []
    for pair in labels["pairs"]:
        distance = float(
            1.0
            - np.clip(
                np.dot(
                    vectors[pair["left_reference_id"]],
                    vectors[pair["right_reference_id"]],
                ),
                -1.0,
                1.0,
            )
        )
        results.append({**copy.deepcopy(pair), "cosine_distance": distance})
    threshold, fit = _fit_threshold(results)
    held_out = _held_out_result(results, threshold)
    eligible = bool(
        threshold is not None
        and held_out["positive_count"]
        and held_out["negative_count"]
        and held_out["boundary_violation_count"] == 0
    )
    body = {
        "schema": REPORT_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "inventory_id": inventory["inventory_id"],
        "labels_id": labels["labels_id"],
        "model": copy.deepcopy(model),
        "evaluated_reference_count": len(required_ids),
        "pair_count": len(results),
        "fit": fit,
        "held_out": held_out,
        "threshold": threshold,
        "threshold_eligible": eligible,
        "authority": "diagnostic-only",
        "pairs": results,
    }
    return {**body, "report_id": canonical_document_sha256(body)}


def write_speaker_identity_report(document, output):
    if document.get("schema") != REPORT_SCHEMA:
        raise SpeakerIdentityError("Unsupported speaker-identity report")
    return _write_no_replace(output, document, "speaker-identity report")


def make_speechbrain_embedder(model_directory, *, device="cpu"):
    """Load the pinned ECAPA model; other devices stay disabled until measured."""
    require_speechbrain_runtime(device=device)
    import torch
    import torchaudio.functional as audio_functional
    from speechbrain.inference.speaker import EncoderClassifier

    model_directory = Path(model_directory).expanduser().resolve()
    classifier = EncoderClassifier.from_hparams(
        source=str(model_directory),
        hparams_file="hyperparams.yaml",
        overrides={"pretrained_path": str(model_directory)},
        run_opts={"device": "cpu"},
    )

    def embed(payload):
        try:
            audio, sample_rate = sf.read(
                io.BytesIO(payload), dtype="float32", always_2d=True
            )
        except Exception as error:
            raise SpeakerIdentityError(
                f"Unable to decode reference audio: {error}"
            ) from error
        waveform = torch.from_numpy(audio.mean(axis=1))
        if waveform.numel() == 0 or not torch.isfinite(waveform).all():
            raise SpeakerIdentityError("Reference audio is empty or non-finite")
        if sample_rate != 16000:
            waveform = audio_functional.resample(waveform, sample_rate, 16000)
        with torch.inference_mode():
            return classifier.encode_batch(waveform.unsqueeze(0)).cpu().numpy()

    return embed


def require_speechbrain_runtime(*, device="cpu"):
    """Fail before a model download when the exact optional runtime is unavailable."""
    if device != "cpu":
        raise SpeakerIdentityError("Speaker-identity diagnostics currently require CPU")
    if find_spec("speechbrain") is None:
        raise SpeakerIdentityError(
            "Speaker-identity runtime is unavailable. Run "
            "'uv sync --extra speaker-identity'."
        )
    if version("speechbrain") != IMPLEMENTATION_VERSION:
        raise SpeakerIdentityError(f"SpeechBrain {IMPLEMENTATION_VERSION} is required")


def installed_model_descriptor():
    status = managed_speaker_identity_status()
    if status["status"] != "installed":
        raise SpeakerIdentityError("Managed speaker-identity model is not installed")
    return {
        "model_id": status["model_id"],
        "repository": status["repository"],
        "revision": status["revision"],
        "file_sha256s": status["actual_files"],
        "licenses": status["licenses"],
        "runtime": status["runtime"],
        "implementation_version": IMPLEMENTATION_VERSION,
    }


def _fit_threshold(results):
    fit = [item for item in results if item["partition"] == "fit"]
    positives = [
        item["cosine_distance"]
        for item in fit
        if item["relationship"] == "same-speaker"
    ]
    negatives = [
        item["cosine_distance"]
        for item in fit
        if item["relationship"] != "same-speaker"
    ]
    separated = bool(positives and negatives and max(positives) < min(negatives))
    threshold = (max(positives) + min(negatives)) / 2 if separated else None
    return threshold, {
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "max_positive_distance": max(positives) if positives else None,
        "min_negative_distance": min(negatives) if negatives else None,
        "separated": separated,
    }


def _held_out_result(results, threshold):
    held_out = [item for item in results if item["partition"] == "held-out"]
    counts = {
        "true_positive": 0,
        "true_negative": 0,
        "false_positive": 0,
        "false_negative": 0,
    }
    boundary_violations = []
    for item in held_out:
        actual_positive = item["relationship"] == "same-speaker"
        predicted_positive = (
            threshold is not None and item["cosine_distance"] <= threshold
        )
        key = (
            "true_positive"
            if actual_positive and predicted_positive
            else "false_negative"
            if actual_positive
            else "false_positive"
            if predicted_positive
            else "true_negative"
        )
        counts[key] += 1
        if not actual_positive and predicted_positive:
            boundary_violations.append(
                {
                    "left_reference_id": item["left_reference_id"],
                    "right_reference_id": item["right_reference_id"],
                    "relationship": item["relationship"],
                    "cosine_distance": item["cosine_distance"],
                }
            )
    return {
        "positive_count": sum(
            item["relationship"] == "same-speaker" for item in held_out
        ),
        "negative_count": sum(
            item["relationship"] != "same-speaker" for item in held_out
        ),
        "confusion": counts,
        "boundary_violation_count": len(boundary_violations),
        "boundary_violations": boundary_violations,
    }


def _validate_inventory_shape(document):
    if (
        not isinstance(document, dict)
        or document.get("schema") != INVENTORY_SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(document.get("voice_manifest"), str)
        or not isinstance(document.get("references"), list)
        or document.get("reference_count") != len(document.get("references", ()))
        or document.get("inventory_id")
        != canonical_document_sha256(
            {key: value for key, value in document.items() if key != "inventory_id"}
        )
    ):
        raise SpeakerIdentityError("Unsupported or inconsistent reference inventory")
    required = {
        "reference_id",
        "character",
        "speaker",
        "path",
        "sha256",
        "sample_rate_hz",
        "channels",
        "frames",
        "duration_seconds",
    }
    if any(
        not isinstance(item, dict) or not required <= item.keys()
        for item in document["references"]
    ):
        raise SpeakerIdentityError("Reference inventory contains an invalid entry")


def _validate_labels_shape(document):
    if (
        not isinstance(document, dict)
        or document.get("schema") != LABELS_SCHEMA
        or document.get("schema_version") != SCHEMA_VERSION
        or not isinstance(document.get("pairs"), list)
        or document.get("labels_id")
        != canonical_document_sha256(
            {key: value for key, value in document.items() if key != "labels_id"}
        )
    ):
        raise SpeakerIdentityError("Unsupported or inconsistent speaker labels")
    required = {
        "left_reference_id",
        "right_reference_id",
        "partition",
        "relationship",
    }
    if any(
        not isinstance(item, dict) or not required <= item.keys()
        for item in document["pairs"]
    ):
        raise SpeakerIdentityError("Speaker labels contain an invalid pair")


def _load_json(path, label):
    try:
        document = json.loads(Path(path).expanduser().resolve().read_text("utf-8"))
    except (OSError, ValueError) as error:
        raise SpeakerIdentityError(f"Unable to read {label}: {error}") from error
    if not isinstance(document, dict):
        raise SpeakerIdentityError(f"{label.title()} must be an object")
    return document


def _write_no_replace(output, document, label):
    try:
        return write_json_document_no_replace(output, document, label)
    except AuthoringAuthorityError as error:
        raise SpeakerIdentityError(str(error)) from error


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SpeakerIdentityError(f"{label.capitalize()} is required")
    return value.strip()


def _sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "SpeakerIdentityError",
    "build_labelled_pairs",
    "build_reference_inventory",
    "build_speaker_identity_report",
    "installed_model_descriptor",
    "load_labelled_pairs",
    "load_reference_inventory",
    "make_speechbrain_embedder",
    "require_speechbrain_runtime",
    "write_labelled_pairs",
    "write_reference_inventory",
    "write_speaker_identity_report",
]
