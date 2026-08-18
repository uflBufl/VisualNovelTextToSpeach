"""Checksum-bound, cluster-specific source-reference quality review."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import tempfile
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueError
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    _validate_state_document,
)
from vntts.authoring.game_pack import _rename_directory_no_replace
from vntts.authoring.source_reference_review import (
    REFERENCE_EVALUATION_SCHEMA,
    REFERENCE_EVALUATION_VERSION,
    load_source_reference_plan,
)
from vntts.cli import cli_error, cli_success

QUALITY_REVIEW_SCHEMA = "vntts.authoring-source-reference-quality-review"
QUALITY_REVIEW_VERSION = 1
QUALITY_DECISIONS = frozenset({"accept", "reject", "needs_sample"})


class SourceReferenceQualityError(RuntimeError):
    """Source-reference quality evidence is invalid or cannot be updated."""


@dataclass(frozen=True)
class SourceReferenceQualityResult:
    directory: Path
    variants: int
    generated_samples: int
    excluded_results: int

    @property
    def session(self):
        return self.directory / "review.json"

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "session": str(self.session),
            "variants": self.variants,
            "generated_samples": self.generated_samples,
            "excluded_results": self.excluded_results,
        }


def publish_source_reference_quality_review(
    plan_directory,
    evaluation_directory,
    state_path,
    output,
    *,
    portrait_directory=None,
):
    """Publish a self-contained review card for every exact reference variant."""
    plan_directory = Path(plan_directory).expanduser().resolve()
    evaluation_directory = Path(evaluation_directory).expanduser().resolve()
    state_path = Path(state_path).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if portrait_directory is not None:
        portrait_directory = Path(portrait_directory).expanduser().resolve()
        if not portrait_directory.is_dir():
            raise SourceReferenceQualityError(
                f"Portrait directory is missing: {portrait_directory}"
            )
    if output.exists() or output.is_symlink():
        raise SourceReferenceQualityError(f"Quality review output exists: {output}")

    plan_path = plan_directory / "plan.json"
    plan_payload, plan_snapshot = _read_json(plan_path, "source-reference plan")
    plan_sha256 = hashlib.sha256(plan_payload).hexdigest()
    plan = load_source_reference_plan(plan_directory)
    if plan != plan_snapshot or sha256_file(plan_path) != plan_sha256:
        raise SourceReferenceQualityError(
            "Source-reference plan changed while it was loaded"
        )
    comparison_path = evaluation_directory / "comparison.json"
    comparison_payload, comparison = _read_json(
        comparison_path, "source-reference evaluation"
    )
    comparison_sha256 = hashlib.sha256(comparison_payload).hexdigest()
    if (
        comparison.get("schema") != REFERENCE_EVALUATION_SCHEMA
        or comparison.get("schema_version") != REFERENCE_EVALUATION_VERSION
    ):
        raise SourceReferenceQualityError(
            "Unsupported source-reference evaluation schema"
        )
    if comparison.get("source_reference_plan_sha256") != plan_sha256:
        raise SourceReferenceQualityError(
            "Evaluation belongs to a different source-reference plan"
        )
    queue_path = _contained_file(
        evaluation_directory, comparison.get("queue"), "evaluation queue"
    )
    queue_sha256 = _required_sha256(
        comparison.get("queue_sha256"), "evaluation queue hash"
    )
    if sha256_file(queue_path) != queue_sha256:
        raise SourceReferenceQualityError("Evaluation queue changed")
    try:
        queue_payload = queue_path.read_bytes()
    except OSError as error:
        raise SourceReferenceQualityError(
            f"Unable to read evaluation queue {queue_path}: {error}"
        ) from error
    if hashlib.sha256(queue_payload).hexdigest() != queue_sha256:
        raise SourceReferenceQualityError("Evaluation queue changed while loaded")
    state_payload, state = _read_json(state_path, "generation state")
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="vntts-quality-queue-") as directory:
            queue_snapshot = Path(directory) / "queue.jsonl"
            queue_snapshot.write_bytes(queue_payload)
            queue = VoiceGenerationQueue.load(queue_snapshot)
        _validate_state_document(state, state_path.parent, queue, queue_sha256)
    except (VoiceGenerationQueueError, BulkGenerationError) as error:
        raise SourceReferenceQualityError(str(error)) from error
    queue_by_id = {item.queue_id: item for item in queue.items}

    plan_variants = {}
    for cluster in plan["clusters"]:
        for index, reference in enumerate(cluster["references"], start=1):
            variant_id = f"{cluster['cluster_id']}-anchor-{index}"
            plan_variants[variant_id] = (cluster, reference)
    evaluation_variants = comparison.get("variants")
    if not isinstance(evaluation_variants, list) or not evaluation_variants:
        raise SourceReferenceQualityError("Evaluation variants must be non-empty")
    if len(evaluation_variants) != len(plan_variants):
        raise SourceReferenceQualityError(
            "Evaluation does not cover every source-reference plan variant"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    snapshots = [
        (plan_path, plan_sha256),
        (comparison_path, comparison_sha256),
        (queue_path, queue_sha256),
        (state_path, state_sha256),
    ]
    try:
        cards = []
        seen_variants = set()
        generated_count = 0
        excluded_count = 0
        for position, variant in enumerate(evaluation_variants, start=1):
            if not isinstance(variant, dict):
                raise SourceReferenceQualityError(
                    f"Evaluation variant {position} must be an object"
                )
            variant_id = _required_text(
                variant.get("variant_id"), f"evaluation variant {position} ID"
            )
            if variant_id in seen_variants or variant_id not in plan_variants:
                raise SourceReferenceQualityError(
                    f"Evaluation variant identity is invalid: {variant_id}"
                )
            seen_variants.add(variant_id)
            cluster, reference = plan_variants[variant_id]
            _validate_variant_identity(variant, cluster, reference, variant_id)
            source = _contained_file(
                evaluation_directory,
                variant.get("source_audio"),
                f"variant {variant_id} source audio",
            )
            source_sha256 = _required_sha256(
                variant.get("source_audio_sha256"),
                f"variant {variant_id} source audio hash",
            )
            if sha256_file(source) != source_sha256:
                raise SourceReferenceQualityError(
                    f"Evaluation source audio changed: {variant_id}"
                )
            snapshots.append((source, source_sha256))
            reference_relative = (
                Path("audio") / variant_id / f"reference{source.suffix.lower()}"
            )
            reference_record = _copy_audio(
                source, source_sha256, staging / reference_relative
            )
            reference_record["audio"] = reference_relative.as_posix()

            portrait_image = _copy_optional_portrait(
                portrait_directory,
                cluster.get("portrait"),
                variant_id,
                staging,
                snapshots,
            )

            queue_ids = [
                _required_text(
                    variant.get("source_match_queue_id"),
                    f"variant {variant_id} source-match queue ID",
                )
            ]
            fixed = variant.get("fixed_queue_ids")
            if not isinstance(fixed, list):
                raise SourceReferenceQualityError(
                    f"Variant {variant_id} fixed queue IDs must be a list"
                )
            queue_ids.extend(
                _required_text(value, f"variant {variant_id} fixed queue ID")
                for value in fixed
            )
            if len(queue_ids) != len(set(queue_ids)):
                raise SourceReferenceQualityError(
                    f"Variant {variant_id} queue IDs are duplicated"
                )
            generated = []
            excluded = []
            for item_index, queue_id in enumerate(queue_ids):
                item = queue_by_id.get(queue_id)
                if item is None:
                    raise SourceReferenceQualityError(
                        f"Variant {variant_id} queue item is absent: {queue_id}"
                    )
                expected_kind = (
                    "source-match" if item_index == 0 else f"fixed-{item_index}"
                )
                if (
                    item.document.get("reference_cluster_id") != cluster["cluster_id"]
                    or item.document.get("evaluation_kind") != expected_kind
                    or item.speaker != cluster["character"]
                ):
                    raise SourceReferenceQualityError(
                        f"Variant {variant_id} queue binding changed: {queue_id}"
                    )
                result = state["items"].get(queue_id)
                status = result.get("status") if isinstance(result, dict) else "pending"
                common = {
                    "queue_id": queue_id,
                    "evaluation_kind": expected_kind,
                    "text": item.text,
                    "text_sha256": item.text_sha256,
                }
                if status in {"generated", "approved"}:
                    relative = _required_text(
                        result.get("path"), f"generated result {queue_id} path"
                    )
                    generated_source = _contained_file(
                        state_path.parent, relative, f"generated result {queue_id}"
                    )
                    generated_sha256 = _required_sha256(
                        result.get("file_sha256"),
                        f"generated result {queue_id} hash",
                    )
                    if sha256_file(generated_source) != generated_sha256:
                        raise SourceReferenceQualityError(
                            f"Generated evaluation audio changed: {queue_id}"
                        )
                    snapshots.append((generated_source, generated_sha256))
                    generated_relative = (
                        Path("audio")
                        / variant_id
                        / f"generated-{item_index + 1:02d}.wav"
                    )
                    audio_record = _copy_audio(
                        generated_source,
                        generated_sha256,
                        staging / generated_relative,
                    )
                    generated.append(
                        {
                            **common,
                            "audio": generated_relative.as_posix(),
                            **audio_record,
                        }
                    )
                    generated_count += 1
                else:
                    excluded.append(
                        {
                            **common,
                            "status": status,
                            "attempts": (
                                result.get("attempts", 0)
                                if isinstance(result, dict)
                                else 0
                            ),
                            "error": (
                                result.get("last_error")
                                if isinstance(result, dict)
                                and isinstance(result.get("last_error"), str)
                                else None
                            ),
                            "completion": (
                                result.get("failure", {}).get("completion")
                                if isinstance(result, dict)
                                and isinstance(result.get("failure"), dict)
                                and isinstance(
                                    result.get("failure", {}).get("completion"), str
                                )
                                else None
                            ),
                            "failure_kind": (
                                result.get("failure", {}).get("kind")
                                if isinstance(result, dict)
                                and isinstance(result.get("failure"), dict)
                                and isinstance(
                                    result.get("failure", {}).get("kind"), str
                                )
                                else None
                            ),
                        }
                    )
                    excluded_count += 1
            cards.append(
                {
                    "variant_id": variant_id,
                    "cluster_id": cluster["cluster_id"],
                    "character": cluster["character"],
                    "portrait": cluster["portrait"],
                    "portrait_image": portrait_image,
                    "source_bank": cluster["source_bank"],
                    "media_id": reference["media_id"],
                    "affected_queue_item_count": len(cluster["queue_items"]),
                    "reference": reference_record,
                    "generated_samples": generated,
                    "excluded_results": excluded,
                    "decision": None,
                }
            )
        if seen_variants != set(plan_variants):
            raise SourceReferenceQualityError(
                "Evaluation variant inventory does not match the plan"
            )
        now = _utc_now()
        session = {
            "schema": QUALITY_REVIEW_SCHEMA,
            "schema_version": QUALITY_REVIEW_VERSION,
            "created_at": now,
            "updated_at": now,
            "source_reference_plan_sha256": plan_sha256,
            "source_reference_evaluation_sha256": comparison_sha256,
            "generation_state_sha256": state_sha256,
            "variant_count": len(cards),
            "completed_count": 0,
            "variants": cards,
        }
        review_path = staging / "review.json"
        atomic_write_json(review_path, session, sort_keys=True)
        load_source_reference_quality_review(review_path)
        for source, digest in snapshots:
            if sha256_file(source) != digest:
                raise SourceReferenceQualityError(
                    f"Source changed during quality review publication: {source.name}"
                )
        _rename_directory_no_replace(staging, output)
        return SourceReferenceQualityResult(
            output, len(cards), generated_count, excluded_count
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_source_reference_quality_review(path):
    """Load and fully validate one self-contained quality review."""
    path = Path(path).expanduser().resolve()
    _payload, session = _read_json(path, "source-reference quality review")
    if (
        session.get("schema") != QUALITY_REVIEW_SCHEMA
        or session.get("schema_version") != QUALITY_REVIEW_VERSION
    ):
        raise SourceReferenceQualityError(
            "Unsupported source-reference quality review schema"
        )
    _aware_timestamp(session.get("created_at"), "quality review created_at")
    _aware_timestamp(session.get("updated_at"), "quality review updated_at")
    variants = session.get("variants")
    if (
        not isinstance(variants, list)
        or not variants
        or session.get("variant_count") != len(variants)
    ):
        raise SourceReferenceQualityError("Quality review variant count is invalid")
    seen = set()
    completed = 0
    for index, card in enumerate(variants):
        if not isinstance(card, dict):
            raise SourceReferenceQualityError(
                f"Quality review variant {index} must be an object"
            )
        variant_id = _required_text(card.get("variant_id"), "quality variant ID")
        if variant_id in seen:
            raise SourceReferenceQualityError(
                f"Quality review variant is duplicated: {variant_id}"
            )
        seen.add(variant_id)
        for field in ("cluster_id", "character", "source_bank"):
            _required_text(card.get(field), f"quality variant {variant_id} {field}")
        media_id = card.get("media_id")
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 0:
            raise SourceReferenceQualityError(
                f"Quality variant {variant_id} media ID is invalid"
            )
        portrait = card.get("portrait")
        if portrait is not None and (
            not isinstance(portrait, str) or not portrait.strip()
        ):
            raise SourceReferenceQualityError(
                f"Quality variant {variant_id} portrait is invalid"
            )
        portrait_image = card.get("portrait_image")
        if portrait_image is not None:
            _validate_portrait_record(path.parent, portrait_image, variant_id)
        _positive_integer(
            card.get("affected_queue_item_count"),
            f"quality variant {variant_id} affected count",
        )
        _validate_audio_record(path.parent, card.get("reference"), variant_id)
        generated = card.get("generated_samples")
        excluded = card.get("excluded_results")
        if not isinstance(generated, list) or not isinstance(excluded, list):
            raise SourceReferenceQualityError(
                f"Quality variant {variant_id} outcomes are invalid"
            )
        queue_ids = set()
        for sample in generated:
            queue_id = _validate_sample(path.parent, sample, variant_id, audio=True)
            if queue_id in queue_ids:
                raise SourceReferenceQualityError(
                    f"Quality variant {variant_id} queue ID is duplicated"
                )
            queue_ids.add(queue_id)
        for sample in excluded:
            queue_id = _validate_sample(path.parent, sample, variant_id, audio=False)
            if queue_id in queue_ids:
                raise SourceReferenceQualityError(
                    f"Quality variant {variant_id} queue ID is duplicated"
                )
            queue_ids.add(queue_id)
            _required_text(sample.get("status"), f"excluded {queue_id} status")
            attempts = sample.get("attempts")
            if (
                isinstance(attempts, bool)
                or not isinstance(attempts, int)
                or attempts < 0
            ):
                raise SourceReferenceQualityError(
                    f"Excluded result {queue_id} attempts are invalid"
                )
        decision = card.get("decision")
        if decision is not None:
            if (
                not isinstance(decision, dict)
                or decision.get("decision") not in QUALITY_DECISIONS
            ):
                raise SourceReferenceQualityError(
                    f"Quality variant {variant_id} decision is invalid"
                )
            _aware_timestamp(
                decision.get("reviewed_at"),
                f"quality variant {variant_id} reviewed_at",
            )
            if decision["decision"] == "accept" and not generated:
                raise SourceReferenceQualityError(
                    f"Quality variant {variant_id} was accepted without generated audio"
                )
            completed += 1
    if session.get("completed_count") != completed:
        raise SourceReferenceQualityError("Quality review progress is inconsistent")
    for field in (
        "source_reference_plan_sha256",
        "source_reference_evaluation_sha256",
        "generation_state_sha256",
    ):
        _required_sha256(session.get(field), f"quality review {field}")
    return session


def quality_review_progress(session):
    completed = sum(card.get("decision") is not None for card in session["variants"])
    return completed, len(session["variants"])


def next_pending_quality_variant(session):
    return next(
        (card for card in session["variants"] if card.get("decision") is None), None
    )


def record_source_reference_quality_decision(
    session_path, variant_id, decision, *, overwrite=False
):
    if decision not in QUALITY_DECISIONS:
        raise SourceReferenceQualityError(
            "Quality decision must be accept, reject, or needs_sample"
        )
    session_path = Path(session_path).expanduser().resolve()
    with _decision_lock(session_path):
        try:
            original_payload = session_path.read_bytes()
        except OSError as error:
            raise SourceReferenceQualityError(str(error)) from error
        session = load_source_reference_quality_review(session_path)
        if session_path.read_bytes() != original_payload:
            raise SourceReferenceQualityError(
                "Quality review changed while the decision was loaded"
            )
        original_session = json.loads(original_payload)
        card = next(
            (item for item in session["variants"] if item["variant_id"] == variant_id),
            None,
        )
        if card is None:
            raise SourceReferenceQualityError(f"Unknown quality variant: {variant_id}")
        if card.get("decision") is not None and not overwrite:
            raise SourceReferenceQualityError(
                f"Quality variant is already rated: {variant_id}"
            )
        if decision == "accept" and not card["generated_samples"]:
            raise SourceReferenceQualityError(
                "A reference without generated samples cannot be accepted"
            )
        card["decision"] = {"decision": decision, "reviewed_at": _utc_now()}
        session["completed_count"] = quality_review_progress(session)[0]
        session["updated_at"] = _utc_now()
        if session_path.read_bytes() != original_payload:
            raise SourceReferenceQualityError(
                "Quality review changed before the decision was saved"
            )
        load_source_reference_quality_review(session_path)
        try:
            atomic_write_json(session_path, session, sort_keys=True)
            return load_source_reference_quality_review(session_path)
        except Exception:
            atomic_write_json(session_path, original_session, sort_keys=True)
            raise


def accepted_source_reference_variants(session, *, require_complete=True):
    completed, total = quality_review_progress(session)
    if require_complete and completed != total:
        raise SourceReferenceQualityError("Quality review is incomplete")
    return tuple(
        card["variant_id"]
        for card in session["variants"]
        if (card.get("decision") or {}).get("decision") == "accept"
    )


def create_parser():
    parser = argparse.ArgumentParser(
        description="Review source-reference quality by exact character cluster"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--plan", type=Path, required=True)
    create.add_argument("--evaluation", type=Path, required=True)
    create.add_argument("--state", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--portrait-directory", type=Path)
    for command in ("status", "next", "ui"):
        child = subparsers.add_parser(command)
        child.add_argument("--session", type=Path, required=True)
    decide = subparsers.add_parser("decide")
    decide.add_argument("variant_id")
    decide.add_argument("--session", type=Path, required=True)
    decide.add_argument("--decision", choices=sorted(QUALITY_DECISIONS), required=True)
    decide.add_argument("--overwrite", action="store_true")
    return parser


def main(argv=None):
    options = create_parser().parse_args(argv)
    try:
        if options.command == "create":
            result = publish_source_reference_quality_review(
                options.plan,
                options.evaluation,
                options.state,
                options.output,
                portrait_directory=options.portrait_directory,
            )
            return cli_success(
                f"Created source-reference quality review: {result.session}"
            )
        if options.command == "ui":
            from vntts.authoring.source_reference_quality_ui import (
                launch_source_reference_quality_review,
            )

            return launch_source_reference_quality_review(options.session)
        session = load_source_reference_quality_review(options.session)
        if options.command == "status":
            completed, total = quality_review_progress(session)
            accepted = len(
                accepted_source_reference_variants(session, require_complete=False)
            )
            return cli_success(
                f"Source-reference review: {completed}/{total}; accepted {accepted}"
            )
        if options.command == "next":
            card = next_pending_quality_variant(session)
            if card is None:
                return cli_success("Source-reference quality review is complete")
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return 0
        updated = record_source_reference_quality_decision(
            options.session,
            options.variant_id,
            options.decision,
            overwrite=options.overwrite,
        )
        completed, total = quality_review_progress(updated)
        return cli_success(f"Saved {options.variant_id}; progress: {completed}/{total}")
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            return cli_error("Qt UI is not installed")
        raise
    except (SourceReferenceQualityError, OSError, json.JSONDecodeError) as error:
        return cli_error(error)


def _validate_variant_identity(variant, cluster, reference, variant_id):
    expected = {
        "character": cluster["character"],
        "portrait": cluster["portrait"],
        "source_bank": cluster["source_bank"],
        "media_id": reference["media_id"],
        "source_audio_sha256": reference["sha256"],
    }
    for field, value in expected.items():
        if variant.get(field) != value:
            raise SourceReferenceQualityError(
                f"Evaluation variant {variant_id} changed {field}"
            )


def _copy_audio(source, digest, destination):
    try:
        info = probe_pcm16_mono_wav(source)
    except Pcm16MonoWavError as error:
        raise SourceReferenceQualityError(
            f"Invalid review WAV {source}: {error}"
        ) from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if sha256_file(destination) != digest:
        raise SourceReferenceQualityError(f"Review WAV changed while copied: {source}")
    return {
        "audio_sha256": digest,
        "sample_rate": info.sample_rate,
        "sample_count": info.sample_count,
        "duration_seconds": round(info.duration_seconds, 6),
    }


def _copy_optional_portrait(root, portrait, variant_id, staging, snapshots):
    if root is None or portrait is None:
        return None
    portrait = _required_text(portrait, f"variant {variant_id} portrait")
    if "\\" in portrait:
        raise SourceReferenceQualityError("Portrait identity must be a filename")
    identity = PurePosixPath(portrait)
    if len(identity.parts) != 1 or identity.name in {"", ".", ".."}:
        raise SourceReferenceQualityError("Portrait identity must be a filename")
    filename = identity.name
    if not filename.lower().endswith(".png"):
        filename = f"{filename}.png"
    source = (root / filename).resolve()
    try:
        source.relative_to(root)
    except ValueError as error:
        raise SourceReferenceQualityError("Portrait image leaves its root") from error
    if not source.exists():
        return None
    if not source.is_file():
        raise SourceReferenceQualityError(f"Portrait image is not a file: {source}")
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise SourceReferenceQualityError(
            f"Unable to read portrait image {source}: {error}"
        ) from error
    digest = hashlib.sha256(payload).hexdigest()
    width, height = _probe_png(payload, f"portrait image {filename}")
    relative = Path("portraits") / f"{variant_id}.png"
    destination = staging / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise SourceReferenceQualityError(
            f"Portrait image changed while copied: {source}"
        )
    snapshots.append((source, digest))
    return {
        "image": relative.as_posix(),
        "image_sha256": digest,
        "width": width,
        "height": height,
    }


def _validate_portrait_record(root, value, label):
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"Quality portrait {label} must be an object")
    path = _contained_file(root, value.get("image"), f"quality portrait {label}")
    digest = _required_sha256(
        value.get("image_sha256"), f"quality portrait {label} hash"
    )
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SourceReferenceQualityError(
            f"Unable to read quality portrait {label}: {error}"
        ) from error
    if hashlib.sha256(payload).hexdigest() != digest:
        raise SourceReferenceQualityError(f"Quality portrait changed: {label}")
    width, height = _probe_png(payload, f"quality portrait {label}")
    if value.get("width") != width or value.get("height") != height:
        raise SourceReferenceQualityError(f"Quality portrait metadata changed: {label}")
    return path


def _probe_png(payload, label):
    if not isinstance(payload, bytes) or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SourceReferenceQualityError(f"{label.title()} is not a PNG")
    offset = 8
    width = height = None
    idat_parts = []
    saw_iend = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise SourceReferenceQualityError(f"{label.title()} is truncated")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise SourceReferenceQualityError(f"{label.title()} is truncated")
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise SourceReferenceQualityError(f"{label.title()} has an invalid CRC")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise SourceReferenceQualityError(f"{label.title()} has no valid IHDR")
            width, height = struct.unpack(">II", data[:8])
            if width < 1 or height < 1:
                raise SourceReferenceQualityError(
                    f"{label.title()} has invalid dimensions"
                )
        elif kind == b"IDAT":
            idat_parts.append(data)
        elif kind == b"IEND":
            if length != 0 or chunk_end != len(payload):
                raise SourceReferenceQualityError(f"{label.title()} has invalid IEND")
            saw_iend = True
        offset = chunk_end
    if width is None or not idat_parts or not saw_iend:
        raise SourceReferenceQualityError(f"{label.title()} is incomplete")
    try:
        decoded = zlib.decompress(b"".join(idat_parts))
    except zlib.error as error:
        raise SourceReferenceQualityError(
            f"{label.title()} has invalid image data"
        ) from error
    if not decoded:
        raise SourceReferenceQualityError(f"{label.title()} has empty image data")
    return width, height


def _validate_audio_record(root, value, label):
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"Quality audio {label} must be an object")
    path = _contained_file(root, value.get("audio"), f"quality audio {label}")
    digest = _required_sha256(value.get("audio_sha256"), f"quality audio {label} hash")
    if sha256_file(path) != digest:
        raise SourceReferenceQualityError(f"Quality audio changed: {label}")
    try:
        info = probe_pcm16_mono_wav(path)
    except Pcm16MonoWavError as error:
        raise SourceReferenceQualityError(
            f"Invalid quality WAV {label}: {error}"
        ) from error
    if (
        value.get("sample_rate") != info.sample_rate
        or value.get("sample_count") != info.sample_count
        or value.get("duration_seconds") != round(info.duration_seconds, 6)
    ):
        raise SourceReferenceQualityError(f"Quality audio metadata changed: {label}")
    return path


def _validate_sample(root, sample, variant_id, *, audio):
    if not isinstance(sample, dict):
        raise SourceReferenceQualityError(
            f"Quality variant {variant_id} sample must be an object"
        )
    queue_id = _required_text(sample.get("queue_id"), "quality sample queue ID")
    _required_text(sample.get("evaluation_kind"), f"quality sample {queue_id} kind")
    text = _required_text(sample.get("text"), f"quality sample {queue_id} text")
    digest = _required_sha256(
        sample.get("text_sha256"), f"quality sample {queue_id} text hash"
    )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise SourceReferenceQualityError(f"Quality sample text changed: {queue_id}")
    if audio:
        _validate_audio_record(root, sample, queue_id)
    return queue_id


def _read_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReferenceQualityError(
            f"Unable to read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"{label.title()} must be an object")
    return payload, value


def _contained_file(root, value, label):
    value = _required_text(value, label)
    if "\\" in value:
        raise SourceReferenceQualityError(f"{label.title()} must use POSIX separators")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SourceReferenceQualityError(
            f"{label.title()} must be a safe relative path"
        )
    root = Path(root).expanduser().resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise SourceReferenceQualityError(f"{label.title()} leaves its root") from error
    if not candidate.is_file():
        raise SourceReferenceQualityError(f"{label.title()} is missing: {candidate}")
    return candidate


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SourceReferenceQualityError(f"{label.title()} must be non-empty text")
    return value.strip()


def _required_sha256(value, label):
    value = _required_text(value, label)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise SourceReferenceQualityError(f"{label.title()} must be lowercase SHA-256")
    return value


def _positive_integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceReferenceQualityError(f"{label.title()} must be positive")
    return value


def _aware_timestamp(value, label):
    value = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise SourceReferenceQualityError(f"{label.title()} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceReferenceQualityError(f"{label.title()} must include a timezone")
    return parsed


@contextmanager
def _decision_lock(session_path):
    lock_path = session_path.with_name(f".{session_path.name}.lock")
    token = uuid.uuid4().hex
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as error:
        raise SourceReferenceQualityError(
            "Another source-reference decision is being saved"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            if lock_path.read_text(encoding="utf-8") == token:
                lock_path.unlink()
        except FileNotFoundError:
            pass


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "QUALITY_DECISIONS",
    "QUALITY_REVIEW_SCHEMA",
    "QUALITY_REVIEW_VERSION",
    "SourceReferenceQualityError",
    "SourceReferenceQualityResult",
    "accepted_source_reference_variants",
    "load_source_reference_quality_review",
    "next_pending_quality_variant",
    "publish_source_reference_quality_review",
    "quality_review_progress",
    "record_source_reference_quality_decision",
]


if __name__ == "__main__":
    raise SystemExit(main())
