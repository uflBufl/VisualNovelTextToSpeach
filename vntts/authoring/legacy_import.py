"""Non-destructive import of Reverse: 1999 pregeneration work."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TypeAlias

from platformdirs import user_data_path
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import (
    Pcm16MonoWavError,
    Pcm16MonoWavInfo,
    probe_pcm16_mono_wav,
)
from vntts_artifacts.file_integrity import sha256_file
from vntts_artifacts.generated_audio import (
    GeneratedAudioEntry,
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)
from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)

from vntts.authoring.generation_lease import inspect_process_status
from vntts.authoring.import_paths import default_import_root
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.workspace_foundation import load_json_object

LEGACY_JOB_SCHEMA = "r1999.pregeneration-job"
LEGACY_JOB_SCHEMA_VERSION = 1
LEGACY_STATE_SCHEMA = "r1999.bulk-generation-state"
LEGACY_STATE_SCHEMA_VERSION = 1
IMPORT_SCHEMA = "vntts.authoring-legacy-import"
IMPORT_SCHEMA_VERSION = 2
SUPPORTED_IMPORT_SCHEMA_VERSIONS = frozenset({1, IMPORT_SCHEMA_VERSION})
CONTROL_ARTIFACT_ROLES = {
    "legacy_job",
    "generation_queue",
    "generation_state",
    "generated_audio_manifest",
    "stale_generated_audio_manifest",
}


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
    def compatible(self) -> bool:
        return self.compatibility_error is None


@dataclass(frozen=True)
class LegacyImportResult:
    destination: Path
    manifest: dict[str, object]
    created: bool


@dataclass(frozen=True)
class StandaloneImportInspection:
    queue_path: Path
    output_directory: Path
    logical_identity: str
    source_fingerprint: str
    summary: dict[str, object]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class _CopyArtifact:
    role: str
    source: Path
    destination: Path
    sha256: str


ArtifactMap: TypeAlias = dict[Path, _CopyArtifact]
GeneratedFiles: TypeAlias = dict[Path, tuple[Path, Path, str]]
JsonDocument: TypeAlias = dict[str, object]
StateItems: TypeAlias = dict[str, JsonDocument]


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
    source_kind: str = "reverse1999-extractor-pregeneration-job"
    source_diagnostics: tuple[str, ...] = ()
    runtime_status: str = "snapshot"


def default_legacy_jobs_root(*, environment: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environment is None else environment
    configured = environment.get("R1999_EXTRACTOR_DATA")
    data_root = (
        Path(configured).expanduser()
        if configured
        else user_data_path("Reverse1999Extractor", appauthor=False)
    )
    return data_root / "reverse1999" / "pregeneration-jobs"


def discover_legacy_jobs(
    jobs_root: str | Path | None = None,
) -> tuple[LegacyImportCandidate, ...]:
    """Return every legacy job, retaining actionable compatibility failures."""
    root = Path(jobs_root or default_legacy_jobs_root()).expanduser().resolve()
    if not root.is_dir():
        return ()
    candidates: list[LegacyImportCandidate] = []
    referenced_queues: set[Path] = set()
    referenced_outputs: set[Path] = set()
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
            generated_items = plan.summary.get("generated_items")
            if isinstance(generated_items, bool) or not isinstance(
                generated_items, int
            ):
                raise LegacyAuthoringImportError(
                    "Legacy import summary has an invalid generated item count"
                )
            candidates.append(
                LegacyImportCandidate(
                    job_directory=job_directory,
                    title=_optional_text(plan.job.get("title")) or job_directory.name,
                    status=plan.runtime_status,
                    queue_items=len(plan.queue.items),
                    generated_items=generated_items,
                    diagnostics=plan.manifest_diagnostics + plan.source_diagnostics,
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


def import_legacy_job(
    job_directory: str | Path,
    destination_root: str | Path | None = None,
) -> LegacyImportResult:
    """Validate and copy one legacy job without changing either source or prior imports."""
    plan = _build_import_plan(job_directory)
    destination_root = (
        Path(destination_root or default_import_root()).expanduser().resolve()
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    import_id = _import_id(plan)
    destination = destination_root / import_id
    if destination.exists():
        return _validate_existing_import(destination, plan)
    _validate_import_root_collisions(destination_root, plan)

    with staged_directory(destination_root, prefix=f".{import_id}-") as staging:
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
        _verify_source_controls_unchanged(plan)
        try:
            rename_directory_no_replace(staging, destination)
        except AtomicPublicationError, OSError:
            if destination.exists():
                return _validate_existing_import(destination, plan)
            raise
    return LegacyImportResult(destination, manifest, True)


def inspect_standalone_generation(
    queue_path: str | Path, output_directory: str | Path
) -> StandaloneImportInspection:
    """Validate one explicit queue/output pairing without copying it."""
    plan = _build_standalone_import_plan(queue_path, output_directory)
    queue_artifact = next(
        artifact for artifact in plan.artifacts if artifact.role == "generation_queue"
    )
    return StandaloneImportInspection(
        queue_path=queue_artifact.source,
        output_directory=plan.job_directory,
        logical_identity=plan.logical_identity,
        source_fingerprint=plan.source_fingerprint,
        summary=plan.summary,
        diagnostics=plan.manifest_diagnostics,
    )


def import_standalone_generation(
    queue_path: str | Path,
    output_directory: str | Path,
    destination_root: str | Path | None = None,
) -> LegacyImportResult:
    """Import one explicitly selected standalone queue/output pair."""
    plan = _build_standalone_import_plan(queue_path, output_directory)
    destination_root = (
        Path(destination_root or default_import_root()).expanduser().resolve()
    )
    destination_root.mkdir(parents=True, exist_ok=True)
    import_id = _import_id(plan)
    destination = destination_root / import_id
    if destination.exists():
        return _validate_existing_import(destination, plan)
    _validate_import_root_collisions(destination_root, plan)

    with staged_directory(destination_root, prefix=f".{import_id}-") as staging:
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
        _verify_source_controls_unchanged(plan)
        try:
            rename_directory_no_replace(staging, destination)
        except AtomicPublicationError, OSError:
            if destination.exists():
                return _validate_existing_import(destination, plan)
            raise
    return LegacyImportResult(destination, manifest, True)


def _build_standalone_import_plan(
    queue_path: str | Path, output_directory: str | Path
) -> _ImportPlan:
    queue_path = Path(queue_path).expanduser().resolve()
    output = Path(output_directory).expanduser().resolve()
    if not output.is_dir():
        raise LegacyAuthoringImportError(
            f"Standalone generation output is not a directory: {output}"
        )
    queue, queue_sha256 = _load_queue_snapshot(queue_path)
    state_path = output / "generation-state.json"
    manifest_path = output / "manifest.json"
    if not state_path.is_file() and not manifest_path.is_file():
        raise LegacyAuthoringImportError(
            "Standalone output must contain generation-state.json or manifest.json"
        )

    artifacts: ArtifactMap = {}
    _add_artifact(
        artifacts,
        "generation_queue",
        queue_path,
        Path("queue.jsonl"),
        expected_sha256=queue_sha256,
    )
    state = None
    state_items: StateItems = {}
    generated_files: GeneratedFiles = {}
    if state_path.is_file():
        state, state_sha256 = _load_json_snapshot(state_path, "generation state")
        if state.get("queue_sha256") != queue_sha256:
            raise LegacyAuthoringImportError(
                "Explicit standalone pairing failed: generation-state.json does not "
                "contain the selected queue's full SHA-256"
            )
        state_items, generated_files = _validate_state(
            state, state_path, output, queue, queue_sha256
        )
        _add_artifact(
            artifacts,
            "generation_state",
            state_path,
            Path("generated-audio/generation-state.json"),
            expected_sha256=state_sha256,
        )
        for source, relative, digest in generated_files.values():
            _add_artifact(
                artifacts,
                "generated_wav",
                source,
                Path("generated-audio") / relative,
                expected_sha256=digest,
            )

    generated_index = None
    diagnostics: tuple[str, ...] = ()
    if manifest_path.is_file():
        generated_index, raw_manifest, manifest_sha256 = _load_generated_index_snapshot(
            manifest_path
        )
        manifest_queue_sha256 = raw_manifest.get("source_queue_sha256")
        if state is None and manifest_queue_sha256 != queue_sha256:
            raise LegacyAuthoringImportError(
                "Explicit standalone pairing failed: manifest.json does not contain "
                "the selected queue's full SHA-256"
            )
        generated_index, manifest_files, diagnostics = _validate_generated_manifest(
            manifest_path,
            output,
            queue,
            queue_sha256,
            state_items,
            generated_index,
            raw_manifest,
            state_exists=state is not None,
        )
        current = not diagnostics
        _add_artifact(
            artifacts,
            "generated_audio_manifest" if current else "stale_generated_audio_manifest",
            manifest_path,
            (
                Path("generated-audio/manifest.json")
                if current
                else Path("legacy/stale-generated-audio-manifest.json")
            ),
            expected_sha256=manifest_sha256,
        )
        for source, relative, digest in manifest_files.values():
            _add_artifact(
                artifacts,
                "generated_wav",
                source,
                Path("generated-audio") / relative,
                expected_sha256=digest,
            )

    summary = _generation_summary(queue, state_items, generated_index, diagnostics)
    ordered_artifacts = tuple(
        sorted(artifacts.values(), key=lambda item: item.destination.as_posix())
    )
    logical_identity = hashlib.sha256(
        f"{queue_sha256}\n{output}".encode("utf-8")
    ).hexdigest()
    plan = _ImportPlan(
        job_directory=output,
        job={},
        queue=queue,
        state=state,
        generated_index=generated_index,
        artifacts=ordered_artifacts,
        source_fingerprint=_control_fingerprint(ordered_artifacts),
        summary=summary,
        external_inputs=(),
        logical_identity=logical_identity,
        manifest_diagnostics=diagnostics,
        source_kind="reverse1999-extractor-standalone-generation",
    )
    _verify_source_controls_unchanged(plan)
    return plan


def _build_import_plan(job_directory: str | Path) -> _ImportPlan:
    job_directory = Path(job_directory).expanduser().resolve()
    job_path = job_directory / "job.json"
    job, job_sha256 = _load_json_snapshot(job_path, "pregeneration job")
    if (
        job.get("schema") != LEGACY_JOB_SCHEMA
        or job.get("schema_version") != LEGACY_JOB_SCHEMA_VERSION
    ):
        raise LegacyAuthoringImportError(
            "Unsupported pregeneration job schema; expected "
            f"{LEGACY_JOB_SCHEMA!r} version {LEGACY_JOB_SCHEMA_VERSION}"
        )
    _validate_job(job)
    runtime_status, source_diagnostics = _legacy_runtime_status(job)

    queue_path = _job_path(job_directory, job.get("queue"), "queue")
    queue, queue_sha256 = _load_queue_snapshot(queue_path)
    output = _job_path(job_directory, job.get("output"), "output directory")
    if output.exists() and not output.is_dir():
        raise LegacyAuthoringImportError(
            f"Pregeneration output path is not a directory: {output}"
        )
    state_path = output / "generation-state.json"
    manifest_path = output / "manifest.json"

    artifacts: ArtifactMap = {}
    _add_artifact(
        artifacts,
        "legacy_job",
        job_path,
        Path("legacy/job.json"),
        expected_sha256=job_sha256,
    )
    _add_artifact(
        artifacts,
        "generation_queue",
        queue_path,
        Path("queue.jsonl"),
        expected_sha256=queue_sha256,
    )

    state = None
    state_items: StateItems = {}
    generated_files: GeneratedFiles = {}
    if state_path.is_file():
        state, state_sha256 = _load_json_snapshot(state_path, "generation state")
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
            expected_sha256=state_sha256,
        )
        for source, relative, digest in generated_files.values():
            _add_artifact(
                artifacts,
                "generated_wav",
                source,
                Path("generated-audio") / relative,
                expected_sha256=digest,
            )

    generated_index = None
    manifest_diagnostics: tuple[str, ...] = ()
    if manifest_path.is_file():
        generated_index, raw_manifest, manifest_sha256 = _load_generated_index_snapshot(
            manifest_path
        )
        generated_index, manifest_files, manifest_diagnostics = (
            _validate_generated_manifest(
                manifest_path,
                output,
                queue,
                queue_sha256,
                state_items,
                generated_index,
                raw_manifest,
                state_exists=state is not None,
            )
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
            expected_sha256=manifest_sha256,
        )
        for source, relative, digest in manifest_files.values():
            _add_artifact(
                artifacts,
                "generated_wav",
                source,
                Path("generated-audio") / relative,
                expected_sha256=digest,
            )

    summary = _generation_summary(
        queue, state_items, generated_index, manifest_diagnostics
    )
    ordered_artifacts = tuple(
        sorted(artifacts.values(), key=lambda item: item.destination.as_posix())
    )
    logical_identity = hashlib.sha256(
        f"{queue_sha256}\n{output}".encode("utf-8")
    ).hexdigest()
    plan = _ImportPlan(
        job_directory=job_directory,
        job=job,
        queue=queue,
        state=state,
        generated_index=generated_index,
        artifacts=ordered_artifacts,
        source_fingerprint=_control_fingerprint(ordered_artifacts),
        summary=summary,
        external_inputs=_external_inputs(job_directory, job),
        logical_identity=logical_identity,
        manifest_diagnostics=manifest_diagnostics,
        source_diagnostics=source_diagnostics,
        runtime_status=runtime_status,
    )
    _verify_source_controls_unchanged(plan)
    return plan


def _generation_summary(
    queue: VoiceGenerationQueue,
    state_items: StateItems,
    generated_index: GeneratedAudioIndex | None,
    diagnostics: tuple[str, ...],
) -> JsonDocument:
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
    return {
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
            if diagnostics
            else "current"
        ),
        "generated_manifest_diagnostics": list(diagnostics),
    }


def _validate_job(job: JsonDocument) -> None:
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
        raise LegacyAuthoringImportError("Pregeneration job model must be text or null")
    created_at = _validate_job_timestamp(job["created_at"], "created_at")
    if job.get("updated_at") is not None:
        updated_at = _validate_job_timestamp(job["updated_at"], "updated_at")
        if updated_at < created_at:
            raise LegacyAuthoringImportError(
                "Pregeneration job updated_at must not precede created_at"
            )
    targets = job.get("targets")
    if not isinstance(targets, list):
        raise LegacyAuthoringImportError("Pregeneration job targets must be a list")
    for index, target in enumerate(targets):
        _validate_job_target(index, target)


def _validate_job_target(index: int, target: object) -> None:
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
    scan_root: Path,
    referenced_queues: set[Path],
    referenced_outputs: set[Path],
) -> list[LegacyImportCandidate]:
    candidates: list[LegacyImportCandidate] = []
    if not scan_root.is_dir():
        return candidates
    candidates.extend(_unsupported_queue_candidates(scan_root, referenced_queues))
    candidates.extend(_unsupported_output_candidates(scan_root, referenced_outputs))
    candidates.extend(_listening_session_candidates(scan_root))
    return candidates


def _unsupported_queue_candidates(
    scan_root: Path, referenced_queues: set[Path]
) -> list[LegacyImportCandidate]:
    candidates: list[LegacyImportCandidate] = []
    for path in sorted(scan_root.rglob("*.jsonl")):
        if path.resolve() in referenced_queues:
            continue
        metadata = _load_jsonl_metadata_optional(path)
        if metadata.get("schema") != "vntts.voice-generation-queue":
            continue
        queue_items = 0
        error = (
            "Standalone queue requires an explicit full-SHA pairing. Select this "
            "queue and one output with `vntts-pregenerate inspect-standalone "
            "--queue ... --output ...` before import; no filename or timestamp "
            "matching is performed."
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
    return candidates


def _unsupported_output_candidates(
    scan_root: Path, referenced_outputs: set[Path]
) -> list[LegacyImportCandidate]:
    output_directories: set[Path] = set()
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
    return [
        LegacyImportCandidate(
            job_directory=directory,
            title=directory.name,
            status="unsupported",
            queue_items=0,
            generated_items=0,
            compatibility_error=(
                "Standalone output requires an explicitly selected queue whose "
                "full SHA-256 matches its state/manifest. Use "
                "`vntts-pregenerate inspect-standalone --queue ... --output ...`."
            ),
            kind="standalone-generation-output",
        )
        for directory in sorted(output_directories)
    ]


def _listening_session_candidates(scan_root: Path) -> list[LegacyImportCandidate]:
    candidates: list[LegacyImportCandidate] = []
    for session_path in sorted(scan_root.rglob("session.json")):
        document = _load_json_optional(session_path)
        if document.get("schema") != "r1999.model-listening-session":
            continue
        from vntts.authoring.listening_import import (
            ListeningImportError,
            inspect_listening_session,
        )

        try:
            inspection = inspect_listening_session(session_path.parent)
            candidates.append(
                LegacyImportCandidate(
                    job_directory=session_path.parent.resolve(),
                    title=session_path.parent.name,
                    status="preserve-ready",
                    queue_items=inspection.trial_count,
                    generated_items=inspection.completed_count,
                    kind="model-listening-session",
                )
            )
        except ListeningImportError as error:
            candidates.append(
                LegacyImportCandidate(
                    job_directory=session_path.parent.resolve(),
                    title=session_path.parent.name,
                    status="incompatible",
                    queue_items=0,
                    generated_items=0,
                    compatibility_error=str(error),
                    kind="model-listening-session",
                )
            )
    return candidates


def _control_fingerprint(artifacts: tuple[_CopyArtifact, ...]) -> str:
    controls = [
        (artifact.role, artifact.destination.as_posix(), artifact.sha256)
        for artifact in artifacts
        if artifact.role in CONTROL_ARTIFACT_ROLES and artifact.role != "legacy_job"
    ]
    payload = json.dumps(controls, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_state(
    state: JsonDocument,
    state_path: Path,
    output: Path,
    queue: VoiceGenerationQueue,
    queue_sha256: str,
) -> tuple[StateItems, GeneratedFiles]:
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
    queue_by_id: dict[str, VoiceGenerationQueueItem] = {
        item.queue_id: item for item in queue.items
    }
    files: GeneratedFiles = {}
    for queue_id, value in items.items():
        generated = _validated_state_generated_file(
            queue_id, value, queue_by_id, output
        )
        if generated is not None:
            source, relative, digest = generated
            files[source] = source, relative, digest
    return items, files


def _validated_state_generated_file(
    queue_id: object,
    value: object,
    queue_by_id: dict[str, VoiceGenerationQueueItem],
    output: Path,
) -> tuple[Path, Path, str] | None:
    if not isinstance(queue_id, str) or queue_id not in queue_by_id:
        raise LegacyAuthoringImportError(
            f"Generation state references unknown queue_id {queue_id!r}"
        )
    if not isinstance(value, dict):
        raise LegacyAuthoringImportError(
            f"Generation state item {queue_id!r} must be an object"
        )
    _validate_attempt_fields(queue_id, value)
    status = _validated_state_status(queue_id, value)
    if status == "failed":
        return None
    queue_item = queue_by_id[queue_id]
    if (
        value.get("line_id") != queue_item.line_id
        or value.get("text_sha256") != queue_item.text_sha256
    ):
        raise LegacyAuthoringImportError(
            f"Generation state identity does not match queue item {queue_id!r}"
        )
    relative = _safe_relative(value.get("path"), f"state item {queue_id!r} path")
    if relative.suffix.casefold() != ".wav":
        raise LegacyAuthoringImportError(
            f"Generation state item {queue_id!r} must reference a WAV file"
        )
    source = _within(output, relative, f"state item {queue_id!r} path")
    digest = value.get("file_sha256")
    info = _validate_generated_wav(source, digest, queue_id)
    _validate_quality(queue_id, value.get("quality"), info)
    if not isinstance(digest, str):
        raise LegacyAuthoringImportError(
            f"Generated WAV checksum is invalid for {queue_id!r}"
        )
    return source, relative, digest


def _validated_state_status(queue_id: str, value: JsonDocument) -> str:
    status = value.get("status")
    if not isinstance(status, str) or status not in {"failed", "generated", "approved"}:
        raise LegacyAuthoringImportError(
            f"Generation state item {queue_id!r} has unsupported status {status!r}"
        )
    review = value.get("review_status")
    valid_reviews: dict[str, set[str | None]] = {
        "failed": {None},
        "generated": {"pending_review", "rejected"},
        "approved": {"approved"},
    }
    if (
        review is not None
        and not isinstance(review, str)
        or review not in valid_reviews[status]
    ):
        raise LegacyAuthoringImportError(
            f"Generation state item {queue_id!r} has invalid {status!r}/{review!r} status and review combination"
        )
    return status


def _validate_generated_manifest(
    manifest_path: Path,
    output: Path,
    queue: VoiceGenerationQueue,
    queue_sha256: str,
    state_items: StateItems,
    index: GeneratedAudioIndex,
    raw: JsonDocument,
    *,
    state_exists: bool,
) -> tuple[GeneratedAudioIndex, GeneratedFiles, tuple[str, ...]]:
    diagnostics: list[str] = []
    if index.metadata.get("source_queue_sha256") != queue_sha256:
        diagnostics.append("source_queue_sha256 does not match the imported queue")
    if not state_exists:
        diagnostics.append(
            "generation state is absent, so approvals cannot be confirmed"
        )
    queue_by_identity = {(item.line_id, item.text_sha256): item for item in queue.items}
    raw_entries = raw.get("entries", [])
    if not isinstance(raw_entries, list) or not all(
        isinstance(raw_entry, dict) for raw_entry in raw_entries
    ):
        raise LegacyAuthoringImportError(
            f"Generated audio manifest entries must be objects: {manifest_path}"
        )
    files: GeneratedFiles = {}
    published_queue_ids: set[str] = set()
    for entry, raw_entry in zip(index.entries, raw_entries, strict=True):
        queue_item = _manifest_queue_item(
            entry, raw_entry, queue_by_identity, diagnostics
        )
        if queue_item is not None:
            published_queue_ids.add(queue_item.queue_id)
        if index.find(entry.line_id, entry.text_sha256) is None:
            raise LegacyAuthoringImportError(
                f"Generated WAV is missing or modified for line {entry.line_id!r}"
            )
        relative = _relative_within(output, entry.audio, "generated WAV")
        files[entry.audio] = (entry.audio, relative, entry.audio_sha256)
        if state_exists and queue_item is not None:
            _append_manifest_state_diagnostics(
                entry,
                raw_entry,
                queue_item,
                state_items,
                output,
                diagnostics,
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
            diagnostics.append(f"manifest omits {len(missing)} approved state item(s)")
    return index, files, tuple(dict.fromkeys(diagnostics))


def _manifest_queue_item(
    entry: GeneratedAudioEntry,
    raw_entry: JsonDocument,
    queue_by_identity: dict[tuple[str, str], VoiceGenerationQueueItem],
    diagnostics: list[str],
) -> VoiceGenerationQueueItem | None:
    queue_item = queue_by_identity.get((entry.line_id, entry.text_sha256))
    if queue_item is None:
        diagnostics.append(f"line {entry.line_id!r} is absent from the imported queue")
        return None
    declared_queue_id = raw_entry.get("queue_id")
    if declared_queue_id not in {None, queue_item.queue_id}:
        diagnostics.append(f"queue_id does not match published line {entry.line_id!r}")
    return queue_item


def _append_manifest_state_diagnostics(
    entry: GeneratedAudioEntry,
    raw_entry: JsonDocument,
    queue_item: VoiceGenerationQueueItem,
    state_items: StateItems,
    output: Path,
    diagnostics: list[str],
) -> None:
    state_item = state_items.get(queue_item.queue_id)
    if not isinstance(state_item, dict) or (
        state_item.get("status") != "approved"
        or state_item.get("review_status") != "approved"
    ):
        diagnostics.append(
            f"published line {entry.line_id!r} lacks a current approved state decision"
        )
        return
    state_path = _safe_relative(
        state_item.get("path"), f"state item {queue_item.queue_id!r} path"
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
                f"{manifest_field} does not match state item {queue_item.queue_id!r}"
            )
    quality = state_item.get("quality")
    if not isinstance(quality, dict):
        quality = {}
    for field in ("sample_rate", "sample_count"):
        if raw_entry.get(field) != quality.get(field):
            diagnostics.append(
                f"{field} does not match state item {queue_item.queue_id!r}"
            )


def _validate_generated_wav(
    path: Path, expected_hash: object, queue_id: str
) -> Pcm16MonoWavInfo:
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


def _validate_quality(queue_id: str, quality: object, info: Pcm16MonoWavInfo) -> None:
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


def _validate_attempt_fields(queue_id: str, value: JsonDocument) -> None:
    for field in ("attempts", "seed"):
        field_value = value.get(field)
        if field_value is not None and (
            isinstance(field_value, bool) or not isinstance(field_value, int)
        ):
            raise LegacyAuthoringImportError(
                f"Generation state item {queue_id!r} {field} must be an integer"
            )
    attempts = value.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts < 0:
        raise LegacyAuthoringImportError(
            f"Generation state item {queue_id!r} attempts must not be negative"
        )


def _external_inputs(
    job_directory: Path, job: JsonDocument
) -> tuple[JsonDocument, ...]:
    inputs: list[JsonDocument] = []
    for name in ("story_index", "voice_manifest"):
        raw_path = job.get(name)
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = _resolve_path(job_directory, raw_path)
        value: JsonDocument = {
            "role": name,
            "source_path": str(path),
            "exists": path.is_file(),
        }
        if path.is_file():
            value["sha256"] = sha256_file(path)
        inputs.append(value)
    return tuple(inputs)


def _import_manifest(plan: _ImportPlan, import_id: str) -> JsonDocument:
    source: JsonDocument = {
        "kind": plan.source_kind,
        "source_directory": str(plan.job_directory),
        "source_fingerprint": plan.source_fingerprint,
        "logical_identity": plan.logical_identity,
    }
    manifest = {
        "schema": IMPORT_SCHEMA,
        "schema_version": IMPORT_SCHEMA_VERSION,
        "import_id": import_id,
        "imported_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
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
    if plan.source_kind == "reverse1999-extractor-pregeneration-job":
        source.update(
            {
                "job_directory": str(plan.job_directory),
                "job_schema": LEGACY_JOB_SCHEMA,
                "job_schema_version": LEGACY_JOB_SCHEMA_VERSION,
            }
        )
        legacy_job: JsonDocument = {
            "title": plan.job.get("title"),
            "status": plan.job.get("status"),
            "model": plan.job.get("model"),
            "narrator_character": plan.job.get("narrator_character"),
            "created_at": plan.job.get("created_at"),
        }
        manifest["legacy_job"] = legacy_job
        if plan.job.get("updated_at") is not None:
            legacy_job["updated_at"] = plan.job["updated_at"]
        if plan.source_diagnostics:
            source["diagnostics"] = list(plan.source_diagnostics)
            legacy_job["snapshot_status"] = plan.runtime_status
    return manifest


def _import_identities(plan: _ImportPlan) -> list[JsonDocument]:
    raw_state_items = plan.state.get("items", {}) if plan.state is not None else {}
    state_items = raw_state_items if isinstance(raw_state_items, dict) else {}
    identities: list[JsonDocument] = []
    for queue_item in plan.queue.items:
        state = state_items.get(queue_item.queue_id, {})
        identities.append(
            {
                "queue_id": queue_item.queue_id,
                "line_id": queue_item.line_id,
                "text_sha256": queue_item.text_sha256,
                "queue_item_sha256": hashlib.sha256(
                    json.dumps(
                        queue_item.document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
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


def _validate_import_root_collisions(destination_root: Path, plan: _ImportPlan) -> None:
    proposed = {item["queue_id"]: item for item in _import_identities(plan)}
    for manifest_path in destination_root.glob("*/import.json"):
        manifest = _load_json(manifest_path, "existing authoring import")
        if (
            manifest.get("schema") != IMPORT_SCHEMA
            or manifest.get("schema_version") not in SUPPORTED_IMPORT_SCHEMA_VERSIONS
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
            immutable_fields = ("line_id", "text_sha256", "queue_item_sha256")
            if current is not None and any(
                not isinstance(existing.get(field), str)
                or existing.get(field) != current.get(field)
                for field in immutable_fields
            ):
                raise LegacyAuthoringImportError(
                    f"Immutable queue identity {queue_id!r} conflicts with existing import "
                    f"{manifest_path.parent}. No application data was changed."
                )


def _validate_existing_import(
    destination: Path, plan: _ImportPlan
) -> LegacyImportResult:
    manifest_path = destination / "import.json"
    manifest = _load_json(manifest_path, "existing authoring import")
    if (
        manifest.get("schema") != IMPORT_SCHEMA
        or manifest.get("schema_version") not in SUPPORTED_IMPORT_SCHEMA_VERSIONS
    ):
        raise LegacyAuthoringImportError(
            f"Import destination already exists with an unsupported manifest: {destination}"
        )
    expected = _existing_import_expected_manifest(manifest, plan)
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or source.get("logical_identity") != plan.logical_identity
    ):
        raise LegacyAuthoringImportError(
            f"Import ID collision at {destination}; choose a different destination root"
        )
    if source.get("source_fingerprint") != plan.source_fingerprint:
        raise LegacyAuthoringImportError(
            "Legacy source changed after it was imported. Existing application data was "
            f"left untouched at {destination}."
        )
    _validate_existing_manifest_equivalence(
        manifest, expected, source, plan, manifest_path
    )
    _validate_existing_artifacts(manifest, destination, plan, manifest_path)
    _verify_source_controls_unchanged(plan)
    return LegacyImportResult(destination, manifest, False)


def _existing_import_expected_manifest(
    manifest: JsonDocument, plan: _ImportPlan
) -> JsonDocument:
    expected = _import_manifest(plan, _import_id(plan))
    expected["imported_at"] = manifest.get("imported_at")
    if manifest.get("schema_version") == 1:
        expected["schema_version"] = 1
        expected_legacy = expected.get("legacy_job")
        if isinstance(expected_legacy, dict):
            expected_legacy.pop("created_at", None)
            expected_legacy.pop("updated_at", None)
    return expected


def _validate_existing_manifest_equivalence(
    manifest: JsonDocument,
    expected: JsonDocument,
    source: JsonDocument,
    plan: _ImportPlan,
    manifest_path: Path,
) -> None:
    if source.get("source_directory") == str(plan.job_directory):
        if manifest != expected:
            raise LegacyAuthoringImportError(
                f"Existing import manifest was modified: {manifest_path}. No files were overwritten."
            )
        return
    for field in ("schema", "schema_version", "import_id", "summary", "identities"):
        if manifest.get(field) != expected.get(field):
            raise LegacyAuthoringImportError(
                f"Existing logical import conflicts in {field}: {manifest_path}"
            )
    expected_source = expected.get("source")
    if not isinstance(expected_source, dict):
        raise LegacyAuthoringImportError(
            f"Expected import source is invalid: {manifest_path}"
        )
    for field in ("kind", "source_fingerprint", "logical_identity"):
        if source.get(field) != expected_source.get(field):
            raise LegacyAuthoringImportError(
                f"Existing logical import source conflicts in {field}: {manifest_path}"
            )
    _validate_logical_import_inventory(manifest, expected, manifest_path)


def _validate_logical_import_inventory(
    manifest: JsonDocument, expected: JsonDocument, manifest_path: Path
) -> None:
    actual_artifacts = manifest.get("artifacts")
    if not isinstance(actual_artifacts, list):
        raise LegacyAuthoringImportError(
            f"Existing import inventory is invalid: {manifest_path}"
        )
    actual_inventory = {
        item.get("path"): item for item in actual_artifacts if isinstance(item, dict)
    }
    expected_artifacts = expected.get("artifacts")
    if not isinstance(expected_artifacts, list):
        raise LegacyAuthoringImportError(
            f"Expected import inventory is invalid: {manifest_path}"
        )
    expected_inventory = {
        item["path"]: item for item in expected_artifacts if isinstance(item, dict)
    }
    if set(actual_inventory) != set(expected_inventory):
        raise LegacyAuthoringImportError(
            f"Existing import artifact inventory was modified: {manifest_path}"
        )
    for path, expected_item in expected_inventory.items():
        actual_item = actual_inventory[path]
        if path == "legacy/job.json":
            if actual_item.get("role") != "legacy_job":
                raise LegacyAuthoringImportError(
                    f"Existing legacy job inventory was modified: {manifest_path}"
                )
        elif actual_item != expected_item:
            raise LegacyAuthoringImportError(
                f"Existing import artifact inventory conflicts at {path}: {manifest_path}"
            )


def _validate_existing_artifacts(
    manifest: JsonDocument, destination: Path, plan: _ImportPlan, manifest_path: Path
) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != len(plan.artifacts):
        raise LegacyAuthoringImportError(
            f"Existing import artifact inventory is incomplete: {manifest_path}"
        )
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise LegacyAuthoringImportError(
                f"Existing import manifest is malformed: {manifest_path}"
            )
        relative = _safe_relative(artifact.get("path"), "imported artifact path")
        path = _within(destination, relative, "imported artifact path")
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            raise LegacyAuthoringImportError(
                f"Existing imported artifact is missing or modified: {path}. No files were overwritten."
            )


def _verify_source_controls_unchanged(plan: _ImportPlan) -> None:
    for artifact in plan.artifacts:
        if (
            not artifact.source.is_file()
            or sha256_file(artifact.source) != artifact.sha256
        ):
            raise LegacyAuthoringImportError(
                "Legacy source is active or changed during import; retry when idle. "
                "No application data was published."
            )


def _legacy_runtime_status(job: JsonDocument) -> tuple[str, tuple[str, ...]]:
    status = _optional_text(job.get("status")) or "unknown"
    pid = job.get("pid")
    pid_status = _pid_status(pid)
    if status == "running":
        if pid_status == "dead":
            diagnostic = (
                f"Legacy job remains marked running, but recorded PID {pid} is "
                "proven absent; this stable snapshot is preserved as interrupted."
            )
            return "interrupted", (diagnostic,)
        if pid_status == "live":
            raise LegacyAuthoringImportError(
                "Pregeneration source is active; retry import when the legacy job is idle"
            )
        raise LegacyAuthoringImportError(
            "Legacy job is marked running, but its recorded PID is missing, invalid, "
            "or cannot be inspected. Stop the producer and retry when its status is "
            "known idle."
        )
    if pid_status == "live":
        raise LegacyAuthoringImportError(
            "Pregeneration source has a live recorded PID; retry import when idle"
        )
    return status, ()


def _pid_status(value: object) -> str:
    if isinstance(value, bool):
        return "unknown"
    status = inspect_process_status(value)
    return status if status in {"live", "dead", "unknown"} else "unknown"


def _add_artifact(
    artifacts: ArtifactMap,
    role: str,
    source: str | Path,
    destination: str | Path,
    *,
    expected_sha256: str | None = None,
) -> None:
    source = Path(source).resolve()
    destination = Path(destination)
    digest = sha256_file(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise LegacyAuthoringImportError(
            "Legacy source changed while it was being validated; retry when idle"
        )
    existing = artifacts.get(destination)
    if existing is not None:
        if existing.source != source or existing.sha256 != digest:
            raise LegacyAuthoringImportError(
                f"Two source artifacts collide at imported path {destination}"
            )
        return
    artifacts[destination] = _CopyArtifact(role, source, destination, digest)


def _import_id(plan: _ImportPlan) -> str:
    return f"legacy-{plan.logical_identity[:24]}"


def _job_path(job_directory: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise LegacyAuthoringImportError(
            f"Pregeneration job is missing its {label} path"
        )
    return _resolve_path(job_directory, value)


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _safe_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise LegacyAuthoringImportError(f"{label} must be a non-empty relative path")
    if "\\" in value:
        raise LegacyAuthoringImportError(f"{label} must use POSIX separators")
    parts = value.split("/")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise LegacyAuthoringImportError(
            f"{label} must stay within its owning directory"
        )
    return Path(*path.parts)


def _within(root: str | Path, relative: Path, label: str) -> Path:
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise LegacyAuthoringImportError(
            f"{label} leaves its owning directory"
        ) from error
    return path


def _relative_within(root: str | Path, path: str | Path, label: str) -> Path:
    root = Path(root).resolve()
    try:
        return Path(path).resolve().relative_to(root)
    except ValueError as error:
        raise LegacyAuthoringImportError(
            f"{label} leaves the generation output"
        ) from error


def _load_json(path: str | Path, description: str) -> JsonDocument:
    value = load_json_object(path, description, error_type=LegacyAuthoringImportError)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise LegacyAuthoringImportError(f"{description} must be a JSON object: {path}")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _load_json_snapshot(path: str | Path, description: str) -> tuple[JsonDocument, str]:
    payload, digest = _read_snapshot(path, description)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LegacyAuthoringImportError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise LegacyAuthoringImportError(f"{description.title()} must be a JSON object")
    return value, digest


def _load_queue_snapshot(path: str | Path) -> tuple[VoiceGenerationQueue, str]:
    path = Path(path).expanduser().resolve()
    payload, digest = _read_snapshot(path, "generation queue")
    with tempfile.TemporaryDirectory(prefix="vntts-legacy-queue-") as directory:
        snapshot = Path(directory) / "queue.jsonl"
        snapshot.write_bytes(payload)
        try:
            parsed = VoiceGenerationQueue.load(snapshot)
        except VoiceGenerationQueueError as error:
            raise LegacyAuthoringImportError(
                f"Incompatible generation queue {path}: {error}. "
                "Re-export it with vntts-artifacts v0.6 before importing."
            ) from error
    return VoiceGenerationQueue(path, parsed.metadata, parsed.items), digest


def _load_generated_index_snapshot(
    path: str | Path,
) -> tuple[GeneratedAudioIndex, JsonDocument, str]:
    path = Path(path).expanduser().resolve()
    payload, digest = _read_snapshot(path, "generated-audio manifest")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LegacyAuthoringImportError(
            f"Unable to read generated-audio manifest {path}: {error}"
        ) from error
    with tempfile.TemporaryDirectory(prefix="vntts-legacy-manifest-") as directory:
        snapshot = Path(directory) / "manifest.json"
        snapshot.write_bytes(payload)
        try:
            parsed = GeneratedAudioIndex.load(snapshot)
        except GeneratedAudioManifestError as error:
            raise LegacyAuthoringImportError(
                f"Incompatible generated-audio manifest {path}: {error}"
            ) from error
    raw_entries = raw.get("entries", []) if isinstance(raw, dict) else []
    entries = []
    for entry, record in zip(parsed.entries, raw_entries, strict=True):
        relative = _safe_relative(record.get("audio"), "generated WAV path")
        entries.append(
            type(entry)(
                line_id=entry.line_id,
                text_sha256=entry.text_sha256,
                audio=_within(path.parent, relative, "generated WAV path"),
                audio_format=entry.audio_format,
                audio_sha256=entry.audio_sha256,
                sample_rate=entry.sample_rate,
                sample_count=entry.sample_count,
            )
        )
    return GeneratedAudioIndex(path, parsed.metadata, entries), raw, digest


def _read_snapshot(path: str | Path, description: str) -> tuple[bytes, str]:
    path = Path(path)
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise LegacyAuthoringImportError(
            f"Unable to read {description} {path}: {error}"
        ) from error
    return payload, hashlib.sha256(payload).hexdigest()


def _load_json_optional(path: str | Path) -> JsonDocument:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _load_jsonl_metadata_optional(path: str | Path) -> JsonDocument:
    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.loads(next(stream))
    except OSError, StopIteration, json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_job_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LegacyAuthoringImportError(
            f"Pregeneration job {field} must be an ISO-8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise LegacyAuthoringImportError(
            f"Pregeneration job {field} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise LegacyAuthoringImportError(
            f"Pregeneration job {field} must include a timezone"
        )
    return parsed
