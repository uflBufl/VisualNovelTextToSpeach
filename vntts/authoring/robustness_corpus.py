"""Immutable human-labelled speech robustness corpus publication."""

from __future__ import annotations

import copy
import hashlib
import io
import math
import re
import shutil
import tempfile
import wave
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueError
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.hashing import text_sha256

from vntts.authoring.authority import (
    AuthoringAuthorityError,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
)
from vntts.authoring.bulk_generation import (
    BulkGenerationError,
    load_generation_state,
    measure_generated_speech_bytes,
    normalized_failure_record,
)
from vntts.authoring.cohort_review import (
    CohortReviewError,
    load_cohort_review_decision,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)
from vntts.authoring.workbench import (
    AuthoringWorkbenchError,
    contained_workspace_path,
    load_workspace_authority,
    safe_workspace_relative_path,
)

SPEECH_ROBUSTNESS_CORPUS_SCHEMA = "vntts.speech-robustness-corpus"
SPEECH_ROBUSTNESS_CORPUS_VERSION = 2
SPEECH_ROBUSTNESS_ANALYSIS_VERSION = 1
_HUMAN_LABELS = frozenset({"acceptable", "bad"})


class SpeechRobustnessCorpusError(RuntimeError):
    """Human speech evidence cannot be published or validated safely."""


@dataclass(frozen=True)
class SpeechRobustnessCorpus:
    """One fully validated self-contained robustness corpus."""

    directory: Path
    corpus_id: str
    document: dict

    @property
    def sample_count(self):
        return len(self.document["samples"])

    @property
    def failure_count(self):
        return len(self.document["failures"])

    def to_dict(self):
        return copy.deepcopy(self.document)


@dataclass(frozen=True)
class SpeechRobustnessCorpusResult:
    """Publication result for one immutable corpus directory."""

    directory: Path
    corpus_id: str
    sample_count: int
    failure_count: int
    created: bool

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "corpus_id": self.corpus_id,
            "sample_count": self.sample_count,
            "failure_count": self.failure_count,
            "created": self.created,
        }


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value, label):
    if not isinstance(value, str) or len(value) != 64:
        raise SpeechRobustnessCorpusError(f"{label} must be a full SHA-256")
    try:
        int(value, 16)
    except ValueError as error:
        raise SpeechRobustnessCorpusError(f"{label} must be hexadecimal") from error
    return value


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise SpeechRobustnessCorpusError(f"{label} must be non-empty text")
    return value


def _relative(value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise SpeechRobustnessCorpusError(f"{label} must be a POSIX-relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise SpeechRobustnessCorpusError(f"{label} must stay inside the corpus")
    return Path(*pure.parts)


def _contained(root, relative, label):
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


def _json_snapshot(path, label, *, root=None):
    try:
        snapshot = capture_authority_file(path, label, root=root)
        document = snapshot.json_document(label)
    except AuthoringAuthorityError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    return snapshot, document


def _workspace_snapshot(workspace_directory):
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


def _read_pcm16(payload):
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
            samples = np.frombuffer(source.readframes(count), dtype="<i2").copy()
    except (EOFError, OSError, ValueError, wave.Error) as error:
        raise SpeechRobustnessCorpusError(
            f"Unable to decode robustness audio: {error}"
        ) from error
    if rate < 1 or len(samples) != count or count < 1:
        raise SpeechRobustnessCorpusError("Robustness audio WAV data is invalid")
    return samples, rate


def _max_exact_active_repeat(samples, sample_rate):
    """Return the longest exact repeated active 20 ms block run."""
    block_size = max(1, round(sample_rate * 0.02))
    block_count = len(samples) // block_size
    if block_count < 2:
        return {"seconds": 0.0, "lag_seconds": None}
    blocks = samples[: block_count * block_size].reshape(block_count, block_size)
    active = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1)) >= 184.0
    digests = [_sha256(block.tobytes()) for block in blocks]
    positions = {}
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


def analyze_speech_robustness_bytes(payload):
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
    signals = []
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


def _text_boundaries(text):
    words = tuple(re.finditer(r"[^\W_]+(?:['’][^\W_]+)*", text, flags=re.UNICODE))
    sentence = []
    clause = []
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


def analyze_text_timing_bytes(payload, text):
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
    active_indices = np.flatnonzero(~silent)
    words, sentence_boundaries, clause_boundaries = _text_boundaries(text)
    pauses = []
    if len(active_indices):
        first_active = int(active_indices[0])
        last_active = int(active_indices[-1])
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
            boundary_kind = None
            boundary_distance = None
            for kind, positions in (
                ("sentence", sentence_boundaries),
                ("clause", clause_boundaries),
            ):
                for position in positions:
                    distance = abs(relative - position)
                    if boundary_distance is None or distance < boundary_distance:
                        boundary_kind = kind
                        boundary_distance = distance
            pauses.append(
                {
                    "start_seconds": round(start * frame_samples / sample_rate, 3),
                    "duration_seconds": round(duration, 3),
                    "relative_position": round(relative, 6),
                    "nearest_boundary_kind": boundary_kind,
                    "nearest_boundary_distance": (
                        None
                        if boundary_distance is None
                        else round(boundary_distance, 6)
                    ),
                }
            )
    active_seconds = max(
        frame_samples / sample_rate,
        float(np.sum(~silent)) * frame_samples / sample_rate,
    )
    active_words_per_minute = 60.0 * len(words) / active_seconds
    signals = []
    if any(
        pause["duration_seconds"] >= 0.75
        and (
            pause["nearest_boundary_distance"] is None
            or pause["nearest_boundary_distance"] > 0.15
        )
        for pause in pauses
    ):
        signals.append("unmatched_long_pause_candidate")
    if len(words) >= 4 and active_words_per_minute < 80:
        signals.append("slow_active_speech_candidate")
    if len(words) >= 4 and active_words_per_minute > 260:
        signals.append("fast_active_speech_candidate")
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
        "signals": signals,
    }


def _decision_paths(inputs):
    paths = []
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


def _sample_metadata(item):
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


def _sample_key(workspace_id, queue_id, audio_sha256):
    return (workspace_id, queue_id, audio_sha256)


def _build_sources(decision_inputs, failure_workspaces):
    snapshots = []
    decision_documents = {}
    audio_payloads = {}
    samples = {}
    workspace_cache = {}

    def workspace_authority(path):
        resolved = Path(path).expanduser().resolve()
        cached = workspace_cache.get(resolved)
        if cached is None:
            cached = _workspace_snapshot(resolved)
            workspace_cache[resolved] = cached
            snapshots.append(cached[3])
            snapshots.append(cached[5])
        return cached

    for decision_path in _decision_paths(decision_inputs):
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
        snapshots.append(decision_snapshot)
        raw_assessments = decision.get("sample_assessments")
        if raw_assessments is None:
            # Schema-v1 decisions published before per-sample quality labels
            # remain valid review authority, but cannot be guessed into this
            # explicitly human-labelled corpus.
            continue
        if not isinstance(raw_assessments, list):
            raise SpeechRobustnessCorpusError(
                "Cohort decision sample assessments must be a list"
            )
        assessments = {
            row["queue_id"]: row["assessment"]
            for row in raw_assessments
            if row.get("assessment") in _HUMAN_LABELS
        }
        if not assessments:
            continue
        workspace_path = decision_path.parent.parent
        (
            directory,
            workspace,
            workspace_sha256,
            _state_snapshot,
            state,
            queue_snapshot,
            queue_items,
        ) = workspace_authority(workspace_path)
        workspace_id = _required_text(workspace.get("workspace_id"), "Workspace ID")
        reviewed = {row["queue_id"]: row for row in decision["reviewed_samples"]}
        for queue_id, label in sorted(assessments.items()):
            evidence = reviewed.get(queue_id)
            item = state["items"].get(queue_id)
            queue_item = queue_items.get(queue_id)
            if (
                not isinstance(evidence, dict)
                or not isinstance(item, dict)
                or queue_item is None
            ):
                raise SpeechRobustnessCorpusError(
                    f"Cohort evidence is missing state authority for {queue_id!r}"
                )
            audio_sha256 = _require_sha256(
                evidence.get("audio_sha256"), "Reviewed audio SHA-256"
            )
            if item.get("file_sha256") != audio_sha256:
                raise SpeechRobustnessCorpusError(
                    f"Reviewed audio authority changed for {queue_id!r}"
                )
            try:
                relative = safe_workspace_relative_path(
                    item.get("path"), f"Robustness audio {queue_id!r} path"
                )
                audio_path = contained_workspace_path(
                    directory / "generated-audio", relative, "Robustness audio"
                )
                audio_snapshot = capture_authority_file(
                    audio_path, "robustness audio", root=directory / "generated-audio"
                )
            except (AuthoringAuthorityError, AuthoringWorkbenchError) as error:
                raise SpeechRobustnessCorpusError(str(error)) from error
            if audio_snapshot.sha256 != audio_sha256:
                raise SpeechRobustnessCorpusError(
                    f"Reviewed audio checksum changed for {queue_id!r}"
                )
            snapshots.append(audio_snapshot)
            existing_audio = audio_payloads.setdefault(
                audio_sha256, audio_snapshot.payload
            )
            if existing_audio != audio_snapshot.payload:
                raise SpeechRobustnessCorpusError(
                    f"SHA-256 collision in robustness audio {audio_sha256}"
                )
            key = _sample_key(workspace_id, queue_id, audio_sha256)
            record = samples.get(key)
            if record is None:
                analysis = analyze_speech_robustness_bytes(audio_snapshot.payload)
                record = {
                    "workspace_id": workspace_id,
                    "workspace_sha256": workspace_sha256,
                    "queue_id": queue_id,
                    "queue_sha256": queue_snapshot.sha256,
                    "line_id": evidence["line_id"],
                    "text": queue_item.text,
                    "text_sha256": evidence["text_sha256"],
                    "speaker": queue_item.speaker,
                    "voice_character": queue_item.voice_character,
                    "audio_sha256": audio_sha256,
                    "audio": f"audio/{audio_sha256}.wav",
                    "human_label": label,
                    "technical_flags": sorted(set(evidence["technical_flags"])),
                    "state_item_sha256": canonical_document_sha256(item),
                    "synthesis": _sample_metadata(item),
                    "analysis": analysis,
                    "text_timing": analyze_text_timing_bytes(
                        audio_snapshot.payload, queue_item.text
                    ),
                    "decision_ids": [],
                }
                samples[key] = record
            elif record["human_label"] != label:
                raise SpeechRobustnessCorpusError(
                    f"Conflicting human labels for {workspace_id}/{queue_id}"
                )
            record["decision_ids"].append(decision["decision_id"])
        previous = decision_documents.setdefault(
            decision["decision_id"], decision_snapshot.payload
        )
        if previous != decision_snapshot.payload:
            raise SpeechRobustnessCorpusError(
                f"Decision ID {decision['decision_id']} has conflicting bytes"
            )

    failures = []
    for workspace_input in sorted(
        {Path(path).expanduser().resolve() for path in failure_workspaces}, key=str
    ):
        (
            _directory,
            workspace,
            workspace_sha256,
            state_snapshot,
            state,
            queue_snapshot,
            queue_items,
        ) = workspace_authority(workspace_input)
        workspace_id = _required_text(workspace.get("workspace_id"), "Workspace ID")
        for queue_id, item in sorted(state["items"].items()):
            if item.get("status") != "failed":
                continue
            queue_item = queue_items.get(queue_id)
            if queue_item is None:
                raise SpeechRobustnessCorpusError(
                    f"Failed robustness item is absent from its queue: {queue_id!r}"
                )
            failure = normalized_failure_record(item, text=queue_item.text)
            failures.append(
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
                    "failure": failure,
                    "synthesis": _sample_metadata(item),
                }
            )
    for record in samples.values():
        record["decision_ids"] = sorted(set(record["decision_ids"]))
    return (
        snapshots,
        decision_documents,
        audio_payloads,
        sorted(
            samples.values(), key=lambda row: (row["workspace_id"], row["queue_id"])
        ),
        sorted(failures, key=lambda row: (row["workspace_id"], row["queue_id"])),
    )


def _counts(samples, failures):
    labels = Counter(row["human_label"] for row in samples)
    providers = Counter(
        str(row["synthesis"].get("provider") or "unknown") for row in samples
    )
    provider_labels = Counter(
        (
            str(row["synthesis"].get("provider") or "unknown"),
            row["human_label"],
        )
        for row in samples
    )
    signals = Counter(
        signal for row in samples for signal in row["analysis"]["signals"]
    )
    signal_labels = Counter(
        (signal, row["human_label"])
        for row in samples
        for signal in row["analysis"]["signals"]
    )
    timing_signals = Counter(
        signal
        for row in samples
        for signal in row.get("text_timing", {}).get("signals", ())
    )
    timing_signal_labels = Counter(
        (signal, row["human_label"])
        for row in samples
        for signal in row.get("text_timing", {}).get("signals", ())
    )
    technical_flags = Counter(
        flag for row in samples for flag in row["technical_flags"]
    )
    failure_kinds = Counter(row["failure"]["kind"] for row in failures)
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
            row["human_label"] == "bad" and not row["analysis"]["signals"]
            for row in samples
        ),
    }
    if any("text_timing" in row for row in samples):
        summary["text_timing_signals"] = dict(sorted(timing_signals.items()))
        summary["text_timing_signal_human_labels"] = {
            f"{signal}:{label}": count
            for (signal, label), count in sorted(timing_signal_labels.items())
        }
    return summary


def _document(samples, failures, decisions, audio_payloads):
    inventory = []
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
        "samples": samples,
        "failures": failures,
        "summary": _counts(samples, failures),
        "artifacts": inventory,
    }
    return {**body, "corpus_id": canonical_document_sha256(body)}


def _validate_document(document):
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
    version = document.get("schema_version")
    if document.get("schema") != SPEECH_ROBUSTNESS_CORPUS_SCHEMA or version not in {
        1,
        SPEECH_ROBUSTNESS_CORPUS_VERSION,
    }:
        raise SpeechRobustnessCorpusError("Robustness corpus schema is unsupported")
    policy = document.get("analysis_policy")
    if policy != {
        "schema_version": SPEECH_ROBUSTNESS_ANALYSIS_VERSION,
        "diagnostic_only": True,
        "automatic_rejection": False,
        "human_labels_are_authoritative": True,
    }:
        raise SpeechRobustnessCorpusError("Robustness corpus policy is invalid")
    samples = document.get("samples")
    failures = document.get("failures")
    artifacts = document.get("artifacts")
    if not all(isinstance(value, list) for value in (samples, failures, artifacts)):
        raise SpeechRobustnessCorpusError("Robustness corpus lists are invalid")
    expected_id = canonical_document_sha256(
        {key: value for key, value in document.items() if key != "corpus_id"}
    )
    if document.get("corpus_id") != expected_id:
        raise SpeechRobustnessCorpusError("Robustness corpus identity is invalid")
    if document.get("summary") != _counts(samples, failures):
        raise SpeechRobustnessCorpusError("Robustness corpus summary is invalid")
    paths = [row.get("path") for row in artifacts if isinstance(row, dict)]
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
    sample_keys = {
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
    if version == 2:
        sample_keys |= {
            "queue_sha256",
            "text",
            "speaker",
            "voice_character",
            "text_timing",
        }
    sample_identities = set()
    for sample in samples:
        if (
            not isinstance(sample, dict)
            or set(sample) != sample_keys
            or sample.get("human_label") not in _HUMAN_LABELS
        ):
            raise SpeechRobustnessCorpusError(
                "Robustness corpus human label is invalid"
            )
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
        if version == 2:
            _require_sha256(sample["queue_sha256"], "Sample queue SHA-256")
            text = _required_text(sample["text"], "Sample requested text")
            if text_sha256(text) != sample["text_sha256"]:
                raise SpeechRobustnessCorpusError(
                    "Robustness sample text checksum is invalid"
                )
            _required_text(sample["speaker"], "Sample speaker")
            _required_text(sample["voice_character"], "Sample voice character")
            if sample.get("text_timing", {}).get("policy") != {
                "diagnostic_only": True,
                "automatic_rejection": False,
                "alignment": "proportional_word_position_without_asr",
            }:
                raise SpeechRobustnessCorpusError(
                    "Robustness sample text-timing policy is invalid"
                )
        audio_path = _relative(sample["audio"], "Sample audio path").as_posix()
        if audio_path != f"audio/{sample['audio_sha256']}.wav":
            raise SpeechRobustnessCorpusError("Robustness sample audio path is invalid")
        if (
            not isinstance(sample["technical_flags"], list)
            or sample["technical_flags"] != sorted(set(sample["technical_flags"]))
            or not all(
                isinstance(flag, str) and flag for flag in sample["technical_flags"]
            )
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
        if sample.get("analysis", {}).get("policy") != {
            "diagnostic_only": True,
            "automatic_rejection": False,
        }:
            raise SpeechRobustnessCorpusError("Robustness sample policy is invalid")
        identity = (
            sample["workspace_id"],
            sample["queue_id"],
            sample["audio_sha256"],
        )
        if identity in sample_identities:
            raise SpeechRobustnessCorpusError(
                "Robustness corpus contains a duplicate sample identity"
            )
        sample_identities.add(identity)
    failure_keys = {
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
    if version == 2:
        failure_keys |= {
            "queue_sha256",
            "text",
            "speaker",
            "voice_character",
        }
    failure_identities = set()
    for failure in failures:
        if not isinstance(failure, dict) or set(failure) != failure_keys:
            raise SpeechRobustnessCorpusError(
                "Robustness corpus failure record is invalid"
            )
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
        if version == 2:
            _require_sha256(failure["queue_sha256"], "Failure queue SHA-256")
            text = _required_text(failure["text"], "Failure requested text")
            if text_sha256(text) != failure["text_sha256"]:
                raise SpeechRobustnessCorpusError(
                    "Robustness failure text checksum is invalid"
                )
            _required_text(failure["speaker"], "Failure speaker")
            _required_text(failure["voice_character"], "Failure voice character")
        if failure["line_id"] is not None:
            _required_text(failure["line_id"], "Failure line ID")
        if failure["text_sha256"] is not None:
            _require_sha256(failure["text_sha256"], "Failure text SHA-256")
        if (
            not isinstance(failure["failure"], dict)
            or not isinstance(failure["failure"].get("kind"), str)
            or not isinstance(failure["synthesis"], dict)
        ):
            raise SpeechRobustnessCorpusError(
                "Robustness corpus typed failure is invalid"
            )
        identity = (failure["workspace_id"], failure["queue_id"])
        if identity in failure_identities:
            raise SpeechRobustnessCorpusError(
                "Robustness corpus contains a duplicate failure identity"
            )
        failure_identities.add(identity)
    return document


def load_speech_robustness_corpus(directory):
    """Load and fully validate one immutable self-contained corpus."""
    root = Path(directory).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise SpeechRobustnessCorpusError(f"Robustness corpus is unavailable: {root}")
    root = root.resolve()
    snapshot, document = _json_snapshot(
        root / "corpus.json", "robustness corpus", root=root
    )
    _validate_document(document)
    expected_paths = {"corpus.json"}
    inventory = {row["path"]: row for row in document["artifacts"]}
    for sample in document["samples"]:
        if any(
            f"evidence/decision-{decision_id}.json" not in inventory
            for decision_id in sample["decision_ids"]
        ):
            raise SpeechRobustnessCorpusError(
                "Robustness sample decision evidence is not inventoried"
            )
    artifact_snapshots = {}
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
    observed_paths = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SpeechRobustnessCorpusError("Robustness corpus contains a symlink")
        if path.is_file():
            observed_paths.add(path.relative_to(root).as_posix())
    if observed_paths != expected_paths:
        raise SpeechRobustnessCorpusError("Robustness corpus inventory is not exact")
    for sample in document["samples"]:
        record = inventory.get(sample["audio"])
        if record is None or record["sha256"] != sample["audio_sha256"]:
            raise SpeechRobustnessCorpusError(
                "Robustness sample audio is not inventoried"
            )
        payload = artifact_snapshots[sample["audio"]].payload
        if analyze_speech_robustness_bytes(payload) != sample["analysis"]:
            raise SpeechRobustnessCorpusError("Robustness sample analysis is invalid")
        if (
            document["schema_version"] == 2
            and analyze_text_timing_bytes(payload, sample["text"])
            != sample["text_timing"]
        ):
            raise SpeechRobustnessCorpusError(
                "Robustness sample text-timing analysis is invalid"
            )
    try:
        for relative_text, captured in artifact_snapshots.items():
            assert_authority_snapshot(captured, f"robustness artifact {relative_text}")
        assert_authority_snapshot(snapshot, "robustness corpus")
    except AuthoringAuthorityError as error:
        raise SpeechRobustnessCorpusError(str(error)) from error
    return SpeechRobustnessCorpus(root, document["corpus_id"], document)


def publish_speech_robustness_corpus(
    decision_inputs, failure_workspaces, output_directory
):
    """Publish exact human labels and typed failures without mutating sources."""
    output = Path(output_directory).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    for decision_input in decision_inputs:
        source = Path(decision_input).expanduser().resolve()
        if source.is_dir():
            try:
                output.relative_to(source)
            except ValueError:
                pass
            else:
                raise SpeechRobustnessCorpusError(
                    "Robustness corpus output must be outside decision inputs"
                )
    snapshots, decisions, audio, samples, failures = _build_sources(
        decision_inputs, failure_workspaces
    )
    if not samples:
        raise SpeechRobustnessCorpusError(
            "No explicit acceptable/bad cohort assessments were found"
        )
    document = _document(samples, failures, decisions, audio)
    if output.exists():
        loaded = load_speech_robustness_corpus(output)
        if loaded.document != document:
            raise SpeechRobustnessCorpusError(
                f"Robustness corpus destination conflicts: {output}"
            )
        return SpeechRobustnessCorpusResult(
            output,
            document["corpus_id"],
            len(samples),
            len(failures),
            False,
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}.", suffix=".staging", dir=output.parent
        )
    )
    try:
        for audio_sha256, payload in sorted(audio.items()):
            path = staging / "audio" / f"{audio_sha256}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        for decision_id, payload in sorted(decisions.items()):
            path = staging / "evidence" / f"decision-{decision_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        atomic_write_json(staging / "corpus.json", document)
        load_speech_robustness_corpus(staging)
        for snapshot in snapshots:
            try:
                assert_authority_snapshot(snapshot, "robustness source")
            except AuthoringAuthorityError as error:
                raise SpeechRobustnessCorpusError(str(error)) from error
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            if output.exists():
                loaded = load_speech_robustness_corpus(output)
                if loaded.document == document:
                    return SpeechRobustnessCorpusResult(
                        output,
                        document["corpus_id"],
                        len(samples),
                        len(failures),
                        False,
                    )
            raise SpeechRobustnessCorpusError(
                f"Unable to publish robustness corpus: {error}"
            ) from error
        load_speech_robustness_corpus(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return SpeechRobustnessCorpusResult(
        output,
        document["corpus_id"],
        len(samples),
        len(failures),
        True,
    )


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
