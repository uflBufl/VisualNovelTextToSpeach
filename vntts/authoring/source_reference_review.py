"""Import extractor-owned source-reference decisions without flattening variants."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts import expected_voice_generation_queue_id
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import normalize_character_name

from vntts.authoring.game_pack import _rename_directory_no_replace

SOURCE_REPORT_SCHEMA = "r1999.story-voice-reference-candidates"
SOURCE_REPORT_VERSION = 1
SOURCE_REVIEW_SCHEMA = "r1999.story-voice-reference-review"
SOURCE_REVIEW_VERSIONS = frozenset({1, 2})
REFERENCE_PLAN_SCHEMA = "vntts.authoring-source-reference-plan"
REFERENCE_PLAN_VERSION = 1
REFERENCE_DECISIONS = frozenset({"accept", "reject", "uncertain"})
FIXED_EVALUATION_CORPUS = (
    "I knew this path would be difficult, but I chose it anyway.",
    "Wait. Did you hear that behind us?",
    "No, I won't let fear decide what happens next.",
)


class SourceReferenceReviewError(RuntimeError):
    """Extractor source-reference evidence cannot be imported safely."""


@dataclass(frozen=True)
class SourceReferencePlanResult:
    directory: Path
    accepted_clusters: int
    accepted_candidates: int
    mapped_queue_items: int
    pending_candidates: int

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "accepted_clusters": self.accepted_clusters,
            "accepted_candidates": self.accepted_candidates,
            "mapped_queue_items": self.mapped_queue_items,
            "pending_candidates": self.pending_candidates,
        }


def import_source_reference_review(report_path, review_path, story_index_path, output):
    """Publish a no-overwrite self-contained plan from exact extractor evidence."""
    report_path, report_payload, report = _read_json(report_path, "candidate report")
    review_path, review_payload, review = _read_json(review_path, "candidate review")
    story_index_path = Path(story_index_path).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise SourceReferenceReviewError(
            f"Source-reference plan output exists: {output}"
        )
    if (
        report.get("schema") != SOURCE_REPORT_SCHEMA
        or report.get("schema_version") != SOURCE_REPORT_VERSION
    ):
        raise SourceReferenceReviewError(
            "Unsupported extractor candidate report schema"
        )
    if (
        review.get("schema") != SOURCE_REVIEW_SCHEMA
        or review.get("schema_version") not in SOURCE_REVIEW_VERSIONS
    ):
        raise SourceReferenceReviewError(
            "Unsupported extractor candidate review schema"
        )
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    review_sha256 = hashlib.sha256(review_payload).hexdigest()
    declared_report_sha256 = _sha256(
        review.get("candidate_report_sha256"), "Review candidate-report hash"
    )
    if review["schema_version"] == 1 and declared_report_sha256 != report_sha256:
        raise SourceReferenceReviewError(
            "Legacy source-reference review belongs to a different candidate report"
        )
    candidates = _load_candidates(report_path, report)
    decisions, invalidated = _load_decisions(review, candidates)
    try:
        story = load_story_index_document(story_index_path)
    except StoryIndexError as error:
        raise SourceReferenceReviewError(str(error)) from error
    try:
        story_payload = story_index_path.read_bytes()
    except OSError as error:
        raise SourceReferenceReviewError(
            f"Unable to read story index {story_index_path}: {error}"
        ) from error
    story_sha256 = hashlib.sha256(story_payload).hexdigest()

    accepted = [
        candidate
        for candidate in candidates.values()
        if decisions.get(candidate["candidate_key"], {}).get("decision") == "accept"
    ]
    groups = {}
    for candidate in accepted:
        identity = (
            candidate["character"],
            candidate["portrait"],
            candidate["source_bank"],
        )
        groups.setdefault(identity, []).append(candidate)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        clusters = []
        copied_sources = []
        mapped_queue_ids = set()
        for identity, members in sorted(
            groups.items(),
            key=lambda value: tuple(str(part or "").casefold() for part in value[0]),
        ):
            character, portrait, bank = identity
            cluster_id = _cluster_id(character, portrait, bank)
            references = []
            for index, candidate in enumerate(members, start=1):
                suffix = candidate["reference_path"].suffix.lower() or ".wav"
                relative = (
                    Path("references")
                    / cluster_id
                    / f"{index:02d}-{candidate['media_id']}{suffix}"
                )
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = candidate["reference_payload"]
                destination.write_bytes(payload)
                if sha256_file(destination) != candidate["reference_sha256"]:
                    raise SourceReferenceReviewError(
                        f"Copied reference checksum changed: {candidate['reference_relative']}"
                    )
                references.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": candidate["reference_sha256"],
                        "candidate_key": candidate["candidate_key"],
                        "candidate_evidence_sha256": candidate["evidence_sha256"],
                        "media_id": candidate["media_id"],
                        "source_reference": candidate["reference_relative"],
                    }
                )
                copied_sources.append(candidate)
            queue_items = _queue_items_for_cluster(story, identity)
            mapped_queue_ids.update(item["queue_id"] for item in queue_items)
            clusters.append(
                {
                    "cluster_id": cluster_id,
                    "character": character,
                    "portrait": portrait,
                    "source_bank": bank,
                    "references": references,
                    "queue_items": queue_items,
                    "mapped_queue_item_count": len(queue_items),
                    "requires_generated_quality_review": True,
                }
            )

        decision_counts = {
            value: sum(item.get("decision") == value for item in decisions.values())
            for value in sorted(REFERENCE_DECISIONS)
        }
        plan = {
            "schema": REFERENCE_PLAN_SCHEMA,
            "schema_version": REFERENCE_PLAN_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sources": {
                "candidate_report": str(report_path),
                "candidate_report_sha256": report_sha256,
                "candidate_review": str(review_path),
                "candidate_review_sha256": review_sha256,
                "story_index": str(story_index_path),
                "story_index_sha256": story_sha256,
            },
            "summary": {
                "candidate_count": len(candidates),
                "decision_counts": decision_counts,
                "pending_candidates": len(candidates) - len(decisions),
                "invalidated_decisions": len(invalidated),
                "accepted_cluster_count": len(clusters),
                "mapped_queue_item_count": len(mapped_queue_ids),
            },
            "clusters": clusters,
            "invalidated_decisions": invalidated,
            "fixed_evaluation_corpus": [
                {
                    "evaluation_id": f"source-reference-eval-{index}",
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
                for index, text in enumerate(FIXED_EVALUATION_CORPUS, start=1)
            ],
            "authority": (
                "Accepted source anchors are not generated-voice approval. Preserve exact "
                "clusters and queue IDs; run the fixed evaluation corpus and blind A/B gate "
                "before using a cluster for bulk generation."
            ),
        }
        atomic_write_json(staging / "plan.json", plan)
        _assert_source_unchanged(report_path, report_sha256, "candidate report")
        _assert_source_unchanged(review_path, review_sha256, "candidate review")
        _assert_source_unchanged(story_index_path, story_sha256, "story index")
        for candidate in copied_sources:
            _assert_source_unchanged(
                candidate["reference_path"],
                candidate["reference_sha256"],
                f"candidate reference {candidate['reference_relative']}",
            )
        _rename_directory_no_replace(staging, output)
        return SourceReferencePlanResult(
            output,
            len(clusters),
            len(accepted),
            len(mapped_queue_ids),
            len(candidates) - len(decisions),
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_source_reference_plan(directory):
    """Validate a published plan and every copied reference checksum."""
    directory = Path(directory).expanduser().resolve()
    plan_path, _payload, plan = _read_json(
        directory / "plan.json", "source-reference plan"
    )
    if plan_path.parent != directory:
        raise SourceReferenceReviewError("Source-reference plan path is inconsistent")
    if (
        plan.get("schema") != REFERENCE_PLAN_SCHEMA
        or plan.get("schema_version") != REFERENCE_PLAN_VERSION
    ):
        raise SourceReferenceReviewError("Unsupported source-reference plan schema")
    clusters = plan.get("clusters")
    if not isinstance(clusters, list):
        raise SourceReferenceReviewError(
            "Source-reference plan clusters must be a list"
        )
    seen_clusters = set()
    seen_queue_ids = set()
    for cluster_index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            raise SourceReferenceReviewError(
                f"Plan cluster {cluster_index} must be an object"
            )
        cluster_id = _text(cluster.get("cluster_id"), f"cluster {cluster_index} ID")
        if cluster_id in seen_clusters:
            raise SourceReferenceReviewError(
                f"Duplicate source-reference cluster: {cluster_id}"
            )
        seen_clusters.add(cluster_id)
        references = cluster.get("references")
        if not isinstance(references, list) or not references:
            raise SourceReferenceReviewError(
                f"Plan cluster {cluster_index} has no references"
            )
        for reference_index, reference in enumerate(references):
            if not isinstance(reference, dict):
                raise SourceReferenceReviewError(
                    f"Plan reference {cluster_index}:{reference_index} must be an object"
                )
            relative = _text(
                reference.get("path"),
                f"reference {cluster_index}:{reference_index} path",
            )
            path = _contained_file(directory, relative)
            expected = _sha256(
                reference.get("sha256"),
                f"reference {cluster_index}:{reference_index} hash",
            )
            if sha256_file(path) != expected:
                raise SourceReferenceReviewError(f"Plan reference changed: {relative}")
        queue_items = cluster.get("queue_items")
        if not isinstance(queue_items, list):
            raise SourceReferenceReviewError(
                f"Plan cluster {cluster_index} queue_items must be a list"
            )
        for item in queue_items:
            if not isinstance(item, dict):
                raise SourceReferenceReviewError(
                    f"Plan cluster {cluster_index} queue item is invalid"
                )
            queue_id = _text(item.get("queue_id"), "plan queue ID")
            if queue_id in seen_queue_ids:
                raise SourceReferenceReviewError(
                    f"Queue ID belongs to multiple source-reference clusters: {queue_id}"
                )
            seen_queue_ids.add(queue_id)
    return plan


def _load_candidates(report_path, report):
    values = report.get("candidates")
    if not isinstance(values, list) or not values:
        raise SourceReferenceReviewError("Candidate report contains no candidates")
    root = report_path.parent.resolve()
    candidates = {}
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise SourceReferenceReviewError(f"Candidate {index} must be an object")
        character = _text(value.get("character"), f"candidate {index} character")
        portrait = value.get("portrait")
        if portrait is not None and (
            not isinstance(portrait, str) or not portrait.strip()
        ):
            raise SourceReferenceReviewError(f"Candidate {index} portrait is invalid")
        bank = _text(value.get("source_bank"), f"candidate {index} bank")
        media_id = value.get("media_id")
        if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 0:
            raise SourceReferenceReviewError(f"Candidate {index} media ID is invalid")
        relative = _text(value.get("reference"), f"candidate {index} reference")
        reference_path = _contained_file(root, relative)
        payload = reference_path.read_bytes()
        reference_sha256 = _sha256(
            value.get("reference_sha256"), f"candidate {index} reference hash"
        )
        if hashlib.sha256(payload).hexdigest() != reference_sha256:
            raise SourceReferenceReviewError(
                f"Candidate {index} reference checksum changed"
            )
        candidate_key = _candidate_key(
            character, portrait, bank, media_id, reference_sha256
        )
        if candidate_key in candidates:
            raise SourceReferenceReviewError(
                f"Duplicate candidate identity: {candidate_key}"
            )
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        candidates[candidate_key] = {
            "candidate_key": candidate_key,
            "evidence_sha256": evidence_sha256,
            "character": character,
            "portrait": portrait,
            "source_bank": bank,
            "media_id": media_id,
            "reference_relative": relative,
            "reference_path": reference_path,
            "reference_payload": payload,
            "reference_sha256": reference_sha256,
        }
    return candidates


def _load_decisions(review, candidates):
    values = review.get("decisions")
    if not isinstance(values, list):
        raise SourceReferenceReviewError("Candidate review decisions must be a list")
    decisions = {}
    invalidated = []
    version = review["schema_version"]
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise SourceReferenceReviewError(
                f"Review decision {index} must be an object"
            )
        key = _text(value.get("candidate_key"), f"decision {index} key")
        if key in decisions:
            raise SourceReferenceReviewError(f"Review decision {index} is duplicated")
        if value.get("decision") not in REFERENCE_DECISIONS:
            raise SourceReferenceReviewError(
                f"Review decision {index} value is invalid"
            )
        candidate = candidates.get(key)
        if candidate is None:
            if version == 1:
                raise SourceReferenceReviewError(
                    f"Review decision {index} candidate is absent"
                )
            invalidated.append(value)
            continue
        if (
            _sha256(value.get("reference_sha256"), f"decision {index} reference hash")
            != candidate["reference_sha256"]
        ):
            raise SourceReferenceReviewError(
                f"Review decision {index} reference changed"
            )
        if (
            version == 2
            and _sha256(
                value.get("candidate_evidence_sha256"),
                f"decision {index} evidence hash",
            )
            != candidate["evidence_sha256"]
        ):
            invalidated.append(value)
            continue
        decisions[key] = value
    archived = review.get("invalidated_decisions", []) if version == 2 else []
    if not isinstance(archived, list) or any(
        not isinstance(value, dict) for value in archived
    ):
        raise SourceReferenceReviewError("Review invalidated_decisions must be a list")
    invalidated.extend(archived)
    return decisions, invalidated


def _queue_items_for_cluster(story, identity):
    character, portrait, _bank = identity
    target = normalize_character_name(character)
    values = []
    for record in story.records:
        record_portrait = record.producer_fields.get("portrait")
        if (
            normalize_character_name(record.voice_character) != target
            or record_portrait != portrait
            or not record.speakable
            or record.source_audio_status == "available"
        ):
            continue
        values.append(
            {
                "queue_id": expected_voice_generation_queue_id(
                    record.line_id, record.text_sha256
                ),
                "line_id": record.line_id,
                "text_sha256": record.text_sha256,
                "collection_id": record.collection_id,
                "speaker": record.speaker,
                "voice_character": record.voice_character,
                "portrait": record_portrait,
            }
        )
    return values


def _candidate_key(character, portrait, bank, media_id, reference_sha256):
    identity = json.dumps(
        [character, portrait, bank, media_id, reference_sha256],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _cluster_id(character, portrait, bank):
    identity = json.dumps(
        [character, portrait, bank], ensure_ascii=False, separators=(",", ":")
    )
    return f"cluster-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def _read_json(path, label):
    path = Path(path).expanduser().resolve()
    try:
        payload = path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceReferenceReviewError(
            f"Unable to read {label} {path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise SourceReferenceReviewError(f"{label.title()} must be a JSON object")
    return path, payload, document


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SourceReferenceReviewError(f"{label} must be non-empty text")
    return value.strip()


def _sha256(value, label):
    value = _text(value, label)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise SourceReferenceReviewError(f"{label} must be lowercase SHA-256")
    return value


def _contained_file(root, relative):
    relative = _text(relative, "Reference path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise SourceReferenceReviewError(f"Reference path is not contained: {relative}")
    cursor = Path(root).resolve()
    for part in pure.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SourceReferenceReviewError(
                f"Reference path uses a symlink: {relative}"
            )
    resolved = cursor.resolve()
    try:
        resolved.relative_to(Path(root).resolve())
    except ValueError as error:
        raise SourceReferenceReviewError(
            f"Reference path escapes its root: {relative}"
        ) from error
    if not resolved.is_file():
        raise SourceReferenceReviewError(f"Reference file is missing: {relative}")
    return resolved


def _assert_source_unchanged(path, expected_sha256, label):
    try:
        current = sha256_file(path)
    except OSError as error:
        raise SourceReferenceReviewError(
            f"Unable to recheck {label}: {error}"
        ) from error
    if current != expected_sha256:
        raise SourceReferenceReviewError(f"{label.title()} changed during import")
