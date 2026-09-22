"""Immutable human-labelled speech robustness corpus publication."""

from __future__ import annotations

import copy
import hashlib
import io
import math
import re
import tempfile
import wave
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias, TypedDict

import numpy as np
from numpy.typing import NDArray
from vntts_artifacts import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
    VoiceGenerationQueueItem,
)
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.hashing import text_sha256

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    AuthoritySnapshot,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    normalized_failure_record,
)
from vntts.authoring.cohort_review import (
    COHORT_REVIEW_DEFECT_REASONS,
    CohortReviewError,
    load_cohort_review_decision,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.authoring.speech_quality import measure_generated_speech_bytes
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    load_workspace_authority,
    safe_workspace_relative_path,
)
from vntts.authoring.workspace_foundation import require_sha256

SPEECH_ROBUSTNESS_CORPUS_SCHEMA = "vntts.speech-robustness-corpus"
SPEECH_ROBUSTNESS_CORPUS_VERSION = 3
SUPPORTED_SPEECH_ROBUSTNESS_CORPUS_VERSIONS = frozenset({1, 2, 3})
SPEECH_ROBUSTNESS_ANALYSIS_VERSION = 1
_HUMAN_LABELS = frozenset({"acceptable", "bad"})

JsonDocument: TypeAlias = dict[str, object]
SampleKey: TypeAlias = tuple[str, str, str]
FailureKey: TypeAlias = tuple[str, str]
WorkspaceSource: TypeAlias = tuple[
    Path,
    JsonDocument,
    str,
    AuthoritySnapshot,
    JsonDocument,
    AuthoritySnapshot,
    dict[str, VoiceGenerationQueueItem],
]


class _RepeatSignal(TypedDict):
    seconds: float
    lag_seconds: float | None


class _Assessment(TypedDict):
    human_label: str
    human_defect_reasons: list[str]


class _SampleRecord(TypedDict):
    workspace_id: str
    workspace_sha256: str
    queue_id: str
    queue_sha256: str
    line_id: str
    text: str
    text_sha256: str
    speaker: str | None
    voice_character: str | None
    audio_sha256: str
    audio: str
    human_label: str
    human_defect_reasons: list[str]
    technical_flags: list[str]
    state_item_sha256: str
    synthesis: JsonDocument
    analysis: JsonDocument
    text_timing: JsonDocument
    decision_ids: list[str]


class _FailureRecord(TypedDict):
    workspace_id: str
    workspace_sha256: str
    state_sha256: str
    queue_sha256: str
    queue_id: str
    line_id: str | None
    text: str
    text_sha256: str
    speaker: str | None
    voice_character: str | None
    state_item_sha256: str
    failure: JsonDocument
    synthesis: JsonDocument


class _ArtifactRecord(TypedDict):
    path: str
    sha256: str
    size: int


class _Pause(TypedDict):
    start_seconds: float
    duration_seconds: float
    relative_position: float
    nearest_boundary_kind: str | None
    nearest_boundary_distance: float | None


class SpeechRobustnessCorpusError(RuntimeError):
    """Human speech evidence cannot be published or validated safely."""


@dataclass(frozen=True)
class SpeechRobustnessCorpus:
    """One fully validated self-contained robustness corpus."""

    directory: Path
    corpus_id: str
    document: JsonDocument

    @property
    def sample_count(self) -> int:
        return len(_document_rows(self.document.get("samples"), "Corpus samples"))

    @property
    def failure_count(self) -> int:
        return len(_document_rows(self.document.get("failures"), "Corpus failures"))

    def to_dict(self) -> JsonDocument:
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class SpeechRobustnessCorpusResult:
    """Publication result for one immutable corpus directory."""

    directory: Path
    corpus_id: str
    sample_count: int
    failure_count: int
    created: bool

    def to_dict(self) -> JsonDocument:
        return {
            "directory": str(self.directory),
            "corpus_id": self.corpus_id,
            "sample_count": self.sample_count,
            "failure_count": self.failure_count,
            "created": self.created,
        }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: object, label: str) -> str:
    return _required_text(
        require_sha256(value, label, error_type=SpeechRobustnessCorpusError), label
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpeechRobustnessCorpusError(f"{label} must be non-empty text")
    return value


def _relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise SpeechRobustnessCorpusError(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise SpeechRobustnessCorpusError(f"{label} must stay inside the corpus")
    return Path(*pure.parts)


def _contained(root: str | Path, relative: Path, label: str) -> Path:
    root = Path(root).resolve()
    candidate = root / relative
    if candidate.is_symlink():
        raise SpeechRobustnessCorpusError(f"{label} must not be a symlink")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SpeechRobustnessCorpusError(f"{label} leaves the corpus") from error
    return resolved


def _json_snapshot(
    path: str | Path, label: str, *, root: str | Path | None = None
) -> tuple[AuthoritySnapshot, JsonDocument]:
    try:
        snapshot = capture_authority_file(path, label, root=root)
        document = snapshot.json_document(label)
    except AuthoringAuthorityError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    return snapshot, document


def _document_rows(value: object, label: str) -> list[JsonDocument]:
    if not isinstance(value, list):
        raise SpeechRobustnessCorpusError(f"{label} must be a list")
    rows: list[JsonDocument] = []
    for row in value:
        if not isinstance(row, dict) or not all(isinstance(key, str) for key in row):
            raise SpeechRobustnessCorpusError(f"{label} must contain objects")
        rows.append({key: item for key, item in row.items() if isinstance(key, str)})
    return rows


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise SpeechRobustnessCorpusError(f"{label} must be a list of text")
    return list(value)


def _document_map(value: object, label: str) -> dict[str, JsonDocument]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SpeechRobustnessCorpusError(f"{label} must be an object")
    result: dict[str, JsonDocument] = {}
    for key, row in value.items():
        if not isinstance(row, dict) or not all(isinstance(name, str) for name in row):
            raise SpeechRobustnessCorpusError(f"{label} entries must be objects")
        result[key] = {
            name: item for name, item in row.items() if isinstance(name, str)
        }
    return result


def _object_field(document: JsonDocument, field: str, label: str) -> JsonDocument:
    value = document.get(field)
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SpeechRobustnessCorpusError(f"{label} must be an object")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _required_schema_version(document: JsonDocument) -> int:
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SpeechRobustnessCorpusError("Cohort decision schema version is invalid")
    return version


def _workspace_snapshot(
    workspace_directory: str | Path,
) -> tuple[
    Path,
    JsonDocument,
    str,
    AuthoritySnapshot,
    JsonDocument,
    AuthoritySnapshot,
    dict[str, VoiceGenerationQueueItem],
]:
    try:
        directory, workspace, workspace_sha256 = load_workspace_authority(
            workspace_directory
        )
    except AuthoringWorkbenchError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    state_path = directory / "generated-audio/generation-state.json"
    queue_path = directory / "queue.jsonl"
    try:
        queue_snapshot = capture_authority_file(
            queue_path, "robustness source queue", root=directory
        )
        with tempfile.TemporaryDirectory(prefix="vntts-robustness-queue-") as temporary:
            snapshot_path = Path(temporary) / "queue.jsonl"
            snapshot_path.write_bytes(queue_snapshot.payload)
            queue = VoiceGenerationQueue.load(snapshot_path)
        assert_authority_snapshot(queue_snapshot, "robustness source queue")
    except (AuthoringAuthorityError, VoiceGenerationQueueError) as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    state_snapshot, parsed = _json_snapshot(
        state_path, "robustness source generation state", root=directory
    )
    try:
        validated = load_generation_state(state_path, directory / "queue.jsonl")
    except BulkGenerationError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    if parsed != validated:
        raise SpeechRobustnessCorpusError(
            "Robustness source generation state changed while it was loaded"
        )
    try:
        assert_authority_snapshot(state_snapshot, "robustness source generation state")
    except AuthoringAuthorityError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    if parsed.get("active") is not None:
        raise SpeechRobustnessCorpusError("Robustness source has an active generation")
    if (state_path.parent / ".generation-lease.json").exists():
        raise SpeechRobustnessCorpusError("Robustness source has a generation lease")
    if parsed.get("queue_sha256") != queue_snapshot.sha256:
        raise SpeechRobustnessCorpusError(
            "Robustness source state is bound to a different queue"
        )
    return (
        directory,
        workspace,
        workspace_sha256,
        state_snapshot,
        parsed,
        queue_snapshot,
        {item.queue_id: item for item in queue.items},
    )


def _read_pcm16(payload: bytes) -> tuple[NDArray[np.int16], int]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
            ):
                raise SpeechRobustnessCorpusError(
                    "Robustness audio must be mono 16-bit PCM WAV"
                )
            rate = source.getframerate()
            count = source.getnframes()
            samples: NDArray[np.int16] = np.frombuffer(
                source.readframes(count), dtype="<i2"
            ).copy()
    except (EOFError, OSError, ValueError, wave.Error) as error:
        raise SpeechRobustnessCorpusError(
            f"Unable to decode robustness audio: {error}"
        ) from error
    if rate < 1 or len(samples) != count or count < 1:
        raise SpeechRobustnessCorpusError("Robustness audio WAV data is invalid")
    return samples, rate


def _max_exact_active_repeat(
    samples: NDArray[np.int16], sample_rate: int
) -> _RepeatSignal:
    """Return the longest exact repeated active 20 ms block run."""
    block_size = max(1, round(sample_rate * 0.02))
    block_count = len(samples) // block_size
    if block_count < 2:
        return {"seconds": 0.0, "lag_seconds": None}
    blocks = samples[: block_count * block_size].reshape(block_count, block_size)
    active = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1)) >= 184.0
    digests = [_sha256(block.tobytes()) for block in blocks]
    positions: dict[str, list[int]] = {}
    best_blocks = 0
    best_lag = None
    for index, digest in enumerate(digests):
        if active[index]:
            for previous in positions.get(digest, ())[-16:]:
                if previous == index:
                    continue
                length = 0
                while (
                    index + length < block_count
                    and previous + length < index
                    and active[index + length]
                    and active[previous + length]
                    and digests[index + length] == digests[previous + length]
                ):
                    length += 1
                if length > best_blocks:
                    best_blocks = length
                    best_lag = index - previous
            positions.setdefault(digest, []).append(index)
    return {
        "seconds": round(best_blocks * block_size / sample_rate, 4),
        "lag_seconds": (
            None if best_lag is None else round(best_lag * block_size / sample_rate, 4)
        ),
    }


def analyze_speech_robustness_bytes(payload: bytes) -> JsonDocument:
    """Compute versioned diagnostic-only artifact signals for one exact WAV."""
    if not isinstance(payload, bytes):
        raise SpeechRobustnessCorpusError("Robustness audio payload must be bytes")
    samples, sample_rate = _read_pcm16(payload)
    normalized = samples.astype(np.float64) / 32768.0
    absolute = np.abs(normalized)
    differences = np.abs(np.diff(normalized))
    try:
        speech_quality = asdict(measure_generated_speech_bytes(payload))
    except BulkGenerationError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    repeated = _max_exact_active_repeat(samples, sample_rate)
    peak = float(np.max(absolute))
    rms = float(math.sqrt(float(np.mean(normalized**2))))
    clipping_fraction = float(np.mean(absolute >= 0.999))
    dc_offset = float(np.mean(normalized))
    max_jump = float(np.max(differences)) if len(differences) else 0.0
    high_jump_fraction = (
        float(np.mean(differences >= 0.75)) if len(differences) else 0.0
    )
    zero_crossing_rate = (
        float(np.mean(np.signbit(normalized[1:]) != np.signbit(normalized[:-1])))
        if len(normalized) > 1
        else 0.0
    )
    signals: list[str] = []
    if repeated["seconds"] >= 0.24:
        signals.append("exact_pcm_repeat_candidate")
    if peak >= 0.999 or clipping_fraction >= 0.001:
        signals.append("near_clipping_candidate")
    if abs(dc_offset) >= 0.05:
        signals.append("dc_offset_candidate")
    if max_jump >= 1.5 or high_jump_fraction >= 0.0005:
        signals.append("discontinuity_candidate")
    return {
        "schema_version": SPEECH_ROBUSTNESS_ANALYSIS_VERSION,
        "policy": {
            "diagnostic_only": True,
            "automatic_rejection": False,
        },
        "sample_rate": sample_rate,
        "sample_count": len(samples),
        "duration_seconds": round(len(samples) / sample_rate, 4),
        "peak": round(peak, 6),
        "rms": round(rms, 6),
        "crest_factor": None if rms == 0 else round(peak / rms, 6),
        "clipping_fraction": round(clipping_fraction, 8),
        "dc_offset": round(dc_offset, 8),
        "max_adjacent_jump": round(max_jump, 6),
        "high_jump_fraction": round(high_jump_fraction, 8),
        "zero_crossing_rate": round(zero_crossing_rate, 8),
        "exact_active_repeat": repeated,
        "speech_quality": speech_quality,
        "signals": signals,
    }


def _text_boundaries(
    text: str,
) -> tuple[tuple[re.Match[str], ...], list[float], list[float]]:
    words = tuple(re.finditer(r"[^\W_]+(?:['’][^\W_]+)*", text, flags=re.UNICODE))
    sentence: list[float] = []
    clause: list[float] = []
    if len(words) < 2:
        return words, sentence, clause
    for index, current in enumerate(words[:-1]):
        separator = text[current.end() : words[index + 1].start()]
        position = round((index + 1) / len(words), 6)
        if re.search(r"[.!?]", separator):
            sentence.append(position)
        elif re.search(r"[,;:—–-]", separator):
            clause.append(position)
    return words, sentence, clause


def _nearest_text_boundary(
    relative: float, sentence: Sequence[float], clause: Sequence[float]
) -> tuple[str | None, float | None]:
    nearest_kind: str | None = None
    nearest_distance: float | None = None
    for kind, positions in (("sentence", sentence), ("clause", clause)):
        for position in positions:
            distance = abs(relative - position)
            if nearest_distance is None or distance < nearest_distance:
                nearest_kind = kind
                nearest_distance = distance
    return nearest_kind, nearest_distance


def _internal_pauses(
    silent: NDArray[np.bool_],
    frame_samples: int,
    sample_rate: int,
    sentence_boundaries: Sequence[float],
    clause_boundaries: Sequence[float],
) -> list[_Pause]:
    active_indices = np.flatnonzero(~silent)
    if not len(active_indices):
        return []
    first_active = int(active_indices[0])
    last_active = int(active_indices[-1])
    pauses: list[_Pause] = []
    index = first_active + 1
    while index < last_active:
        if not silent[index]:
            index += 1
            continue
        start = index
        while index <= last_active and silent[index]:
            index += 1
        end = index
        duration = (end - start) * frame_samples / sample_rate
        if duration < 0.24:
            continue
        relative = ((start + end) / 2 - first_active) / max(
            1, last_active - first_active
        )
        boundary_kind, boundary_distance = _nearest_text_boundary(
            relative, sentence_boundaries, clause_boundaries
        )
        pauses.append(
            {
                "start_seconds": round(start * frame_samples / sample_rate, 3),
                "duration_seconds": round(duration, 3),
                "relative_position": round(relative, 6),
                "nearest_boundary_kind": boundary_kind,
                "nearest_boundary_distance": (
                    None if boundary_distance is None else round(boundary_distance, 6)
                ),
            }
        )
    return pauses


def _timing_signals(
    pauses: Sequence[_Pause], word_count: int, active_words_per_minute: float
) -> list[str]:
    signals: list[str] = []
    if any(
        pause["duration_seconds"] >= 0.75
        and (
            pause["nearest_boundary_distance"] is None
            or pause["nearest_boundary_distance"] > 0.15
        )
        for pause in pauses
    ):
        signals.append("unmatched_long_pause_candidate")
    if word_count >= 4 and active_words_per_minute < 80:
        signals.append("slow_active_speech_candidate")
    if word_count >= 4 and active_words_per_minute > 260:
        signals.append("fast_active_speech_candidate")
    return signals


def analyze_text_timing_bytes(payload: bytes, text: str) -> JsonDocument:
    """Estimate pause placement against requested text without claiming ASR."""
    text = _required_text(text, "Requested speech text")
    samples, sample_rate = _read_pcm16(payload)
    normalized = samples.astype(np.float64) / 32768.0
    frame_samples = max(1, round(sample_rate * 0.08))
    frame_rms = np.asarray(
        [
            math.sqrt(float(np.mean(normalized[start : start + frame_samples] ** 2)))
            for start in range(0, len(normalized), frame_samples)
        ]
    )
    silent = frame_rms <= 10 ** (-45.0 / 20.0)
    words, sentence_boundaries, clause_boundaries = _text_boundaries(text)
    pauses = _internal_pauses(
        silent,
        frame_samples,
        sample_rate,
        sentence_boundaries,
        clause_boundaries,
    )
    active_seconds = max(
        frame_samples / sample_rate,
        float(np.sum(~silent)) * frame_samples / sample_rate,
    )
    active_words_per_minute = 60.0 * len(words) / active_seconds
    return {
        "schema_version": 1,
        "policy": {
            "diagnostic_only": True,
            "automatic_rejection": False,
            "alignment": "proportional_word_position_without_asr",
        },
        "word_count": len(words),
        "sentence_boundary_positions": sentence_boundaries,
        "clause_boundary_positions": clause_boundaries,
        "active_words_per_minute": round(active_words_per_minute, 3),
        "internal_pauses": pauses,
        "signals": _timing_signals(pauses, len(words), active_words_per_minute),
    }


def _decision_paths(inputs: Iterable[str | Path]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in inputs:
        path = Path(value).expanduser()
        if path.is_symlink():
            raise SpeechRobustnessCorpusError(
                f"Cohort decision input must not be a symlink: {path}"
            )
        if path.is_file():
            paths.append(path.resolve())
        elif path.is_dir():
            paths.extend(
                candidate.resolve()
                for candidate in path.rglob("decision-*.json")
                if candidate.is_file() and not candidate.is_symlink()
            )
        else:
            raise SpeechRobustnessCorpusError(
                f"Cohort decision input is unavailable: {path}"
            )
    return tuple(sorted(set(paths), key=str))


def _sample_metadata(item: JsonDocument) -> JsonDocument:
    keys = (
        "provider",
        "model",
        "generation_profile",
        "speaker",
        "voice_character",
        "requested_voice_character",
        "seed",
        "seed_applied",
        "attempts",
        "completion",
        "quality",
        "speech_quality",
        "failure_repair",
        "text_transform",
    )
    return {key: copy.deepcopy(item[key]) for key in keys if key in item}


def _sample_key(workspace_id: str, queue_id: str, audio_sha256: str) -> SampleKey:
    return (workspace_id, queue_id, audio_sha256)


@dataclass
class _SourceBuilder:
    snapshots: list[AuthoritySnapshot]
    decision_documents: dict[str, bytes]
    audio_payloads: dict[str, bytes]
    samples: dict[SampleKey, _SampleRecord]
    sample_decision_versions: dict[SampleKey, int]
    workspace_cache: dict[Path, WorkspaceSource]
    failures: list[_FailureRecord]

    def workspace_authority(self, path: str | Path) -> WorkspaceSource:
        resolved = Path(path).expanduser().resolve()
        cached = self.workspace_cache.get(resolved)
        if cached is None:
            cached = _workspace_snapshot(resolved)
            self.workspace_cache[resolved] = cached
            self.snapshots.extend((cached[3], cached[5]))
        return cached

    def add_decision(self, decision_path: Path) -> None:
        decision_snapshot, raw_decision = _json_snapshot(
            decision_path, "cohort review decision"
        )
        try:
            decision = load_cohort_review_decision(decision_path).document
        except CohortReviewError as error:
            raise SpeechRobustnessCorpusError(str(error)) from error
        if decision != raw_decision:
            raise SpeechRobustnessCorpusError(
                "Cohort decision changed while it was loaded"
            )
        self.snapshots.append(decision_snapshot)
        assessments = _decision_assessments(decision)
        if not assessments:
            return
        self.add_assessments(decision_path, decision, assessments)
        decision_id = _require_sha256(decision.get("decision_id"), "Cohort decision ID")
        previous = self.decision_documents.setdefault(
            decision_id, decision_snapshot.payload
        )
        if previous != decision_snapshot.payload:
            raise SpeechRobustnessCorpusError(
                f"Decision ID {decision['decision_id']} has conflicting bytes"
            )

    def add_assessments(
        self,
        decision_path: Path,
        decision: JsonDocument,
        assessments: dict[str, _Assessment],
    ) -> None:
        (
            directory,
            workspace,
            workspace_sha256,
            _state_snapshot,
            state,
            queue_snapshot,
            queue_items,
        ) = self.workspace_authority(decision_path.parent.parent)
        workspace_id = _required_text(workspace.get("workspace_id"), "Workspace ID")
        reviewed = _reviewed_samples(decision)
        state_items = _document_map(state.get("items"), "Generation state items")
        for queue_id, assessment in sorted(assessments.items()):
            self.add_assessment(
                decision,
                directory,
                workspace_id,
                workspace_sha256,
                queue_snapshot,
                queue_items,
                reviewed,
                state_items,
                queue_id,
                assessment,
            )

    def add_assessment(
        self,
        decision: JsonDocument,
        directory: Path,
        workspace_id: str,
        workspace_sha256: str,
        queue_snapshot: AuthoritySnapshot,
        queue_items: dict[str, VoiceGenerationQueueItem],
        reviewed: dict[str, JsonDocument],
        state_items: dict[str, JsonDocument],
        queue_id: str,
        assessment: _Assessment,
    ) -> None:
        evidence, item, queue_item = _assessment_authority(
            reviewed, state_items, queue_items, queue_id
        )
        audio_sha256 = _require_sha256(
            evidence.get("audio_sha256"), "Reviewed audio SHA-256"
        )
        if item.get("file_sha256") != audio_sha256:
            raise SpeechRobustnessCorpusError(
                f"Reviewed audio authority changed for {queue_id!r}"
            )
        audio_snapshot = _source_audio_snapshot(directory, item, queue_id)
        if audio_snapshot.sha256 != audio_sha256:
            raise SpeechRobustnessCorpusError(
                f"Reviewed audio checksum changed for {queue_id!r}"
            )
        self.snapshots.append(audio_snapshot)
        existing_audio = self.audio_payloads.setdefault(
            audio_sha256, audio_snapshot.payload
        )
        if existing_audio != audio_snapshot.payload:
            raise SpeechRobustnessCorpusError(
                f"SHA-256 collision in robustness audio {audio_sha256}"
            )
        key = _sample_key(workspace_id, queue_id, audio_sha256)
        record = self.samples.get(key)
        if record is None:
            record = _sample_record(
                workspace_id,
                workspace_sha256,
                queue_id,
                queue_snapshot.sha256,
                evidence,
                item,
                queue_item,
                audio_sha256,
                audio_snapshot.payload,
                assessment,
            )
            self.samples[key] = record
            self.sample_decision_versions[key] = _required_schema_version(decision)
        else:
            self.merge_assessment(key, record, decision, assessment)
        record["decision_ids"].append(
            _require_sha256(decision.get("decision_id"), "Cohort decision ID")
        )

    def merge_assessment(
        self,
        key: SampleKey,
        record: _SampleRecord,
        decision: JsonDocument,
        assessment: _Assessment,
    ) -> None:
        label = assessment["human_label"]
        current_version = _required_schema_version(decision)
        if record["human_label"] != label:
            previous_version = self.sample_decision_versions[key]
            if {previous_version, current_version} != {1, 4}:
                raise SpeechRobustnessCorpusError(
                    f"Conflicting human labels for {key[0]}/{key[1]}"
                )
            if current_version == 4:
                record["human_label"] = label
                record["human_defect_reasons"] = assessment["human_defect_reasons"]
                self.sample_decision_versions[key] = current_version
            return
        self.sample_decision_versions[key] = max(
            self.sample_decision_versions[key], current_version
        )
        record["human_defect_reasons"] = sorted(
            set(record["human_defect_reasons"])
            | set(assessment["human_defect_reasons"])
        )

    def add_failure_workspace(self, workspace_input: Path) -> None:
        (
            _directory,
            workspace,
            workspace_sha256,
            state_snapshot,
            state,
            queue_snapshot,
            queue_items,
        ) = self.workspace_authority(workspace_input)
        workspace_id = _required_text(workspace.get("workspace_id"), "Workspace ID")
        for queue_id, item in sorted(
            _document_map(state.get("items"), "Generation state items").items()
        ):
            if item.get("status") == "failed":
                self.add_failure(
                    workspace_id,
                    workspace_sha256,
                    state_snapshot,
                    queue_snapshot,
                    queue_items,
                    queue_id,
                    item,
                )

    def add_failure(
        self,
        workspace_id: str,
        workspace_sha256: str,
        state_snapshot: AuthoritySnapshot,
        queue_snapshot: AuthoritySnapshot,
        queue_items: dict[str, VoiceGenerationQueueItem],
        queue_id: str,
        item: JsonDocument,
    ) -> None:
        queue_item = queue_items.get(queue_id)
        if queue_item is None:
            raise SpeechRobustnessCorpusError(
                f"Failed robustness item is absent from its queue: {queue_id!r}"
            )
        self.failures.append(
            {
                "workspace_id": workspace_id,
                "workspace_sha256": workspace_sha256,
                "state_sha256": state_snapshot.sha256,
                "queue_sha256": queue_snapshot.sha256,
                "queue_id": queue_id,
                "line_id": queue_item.line_id,
                "text": queue_item.text,
                "text_sha256": queue_item.text_sha256,
                "speaker": queue_item.speaker,
                "voice_character": queue_item.voice_character,
                "state_item_sha256": canonical_document_sha256(item),
                "failure": normalized_failure_record(item, text=queue_item.text),
                "synthesis": _sample_metadata(item),
            }
        )


def _decision_assessments(decision: JsonDocument) -> dict[str, _Assessment]:
    raw_assessments = decision.get("sample_assessments")
    if raw_assessments is None:
        return {}
    assessments: dict[str, _Assessment] = {}
    for row in _document_rows(raw_assessments, "Cohort decision sample assessments"):
        label = row.get("assessment")
        if label in _HUMAN_LABELS:
            queue_id = _required_text(row.get("queue_id"), "Cohort assessment queue ID")
            assessments[queue_id] = {
                "human_label": _required_text(label, "Cohort assessment label"),
                "human_defect_reasons": _string_list(
                    row.get("defect_reasons", ()), "Cohort assessment defect reasons"
                ),
            }
    return assessments


def _reviewed_samples(decision: JsonDocument) -> dict[str, JsonDocument]:
    return {
        _required_text(row.get("queue_id"), "Reviewed sample queue ID"): row
        for row in _document_rows(
            decision.get("reviewed_samples"), "Cohort reviewed samples"
        )
    }


def _assessment_authority(
    reviewed: dict[str, JsonDocument],
    state_items: dict[str, JsonDocument],
    queue_items: dict[str, VoiceGenerationQueueItem],
    queue_id: str,
) -> tuple[JsonDocument, JsonDocument, VoiceGenerationQueueItem]:
    evidence = reviewed.get(queue_id)
    item = state_items.get(queue_id)
    queue_item = queue_items.get(queue_id)
    if evidence is None or item is None or queue_item is None:
        raise SpeechRobustnessCorpusError(
            f"Cohort evidence is missing state authority for {queue_id!r}"
        )
    return evidence, item, queue_item


def _source_audio_snapshot(
    directory: Path, item: JsonDocument, queue_id: str
) -> AuthoritySnapshot:
    try:
        relative = safe_workspace_relative_path(
            item.get("path"), f"Robustness audio {queue_id!r} path"
        )
        audio_path = contained_workspace_path(
            directory / "generated-audio", relative, "Robustness audio"
        )
        return capture_authority_file(
            audio_path, "robustness audio", root=directory / "generated-audio"
        )
    except (AuthoringAuthorityError, AuthoringWorkbenchError) as error:
        raise SpeechRobustnessCorpusError(str(error)) from error


def _sample_record(
    workspace_id: str,
    workspace_sha256: str,
    queue_id: str,
    queue_sha256: str,
    evidence: JsonDocument,
    item: JsonDocument,
    queue_item: VoiceGenerationQueueItem,
    audio_sha256: str,
    audio_payload: bytes,
    assessment: _Assessment,
) -> _SampleRecord:
    return {
        "workspace_id": workspace_id,
        "workspace_sha256": workspace_sha256,
        "queue_id": queue_id,
        "queue_sha256": queue_sha256,
        "line_id": _required_text(evidence.get("line_id"), "Reviewed line ID"),
        "text": queue_item.text,
        "text_sha256": _require_sha256(
            evidence.get("text_sha256"), "Reviewed text SHA-256"
        ),
        "speaker": queue_item.speaker,
        "voice_character": queue_item.voice_character,
        "audio_sha256": audio_sha256,
        "audio": f"audio/{audio_sha256}.wav",
        "human_label": assessment["human_label"],
        "human_defect_reasons": assessment["human_defect_reasons"],
        "technical_flags": sorted(
            set(_string_list(evidence.get("technical_flags"), "Technical flags"))
        ),
        "state_item_sha256": canonical_document_sha256(item),
        "synthesis": _sample_metadata(item),
        "analysis": analyze_speech_robustness_bytes(audio_payload),
        "text_timing": analyze_text_timing_bytes(audio_payload, queue_item.text),
        "decision_ids": [],
    }


def _build_sources(
    decision_inputs: Iterable[str | Path], failure_workspaces: Iterable[str | Path]
) -> tuple[
    list[AuthoritySnapshot],
    dict[str, bytes],
    dict[str, bytes],
    list[_SampleRecord],
    list[_FailureRecord],
]:
    builder = _SourceBuilder([], {}, {}, {}, {}, {}, [])
    for decision_path in _decision_paths(decision_inputs):
        builder.add_decision(decision_path)
    for workspace_input in sorted(
        {Path(path).expanduser().resolve() for path in failure_workspaces}, key=str
    ):
        builder.add_failure_workspace(workspace_input)
    for record in builder.samples.values():
        record["decision_ids"] = sorted(set(record["decision_ids"]))
    return (
        builder.snapshots,
        builder.decision_documents,
        builder.audio_payloads,
        sorted(
            builder.samples.values(),
            key=lambda row: (row["workspace_id"], row["queue_id"]),
        ),
        sorted(
            builder.failures, key=lambda row: (row["workspace_id"], row["queue_id"])
        ),
    )


def _counts(
    samples: Sequence[JsonDocument], failures: Sequence[JsonDocument]
) -> JsonDocument:
    labels = Counter(
        _required_text(row.get("human_label"), "Human label") for row in samples
    )
    providers = Counter(
        str(
            _object_field(row, "synthesis", "Sample synthesis").get("provider")
            or "unknown"
        )
        for row in samples
    )
    provider_labels = Counter(
        (
            str(
                _object_field(row, "synthesis", "Sample synthesis").get("provider")
                or "unknown"
            ),
            _required_text(row.get("human_label"), "Human label"),
        )
        for row in samples
    )
    signals = Counter(
        signal
        for row in samples
        for signal in _string_list(
            _object_field(row, "analysis", "Sample analysis").get("signals"),
            "Analysis signals",
        )
    )
    signal_labels = Counter(
        (signal, _required_text(row.get("human_label"), "Human label"))
        for row in samples
        for signal in _string_list(
            _object_field(row, "analysis", "Sample analysis").get("signals"),
            "Analysis signals",
        )
    )
    timing_signals = Counter(
        signal
        for row in samples
        for signal in _string_list(
            _object_field(row, "text_timing", "Sample text timing").get("signals"),
            "Timing signals",
        )
    )
    timing_signal_labels = Counter(
        (signal, _required_text(row.get("human_label"), "Human label"))
        for row in samples
        for signal in _string_list(
            _object_field(row, "text_timing", "Sample text timing").get("signals"),
            "Timing signals",
        )
    )
    defect_reasons = Counter(
        reason
        for row in samples
        for reason in _string_list(
            row.get("human_defect_reasons", ()), "Human defect reasons"
        )
    )
    technical_flags = Counter(
        flag
        for row in samples
        for flag in _string_list(row.get("technical_flags"), "Technical flags")
    )
    failure_kinds = Counter(
        _required_text(
            _object_field(row, "failure", "Failure record").get("kind"), "Failure kind"
        )
        for row in failures
    )
    summary = {
        "sample_count": len(samples),
        "failure_count": len(failures),
        "human_labels": dict(sorted(labels.items())),
        "providers": dict(sorted(providers.items())),
        "provider_labels": {
            f"{provider}:{label}": count
            for (provider, label), count in sorted(provider_labels.items())
        },
        "diagnostic_signals": dict(sorted(signals.items())),
        "diagnostic_signal_human_labels": {
            f"{signal}:{label}": count
            for (signal, label), count in sorted(signal_labels.items())
        },
        "technical_flags": dict(sorted(technical_flags.items())),
        "failure_kinds": dict(sorted(failure_kinds.items())),
        "bad_without_diagnostic_signal": sum(
            row.get("human_label") == "bad"
            and not _string_list(
                _object_field(row, "analysis", "Sample analysis").get("signals"),
                "Analysis signals",
            )
            for row in samples
        ),
    }
    if any("text_timing" in row for row in samples):
        summary["text_timing_signals"] = dict(sorted(timing_signals.items()))
        summary["text_timing_signal_human_labels"] = {
            f"{signal}:{label}": count
            for (signal, label), count in sorted(timing_signal_labels.items())
        }
    if any("human_defect_reasons" in row for row in samples):
        summary["human_defect_reasons"] = dict(sorted(defect_reasons.items()))
    return summary


def _document(
    samples: Sequence[_SampleRecord],
    failures: Sequence[_FailureRecord],
    decisions: dict[str, bytes],
    audio_payloads: dict[str, bytes],
) -> JsonDocument:
    inventory: list[_ArtifactRecord] = []
    for audio_sha256, payload in sorted(audio_payloads.items()):
        inventory.append(
            {
                "path": f"audio/{audio_sha256}.wav",
                "sha256": _sha256(payload),
                "size": len(payload),
            }
        )
    for decision_id, payload in sorted(decisions.items()):
        inventory.append(
            {
                "path": f"evidence/decision-{decision_id}.json",
                "sha256": _sha256(payload),
                "size": len(payload),
            }
        )
    body = {
        "schema": SPEECH_ROBUSTNESS_CORPUS_SCHEMA,
        "schema_version": SPEECH_ROBUSTNESS_CORPUS_VERSION,
        "analysis_policy": {
            "schema_version": SPEECH_ROBUSTNESS_ANALYSIS_VERSION,
            "diagnostic_only": True,
            "automatic_rejection": False,
            "human_labels_are_authoritative": True,
        },
        "samples": [dict(sample) for sample in samples],
        "failures": [dict(failure) for failure in failures],
        "summary": _counts(
            [dict(sample) for sample in samples],
            [dict(failure) for failure in failures],
        ),
        "artifacts": inventory,
    }
    return {**body, "corpus_id": canonical_document_sha256(body)}


def _corpus_version(document: JsonDocument) -> int:
    version = document.get("schema_version")
    if (
        document.get("schema") != SPEECH_ROBUSTNESS_CORPUS_SCHEMA
        or version not in SUPPORTED_SPEECH_ROBUSTNESS_CORPUS_VERSIONS
    ):
        raise SpeechRobustnessCorpusError("Robustness corpus schema is unsupported")
    assert isinstance(version, int)
    return version


def _sample_keys(version: int) -> set[str]:
    keys = {
        "workspace_id",
        "workspace_sha256",
        "queue_id",
        "line_id",
        "text_sha256",
        "audio_sha256",
        "audio",
        "human_label",
        "technical_flags",
        "state_item_sha256",
        "synthesis",
        "analysis",
        "decision_ids",
    }
    if version >= 2:
        keys |= {"queue_sha256", "text", "speaker", "voice_character", "text_timing"}
    if version >= 3:
        keys.add("human_defect_reasons")
    return keys


def _failure_keys(version: int) -> set[str]:
    keys = {
        "workspace_id",
        "workspace_sha256",
        "state_sha256",
        "queue_id",
        "line_id",
        "text_sha256",
        "state_item_sha256",
        "failure",
        "synthesis",
    }
    if version >= 2:
        keys |= {"queue_sha256", "text", "speaker", "voice_character"}
    return keys


def _validate_artifacts(artifacts: Sequence[JsonDocument]) -> None:
    paths = [row.get("path") for row in artifacts]
    if len(paths) != len(artifacts) or len(set(paths)) != len(paths):
        raise SpeechRobustnessCorpusError(
            "Robustness corpus artifact inventory is invalid"
        )
    for artifact in artifacts:
        if set(artifact) != {"path", "sha256", "size"}:
            raise SpeechRobustnessCorpusError(
                "Robustness corpus artifact record is invalid"
            )
        _relative(artifact["path"], "Robustness artifact path")
        _require_sha256(artifact["sha256"], "Artifact SHA-256")
        if not isinstance(artifact["size"], int) or artifact["size"] < 1:
            raise SpeechRobustnessCorpusError(
                "Robustness artifact size must be positive"
            )


def _validate_sample_version(sample: JsonDocument, version: int) -> None:
    if version < 2:
        return
    _require_sha256(sample["queue_sha256"], "Sample queue SHA-256")
    text = _required_text(sample["text"], "Sample requested text")
    if text_sha256(text) != sample["text_sha256"]:
        raise SpeechRobustnessCorpusError("Robustness sample text checksum is invalid")
    _required_text(sample["speaker"], "Sample speaker")
    _required_text(sample["voice_character"], "Sample voice character")
    if _object_field(sample, "text_timing", "Sample text timing").get("policy") != {
        "diagnostic_only": True,
        "automatic_rejection": False,
        "alignment": "proportional_word_position_without_asr",
    }:
        raise SpeechRobustnessCorpusError(
            "Robustness sample text-timing policy is invalid"
        )


def _validate_sample_reasons(sample: JsonDocument, version: int) -> None:
    if version < 3:
        return
    reasons = sample["human_defect_reasons"]
    if (
        not isinstance(reasons, list)
        or reasons != sorted(set(reasons))
        or any(reason not in COHORT_REVIEW_DEFECT_REASONS for reason in reasons)
        or (sample["human_label"] == "acceptable" and reasons)
    ):
        raise SpeechRobustnessCorpusError(
            "Robustness sample human defect reasons are invalid"
        )


def _validate_sample_evidence(sample: JsonDocument) -> None:
    if (
        not isinstance(sample["technical_flags"], list)
        or sample["technical_flags"] != sorted(set(sample["technical_flags"]))
        or not all(isinstance(flag, str) and flag for flag in sample["technical_flags"])
        or not isinstance(sample["synthesis"], dict)
        or not isinstance(sample["analysis"], dict)
        or not isinstance(sample["decision_ids"], list)
        or not sample["decision_ids"]
        or sample["decision_ids"] != sorted(set(sample["decision_ids"]))
    ):
        raise SpeechRobustnessCorpusError(
            "Robustness sample evidence fields are invalid"
        )
    for decision_id in sample["decision_ids"]:
        _require_sha256(decision_id, "Sample decision ID")
    if _object_field(sample, "analysis", "Sample analysis").get("policy") != {
        "diagnostic_only": True,
        "automatic_rejection": False,
    }:
        raise SpeechRobustnessCorpusError("Robustness sample policy is invalid")


def _validate_sample(
    sample: JsonDocument, version: int, identities: set[SampleKey]
) -> None:
    if (
        set(sample) != _sample_keys(version)
        or sample.get("human_label") not in _HUMAN_LABELS
    ):
        raise SpeechRobustnessCorpusError("Robustness corpus human label is invalid")
    for field, label in (
        ("workspace_id", "Sample workspace ID"),
        ("queue_id", "Sample queue ID"),
        ("line_id", "Sample line ID"),
    ):
        _required_text(sample[field], label)
    for field, label in (
        ("workspace_sha256", "Sample workspace SHA-256"),
        ("text_sha256", "Sample text SHA-256"),
        ("audio_sha256", "Sample audio SHA-256"),
        ("state_item_sha256", "Sample state item SHA-256"),
    ):
        _require_sha256(sample[field], label)
    _validate_sample_version(sample, version)
    _validate_sample_reasons(sample, version)
    if (
        _relative(sample["audio"], "Sample audio path").as_posix()
        != f"audio/{sample['audio_sha256']}.wav"
    ):
        raise SpeechRobustnessCorpusError("Robustness sample audio path is invalid")
    _validate_sample_evidence(sample)
    identity: SampleKey = (
        _required_text(sample.get("workspace_id"), "Sample workspace ID"),
        _required_text(sample.get("queue_id"), "Sample queue ID"),
        _require_sha256(sample.get("audio_sha256"), "Sample audio SHA-256"),
    )
    if identity in identities:
        raise SpeechRobustnessCorpusError(
            "Robustness corpus contains a duplicate sample identity"
        )
    identities.add(identity)


def _validate_samples(samples: Sequence[JsonDocument], version: int) -> None:
    identities: set[SampleKey] = set()
    for sample in samples:
        _validate_sample(sample, version, identities)


def _validate_failure_version(failure: JsonDocument, version: int) -> None:
    if version < 2:
        return
    _require_sha256(failure["queue_sha256"], "Failure queue SHA-256")
    text = _required_text(failure["text"], "Failure requested text")
    if text_sha256(text) != failure["text_sha256"]:
        raise SpeechRobustnessCorpusError("Robustness failure text checksum is invalid")
    _required_text(failure["speaker"], "Failure speaker")
    _required_text(failure["voice_character"], "Failure voice character")


def _validate_failure(
    failure: JsonDocument, version: int, identities: set[FailureKey]
) -> None:
    if set(failure) != _failure_keys(version):
        raise SpeechRobustnessCorpusError("Robustness corpus failure record is invalid")
    for field, label in (
        ("workspace_id", "Failure workspace ID"),
        ("queue_id", "Failure queue ID"),
    ):
        _required_text(failure[field], label)
    for field, label in (
        ("workspace_sha256", "Failure workspace SHA-256"),
        ("state_sha256", "Failure state SHA-256"),
        ("state_item_sha256", "Failure state item SHA-256"),
    ):
        _require_sha256(failure[field], label)
    _validate_failure_version(failure, version)
    if failure["line_id"] is not None:
        _required_text(failure["line_id"], "Failure line ID")
    if failure["text_sha256"] is not None:
        _require_sha256(failure["text_sha256"], "Failure text SHA-256")
    if (
        not isinstance(failure["failure"], dict)
        or not isinstance(failure["failure"].get("kind"), str)
        or not isinstance(failure["synthesis"], dict)
    ):
        raise SpeechRobustnessCorpusError("Robustness corpus typed failure is invalid")
    identity: FailureKey = (
        _required_text(failure.get("workspace_id"), "Failure workspace ID"),
        _required_text(failure.get("queue_id"), "Failure queue ID"),
    )
    if identity in identities:
        raise SpeechRobustnessCorpusError(
            "Robustness corpus contains a duplicate failure identity"
        )
    identities.add(identity)


def _validate_failures(failures: Sequence[JsonDocument], version: int) -> None:
    identities: set[FailureKey] = set()
    for failure in failures:
        _validate_failure(failure, version, identities)


def _validate_document(document: JsonDocument) -> JsonDocument:
    expected = {
        "schema",
        "schema_version",
        "analysis_policy",
        "samples",
        "failures",
        "summary",
        "artifacts",
        "corpus_id",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise SpeechRobustnessCorpusError("Robustness corpus document shape is invalid")
    version = _corpus_version(document)
    if document.get("analysis_policy") != {
        "schema_version": SPEECH_ROBUSTNESS_ANALYSIS_VERSION,
        "diagnostic_only": True,
        "automatic_rejection": False,
        "human_labels_are_authoritative": True,
    }:
        raise SpeechRobustnessCorpusError("Robustness corpus policy is invalid")
    samples = _document_rows(document.get("samples"), "Robustness corpus samples")
    failures = _document_rows(document.get("failures"), "Robustness corpus failures")
    artifacts = _document_rows(document.get("artifacts"), "Robustness corpus artifacts")
    expected_id = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "corpus_id"}
    )
    if document.get("corpus_id") != expected_id:
        raise SpeechRobustnessCorpusError("Robustness corpus identity is invalid")
    if document.get("summary") != _counts(samples, failures):
        raise SpeechRobustnessCorpusError("Robustness corpus summary is invalid")
    _validate_artifacts(artifacts)
    _validate_samples(samples, version)
    _validate_failures(failures, version)
    return document


def _corpus_inventory(
    document: JsonDocument,
) -> tuple[list[JsonDocument], int, set[str], dict[str, JsonDocument]]:
    samples = _document_rows(document.get("samples"), "Robustness corpus samples")
    artifacts = _document_rows(document.get("artifacts"), "Robustness corpus artifacts")
    version = _required_schema_version(document)
    expected_paths = {"corpus.json"}
    inventory = {
        _required_text(row.get("path"), "Robustness artifact path"): row
        for row in artifacts
    }
    return samples, version, expected_paths, inventory


def _validate_sample_decision_inventory(
    samples: Sequence[JsonDocument], inventory: dict[str, JsonDocument]
) -> None:
    for sample in samples:
        if any(
            f"evidence/decision-{decision_id}.json" not in inventory
            for decision_id in _string_list(
                sample.get("decision_ids"), "Sample decision IDs"
            )
        ):
            raise SpeechRobustnessCorpusError(
                "Robustness sample decision evidence is not inventoried"
            )


def _capture_corpus_artifacts(
    root: Path, inventory: dict[str, JsonDocument], expected_paths: set[str]
) -> dict[str, AuthoritySnapshot]:
    artifact_snapshots: dict[str, AuthoritySnapshot] = {}
    for relative_text, record in inventory.items():
        relative = _relative(relative_text, "Robustness artifact path")
        path = _contained(root, relative, "Robustness artifact")
        try:
            captured = capture_authority_file(path, "robustness artifact", root=root)
        except AuthoringAuthorityError as error:
            raise SpeechRobustnessCorpusError(str(error)) from error
        if captured.sha256 != _require_sha256(
            record.get("sha256"), "Artifact SHA-256"
        ) or len(captured.payload) != record.get("size"):
            raise SpeechRobustnessCorpusError(
                f"Robustness artifact changed: {relative_text}"
            )
        expected_paths.add(relative.as_posix())
        artifact_snapshots[relative_text] = captured
    return artifact_snapshots


def _validate_corpus_layout(root: Path, expected_paths: set[str]) -> None:
    observed_paths = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SpeechRobustnessCorpusError("Robustness corpus contains a symlink")
        if path.is_file():
            observed_paths.add(path.relative_to(root).as_posix())
    if observed_paths != expected_paths:
        raise SpeechRobustnessCorpusError("Robustness corpus inventory is not exact")


def _validate_sample_artifacts(
    samples: Sequence[JsonDocument],
    inventory: dict[str, JsonDocument],
    artifact_snapshots: dict[str, AuthoritySnapshot],
    version: int,
) -> None:
    for sample in samples:
        audio = _required_text(sample.get("audio"), "Sample audio path")
        audio_record = inventory.get(audio)
        if audio_record is None or audio_record.get("sha256") != sample.get(
            "audio_sha256"
        ):
            raise SpeechRobustnessCorpusError(
                "Robustness sample audio is not inventoried"
            )
        payload = artifact_snapshots[audio].payload
        if analyze_speech_robustness_bytes(payload) != sample.get("analysis"):
            raise SpeechRobustnessCorpusError("Robustness sample analysis is invalid")
        if version >= 2 and analyze_text_timing_bytes(
            payload, _required_text(sample.get("text"), "Sample requested text")
        ) != sample.get("text_timing"):
            raise SpeechRobustnessCorpusError(
                "Robustness sample text-timing analysis is invalid"
            )


def _assert_corpus_snapshots(
    artifact_snapshots: dict[str, AuthoritySnapshot], snapshot: AuthoritySnapshot
) -> None:
    try:
        for relative_text, captured in artifact_snapshots.items():
            assert_authority_snapshot(captured, f"robustness artifact {relative_text}")
        assert_authority_snapshot(snapshot, "robustness corpus")
    except AuthoringAuthorityError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error


def load_speech_robustness_corpus(
    directory: str | Path,
) -> SpeechRobustnessCorpus:
    """Load and fully validate one immutable self-contained corpus."""
    root = Path(directory).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise SpeechRobustnessCorpusError(f"Robustness corpus is unavailable: {root}")
    root = root.resolve()
    snapshot, document = _json_snapshot(
        root / "corpus.json", "robustness corpus", root=root
    )
    _validate_document(document)
    samples, version, expected_paths, inventory = _corpus_inventory(document)
    _validate_sample_decision_inventory(samples, inventory)
    artifact_snapshots = _capture_corpus_artifacts(root, inventory, expected_paths)
    _validate_corpus_layout(root, expected_paths)
    _validate_sample_artifacts(samples, inventory, artifact_snapshots, version)
    _assert_corpus_snapshots(artifact_snapshots, snapshot)
    return SpeechRobustnessCorpus(
        root, _require_sha256(document.get("corpus_id"), "Corpus ID"), document
    )


def _publication_output(
    output_directory: str | Path, decision_inputs: Sequence[str | Path]
) -> Path:
    output = Path(output_directory).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    for decision_input in decision_inputs:
        source = Path(decision_input).expanduser().resolve()
        if source.is_dir():
            try:
                output.relative_to(source)
            except ValueError:
                continue
            raise SpeechRobustnessCorpusError(
                "Robustness corpus output must be outside decision inputs"
            )
    return output


def _corpus_result(
    output: Path,
    document: JsonDocument,
    samples: Sequence[_SampleRecord],
    failures: Sequence[_FailureRecord],
    created: bool,
) -> SpeechRobustnessCorpusResult:
    return SpeechRobustnessCorpusResult(
        output,
        _require_sha256(document.get("corpus_id"), "Corpus ID"),
        len(samples),
        len(failures),
        created,
    )


def _existing_corpus_result(
    output: Path,
    document: JsonDocument,
    samples: Sequence[_SampleRecord],
    failures: Sequence[_FailureRecord],
) -> SpeechRobustnessCorpusResult | None:
    if not output.exists():
        return None
    loaded = load_speech_robustness_corpus(output)
    if loaded.document != document:
        raise SpeechRobustnessCorpusError(
            f"Robustness corpus destination conflicts: {output}"
        )
    return _corpus_result(output, document, samples, failures, False)


def _write_staged_corpus(
    staging: Path,
    document: JsonDocument,
    decisions: dict[str, bytes],
    audio: dict[str, bytes],
) -> None:
    for audio_sha256, payload in sorted(audio.items()):
        path = staging / "audio" / f"{audio_sha256}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    for decision_id, payload in sorted(decisions.items()):
        path = staging / "evidence" / f"decision-{decision_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    atomic_write_json(staging / "corpus.json", document)


def _assert_source_snapshots(snapshots: Sequence[AuthoritySnapshot]) -> None:
    for snapshot in snapshots:
        try:
            assert_authority_snapshot(snapshot, "robustness source")
        except AuthoringAuthorityError as error:
            raise SpeechRobustnessCorpusError(str(error)) from error


def _rename_staged_corpus(
    staging: Path,
    output: Path,
    document: JsonDocument,
    samples: Sequence[_SampleRecord],
    failures: Sequence[_FailureRecord],
) -> SpeechRobustnessCorpusResult | None:
    try:
        rename_directory_no_replace(staging, output)
    except (AtomicPublicationError, OSError) as error:
        existing = _existing_corpus_result(output, document, samples, failures)
        if existing is not None:
            return existing
        raise SpeechRobustnessCorpusError(
            f"Unable to publish robustness corpus: {error}"
        ) from error
    return None


def publish_speech_robustness_corpus(
    decision_inputs: Iterable[str | Path],
    failure_workspaces: Iterable[str | Path],
    output_directory: str | Path,
) -> SpeechRobustnessCorpusResult:
    """Publish exact human labels and typed failures without mutating sources."""
    decision_input_paths = tuple(decision_inputs)
    failure_workspace_paths = tuple(failure_workspaces)
    output = _publication_output(output_directory, decision_input_paths)
    snapshots, decisions, audio, samples, failures = _build_sources(
        decision_input_paths, failure_workspace_paths
    )
    if not samples:
        raise SpeechRobustnessCorpusError(
            "No explicit acceptable/bad cohort assessments were found"
        )
    document = _document(samples, failures, decisions, audio)
    existing = _existing_corpus_result(output, document, samples, failures)
    if existing is not None:
        return existing
    with staged_directory(output.parent, prefix=f".{output.name}.staging-") as staging:
        _write_staged_corpus(staging, document, decisions, audio)
        load_speech_robustness_corpus(staging)
        _assert_source_snapshots(snapshots)
        raced = _rename_staged_corpus(staging, output, document, samples, failures)
        if raced is not None:
            return raced
        load_speech_robustness_corpus(output)
    return _corpus_result(output, document, samples, failures, True)


__all__ = [
    "SPEECH_ROBUSTNESS_ANALYSIS_VERSION",
    "SPEECH_ROBUSTNESS_CORPUS_SCHEMA",
    "SPEECH_ROBUSTNESS_CORPUS_VERSION",
    "SpeechRobustnessCorpus",
    "SpeechRobustnessCorpusError",
    "SpeechRobustnessCorpusResult",
    "analyze_speech_robustness_bytes",
    "load_speech_robustness_corpus",
    "publish_speech_robustness_corpus",
]
