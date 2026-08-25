"""Import extractor-owned source-reference decisions without flattening variants."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from vntts_artifacts import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    expected_voice_generation_queue_id,
    write_voice_generation_queue,
)
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.story_index import StoryIndexError, load_story_index_document
from vntts_artifacts.voice_manifest import (
    VoiceManifestError,
    load_voice_manifest,
    normalize_character_name,
    write_voice_manifest,
)

from vntts.authoring.bulk_generation import BulkGenerationError, load_generation_state
from vntts.authoring.game_pack import _rename_directory_no_replace
from vntts.authoring.listening import (
    ModelListeningError,
    create_listening_session_from_reports,
    load_listening_session,
)
from vntts.authoring.source_reference_bindings import (
    SOURCE_REFERENCE_BINDINGS_FIELD,
    SOURCE_REFERENCE_BINDINGS_SCHEMA,
    SOURCE_REFERENCE_BINDINGS_VERSION,
    queue_voice_overrides_sha256,
)

SOURCE_REPORT_SCHEMA = "r1999.story-voice-reference-candidates"
SOURCE_REPORT_VERSIONS = frozenset({1, 2})
SOURCE_ORIGIN_STORY_LINE = "story_line_route"
SOURCE_ORIGIN_EXACT_BANK = "exact_bank_unrouted_media"
SOURCE_REVIEW_SCHEMA = "r1999.story-voice-reference-review"
SOURCE_REVIEW_VERSIONS = frozenset({1, 2})
REFERENCE_PLAN_SCHEMA = "vntts.authoring-source-reference-plan"
REFERENCE_PLAN_VERSION = 1
REFERENCE_EVALUATION_SCHEMA = "vntts.authoring-source-reference-evaluation"
REFERENCE_EVALUATION_VERSION = 1
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


@dataclass(frozen=True)
class SourceReferenceEvaluationResult:
    directory: Path
    variants: int
    queue_items: int

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "variants": self.variants,
            "queue_items": self.queue_items,
        }


@dataclass(frozen=True)
class SourceReferenceListeningReportsResult:
    directory: Path
    reports: int
    samples: int
    blind_trials: int

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "reports": self.reports,
            "samples": self.samples,
            "blind_trials": self.blind_trials,
        }


@dataclass(frozen=True)
class SourceReferenceBindingsResult:
    directory: Path
    selected_variants: int
    bound_queue_items: int

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "manifest": str(self.directory / "voice-manifest.json"),
            "selected_variants": self.selected_variants,
            "bound_queue_items": self.bound_queue_items,
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
        or report.get("schema_version") not in SOURCE_REPORT_VERSIONS
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
                        "candidate_origin": candidate["candidate_origin"],
                        "source_event_ids": list(candidate["source_event_ids"]),
                        "source_reference": candidate["reference_relative"],
                        "source_transcripts": list(candidate["transcripts"]),
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


def publish_source_reference_bindings(
    plan_directory,
    base_voice_manifest,
    narrator_character,
    selected_variant_ids,
    output,
    *,
    quality_review=None,
    base_characters=(),
):
    """Publish a partial manifest with explicit queue-to-variant bindings."""
    plan_directory = Path(plan_directory).expanduser().resolve()
    plan = load_source_reference_plan(plan_directory)
    plan_path = plan_directory / "plan.json"
    plan_sha256 = sha256_file(plan_path)
    quality_review_path = None
    quality_review_sha256 = None
    if quality_review is not None:
        if selected_variant_ids is not None:
            raise SourceReferenceReviewError(
                "Quality-reviewed bindings cannot also accept variant IDs"
            )
        from vntts.authoring.source_reference_quality import (
            SourceReferenceQualityError,
            accepted_source_reference_variants,
            load_source_reference_quality_review,
        )

        quality_review_path = Path(quality_review).expanduser().resolve()
        try:
            quality_review_payload = quality_review_path.read_bytes()
            quality_review_document = load_source_reference_quality_review(
                quality_review_path
            )
            if quality_review_path.read_bytes() != quality_review_payload:
                raise SourceReferenceReviewError(
                    "Source-reference quality review changed while it was loaded"
                )
            if quality_review_document["source_reference_plan_sha256"] != plan_sha256:
                raise SourceReferenceReviewError(
                    "Quality review belongs to a different source-reference plan"
                )
            selected_variant_ids = accepted_source_reference_variants(
                quality_review_document
            )
        except (OSError, SourceReferenceQualityError) as error:
            raise SourceReferenceReviewError(str(error)) from error
        if not selected_variant_ids:
            raise SourceReferenceReviewError(
                "Completed quality review accepts no source-reference variants"
            )
        quality_review_sha256 = hashlib.sha256(quality_review_payload).hexdigest()
    base_voice_manifest = Path(base_voice_manifest).expanduser().resolve()
    try:
        base_payload = base_voice_manifest.read_bytes()
        base_document, base_voices = load_voice_manifest(
            base_voice_manifest, allow_legacy=False
        )
    except (OSError, VoiceManifestError) as error:
        raise SourceReferenceReviewError(str(error)) from error
    base_sha256 = hashlib.sha256(base_payload).hexdigest()
    narrator_character = _text(narrator_character, "Narrator character")
    narrator = next(
        (
            voice
            for voice in base_voices
            if normalize_character_name(voice.character)
            == normalize_character_name(narrator_character)
        ),
        None,
    )
    if narrator is None or not narrator.references:
        raise SourceReferenceReviewError(
            f"Narrator character has no references: {narrator_character}"
        )
    requested_base_characters = tuple(
        _text(value, "Included base character") for value in base_characters
    )
    normalized_base_characters = tuple(
        normalize_character_name(value) for value in requested_base_characters
    )
    if len(normalized_base_characters) != len(set(normalized_base_characters)):
        raise SourceReferenceReviewError("Included base characters must be distinct")
    normalized_narrator = normalize_character_name(narrator.character)
    if normalized_narrator in normalized_base_characters:
        raise SourceReferenceReviewError(
            "Narrator is already included and must not be repeated as a base character"
        )
    base_voices_by_character = {
        normalize_character_name(voice.character): voice for voice in base_voices
    }
    included_base_voices = []
    for character, normalized in zip(
        requested_base_characters, normalized_base_characters, strict=True
    ):
        voice = base_voices_by_character.get(normalized)
        if voice is None or not voice.references:
            raise SourceReferenceReviewError(
                f"Included base character has no references: {character}"
            )
        included_base_voices.append(voice)
    requested_variants = tuple(
        _text(value, "Selected source-reference variant")
        for value in selected_variant_ids
    )
    if not requested_variants or len(requested_variants) != len(
        set(requested_variants)
    ):
        raise SourceReferenceReviewError(
            "Select one or more distinct source-reference variants"
        )
    available = {}
    for cluster in plan["clusters"]:
        for reference_index, reference in enumerate(cluster["references"], start=1):
            variant_id = f"{cluster['cluster_id']}-anchor-{reference_index}"
            available[variant_id] = (cluster, reference)
    unknown = set(requested_variants) - set(available)
    if unknown:
        raise SourceReferenceReviewError(
            "Selected source-reference variants are absent from the plan: "
            + ", ".join(sorted(unknown))
        )
    selected_clusters = [
        available[value][0]["cluster_id"] for value in requested_variants
    ]
    if len(selected_clusters) != len(set(selected_clusters)):
        raise SourceReferenceReviewError(
            "Select at most one source-reference variant per portrait cluster"
        )

    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise SourceReferenceReviewError(
            f"Source-reference bindings output exists: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    source_snapshots = []
    try:
        voices = []
        narrator_references = []
        for index, relative in enumerate(narrator.references, start=1):
            source = _contained_file(base_voice_manifest.parent, relative)
            digest = sha256_file(source)
            suffix = source.suffix.lower() or ".wav"
            target_relative = Path("references") / "narrator" / f"{index:02d}{suffix}"
            target = staging / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if sha256_file(target) != digest:
                raise SourceReferenceReviewError(
                    "Narrator reference changed while copied"
                )
            narrator_references.append(target_relative.as_posix())
            source_snapshots.append((source, digest))
        voices.append(
            {
                "character": narrator.character,
                "speaker": narrator.speaker,
                "aliases": list(narrator.aliases),
                "references": narrator_references,
            }
        )

        included_base_characters = []
        for voice_index, voice in enumerate(included_base_voices, start=1):
            copied_references = []
            reference_sha256s = []
            for reference_index, relative in enumerate(voice.references, start=1):
                source = _contained_file(base_voice_manifest.parent, relative)
                digest = sha256_file(source)
                suffix = source.suffix.lower() or ".wav"
                target_relative = (
                    Path("references")
                    / "base"
                    / f"{voice_index:02d}"
                    / f"{reference_index:02d}{suffix}"
                )
                target = staging / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if sha256_file(target) != digest:
                    raise SourceReferenceReviewError(
                        f"Base voice reference changed while copied: {voice.character}"
                    )
                copied_references.append(target_relative.as_posix())
                reference_sha256s.append(digest)
                source_snapshots.append((source, digest))
            voices.append(
                {
                    "character": voice.character,
                    "speaker": voice.speaker,
                    "aliases": list(voice.aliases),
                    "references": copied_references,
                }
            )
            included_base_characters.append(
                {
                    "character": voice.character,
                    "reference_sha256s": reference_sha256s,
                }
            )

        queue_overrides = {}
        selected_variants = []
        for variant_id in requested_variants:
            cluster, reference = available[variant_id]
            source = _contained_file(plan_directory, reference["path"])
            digest = _sha256(
                reference.get("sha256"), f"variant {variant_id} reference hash"
            )
            if sha256_file(source) != digest:
                raise SourceReferenceReviewError(
                    f"Source-reference plan artifact changed: {variant_id}"
                )
            suffix = source.suffix.lower() or ".wav"
            target_relative = Path("references") / variant_id / f"source{suffix}"
            target = staging / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if sha256_file(target) != digest:
                raise SourceReferenceReviewError(
                    f"Source-reference variant changed while copied: {variant_id}"
                )
            source_snapshots.append((source, digest))
            voice_character = f"Source reference {cluster['character']} {variant_id}"
            voices.append(
                {
                    "character": voice_character,
                    "speaker": f"source-reference:{variant_id}",
                    "references": [target_relative.as_posix()],
                }
            )
            queue_ids = []
            for item in cluster["queue_items"]:
                queue_id = _text(item.get("queue_id"), "Bound queue ID")
                if queue_id in queue_overrides:
                    raise SourceReferenceReviewError(
                        f"Queue ID belongs to multiple selected variants: {queue_id}"
                    )
                queue_overrides[queue_id] = voice_character
                queue_ids.append(queue_id)
            selected_variants.append(
                {
                    "variant_id": variant_id,
                    "cluster_id": cluster["cluster_id"],
                    "character": cluster["character"],
                    "portrait": cluster["portrait"],
                    "source_bank": cluster["source_bank"],
                    "voice_character": voice_character,
                    "reference_sha256": digest,
                    "queue_ids": queue_ids,
                }
            )
        bindings = {
            "schema": SOURCE_REFERENCE_BINDINGS_SCHEMA,
            "schema_version": SOURCE_REFERENCE_BINDINGS_VERSION,
            "source_reference_plan_sha256": plan_sha256,
            "selected_variants": selected_variants,
            "queue_voice_overrides": dict(sorted(queue_overrides.items())),
            "queue_voice_overrides_sha256": queue_voice_overrides_sha256(
                queue_overrides
            ),
        }
        if included_base_characters:
            bindings["included_base_characters"] = included_base_characters
        if quality_review_sha256 is not None:
            bindings["source_reference_quality_review_sha256"] = quality_review_sha256
        manifest = {
            "version": 2,
            "game": base_document.get("game"),
            "language": base_document.get("language"),
            "voices": voices,
            SOURCE_REFERENCE_BINDINGS_FIELD: bindings,
        }
        manifest_path = staging / "voice-manifest.json"
        write_voice_manifest(manifest_path, manifest)
        if sha256_file(plan_path) != plan_sha256:
            raise SourceReferenceReviewError(
                "Source-reference plan changed during binding publication"
            )
        if sha256_file(base_voice_manifest) != base_sha256:
            raise SourceReferenceReviewError(
                "Base voice manifest changed during binding publication"
            )
        for source, digest in source_snapshots:
            _assert_source_unchanged(source, digest, f"voice reference {source.name}")
        if (
            quality_review_path is not None
            and sha256_file(quality_review_path) != quality_review_sha256
        ):
            raise SourceReferenceReviewError(
                "Source-reference quality review changed during binding publication"
            )
        _rename_directory_no_replace(staging, output)
        return SourceReferenceBindingsResult(
            output, len(selected_variants), len(queue_overrides)
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def publish_source_reference_evaluation(plan_directory, output):
    """Publish self-contained fixed-corpus inputs for every accepted anchor."""
    plan_directory = Path(plan_directory).expanduser().resolve()
    plan = load_source_reference_plan(plan_directory)
    plan_path = plan_directory / "plan.json"
    plan_sha256 = sha256_file(plan_path)
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise SourceReferenceReviewError(
            f"Source-reference evaluation output exists: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        voices = []
        items = []
        variants = []
        source_snapshots = []
        clusters = sorted(
            plan["clusters"],
            key=lambda cluster: (-len(cluster["queue_items"]), cluster["cluster_id"]),
        )
        for cluster in clusters:
            for reference_index, reference in enumerate(cluster["references"], start=1):
                variant_id = f"{cluster['cluster_id']}-anchor-{reference_index}"
                evaluation_character = (
                    f"Source reference {cluster['character']} {variant_id}"
                )
                source = _contained_file(plan_directory, reference["path"])
                relative = (
                    Path("references")
                    / variant_id
                    / f"source-{reference['media_id']}.wav"
                )
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                payload = source.read_bytes()
                if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
                    raise SourceReferenceReviewError(
                        f"Source-reference plan changed during evaluation: {reference['path']}"
                    )
                destination.write_bytes(payload)
                source_snapshots.append((source, reference["sha256"]))
                voices.append(
                    {
                        "character": evaluation_character,
                        "speaker": f"source-reference:{variant_id}",
                        "references": [relative.as_posix()],
                    }
                )
                source_transcripts = reference.get("source_transcripts")
                if not isinstance(source_transcripts, list) or any(
                    not isinstance(text, str) or not text.strip()
                    for text in source_transcripts
                ):
                    raise SourceReferenceReviewError(
                        f"Source-reference anchor transcripts are invalid: {variant_id}"
                    )
                evaluation_texts = []
                if source_transcripts:
                    evaluation_texts.append(("source-match", source_transcripts[0]))
                evaluation_texts.extend(
                    (f"fixed-{index}", text)
                    for index, text in enumerate(FIXED_EVALUATION_CORPUS, start=1)
                )
                queue_ids = {}
                for evaluation_kind, text in evaluation_texts:
                    text_hash = hashlib.sha256(text.encode()).hexdigest()
                    line_id = f"source-reference:{variant_id}:{evaluation_kind}"
                    queue_id = expected_voice_generation_queue_id(line_id, text_hash)
                    queue_ids[evaluation_kind] = queue_id
                    items.append(
                        {
                            "record_type": "generation_item",
                            "queue_id": queue_id,
                            "line_id": line_id,
                            "text": text,
                            "text_sha256": text_hash,
                            "speaker": cluster["character"],
                            "voice_character": evaluation_character,
                            "source_audio_status": "absent",
                            "source_audio_reason": "source_reference_evaluation",
                            "source_kind": "authoring_evaluation",
                            "action": "generate",
                            "state": "pending",
                            "reference_cluster_id": cluster["cluster_id"],
                            "reference_candidate_key": reference["candidate_key"],
                            "evaluation_kind": evaluation_kind,
                        }
                    )
                variant = {
                    "variant_id": variant_id,
                    "character": cluster["character"],
                    "portrait": cluster["portrait"],
                    "source_bank": cluster["source_bank"],
                    "media_id": reference["media_id"],
                    "source_audio": relative.as_posix(),
                    "source_audio_sha256": reference["sha256"],
                    "fixed_queue_ids": [
                        queue_ids[f"fixed-{index}"]
                        for index in range(1, len(FIXED_EVALUATION_CORPUS) + 1)
                    ],
                    "affected_queue_item_count": len(cluster["queue_items"]),
                    "manual_blind_review_required": True,
                }
                if "source-match" in queue_ids:
                    variant["source_match_queue_id"] = queue_ids["source-match"]
                variants.append(variant)
        manifest_path = staging / "voice-manifest.json"
        write_voice_manifest(
            manifest_path,
            {
                "version": 2,
                "game": "Source reference evaluation",
                "language": "en",
                "voices": voices,
                "vntts.authoring.source_reference_plan_sha256": plan_sha256,
            },
        )
        queue_path = staging / "queue.jsonl"
        write_voice_generation_queue(
            queue_path,
            {
                "game": "Source reference evaluation",
                "language": "en",
                "source_reference_plan_sha256": plan_sha256,
                "variant_count": len(variants),
            },
            items,
        )
        comparison = {
            "schema": REFERENCE_EVALUATION_SCHEMA,
            "schema_version": REFERENCE_EVALUATION_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_reference_plan": str(plan_directory),
            "source_reference_plan_sha256": plan_sha256,
            "voice_manifest": manifest_path.name,
            "voice_manifest_sha256": sha256_file(manifest_path),
            "queue": queue_path.name,
            "queue_sha256": sha256_file(queue_path),
            "variants": variants,
            "review_policy": (
                "Source-match trials compare original and generated audio blindly. "
                "Fixed-corpus trials compare accepted anchors under identical text. "
                "No result authorizes bulk generation until manually approved."
            ),
        }
        atomic_write_json(staging / "comparison.json", comparison)
        load_voice_manifest(manifest_path)
        VoiceGenerationQueue.load(queue_path)
        if sha256_file(plan_path) != plan_sha256:
            raise SourceReferenceReviewError(
                "Source-reference plan changed during evaluation publication"
            )
        for source, expected_sha256 in source_snapshots:
            _assert_source_unchanged(
                source,
                expected_sha256,
                f"source-reference plan artifact {source.name}",
            )
        for variant in variants:
            source = staging / variant["source_audio"]
            if sha256_file(source) != variant["source_audio_sha256"]:
                raise SourceReferenceReviewError(
                    f"Evaluation reference changed before publication: {source}"
                )
        _rename_directory_no_replace(staging, output)
        return SourceReferenceEvaluationResult(output, len(variants), len(items))
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def publish_source_reference_listening_reports(
    evaluation_directory, state_path, output
):
    """Publish strict reports for blind original/generated and variant review."""
    evaluation_directory = Path(evaluation_directory).expanduser().resolve()
    comparison_path, comparison_payload, comparison = _read_json(
        evaluation_directory / "comparison.json", "source-reference evaluation"
    )
    if (
        comparison.get("schema") != REFERENCE_EVALUATION_SCHEMA
        or comparison.get("schema_version") != REFERENCE_EVALUATION_VERSION
    ):
        raise SourceReferenceReviewError(
            "Unsupported source-reference evaluation schema"
        )
    queue_path = _contained_file(
        evaluation_directory,
        _text(comparison.get("queue"), "Evaluation queue path"),
    )
    manifest_path = _contained_file(
        evaluation_directory,
        _text(comparison.get("voice_manifest"), "Evaluation manifest path"),
    )
    queue_sha256 = _sha256(comparison.get("queue_sha256"), "Evaluation queue hash")
    manifest_sha256 = _sha256(
        comparison.get("voice_manifest_sha256"), "Evaluation manifest hash"
    )
    if sha256_file(queue_path) != queue_sha256:
        raise SourceReferenceReviewError("Evaluation queue changed")
    if sha256_file(manifest_path) != manifest_sha256:
        raise SourceReferenceReviewError("Evaluation voice manifest changed")
    try:
        queue = VoiceGenerationQueue.load(queue_path)
        _manifest, voices = load_voice_manifest(manifest_path, allow_legacy=False)
        state = load_generation_state(state_path, queue_path)
    except (
        BulkGenerationError,
        VoiceGenerationQueueError,
        VoiceManifestError,
    ) as error:
        raise SourceReferenceReviewError(str(error)) from error
    comparison_sha256 = hashlib.sha256(comparison_payload).hexdigest()
    state_path = Path(state_path).expanduser().resolve()
    state_sha256 = sha256_file(state_path)
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise SourceReferenceReviewError(
            f"Source-reference listening reports output exists: {output}"
        )

    queue_by_id = {item.queue_id: item for item in queue.items}
    voices_by_character = {voice.character: voice for voice in voices}
    variants = comparison.get("variants")
    if not isinstance(variants, list) or not variants:
        raise SourceReferenceReviewError("Evaluation variants must be a non-empty list")
    originals = []
    generated_reports = []
    checked_audio = []
    seen_variants = set()
    for variant_index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            raise SourceReferenceReviewError(
                f"Evaluation variant {variant_index} must be an object"
            )
        variant_id = _text(
            variant.get("variant_id"), f"evaluation variant {variant_index} ID"
        )
        if variant_id in seen_variants:
            raise SourceReferenceReviewError(
                f"Duplicate evaluation variant: {variant_id}"
            )
        seen_variants.add(variant_id)
        source_relative = _text(
            variant.get("source_audio"), f"variant {variant_id} source"
        )
        source = _contained_file(
            evaluation_directory,
            source_relative,
        )
        source_sha256 = _sha256(
            variant.get("source_audio_sha256"), f"variant {variant_id} source hash"
        )
        if sha256_file(source) != source_sha256:
            raise SourceReferenceReviewError(
                f"Evaluation source audio changed: {variant_id}"
            )
        checked_audio.append((source, source_sha256))
        character = _text(
            variant.get("character"), f"evaluation variant {variant_id} character"
        )
        evaluation_character = f"Source reference {character} {variant_id}"
        voice = voices_by_character.get(evaluation_character)
        if voice is None or voice.references != (source_relative,):
            raise SourceReferenceReviewError(
                f"Evaluation variant voice binding changed: {variant_id}"
            )
        cluster_id, separator, anchor = variant_id.rpartition("-anchor-")
        if not separator or not cluster_id or not anchor.isdigit():
            raise SourceReferenceReviewError(
                f"Evaluation variant ID is invalid: {variant_id}"
            )
        queue_ids = []
        source_match_queue_id = variant.get("source_match_queue_id")
        if source_match_queue_id is not None:
            queue_ids.append(
                (
                    "source-match",
                    _text(
                        source_match_queue_id,
                        f"variant {variant_id} source-match queue ID",
                    ),
                )
            )
        fixed_queue_ids = variant.get("fixed_queue_ids")
        if not isinstance(fixed_queue_ids, list):
            raise SourceReferenceReviewError(
                f"Variant {variant_id} fixed queue IDs must be a list"
            )
        queue_ids.extend(
            (
                f"fixed-{index}",
                _text(value, f"variant {variant_id} fixed queue ID"),
            )
            for index, value in enumerate(fixed_queue_ids, start=1)
        )
        if len(queue_ids) != len({queue_id for _kind, queue_id in queue_ids}):
            raise SourceReferenceReviewError(
                f"Variant {variant_id} evaluation queue IDs are duplicated"
            )
        samples = []
        report_provider = None
        report_model = None
        for position, (expected_kind, queue_id) in enumerate(queue_ids):
            item = queue_by_id.get(queue_id)
            if item is None:
                raise SourceReferenceReviewError(
                    f"Variant {variant_id} queue ID is missing: {queue_id}"
                )
            if (
                item.speaker != character
                or item.voice_character != evaluation_character
                or item.document.get("reference_cluster_id") != cluster_id
                or item.document.get("evaluation_kind") != expected_kind
            ):
                raise SourceReferenceReviewError(
                    f"Variant {variant_id} queue binding changed: {queue_id}"
                )
            result = state["items"].get(queue_id)
            if not isinstance(result, dict) or result.get("status") not in {
                "generated",
                "approved",
            }:
                continue
            relative_audio = _text(
                result.get("path"), f"generated result {queue_id} path"
            )
            audio = _contained_file(state_path.parent, relative_audio)
            audio_sha256 = _sha256(
                result.get("file_sha256"), f"generated result {queue_id} hash"
            )
            if sha256_file(audio) != audio_sha256:
                raise SourceReferenceReviewError(
                    f"Generated evaluation audio changed: {queue_id}"
                )
            checked_audio.append((audio, audio_sha256))
            sample_id = (
                f"source-match:{variant_id}"
                if expected_kind == "source-match"
                else expected_kind
            )
            sample = {
                "id": sample_id,
                "line_id": item.line_id,
                "character": character,
                "text": item.text,
                "text_sha256": item.text_sha256,
                "audio": str(audio),
                "audio_sha256": audio_sha256,
                "variant_id": variant_id,
                "evaluation_kind": item.document.get("evaluation_kind"),
            }
            samples.append(sample)
            provider = _text(
                result.get("provider"), f"generated result {queue_id} provider"
            )
            model = _text(result.get("model"), f"generated result {queue_id} model")
            if report_provider is None:
                report_provider = provider
                report_model = model
            elif (report_provider, report_model) != (provider, model):
                raise SourceReferenceReviewError(
                    f"Variant {variant_id} mixes generation backends or models"
                )
            if expected_kind == "source-match":
                originals.append(
                    {
                        **sample,
                        "audio": str(source),
                        "audio_sha256": source_sha256,
                    }
                )
        if samples:
            generated_reports.append(
                {
                    "variant_id": variant_id,
                    "samples": samples,
                    "provider": report_provider,
                    "model": report_model,
                    "affected_queue_item_count": variant.get(
                        "affected_queue_item_count"
                    ),
                }
            )
    if not originals:
        raise SourceReferenceReviewError(
            "No successful source-match result is available for blind review; "
            "use the fixed-corpus source-reference quality review for unrouted media"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        reports = []
        original_report = staging / "original-source.json"
        atomic_write_json(
            original_report,
            _model_report(
                "original-source",
                "original-game-audio",
                "checksum-bound accepted source WAV",
                originals,
                comparison_path=comparison_path,
                comparison_sha256=comparison_sha256,
                state_path=state_path,
                state_sha256=state_sha256,
            ),
        )
        reports.append(original_report)
        for index, report in enumerate(generated_reports, start=1):
            path = staging / f"generated-{index:02d}.json"
            atomic_write_json(
                path,
                _model_report(
                    f"generated:{report['variant_id']}",
                    report["provider"],
                    report["model"],
                    report["samples"],
                    comparison_path=comparison_path,
                    comparison_sha256=comparison_sha256,
                    state_path=state_path,
                    state_sha256=state_sha256,
                    affected_queue_item_count=report["affected_queue_item_count"],
                ),
            )
            reports.append(path)
        validation = staging / ".validation-session"
        try:
            session_path = create_listening_session_from_reports(
                reports, validation, seed=0
            )
        except ModelListeningError as error:
            raise SourceReferenceReviewError(str(error)) from error
        blind_trials = load_listening_session(session_path)["trial_count"]
        shutil.rmtree(validation)
        if sha256_file(queue_path) != queue_sha256:
            raise SourceReferenceReviewError(
                "Evaluation queue changed during report publication"
            )
        if sha256_file(comparison_path) != comparison_sha256:
            raise SourceReferenceReviewError(
                "Evaluation comparison changed during report publication"
            )
        if sha256_file(manifest_path) != manifest_sha256:
            raise SourceReferenceReviewError(
                "Evaluation voice manifest changed during report publication"
            )
        if sha256_file(state_path) != state_sha256:
            raise SourceReferenceReviewError(
                "Evaluation generation state changed during report publication"
            )
        for audio, expected_sha256 in checked_audio:
            _assert_source_unchanged(
                audio, expected_sha256, f"evaluation audio {audio.name}"
            )
        _rename_directory_no_replace(staging, output)
        return SourceReferenceListeningReportsResult(
            output,
            len(reports),
            sum(len(report["samples"]) for report in generated_reports)
            + len(originals),
            blind_trials,
        )
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _model_report(
    model_id,
    backend,
    model,
    samples,
    *,
    comparison_path,
    comparison_sha256,
    state_path,
    state_sha256,
    affected_queue_item_count=None,
):
    return {
        "schema": "vntts.voice-model-report",
        "schema_version": 1,
        "model_id": model_id,
        "provider": backend,
        "backend": backend,
        "model": model,
        "samples": samples,
        "source_reference_evaluation": str(comparison_path),
        "source_reference_evaluation_sha256": comparison_sha256,
        "generation_state": str(state_path),
        "generation_state_sha256": state_sha256,
        "affected_queue_item_count": affected_queue_item_count,
    }


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
        report_version = report["schema_version"]
        candidate_origin = value.get("candidate_origin", SOURCE_ORIGIN_STORY_LINE)
        if candidate_origin not in {
            SOURCE_ORIGIN_STORY_LINE,
            SOURCE_ORIGIN_EXACT_BANK,
        }:
            raise SourceReferenceReviewError(f"Candidate {index} origin is invalid")
        source_event_ids = value.get("source_event_ids", [])
        if not isinstance(source_event_ids, list) or any(
            isinstance(event_id, bool) or not isinstance(event_id, int) or event_id < 0
            for event_id in source_event_ids
        ):
            raise SourceReferenceReviewError(
                f"Candidate {index} source event IDs are invalid"
            )
        if report_version >= 2 and not source_event_ids:
            raise SourceReferenceReviewError(
                f"Candidate {index} has no exact source event IDs"
            )
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
            "candidate_origin": candidate_origin,
            "source_event_ids": tuple(source_event_ids),
            "reference_relative": relative,
            "reference_path": reference_path,
            "reference_payload": payload,
            "reference_sha256": reference_sha256,
            "transcripts": _candidate_transcripts(
                value,
                index,
                allow_empty=candidate_origin == SOURCE_ORIGIN_EXACT_BANK,
            ),
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


def _candidate_transcripts(value, index, *, allow_empty=False):
    source_lines = value.get("source_lines")
    if not isinstance(source_lines, list) or (not source_lines and not allow_empty):
        raise SourceReferenceReviewError(f"Candidate {index} source lines are missing")
    if allow_empty and source_lines:
        raise SourceReferenceReviewError(
            f"Candidate {index} unrouted media must not invent source lines"
        )
    transcripts = []
    for line_index, line in enumerate(source_lines):
        if not isinstance(line, dict):
            raise SourceReferenceReviewError(
                f"Candidate {index} source line {line_index} is invalid"
            )
        transcripts.append(
            _text(line.get("text"), f"candidate {index} source transcript")
        )
    return tuple(transcripts)


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
