"""Checksum-bound, cluster-specific source-reference quality review."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections.abc import MutableSequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vntts_artifacts import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file

from vntts.authoring.bulk_generation import (
    BulkGenerationError,
)
from vntts.authoring.generation_state import validate_generation_state_document
from vntts.authoring.publication import rename_directory_no_replace, staged_directory
from vntts.authoring.source_reference_quality_records import (
    QUALITY_DECISIONS,
    QUALITY_REVIEW_SCHEMA,
    QUALITY_REVIEW_VERSION,
    JsonObject,
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
    capture_quality_outcomes,
    load_source_reference_quality_review,
    next_pending_quality_variant,
    quality_review_progress,
    record_source_reference_quality_decision,
    validate_source_reference_quality_review_document,
)
from vntts.authoring.source_reference_review import (
    REFERENCE_EVALUATION_SCHEMA,
    REFERENCE_EVALUATION_VERSION,
    SourceReferenceReviewError,
    _variant_evaluation_queue_ids,
    load_source_reference_plan,
)
from vntts.cli import cli_error, cli_success


@dataclass(frozen=True)
class _QualityReviewPlan:
    path: Path
    document: JsonObject
    sha256: str


@dataclass(frozen=True)
class _QualityReviewEvaluation:
    path: Path
    directory: Path
    document: JsonObject
    sha256: str
    queue_path: Path
    queue_payload: bytes
    queue_sha256: str


@dataclass(frozen=True)
class _QualityReviewGeneration:
    path: Path
    sha256: str
    queue_by_id: dict[str, VoiceGenerationQueueItem]
    state_items: JsonObject


@dataclass(frozen=True)
class _QualityReviewInputs:
    plan: _QualityReviewPlan
    evaluation: _QualityReviewEvaluation
    generation: _QualityReviewGeneration
    plan_variants: dict[str, tuple[JsonObject, JsonObject]]
    evaluation_variants: list[object]


def publish_source_reference_quality_review(
    plan_directory: str | Path,
    evaluation_directory: str | Path,
    state_path: str | Path,
    output: str | Path,
    *,
    portrait_directory: str | Path | None = None,
) -> SourceReferenceQualityResult:
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

    plan = _load_quality_review_plan(plan_directory)
    evaluation = _load_quality_review_evaluation(evaluation_directory, plan.sha256)
    generation = _load_quality_review_generation(state_path, evaluation)
    inputs = _quality_review_inputs(plan, evaluation, generation)
    return _stage_quality_review(output, portrait_directory, inputs)


def _load_quality_review_plan(directory: Path) -> _QualityReviewPlan:
    path = directory / "plan.json"
    payload, snapshot = _read_json(path, "source-reference plan")
    digest = hashlib.sha256(payload).hexdigest()
    document = load_source_reference_plan(directory)
    if document != snapshot or sha256_file(path) != digest:
        raise SourceReferenceQualityError(
            "Source-reference plan changed while it was loaded"
        )
    return _QualityReviewPlan(path, document, digest)


def _load_quality_review_evaluation(
    directory: Path, plan_sha256: str
) -> _QualityReviewEvaluation:
    path = directory / "comparison.json"
    payload, document = _read_json(path, "source-reference evaluation")
    digest = hashlib.sha256(payload).hexdigest()
    if (
        document.get("schema") != REFERENCE_EVALUATION_SCHEMA
        or document.get("schema_version") != REFERENCE_EVALUATION_VERSION
    ):
        raise SourceReferenceQualityError(
            "Unsupported source-reference evaluation schema"
        )
    if document.get("source_reference_plan_sha256") != plan_sha256:
        raise SourceReferenceQualityError(
            "Evaluation belongs to a different source-reference plan"
        )
    queue_path = _contained_file(directory, document.get("queue"), "evaluation queue")
    queue_sha256 = _required_sha256(
        document.get("queue_sha256"), "evaluation queue hash"
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
    return _QualityReviewEvaluation(
        path,
        directory,
        document,
        digest,
        queue_path,
        queue_payload,
        queue_sha256,
    )


def _load_quality_review_generation(
    state_path: Path, evaluation: _QualityReviewEvaluation
) -> _QualityReviewGeneration:
    state_payload, state = _read_json(state_path, "generation state")
    state_sha256 = hashlib.sha256(state_payload).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="vntts-quality-queue-") as directory:
            queue_snapshot = Path(directory) / "queue.jsonl"
            queue_snapshot.write_bytes(evaluation.queue_payload)
            queue = VoiceGenerationQueue.load(queue_snapshot)
        validate_generation_state_document(
            state, state_path.parent, queue, evaluation.queue_sha256
        )
    except (VoiceGenerationQueueError, BulkGenerationError) as error:
        raise SourceReferenceQualityError(str(error)) from error
    queue_by_id = {item.queue_id: item for item in queue.items}
    state_items = _object_field(state, "items", "generation state items")
    return _QualityReviewGeneration(state_path, state_sha256, queue_by_id, state_items)


def _quality_review_inputs(
    plan: _QualityReviewPlan,
    evaluation: _QualityReviewEvaluation,
    generation: _QualityReviewGeneration,
) -> _QualityReviewInputs:
    plan_variants: dict[str, tuple[JsonObject, JsonObject]] = {}
    for cluster in _object_list(plan.document.get("clusters"), "plan clusters"):
        for index, reference in enumerate(
            _object_list(cluster.get("references"), "plan references"), start=1
        ):
            variant_id = f"{cluster['cluster_id']}-anchor-{index}"
            plan_variants[variant_id] = (cluster, reference)
    evaluation_variants = evaluation.document.get("variants")
    if not isinstance(evaluation_variants, list) or not evaluation_variants:
        raise SourceReferenceQualityError("Evaluation variants must be non-empty")
    if len(evaluation_variants) != len(plan_variants):
        raise SourceReferenceQualityError(
            "Evaluation does not cover every source-reference plan variant"
        )
    return _QualityReviewInputs(
        plan, evaluation, generation, plan_variants, evaluation_variants
    )


def _stage_quality_review(
    output: Path,
    portrait_directory: Path | None,
    inputs: _QualityReviewInputs,
) -> SourceReferenceQualityResult:
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshots = [
        (inputs.plan.path, inputs.plan.sha256),
        (inputs.evaluation.path, inputs.evaluation.sha256),
        (inputs.evaluation.queue_path, inputs.evaluation.queue_sha256),
        (inputs.generation.path, inputs.generation.sha256),
    ]
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        cards, generated_count, excluded_count, seen_variants = _stage_quality_cards(
            staging, portrait_directory, inputs, snapshots
        )
        if seen_variants != set(inputs.plan_variants):
            raise SourceReferenceQualityError(
                "Evaluation variant inventory does not match the plan"
            )
        session = _quality_review_session(inputs, cards)
        review_path = staging / "review.json"
        atomic_write_json(review_path, session, sort_keys=True)
        load_source_reference_quality_review(review_path)
        _verify_quality_review_snapshots(snapshots)
        rename_directory_no_replace(staging, output)
        return SourceReferenceQualityResult(
            output, len(cards), generated_count, excluded_count
        )


def _stage_quality_cards(
    staging: Path,
    portrait_directory: Path | None,
    inputs: _QualityReviewInputs,
    snapshots: MutableSequence[tuple[Path, str]],
) -> tuple[list[JsonObject], int, int, set[str]]:
    cards: list[JsonObject] = []
    seen_variants: set[str] = set()
    generated_count = 0
    excluded_count = 0
    for position, variant in enumerate(inputs.evaluation_variants, start=1):
        card, generated, excluded, variant_id = _stage_quality_card(
            position,
            variant,
            staging,
            portrait_directory,
            inputs,
            snapshots,
            seen_variants,
        )
        cards.append(card)
        seen_variants.add(variant_id)
        generated_count += generated
        excluded_count += excluded
    return cards, generated_count, excluded_count, seen_variants


def _stage_quality_card(
    position: int,
    variant: object,
    staging: Path,
    portrait_directory: Path | None,
    inputs: _QualityReviewInputs,
    snapshots: MutableSequence[tuple[Path, str]],
    seen_variants: set[str],
) -> tuple[JsonObject, int, int, str]:
    if not isinstance(variant, dict):
        raise SourceReferenceQualityError(
            f"Evaluation variant {position} must be an object"
        )
    variant_id = _required_text(
        variant.get("variant_id"), f"evaluation variant {position} ID"
    )
    if variant_id in seen_variants or variant_id not in inputs.plan_variants:
        raise SourceReferenceQualityError(
            f"Evaluation variant identity is invalid: {variant_id}"
        )
    cluster, reference = inputs.plan_variants[variant_id]
    _validate_variant_identity(variant, cluster, reference, variant_id)
    reference_record = _stage_reference_audio(
        inputs.evaluation.directory, variant, variant_id, staging, snapshots
    )
    portrait_image = _copy_optional_portrait(
        portrait_directory,
        cluster.get("portrait"),
        variant_id,
        staging,
        snapshots,
    )
    generated, excluded, contexts = _stage_quality_outcomes(
        variant_id, variant, cluster, staging, inputs, snapshots
    )
    return (
        {
            "variant_id": variant_id,
            "cluster_id": cluster["cluster_id"],
            "character": cluster["character"],
            "portrait": cluster["portrait"],
            "portrait_image": portrait_image,
            "source_bank": cluster["source_bank"],
            "media_id": reference["media_id"],
            "affected_queue_item_count": len(
                _object_list(cluster.get("queue_items"), "plan queue items")
            ),
            "reference": reference_record,
            "decision_context": _quality_decision_context(contexts),
            "generated_samples": generated,
            "excluded_results": excluded,
            "decision": None,
        },
        len(generated),
        len(excluded),
        variant_id,
    )


def _stage_reference_audio(
    evaluation_directory: Path,
    variant: JsonObject,
    variant_id: str,
    staging: Path,
    snapshots: MutableSequence[tuple[Path, str]],
) -> JsonObject:
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
    relative = Path("audio") / variant_id / f"reference{source.suffix.lower()}"
    record = _copy_audio(source, source_sha256, staging / relative)
    record["audio"] = relative.as_posix()
    return record


def _stage_quality_outcomes(
    variant_id: str,
    variant: JsonObject,
    cluster: JsonObject,
    staging: Path,
    inputs: _QualityReviewInputs,
    snapshots: MutableSequence[tuple[Path, str]],
) -> tuple[list[JsonObject], list[JsonObject], list[JsonObject]]:
    contexts: list[JsonObject] = []
    try:
        queue_ids = _variant_evaluation_queue_ids(variant, variant_id)
    except SourceReferenceReviewError as error:
        raise SourceReferenceQualityError(str(error)) from error
    outcomes = []
    for item_index, (expected_kind, queue_id) in enumerate(queue_ids, start=1):
        item = inputs.generation.queue_by_id.get(queue_id)
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
        result = inputs.generation.state_items.get(queue_id)
        if isinstance(result, dict):
            contexts.append(
                {
                    "backend": result.get("provider"),
                    "model": result.get("model"),
                    "generation_profile": result.get("generation_profile"),
                    "seed": result.get("seed"),
                }
            )
        outcomes.append(
            (
                {
                    "queue_id": queue_id,
                    "evaluation_kind": expected_kind,
                    "text": item.text,
                    "text_sha256": item.text_sha256,
                },
                result,
                Path("audio") / variant_id / f"generated-{item_index:02d}.wav",
            )
        )
    generated, excluded = capture_quality_outcomes(
        outcomes,
        inputs.generation.path.parent,
        staging,
        snapshots,
    )
    return generated, excluded, contexts


def _quality_review_session(
    inputs: _QualityReviewInputs, cards: list[JsonObject]
) -> JsonObject:
    now = _utc_now()
    return {
        "schema": QUALITY_REVIEW_SCHEMA,
        "schema_version": QUALITY_REVIEW_VERSION,
        "created_at": now,
        "updated_at": now,
        "source_reference_plan_sha256": inputs.plan.sha256,
        "source_reference_evaluation_sha256": inputs.evaluation.sha256,
        "generation_state_sha256": inputs.generation.sha256,
        "variant_count": len(cards),
        "completed_count": 0,
        "variants": cards,
    }


def _verify_quality_review_snapshots(
    snapshots: MutableSequence[tuple[Path, str]],
) -> None:
    for source, digest in snapshots:
        if sha256_file(source) != digest:
            raise SourceReferenceQualityError(
                f"Source changed during quality review publication: {source.name}"
            )


def create_parser() -> argparse.ArgumentParser:
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


def main(argv: list[str] | None = None) -> int:
    options = create_parser().parse_args(argv)
    try:
        if options.command == "create-composite":
            from vntts.authoring.reference_composite import (
                publish_composite_quality_review,
            )

            result = publish_composite_quality_review(
                options.composite, options.state, options.output
            )
            return _exit_code(
                cli_success(f"Created composite quality review: {result.session}")
            )
        if options.command == "create":
            result = publish_source_reference_quality_review(
                options.plan,
                options.evaluation,
                options.state,
                options.output,
                portrait_directory=options.portrait_directory,
            )
            return _exit_code(
                cli_success(
                    f"Created source-reference quality review: {result.session}"
                )
            )
        if options.command == "ui":
            from vntts.authoring.source_reference_quality_ui import (
                launch_source_reference_quality_review,
            )

            return _exit_code(launch_source_reference_quality_review(options.session))
        session = load_source_reference_quality_review(options.session)
        if options.command == "status":
            completed, total = quality_review_progress(session)
            accepted = len(
                accepted_source_reference_variants(session, require_complete=False)
            )
            return _exit_code(
                cli_success(
                    f"Source-reference review: {completed}/{total}; accepted {accepted}"
                )
            )
        if options.command == "next":
            card = next_pending_quality_variant(session)
            if card is None:
                return _exit_code(
                    cli_success("Source-reference quality review is complete")
                )
            print(json.dumps(card, ensure_ascii=False, indent=2))
            return 0
        updated = record_source_reference_quality_decision(
            options.session,
            options.variant_id,
            options.decision,
            overwrite=options.overwrite,
        )
        completed, total = quality_review_progress(updated)
        return _exit_code(
            cli_success(f"Saved {options.variant_id}; progress: {completed}/{total}")
        )
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("PySide6"):
            return _exit_code(cli_error("Qt UI is not installed"))
        raise
    except (SourceReferenceQualityError, OSError, json.JSONDecodeError) as error:
        return _exit_code(cli_error(error))


def _quality_decision_context(values: list[JsonObject]) -> JsonObject:
    def shared(field: str) -> object:
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


def _object_field(document: JsonObject, field: str, label: str) -> JsonObject:
    value = document.get(field)
    if not isinstance(value, dict):
        raise SourceReferenceQualityError(f"Source-reference {label} are invalid")
    return value


def _object_list(value: object, label: str) -> list[JsonObject]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SourceReferenceQualityError(f"Source-reference {label} are invalid")
    return value


def _validate_variant_identity(
    variant: JsonObject,
    cluster: JsonObject,
    reference: JsonObject,
    variant_id: str,
) -> None:
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


def _copy_optional_portrait(
    root: Path | None,
    portrait: object,
    variant_id: str,
    staging: Path,
    snapshots: MutableSequence[tuple[Path, str]],
) -> JsonObject | None:
    if root is None or portrait is None:
        return None
    portrait_text = _required_text(portrait, f"variant {variant_id} portrait")
    if "\\" in portrait_text:
        raise SourceReferenceQualityError("Portrait identity must be a filename")
    identity = PurePosixPath(portrait_text)
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


def _exit_code(value: object) -> int:
    if not isinstance(value, int):
        raise RuntimeError(
            "Source-reference quality CLI returned a non-integer exit code"
        )
    return value


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
