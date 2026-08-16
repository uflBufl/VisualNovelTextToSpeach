"""Non-destructive import of Reverse: 1999 pregeneration work."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from platformdirs import user_data_path
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)

from vntts.settings import get_local_data_directory

LEGACY_JOB_SCHEMA = "r1999.pregeneration-job"
LEGACY_JOB_SCHEMA_VERSION = 1
LEGACY_STATE_SCHEMA = "r1999.bulk-generation-state"
LEGACY_STATE_SCHEMA_VERSION = 1
IMPORT_SCHEMA = "vntts.authoring-legacy-import"
IMPORT_SCHEMA_VERSION = 1


class LegacyAuthoringImportError(RuntimeError):
    """A legacy job cannot be imported without losing or misidentifying work."""


@dataclass(frozen=True)
class LegacyImportCandidate:
    job_directory: Path
    title: str
    status: str
    queue_items: int
    generated_items: int
    compatibility_error: str | None = None
    kind: str = "pregeneration-job"
    diagnostics: tuple[str, ...] = ()

    @property
    def compatible(self):
        return self.compatibility_error is None


@dataclass(frozen=True)
class LegacyImportResult:
    destination: Path
    manifest: dict[str, object]
    created: bool


@dataclass(frozen=True)
class _CopyArtifact:
    role: str
    source: Path
    destination: Path
    sha256: str


@dataclass(frozen=True)
class _ImportPlan:
    job_directory: Path
    job: dict[str, object]
    queue: VoiceGenerationQueue
    state: dict[str, object] | None
    generated_index: GeneratedAudioIndex | None
    artifacts: tuple[_CopyArtifact, ...]
    source_fingerprint: str
    summary: dict[str, object]
    external_inputs: tuple[dict[str, object], ...]
    logical_identity: str
    manifest_diagnostics: tuple[str, ...]


def default_legacy_jobs_root(*, environment=None):
    environment = os.environ if environment is None else environment
    configured = environment.get("R1999_EXTRACTOR_DATA")
    data_root = (
        Path(configured).expanduser()
        if configured
        else user_data_path("Reverse1999Extractor", appauthor=False)
    )
    return data_root / "reverse1999" / "pregeneration-jobs"


def default_import_root():
    return get_local_data_directory() / "authoring" / "legacy-imports"


def discover_legacy_jobs(jobs_root=None):
    """Return every legacy job, retaining actionable compatibility failures."""
    root = Path(jobs_root or default_legacy_jobs_root()).expanduser().resolve()
    if not root.is_dir():
        return ()
    candidates = []
    referenced_queues = set()
    referenced_outputs = set()
    for job_path in sorted(root.glob("*/job.json"), reverse=True):
        job_directory = job_path.parent
        raw_job = _load_json_optional(job_path)
        for field, destinations in (
            ("queue", referenced_queues),
            ("output", referenced_outputs),
        ):
            value = raw_job.get(field)
            if isinstance(value, str) and value.strip():
                destinations.add(_resolve_path(job_directory, value))
        try:
            plan = _build_import_plan(job_directory)
            candidates.append(
                LegacyImportCandidate(
                    job_directory=job_directory,
                    title=_optional_text(plan.job.get("title")) or job_directory.name,
                    status=_optional_text(plan.job.get("status")) or "unknown",
                    queue_items=len(plan.queue.items),
                    generated_items=int(plan.summary["generated_items"]),
                    diagnostics=plan.manifest_diagnostics,
                )
            )
        except LegacyAuthoringImportError as error:
            candidates.append(
                LegacyImportCandidate(
                    job_directory=job_directory,
                    title=_optional_text(raw_job.get("title")) or job_directory.name,
                    status=_optional_text(raw_job.get("status")) or "unknown",
                    queue_items=0,
                    generated_items=0,
                    compatibility_error=str(error),
                )
            )
    candidates.extend(
        _discover_unsupported_legacy_artifacts(
            root.parent,
            referenced_queues,
            referenced_outputs,
        )
    )
    return tuple(candidates)


def import_legacy_job(job_directory, destination_root=None):
    """Validate and copy one legacy job without changing either source or prior imports."""
    plan = _build_import_plan(job_directory)
    destination_root = Path(destination_root or default_import_root()).expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    import_id = _import_id(plan)
    destination = destination_root / import_id
    if destination.exists():
        return _validate_existing_import(destination, plan)
    _validate_import_root_collisions(destination_root, plan)

    staging = Path(tempfile.mkdtemp(prefix=f".{import_id}-", dir=destination_root))
    try:
        for artifact in plan.artifacts:
            target = staging / artifact.destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact.source, target)
            if sha256_file(target) != artifact.sha256:
                raise LegacyAuthoringImportError(
                    f"Copied artifact changed during import: {artifact.source}"
                )
        manifest = _import_manifest(plan, import_id)
        atomic_write_json(staging / "import.json", manifest, sort_keys=True)
        try:
            staging.rename(destination)
        except OSError:
            if destination.exists():
                return _validate_existing_import(destination, plan)
            raise
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
    return LegacyImportResult(destination, manifest, True)


def _build_import_plan(job_directory):
    job_directory = Path(job_directory).expanduser().resolve()
    job_path = job_directory / "job.json"
    job = _load_json(job_path, "pregeneration job")
    if (
        job.get("schema") != LEGACY_JOB_SCHEMA
        or job.get("schema_version") != LEGACY_JOB_SCHEMA_VERSION
    ):
        raise LegacyAuthoringImportError(
            "Unsupported pregeneration job schema; expected "
            f"{LEGACY_JOB_SCHEMA!r} version {LEGACY_JOB_SCHEMA_VERSION}"
        )
    _validate_job(job)

    queue_path = _job_path(job_directory, job.get("queue"), "queue")
    try:
        queue = VoiceGenerationQueue.load(queue_path)
    except VoiceGenerationQueueError as error:
        raise LegacyAuthoringImportError(
            f"Incompatible generation queue {queue_path}: {error}. "
            "Re-export it with vntts-artifacts v0.6 before importing."
        ) from error
    queue_sha256 = sha256_file(queue_path)
    output = _job_path(job_directory, job.get("output"), "output directory")
    if output.exists() and not output.is_dir():
        raise LegacyAuthoringImportError(
            f"Pregeneration output path is not a directory: {output}"
        )
    state_path = output / "generation-state.json"
    manifest_path = output / "manifest.json"

    artifacts = {}
    _add_artifact(artifacts, "legacy_job", job_path, Path("legacy/job.json"))
    _add_artifact(artifacts, "generation_queue", queue_path, Path("queue.jsonl"))

    state = None
    state_items = {}
    generated_files = {}
    if state_path.is_file():
        state = _load_json(state_path, "generation state")
        state_items, generated_files = _validate_state(
            state,
            state_path,
            output,
            queue,
            queue_sha256,
        )
        _add_artifact(
            artifacts,
            "generation_state",
            state_path,
            Path("generated-audio/generation-state.json"),
        )
        for source, relative in generated_files.values():
            _add_artifact(
                artifacts,
                "generated_wav",
                source,
                Path("generated-audio") / relative,
            )

    generated_index = None
    manifest_diagnostics = ()
    if manifest_path.is_file():
        generated_index, manifest_files, manifest_diagnostics = _validate_generated_manifest(
            manifest_path,
            output,
            queue,
            queue_sha256,
            state_items,
            state_exists=state is not None,
        )
        manifest_current = not manifest_diagnostics
        _add_artifact(
            artifacts,
            (
                "generated_audio_manifest"
                if manifest_current
                else "stale_generated_audio_manifest"
            ),
            manifest_path,
            (
                Path("generated-audio/manifest.json")
                if manifest_current
                else Path("legacy/stale-generated-audio-manifest.json")
            ),
        )
        for source, relative in manifest_files.values():
            _add_artifact(
                artifacts,
                "generated_wav",
                source,
                Path("generated-audio") / relative,
            )

    statuses = Counter(
        str(value.get("status") or "unknown")
        for value in state_items.values()
        if isinstance(value, dict)
    )
    reviews = Counter(
        str(value.get("review_status") or "unreviewed")
        for value in state_items.values()
        if isinstance(value, dict)
    )
    summary = {
        "queue_items": len(queue.items),
        "state_items": len(state_items),
        "generated_items": statuses["generated"] + statuses["approved"],
        "status_counts": dict(sorted(statuses.items())),
        "review_counts": dict(sorted(reviews.items())),
        "generated_manifest_entries": (
            len(generated_index.entries) if generated_index is not None else 0
        ),
        "generated_manifest_state": (
            "absent"
            if generated_index is None
            else "stale"
            if manifest_diagnostics
            else "current"
        ),
        "generated_manifest_diagnostics": list(manifest_diagnostics),
    }
    ordered_artifacts = tuple(sorted(artifacts.values(), key=lambda item: item.destination.as_posix()))
    logical_identity = hashlib.sha256(
        f"{queue_sha256}\n{output}".encode("utf-8")
    ).hexdigest()
    fingerprint_input = _meaningful_source_fingerprint(queue_sha256, state_items)
    return _ImportPlan(
        job_directory=job_directory,
        job=job,
        queue=queue,
        state=state,
        generated_index=generated_index,
        artifacts=ordered_artifacts,
        source_fingerprint=hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest(),
        summary=summary,
        external_inputs=_external_inputs(job_directory, job),
        logical_identity=logical_identity,
        manifest_diagnostics=manifest_diagnostics,
    )


def _validate_job(job):
    for field in (
        "created_at",
        "status",
        "title",
        "story_index",
        "queue",
        "output",
        "voice_manifest",
        "vntts_python",
        "narrator_character",
    ):
        if _optional_text(job.get(field)) is None:
            raise LegacyAuthoringImportError(
                f"Pregeneration job requires non-empty {field!r}"
            )
    if job.get("model") is not None and _optional_text(job.get("model")) is None:
        raise LegacyAuthoringImportError(
            "Pregeneration job model must be text or null"
        )
    try:
        created_at = datetime.fromisoformat(str(job["created_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise LegacyAuthoringImportError(
            "Pregeneration job created_at must be an ISO-8601 timestamp"
        ) from error
    if created_at.tzinfo is None:
        raise LegacyAuthoringImportError(
            "Pregeneration job created_at must include a timezone"
        )
    targets = job.get("targets")
    if not isinstance(targets, list):
        raise LegacyAuthoringImportError("Pregeneration job targets must be a list")
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise LegacyAuthoringImportError(
                f"Pregeneration target {index} must be an object"
            )
        for field in ("target_id", "category", "title"):
            if _optional_text(target.get(field)) is None:
                raise LegacyAuthoringImportError(
                    f"Pregeneration target {index} requires non-empty {field!r}"
                )
        chapters = target.get("chapters")
        if not isinstance(chapters, list) or not all(
            isinstance(chapter, str) and chapter.strip() for chapter in chapters
        ):
            raise LegacyAuthoringImportError(
                f"Pregeneration target {index} chapters must be a list of strings"
            )
        for field in ("episode_count", "line_count"):
            value = target.get(field)
            if field == "episode_count" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LegacyAuthoringImportError(
                    f"Pregeneration target {index} {field} must be non-negative"
                )


def _discover_unsupported_legacy_artifacts(
    scan_root,
    referenced_queues,
    referenced_outputs,
):
    candidates = []
    if not scan_root.is_dir():
        return candidates
    for path in sorted(scan_root.rglob("*.jsonl")):
        if path.resolve() in referenced_queues:
            continue
        metadata = _load_jsonl_metadata_optional(path)
        if metadata.get("schema") != "vntts.voice-generation-queue":
            continue
        queue_items = 0
        error = (
            "Standalone generation queue is discoverable but cannot be imported "
            "until it is paired with its generation output or wrapped in a "
            "r1999.pregeneration-job v1 document. Source files were not changed."
        )
        try:
            queue_items = len(VoiceGenerationQueue.load(path).items)
        except VoiceGenerationQueueError as queue_error:
            error = f"Incompatible standalone generation queue: {queue_error}"
        candidates.append(
            LegacyImportCandidate(
                job_directory=path,
                title=path.name,
                status="unsupported",
                queue_items=queue_items,
                generated_items=0,
                compatibility_error=error,
                kind="standalone-generation-queue",
            )
        )
    output_directories = set()
    for name in ("generation-state.json", "manifest.json"):
        for path in scan_root.rglob(name):
            directory = path.parent.resolve()
            if directory in referenced_outputs:
                continue
            document = _load_json_optional(path)
            if document.get("schema") in {
                LEGACY_STATE_SCHEMA,
                "vntts.generated-audio",
            }:
                output_directories.add(directory)
    for directory in sorted(output_directories):
        candidates.append(
            LegacyImportCandidate(
                job_directory=directory,
                title=directory.name,
                status="unsupported",
                queue_items=0,
                generated_items=0,
                compatibility_error=(
                    "Standalone generation state/manifest is discoverable but cannot "
                    "be imported until its exact source queue is selected. Source "
                    "files were not changed."
                ),
                kind="standalone-generation-output",
            )
        )
    for session_path in sorted(scan_root.rglob("session.json")):
        document = _load_json_optional(session_path)
        if document.get("schema") != "r1999.model-listening-session":
            continue
        candidates.append(
            LegacyImportCandidate(
                job_directory=session_path.parent.resolve(),
                title=session_path.parent.name,
                status="unsupported",
                queue_items=0,
                generated_items=0,
                compatibility_error=(
                    "Blind-listening session, key and report are discoverable, but "
                    "their non-destructive import is not implemented yet. Keep the "
                    "entire session directory unchanged."
                ),
                kind="model-listening-session",
            )
        )
    return candidates


def _meaningful_source_fingerprint(queue_sha256, state_items):
    normalized_items = {}
    for queue_id, value in sorted(state_items.items()):
        normalized_items[queue_id] = {
            key: field_value
            for key, field_value in sorted(value.items())
            if key != "updated_at"
        }
    return json.dumps(
        {"queue_sha256": queue_sha256, "items": normalized_items},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_state(state, state_path, output, queue, queue_sha256):
    if (
        state.get("schema") != LEGACY_STATE_SCHEMA
        or state.get("schema_version") != LEGACY_STATE_SCHEMA_VERSION
    ):
        raise LegacyAuthoringImportError(
            f"Unsupported generation state {state_path}; expected "
            f"{LEGACY_STATE_SCHEMA!r} version {LEGACY_STATE_SCHEMA_VERSION}"
        )
    if state.get("queue_sha256") != queue_sha256:
        raise LegacyAuthoringImportError(
            "Generation state belongs to different queue content. Restore the original "
            "queue or import the matching job directory."
        )
    items = state.get("items")
    if not isinstance(items, dict):
        raise LegacyAuthoringImportError("Generation state items must be an object")
    queue_by_id = {item.queue_id: item for item in queue.items}
    files = {}
    for queue_id, value in items.items():
        if queue_id not in queue_by_id:
            raise LegacyAuthoringImportError(
                f"Generation state references unknown queue_id {queue_id!r}"
            )
        if not isinstance(value, dict):
            raise LegacyAuthoringImportError(
                f"Generation state item {queue_id!r} must be an object"
            )
        _validate_attempt_fields(queue_id, value)
        status = value.get("status")
        if status not in {"failed", "generated", "approved"}:
            raise LegacyAuthoringImportError(
                f"Generation state item {queue_id!r} has unsupported status {status!r}"
            )
        review = value.get("review_status")
        if review not in {None, "pending_review", "approved", "rejected"}:
            raise LegacyAuthoringImportError(
                f"Generation state item {queue_id!r} has unsupported review decision {review!r}"
            )
        if status == "approved" and review != "approved":
            raise LegacyAuthoringImportError(
                f"Approved state item {queue_id!r} is missing its approved review decision"
            )
        if status not in {"generated", "approved"}:
            continue
        queue_item = queue_by_id[queue_id]
        if value.get("line_id") != queue_item.line_id:
            raise LegacyAuthoringImportError(
                f"Generation state line_id does not match queue item {queue_id!r}"
            )
        if value.get("text_sha256") != queue_item.text_sha256:
            raise LegacyAuthoringImportError(
                f"Generation state text hash does not match queue item {queue_id!r}"
            )
        relative = _safe_relative(value.get("path"), f"state item {queue_id!r} path")
        if relative.suffix.casefold() != ".wav":
            raise LegacyAuthoringImportError(
                f"Generation state item {queue_id!r} must reference a WAV file"
            )
        source = _within(output, relative, f"state item {queue_id!r} path")
        info = _validate_generated_wav(source, value.get("file_sha256"), queue_id)
        _validate_quality(queue_id, value.get("quality"), info)
        files[source] = (source, relative)
    return items, files


def _validate_generated_manifest(
    manifest_path,
    output,
    queue,
    queue_sha256,
    state_items,
    *,
    state_exists,
):
    try:
        index = GeneratedAudioIndex.load(manifest_path)
    except GeneratedAudioManifestError as error:
        raise LegacyAuthoringImportError(
            f"Incompatible generated-audio manifest {manifest_path}: {error}"
        ) from error
    diagnostics = []
    if index.metadata.get("source_queue_sha256") != queue_sha256:
        diagnostics.append("source_queue_sha256 does not match the imported queue")
    if not state_exists:
        diagnostics.append("generation state is absent, so approvals cannot be confirmed")
    queue_by_identity = {
        (item.line_id, item.text_sha256): item for item in queue.items
    }
    raw = _load_json(manifest_path, "generated-audio manifest")
    raw_entries = raw.get("entries", [])
    files = {}
    published_queue_ids = set()
    for entry, raw_entry in zip(index.entries, raw_entries, strict=True):
        queue_item = queue_by_identity.get((entry.line_id, entry.text_sha256))
        if queue_item is None:
            diagnostics.append(
                f"line {entry.line_id!r} is absent from the imported queue"
            )
        else:
            published_queue_ids.add(queue_item.queue_id)
            declared_queue_id = raw_entry.get("queue_id")
            if declared_queue_id not in {None, queue_item.queue_id}:
                diagnostics.append(
                    f"queue_id does not match published line {entry.line_id!r}"
                )
        if index.find(entry.line_id, entry.text_sha256) is None:
            raise LegacyAuthoringImportError(
                f"Generated WAV is missing or modified for line {entry.line_id!r}"
            )
        relative = _relative_within(output, entry.audio, "generated WAV")
        files[entry.audio] = (entry.audio, relative)
        if state_exists and queue_item is not None:
            state_item = state_items.get(queue_item.queue_id)
            if not isinstance(state_item, dict) or (
                state_item.get("status") != "approved"
                or state_item.get("review_status") != "approved"
            ):
                diagnostics.append(
                    f"published line {entry.line_id!r} lacks a current approved state decision"
                )
                continue
            state_path = _safe_relative(
                state_item.get("path"),
                f"state item {queue_item.queue_id!r} path",
            )
            if entry.audio != _within(output, state_path, "generated WAV path"):
                diagnostics.append(
                    f"audio path does not match state item {queue_item.queue_id!r}"
                )
            expected_fields = {
                "audio_sha256": "file_sha256",
                "provider": "provider",
                "model": "model",
                "prompt_sha256": "prompt_sha256",
                "seed": "seed",
                "review_status": "review_status",
            }
            for manifest_field, state_field in expected_fields.items():
                if raw_entry.get(manifest_field) != state_item.get(state_field):
                    diagnostics.append(
                        f"{manifest_field} does not match state item "
                        f"{queue_item.queue_id!r}"
                    )
            quality = state_item.get("quality", {})
            for field in ("sample_rate", "sample_count"):
                if raw_entry.get(field) != quality.get(field):
                    diagnostics.append(
                        f"{field} does not match state item "
                        f"{queue_item.queue_id!r}"
                    )
    if state_exists:
        approved_queue_ids = {
            queue_id
            for queue_id, state_item in state_items.items()
            if isinstance(state_item, dict)
            and state_item.get("status") == "approved"
            and state_item.get("review_status") == "approved"
        }
        missing = approved_queue_ids.difference(published_queue_ids)
        if missing:
            diagnostics.append(
                f"manifest omits {len(missing)} approved state item(s)"
            )
    return index, files, tuple(dict.fromkeys(diagnostics))


def _validate_generated_wav(path, expected_hash, queue_id):
    if not path.is_file():
        raise LegacyAuthoringImportError(
            f"Generated WAV for {queue_id!r} does not exist: {path}"
        )
    actual_hash = sha256_file(path)
    if expected_hash != actual_hash:
        raise LegacyAuthoringImportError(
            f"Generated WAV checksum mismatch for {queue_id!r}: {path}"
        )
    try:
        return probe_pcm16_mono_wav(path)
    except Pcm16MonoWavError as error:
        raise LegacyAuthoringImportError(
            f"Generated WAV for {queue_id!r} is invalid: {error}"
        ) from error


def _validate_quality(queue_id, quality, info):
    if not isinstance(quality, dict):
        raise LegacyAuthoringImportError(
            f"Generated state item {queue_id!r} requires quality metadata"
        )
    expected = {
        "channels": 1,
        "sample_rate": info.sample_rate,
        "sample_count": info.sample_count,
    }
    for field, value in expected.items():
        if quality.get(field) != value:
            raise LegacyAuthoringImportError(
                f"Generated state item {queue_id!r} quality {field} does not match its WAV"
            )


def _validate_attempt_fields(queue_id, value):
    for field in ("attempts", "seed"):
        field_value = value.get(field)
        if field_value is not None and (
            isinstance(field_value, bool) or not isinstance(field_value, int)
        ):
            raise LegacyAuthoringImportError(
                f"Generation state item {queue_id!r} {field} must be an integer"
            )
    attempts = value.get("attempts")
    if attempts is not None and attempts < 0:
        raise LegacyAuthoringImportError(
            f"Generation state item {queue_id!r} attempts must not be negative"
        )


def _external_inputs(job_directory, job):
    inputs = []
    for name in ("story_index", "voice_manifest"):
        raw_path = job.get(name)
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = _resolve_path(job_directory, raw_path)
        value = {"role": name, "source_path": str(path), "exists": path.is_file()}
        if path.is_file():
            value["sha256"] = sha256_file(path)
        inputs.append(value)
    return tuple(inputs)


def _import_manifest(plan, import_id):
    return {
        "schema": IMPORT_SCHEMA,
        "schema_version": IMPORT_SCHEMA_VERSION,
        "import_id": import_id,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "kind": "reverse1999-extractor-pregeneration-job",
            "job_directory": str(plan.job_directory),
            "job_schema": LEGACY_JOB_SCHEMA,
            "job_schema_version": LEGACY_JOB_SCHEMA_VERSION,
            "source_fingerprint": plan.source_fingerprint,
            "logical_identity": plan.logical_identity,
        },
        "legacy_job": {
            "title": plan.job.get("title"),
            "status": plan.job.get("status"),
            "model": plan.job.get("model"),
            "narrator_character": plan.job.get("narrator_character"),
        },
        "summary": plan.summary,
        "identities": _import_identities(plan),
        "external_inputs": list(plan.external_inputs),
        "artifacts": [
            {
                "role": artifact.role,
                "source_path": str(artifact.source),
                "path": artifact.destination.as_posix(),
                "sha256": artifact.sha256,
            }
            for artifact in plan.artifacts
        ],
    }


def _import_identities(plan):
    state_items = plan.state.get("items", {}) if plan.state is not None else {}
    identities = []
    for queue_item in plan.queue.items:
        state = state_items.get(queue_item.queue_id, {})
        identities.append(
            {
                "queue_id": queue_item.queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "attempts": state.get("attempts"),
                "seed": state.get("seed"),
                "status": state.get("status", "pending"),
                "review_status": state.get("review_status"),
                "path": state.get("path"),
                "file_sha256": state.get("file_sha256"),
                "provider": state.get("provider"),
                "model": state.get("model"),
                "prompt_sha256": state.get("prompt_sha256"),
            }
        )
    return identities


def _validate_import_root_collisions(destination_root, plan):
    proposed = {item["queue_id"]: item for item in _import_identities(plan)}
    for manifest_path in destination_root.glob("*/import.json"):
        manifest = _load_json(manifest_path, "existing authoring import")
        if (
            manifest.get("schema") != IMPORT_SCHEMA
            or manifest.get("schema_version") != IMPORT_SCHEMA_VERSION
        ):
            continue
        identities = manifest.get("identities")
        if not isinstance(identities, list):
            raise LegacyAuthoringImportError(
                f"Existing import has malformed identities: {manifest_path}"
            )
        for existing in identities:
            if not isinstance(existing, dict):
                raise LegacyAuthoringImportError(
                    f"Existing import has malformed identity: {manifest_path}"
                )
            queue_id = existing.get("queue_id")
            current = proposed.get(queue_id)
            if current is not None and current != existing:
                raise LegacyAuthoringImportError(
                    f"Queue identity {queue_id!r} conflicts with existing import "
                    f"{manifest_path.parent}. No application data was changed."
                )


def _validate_existing_import(destination, plan):
    manifest_path = destination / "import.json"
    manifest = _load_json(manifest_path, "existing authoring import")
    if (
        manifest.get("schema") != IMPORT_SCHEMA
        or manifest.get("schema_version") != IMPORT_SCHEMA_VERSION
    ):
        raise LegacyAuthoringImportError(
            f"Import destination already exists with an unsupported manifest: {destination}"
        )
    source = manifest.get("source")
    if not isinstance(source, dict) or source.get("logical_identity") != plan.logical_identity:
        raise LegacyAuthoringImportError(
            f"Import ID collision at {destination}; choose a different destination root"
        )
    if source.get("source_fingerprint") != plan.source_fingerprint:
        raise LegacyAuthoringImportError(
            "Legacy source changed after it was imported. Existing application data was "
            f"left untouched at {destination}."
        )
    for artifact in manifest.get("artifacts", []):
        if not isinstance(artifact, dict):
            raise LegacyAuthoringImportError(
                f"Existing import manifest is malformed: {manifest_path}"
            )
        relative = _safe_relative(artifact.get("path"), "imported artifact path")
        path = _within(destination, relative, "imported artifact path")
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise LegacyAuthoringImportError(
                f"Existing imported artifact is missing or modified: {path}. "
                "No files were overwritten."
            )
    return LegacyImportResult(destination, manifest, False)


def _add_artifact(artifacts, role, source, destination):
    source = Path(source).resolve()
    destination = Path(destination)
    digest = sha256_file(source)
    existing = artifacts.get(destination)
    if existing is not None:
        if existing.source != source or existing.sha256 != digest:
            raise LegacyAuthoringImportError(
                f"Two source artifacts collide at imported path {destination}"
            )
        return
    artifacts[destination] = _CopyArtifact(role, source, destination, digest)


def _import_id(plan):
    return f"legacy-{plan.logical_identity[:24]}"


def _job_path(job_directory, value, label):
    if not isinstance(value, str) or not value.strip():
        raise LegacyAuthoringImportError(f"Pregeneration job is missing its {label} path")
    return _resolve_path(job_directory, value)


def _resolve_path(root, value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _safe_relative(value, label):
    if not isinstance(value, str) or not value.strip():
        raise LegacyAuthoringImportError(f"{label} must be a non-empty relative path")
    if "\\" in value:
        raise LegacyAuthoringImportError(f"{label} must use POSIX separators")
    parts = value.split("/")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise LegacyAuthoringImportError(f"{label} must stay within its owning directory")
    return Path(*path.parts)


def _within(root, relative, label):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise LegacyAuthoringImportError(f"{label} leaves its owning directory") from error
    return path


def _relative_within(root, path, label):
    root = Path(root).resolve()
    try:
        return Path(path).resolve().relative_to(root)
    except ValueError as error:
        raise LegacyAuthoringImportError(f"{label} leaves the generation output") from error


def _load_json(path, description):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LegacyAuthoringImportError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise LegacyAuthoringImportError(f"{description.title()} must be a JSON object")
    return value


def _load_json_optional(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _load_jsonl_metadata_optional(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.loads(next(stream))
    except (OSError, StopIteration, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _optional_text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None
