"""Immutable human review for one source-backed non-verbal audio event."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts import VoiceGenerationQueue, VoiceGenerationQueueError
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.audio_events import audio_event_plan_for_record
from vntts.authoring.authority import (
    AuthoringAuthorityError,
    assert_authority_snapshot,
    canonical_document_sha256,
    capture_authority_file,
    write_json_document_no_replace,
)
from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
)

AUDIO_EVENT_REVIEW_SCHEMA = "vntts.authoring-audio-event-review"
AUDIO_EVENT_REVIEW_VERSION = 1
AUDIO_EVENT_DECISION_SCHEMA = "vntts.authoring-audio-event-decision"
AUDIO_EVENT_DECISION_VERSION = 1
AUDIO_EVENT_DECISIONS = frozenset({"accept", "reject"})


class AudioEventReviewError(RuntimeError):
    """An audio-event review artifact is invalid or cannot be published."""


@dataclass(frozen=True)
class AudioEventReview:
    directory: Path
    review_id: str
    queue_id: str
    audio: Path
    audio_sha256: str
    decision: str | None

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "review_id": self.review_id,
            "queue_id": self.queue_id,
            "audio": str(self.audio),
            "audio_sha256": self.audio_sha256,
            "decision": self.decision,
        }


def publish_source_audio_event_review(
    queue_path,
    queue_id,
    source_story_index,
    source_audio,
    output,
    *,
    source_line_id,
    source_speaker,
    source_event,
    source_bank,
    source_media_id,
    source_audio_id=None,
):
    """Snapshot one exact source clip without changing generation authority."""
    queue_path = Path(queue_path).expanduser().resolve()
    source_story_index = Path(source_story_index).expanduser().resolve()
    source_audio = Path(source_audio).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if output.exists() or output.is_symlink():
        raise AudioEventReviewError(f"Audio-event review output exists: {output}")
    try:
        queue_snapshot = capture_authority_file(queue_path, "generation queue")
        story_snapshot = capture_authority_file(
            source_story_index, "source story index"
        )
        audio_snapshot = capture_authority_file(source_audio, "source event audio")
        queue = _load_queue_snapshot(queue_snapshot.payload)
    except (AuthoringAuthorityError, VoiceGenerationQueueError) as error:
        raise AudioEventReviewError(str(error)) from error
    item = next((value for value in queue.items if value.queue_id == queue_id), None)
    if item is None:
        raise AudioEventReviewError(f"Audio-event queue item is absent: {queue_id}")
    plan = _required_single_tongue_click_plan(item.document)
    source_record = _source_story_record(story_snapshot.payload, source_line_id)
    evidence = {
        "kind": "original-game-line",
        "source_line_id": _required_text(source_line_id, "source line ID"),
        "source_text": "Tsk!",
        "source_text_sha256": hashlib.sha256(b"Tsk!").hexdigest(),
        "source_speaker": _required_text(source_speaker, "source speaker"),
        "source_event": _required_text(source_event, "source event"),
        "source_bank": _required_text(source_bank, "source bank"),
        "source_media_ids": [
            _required_positive_int(source_media_id, "source media ID")
        ],
        "source_audio_id": (
            _required_text(source_audio_id, "source audio ID")
            if source_audio_id is not None
            else None
        ),
        "speaker_identity_claim": False,
        "synthesis_voice_character": None,
        "source_story_index_sha256": story_snapshot.sha256,
    }
    _assert_source_record_matches(evidence, source_record)
    candidate_identity = {
        "queue_sha256": queue_snapshot.sha256,
        "queue_id": item.queue_id,
        "text_sha256": item.text_sha256,
        "audio_event_plan_sha256": canonical_document_sha256(plan),
        "audio_sha256": audio_snapshot.sha256,
        "source": evidence,
    }
    candidate_id = canonical_document_sha256(candidate_identity)
    review_identity = {
        "schema": AUDIO_EVENT_REVIEW_SCHEMA,
        "schema_version": AUDIO_EVENT_REVIEW_VERSION,
        "candidate_id": candidate_id,
        **candidate_identity,
    }
    review_id = canonical_document_sha256(review_identity)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        queue_target = staging / "queue.jsonl"
        queue_target.write_bytes(queue_snapshot.payload)
        audio_target = staging / "audio" / "candidate.wav"
        audio_target.parent.mkdir(parents=True)
        audio_target.write_bytes(audio_snapshot.payload)
        try:
            audio_info = probe_pcm16_mono_wav(audio_target)
        except (OSError, Pcm16MonoWavError) as error:
            raise AudioEventReviewError(
                f"Source event audio is not PCM16 mono WAV: {error}"
            ) from error
        _validate_effect_audio_info(audio_info)
        review = {
            "schema": AUDIO_EVENT_REVIEW_SCHEMA,
            "schema_version": AUDIO_EVENT_REVIEW_VERSION,
            "review_id": review_id,
            "created_at": _utc_now(),
            "queue": "queue.jsonl",
            "queue_sha256": queue_snapshot.sha256,
            "queue_id": item.queue_id,
            "line_id": item.line_id,
            "speaker": item.speaker,
            "voice_character": item.voice_character,
            "text": item.text,
            "text_sha256": item.text_sha256,
            "audio_event_plan": plan,
            "audio_event_plan_sha256": canonical_document_sha256(plan),
            "candidate": {
                "candidate_id": candidate_id,
                "audio": "audio/candidate.wav",
                "audio_sha256": audio_snapshot.sha256,
                "sample_rate": audio_info.sample_rate,
                "sample_count": audio_info.sample_count,
                "duration_seconds": audio_info.duration_seconds,
                "peak": audio_info.peak,
                "source": evidence,
            },
        }
        atomic_write_json(staging / "review.json", review, sort_keys=True)
        load_audio_event_review(staging)
        assert_authority_snapshot(queue_snapshot, "generation queue")
        assert_authority_snapshot(story_snapshot, "source story index")
        assert_authority_snapshot(audio_snapshot, "source event audio")
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise AudioEventReviewError(str(error)) from error
        return load_audio_event_review(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def load_audio_event_review(directory):
    """Load and verify every immutable byte in one audio-event review."""
    directory = Path(directory).expanduser().resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise AudioEventReviewError(f"Audio-event review is unavailable: {directory}")
    try:
        review_snapshot = capture_authority_file(
            directory / "review.json", "audio-event review", root=directory
        )
        review = review_snapshot.json_document("audio-event review")
        queue_snapshot = capture_authority_file(
            _contained_file(directory, review.get("queue"), "review queue"),
            "review queue",
            root=directory,
        )
        candidate = review.get("candidate")
        if not isinstance(candidate, dict):
            raise AudioEventReviewError("Audio-event review candidate is invalid")
        audio_snapshot = capture_authority_file(
            _contained_file(
                directory, candidate.get("audio"), "review candidate audio"
            ),
            "review candidate audio",
            root=directory,
        )
    except AuthoringAuthorityError as error:
        raise AudioEventReviewError(str(error)) from error
    _validate_review_document(review, queue_snapshot, audio_snapshot)
    decision = None
    decision_path = directory / "decision.json"
    if decision_path.exists() or decision_path.is_symlink():
        try:
            decision_snapshot = capture_authority_file(
                decision_path, "audio-event decision", root=directory
            )
            decision_document = decision_snapshot.json_document("audio-event decision")
        except AuthoringAuthorityError as error:
            raise AudioEventReviewError(str(error)) from error
        decision = _validate_decision_document(
            decision_document,
            review,
            review_snapshot.sha256,
        )["decision"]
        assert_authority_snapshot(decision_snapshot, "audio-event decision")
    assert_authority_snapshot(review_snapshot, "audio-event review")
    assert_authority_snapshot(queue_snapshot, "review queue")
    assert_authority_snapshot(audio_snapshot, "review candidate audio")
    return AudioEventReview(
        directory=directory,
        review_id=review["review_id"],
        queue_id=review["queue_id"],
        audio=audio_snapshot.path,
        audio_sha256=audio_snapshot.sha256,
        decision=decision,
    )


def record_audio_event_review_decision(directory, decision):
    """Write exactly one terminal accept/reject decision with no replacement."""
    decision = str(decision).strip()
    if decision not in AUDIO_EVENT_DECISIONS:
        raise AudioEventReviewError("Audio-event decision must be accept or reject")
    review = load_audio_event_review(directory)
    decision_path = review.directory / "decision.json"
    if decision_path.exists() or decision_path.is_symlink():
        current = load_audio_event_review(review.directory)
        if current.decision == decision:
            return current
        raise AudioEventReviewError(
            f"Audio-event review is already decided: {current.decision}"
        )
    try:
        review_snapshot = capture_authority_file(
            review.directory / "review.json",
            "audio-event review",
            root=review.directory,
        )
        review_document = review_snapshot.json_document("audio-event review")
        queue_snapshot = capture_authority_file(
            _contained_file(
                review.directory, review_document.get("queue"), "review queue"
            ),
            "review queue",
            root=review.directory,
        )
        audio_snapshot = capture_authority_file(
            _contained_file(
                review.directory,
                (review_document.get("candidate") or {}).get("audio"),
                "review candidate audio",
            ),
            "review candidate audio",
            root=review.directory,
        )
        _validate_review_document(
            review_document,
            queue_snapshot,
            audio_snapshot,
        )
        if review_document["review_id"] != review.review_id:
            raise AudioEventReviewError("Audio-event review authority changed")
        document = {
            "schema": AUDIO_EVENT_DECISION_SCHEMA,
            "schema_version": AUDIO_EVENT_DECISION_VERSION,
            "review_id": review_document["review_id"],
            "review_sha256": review_snapshot.sha256,
            "queue_sha256": queue_snapshot.sha256,
            "candidate_audio_sha256": audio_snapshot.sha256,
            "decision": decision,
            "reviewed_at": _utc_now(),
        }
        assert_authority_snapshot(review_snapshot, "audio-event review")
        assert_authority_snapshot(queue_snapshot, "review queue")
        assert_authority_snapshot(audio_snapshot, "review candidate audio")
        write_json_document_no_replace(decision_path, document, "audio-event decision")
    except AuthoringAuthorityError as error:
        if (
            decision_path.exists()
            and load_audio_event_review(review.directory).decision == decision
        ):
            return load_audio_event_review(review.directory)
        raise AudioEventReviewError(str(error)) from error
    return load_audio_event_review(review.directory)


def _validate_review_document(review, queue_snapshot, audio_snapshot):
    if (
        review.get("schema") != AUDIO_EVENT_REVIEW_SCHEMA
        or review.get("schema_version") != AUDIO_EVENT_REVIEW_VERSION
    ):
        raise AudioEventReviewError("Unsupported audio-event review schema")
    if review.get("queue_sha256") != queue_snapshot.sha256:
        raise AudioEventReviewError("Audio-event review queue changed")
    try:
        queue = _load_queue_snapshot(queue_snapshot.payload)
    except VoiceGenerationQueueError as error:
        raise AudioEventReviewError(str(error)) from error
    queue_id = _required_text(review.get("queue_id"), "review queue ID")
    item = next((value for value in queue.items if value.queue_id == queue_id), None)
    if item is None:
        raise AudioEventReviewError("Audio-event review queue item is absent")
    plan = _required_single_tongue_click_plan(item.document)
    if review.get("audio_event_plan") != plan or review.get(
        "audio_event_plan_sha256"
    ) != canonical_document_sha256(plan):
        raise AudioEventReviewError("Audio-event review plan changed")
    for field, expected in (
        ("line_id", item.line_id),
        ("speaker", item.speaker),
        ("voice_character", item.voice_character),
        ("text", item.text),
        ("text_sha256", item.text_sha256),
    ):
        if review.get(field) != expected:
            raise AudioEventReviewError(f"Audio-event review {field} changed")
    candidate = review.get("candidate")
    if candidate.get("audio_sha256") != audio_snapshot.sha256:
        raise AudioEventReviewError("Audio-event review audio changed")
    try:
        info = probe_pcm16_mono_wav(audio_snapshot.path)
    except (OSError, Pcm16MonoWavError) as error:
        raise AudioEventReviewError(
            f"Invalid audio-event review WAV: {error}"
        ) from error
    _validate_effect_audio_info(info)
    if (
        candidate.get("sample_rate") != info.sample_rate
        or candidate.get("sample_count") != info.sample_count
        or candidate.get("duration_seconds") != info.duration_seconds
        or candidate.get("peak") != info.peak
    ):
        raise AudioEventReviewError("Audio-event review WAV metadata changed")
    source = _validate_source_evidence(candidate.get("source"))
    candidate_identity = {
        "queue_sha256": queue_snapshot.sha256,
        "queue_id": item.queue_id,
        "text_sha256": item.text_sha256,
        "audio_event_plan_sha256": canonical_document_sha256(plan),
        "audio_sha256": audio_snapshot.sha256,
        "source": source,
    }
    candidate_id = canonical_document_sha256(candidate_identity)
    if candidate.get("candidate_id") != candidate_id:
        raise AudioEventReviewError("Audio-event candidate identity changed")
    review_identity = {
        "schema": AUDIO_EVENT_REVIEW_SCHEMA,
        "schema_version": AUDIO_EVENT_REVIEW_VERSION,
        "candidate_id": candidate_id,
        **candidate_identity,
    }
    if review.get("review_id") != canonical_document_sha256(review_identity):
        raise AudioEventReviewError("Audio-event review identity changed")
    _aware_timestamp(review.get("created_at"), "audio-event review created_at")


def _validate_source_evidence(value):
    if not isinstance(value, dict) or value.get("kind") != "original-game-line":
        raise AudioEventReviewError("Audio-event source evidence is invalid")
    for field in (
        "source_line_id",
        "source_text",
        "source_text_sha256",
        "source_speaker",
        "source_event",
        "source_bank",
    ):
        _required_text(value.get(field), f"audio-event {field}")
    if (
        value["source_text"] != "Tsk!"
        or value["source_text_sha256"] != hashlib.sha256(b"Tsk!").hexdigest()
    ):
        raise AudioEventReviewError("Audio-event source text identity changed")
    media = value.get("source_media_ids")
    if not isinstance(media, list) or len(media) != 1:
        raise AudioEventReviewError("Audio-event source media identity is invalid")
    _required_positive_int(media[0], "audio-event source media ID")
    source_audio_id = value.get("source_audio_id")
    if source_audio_id is not None:
        _required_text(source_audio_id, "audio-event source audio ID")
    if value.get("speaker_identity_claim") is not False:
        raise AudioEventReviewError(
            "Audio-event source must not claim speaker identity"
        )
    if value.get("synthesis_voice_character") is not None:
        raise AudioEventReviewError("Audio-event source must not claim synthesis voice")
    story_digest = value.get("source_story_index_sha256")
    if (
        not isinstance(story_digest, str)
        or len(story_digest) != 64
        or any(character not in "0123456789abcdef" for character in story_digest)
    ):
        raise AudioEventReviewError("Audio-event source story index hash is invalid")
    return value


def _source_story_record(payload, line_id):
    expected = _required_text(line_id, "source line ID")
    matches = []
    try:
        for raw_line in payload.splitlines():
            if not raw_line.strip():
                continue
            value = json.loads(raw_line.decode("utf-8"))
            if isinstance(value, dict) and value.get("line_id") == expected:
                matches.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AudioEventReviewError(
            f"Unable to read source story index: {error}"
        ) from error
    if len(matches) != 1:
        raise AudioEventReviewError(
            f"Source story index must contain one exact line {expected!r}"
        )
    return matches[0]


def _assert_source_record_matches(evidence, record):
    expected = {
        "line_id": evidence["source_line_id"],
        "text": evidence["source_text"],
        "text_sha256": evidence["source_text_sha256"],
        "speaker": evidence["source_speaker"],
        "source_event": evidence["source_event"],
        "source_bank": evidence["source_bank"],
        "source_media_ids": evidence["source_media_ids"],
        "source_audio_id": evidence["source_audio_id"],
        "source_audio_status": "available",
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise AudioEventReviewError(
                f"Source story line {field} does not match review evidence"
            )


def _validate_effect_audio_info(info):
    if not 0.02 <= info.duration_seconds <= 3.0:
        raise AudioEventReviewError(
            "Source event audio duration must be between 0.02 and 3.0 seconds"
        )
    if not 0.001 <= info.peak <= 1.0:
        raise AudioEventReviewError("Source event audio is silent or has invalid peak")


def _validate_decision_document(value, review, review_sha256):
    if (
        value.get("schema") != AUDIO_EVENT_DECISION_SCHEMA
        or value.get("schema_version") != AUDIO_EVENT_DECISION_VERSION
    ):
        raise AudioEventReviewError("Unsupported audio-event decision schema")
    if value.get("review_id") != review["review_id"]:
        raise AudioEventReviewError("Audio-event decision belongs to another review")
    if value.get("review_sha256") != review_sha256:
        raise AudioEventReviewError("Audio-event decision review authority changed")
    if value.get("queue_sha256") != review["queue_sha256"]:
        raise AudioEventReviewError("Audio-event decision queue authority changed")
    if value.get("candidate_audio_sha256") != review["candidate"]["audio_sha256"]:
        raise AudioEventReviewError("Audio-event decision audio authority changed")
    if value.get("decision") not in AUDIO_EVENT_DECISIONS:
        raise AudioEventReviewError("Audio-event decision is invalid")
    _aware_timestamp(value.get("reviewed_at"), "audio-event reviewed_at")
    return value


def _required_single_tongue_click_plan(document):
    try:
        plan = audio_event_plan_for_record(document)
    except ValueError as error:
        raise AudioEventReviewError(str(error)) from error
    if not isinstance(plan, dict):
        raise AudioEventReviewError("Queue item does not require audio-event review")
    events = plan.get("events")
    if (
        document.get("text") != "Tsk!"
        or document.get("text_sha256") != hashlib.sha256(b"Tsk!").hexdigest()
        or plan.get("spoken_text") != ""
        or not isinstance(events, list)
        or len(events) != 1
        or events[0].get("kind") != "tongue-click"
    ):
        raise AudioEventReviewError(
            "Source audio-event review currently requires one exact Tsk tongue-click"
        )
    return plan


def _load_queue_snapshot(payload):
    with tempfile.TemporaryDirectory(prefix="vntts-audio-event-queue-") as directory:
        path = Path(directory) / "queue.jsonl"
        path.write_bytes(payload)
        return VoiceGenerationQueue.load(path)


def _contained_file(root, relative, label):
    if not isinstance(relative, str) or not relative.strip():
        raise AudioEventReviewError(f"{label.capitalize()} path is invalid")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise AudioEventReviewError(f"{label.capitalize()} leaves its review")
    candidate = root / path
    if candidate.is_symlink() or not candidate.is_file():
        raise AudioEventReviewError(f"{label.capitalize()} is unavailable")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AudioEventReviewError(
            f"{label.capitalize()} leaves its review"
        ) from error
    return resolved


def _required_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AudioEventReviewError(f"{label.capitalize()} must be non-empty text")
    return value.strip()


def _required_positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AudioEventReviewError(f"{label.capitalize()} must be a positive integer")
    return value


def _aware_timestamp(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AudioEventReviewError(f"{label.capitalize()} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AudioEventReviewError(f"{label.capitalize()} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AudioEventReviewError(f"{label.capitalize()} must include a timezone")
    return parsed


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AUDIO_EVENT_DECISIONS",
    "AUDIO_EVENT_DECISION_SCHEMA",
    "AUDIO_EVENT_DECISION_VERSION",
    "AUDIO_EVENT_REVIEW_SCHEMA",
    "AUDIO_EVENT_REVIEW_VERSION",
    "AudioEventReview",
    "AudioEventReviewError",
    "load_audio_event_review",
    "publish_source_audio_event_review",
    "record_audio_event_review_decision",
]
