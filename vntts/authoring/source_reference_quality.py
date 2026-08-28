"""Checksum-bound, cluster-specific source-reference quality review."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueError
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    validate_generation_state_document,
)
from vntts.authoring.game_pack import _rename_directory_no_replace
from vntts.authoring.source_reference_quality_records import (
    QUALITY_DECISIONS,
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    SourceReferenceQualityError,
    SourceReferenceQualityResult,
    _contained_file,
    _copy_audio,
    _probe_png,
    _read_json,
    _required_sha256,
    _required_text,
    _utc_now,
    accepted_source_reference_variants,
    load_source_reference_quality_review,
    next_pending_quality_variant,
    quality_review_progress,
    record_source_reference_quality_decision,
    validate_source_reference_quality_review_document,
)
from vntts.authoring.source_reference_review import (
    REFERENCE_EVALUATION_SCHEMA,
    REFERENCE_EVALUATION_VERSION,
    load_source_reference_plan,
)
from vntts.cli import cli_error, cli_success


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
        validate_generation_state_document(
            state, state_path.parent, queue, queue_sha256
        )
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

            queue_ids = []
            source_match_queue_id = variant.get("source_match_queue_id")
            if source_match_queue_id is not None:
                queue_ids.append(
                    (
                        "source-match",
                        _required_text(
                            source_match_queue_id,
                            f"variant {variant_id} source-match queue ID",
                        ),
                    )
                )
            fixed = variant.get("fixed_queue_ids")
            if not isinstance(fixed, list):
                raise SourceReferenceQualityError(
                    f"Variant {variant_id} fixed queue IDs must be a list"
                )
            queue_ids.extend(
                (
                    f"fixed-{index}",
                    _required_text(value, f"variant {variant_id} fixed queue ID"),
                )
                for index, value in enumerate(fixed, start=1)
            )
            if len(queue_ids) != len({queue_id for _kind, queue_id in queue_ids}):
                raise SourceReferenceQualityError(
                    f"Variant {variant_id} queue IDs are duplicated"
                )
            generated = []
            excluded = []
            synthesis_contexts = []
            for item_index, (expected_kind, queue_id) in enumerate(queue_ids):
                item = queue_by_id.get(queue_id)
                if item is None:
                    raise SourceReferenceQualityError(
                        f"Variant {variant_id} queue item is absent: {queue_id}"
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
                if isinstance(result, dict):
                    synthesis_contexts.append(
                        {
                            "backend": result.get("provider"),
                            "model": result.get("model"),
                            "generation_profile": result.get("generation_profile"),
                            "seed": result.get("seed"),
                        }
                    )
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
                    "decision_context": _quality_decision_context(synthesis_contexts),
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
    create_composite = subparsers.add_parser("create-composite")
    create_composite.add_argument("--composite", type=Path, required=True)
    create_composite.add_argument("--state", type=Path, required=True)
    create_composite.add_argument("--output", type=Path, required=True)
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
        if options.command == "create-composite":
            from vntts.authoring.reference_composite import (
                publish_composite_quality_review,
            )

            result = publish_composite_quality_review(
                options.composite, options.state, options.output
            )
            return cli_success(f"Created composite quality review: {result.session}")
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


def _quality_decision_context(values):
    def shared(field):
        candidates = {value.get(field) for value in values}
        candidates.discard(None)
        if len(candidates) == 1:
            return next(iter(candidates))
        return "Mixed" if candidates else "Unknown"

    return {
        "backend": str(shared("backend")),
        "model": str(shared("model")),
        "generation_profile": str(shared("generation_profile")),
        "seed": shared("seed"),
    }


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
    "validate_source_reference_quality_review_document",
]


if __name__ == "__main__":
    raise SystemExit(main())
