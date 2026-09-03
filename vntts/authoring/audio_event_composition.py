"""Immutable production composition authority for one accepted audio event."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.audio import Pcm16MonoWavError, probe_pcm16_mono_wav

from vntts.authoring.audio_event_review import (
    AudioEventReviewError,
    load_audio_event_review,
)
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
from vntts.authoring.workspace_foundation import contained_regular_file

AUDIO_EVENT_COMPOSITION_SCHEMA = "vntts.authoring-audio-event-composition"
AUDIO_EVENT_COMPOSITION_VERSION = 1
AUDIO_EVENT_COMPOSITION_DECISION_SCHEMA = (
    "vntts.authoring-audio-event-composition-decision"
)
AUDIO_EVENT_COMPOSITION_DECISION_VERSION = 1
AUDIO_EVENT_COMPOSITION_DECISIONS = frozenset({"approved", "rejected"})


class AudioEventCompositionError(RuntimeError):
    """An audio-event production composition is invalid or changed."""


@dataclass(frozen=True)
class AudioEventComposition:
    directory: Path
    composition_id: str
    review_id: str
    queue_id: str
    audio: Path
    audio_sha256: str
    decision: str | None
    created: bool = False

    def to_dict(self):
        return {
            "directory": str(self.directory),
            "composition_id": self.composition_id,
            "review_id": self.review_id,
            "queue_id": self.queue_id,
            "audio": str(self.audio),
            "audio_sha256": self.audio_sha256,
            "decision": self.decision,
            "created": self.created,
        }


def publish_audio_event_composition(review_directory, output_directory):
    """Publish one exact, no-transform event-only composition candidate."""
    review_root = _safe_directory(review_directory, "audio-event review")
    output = Path(output_directory).expanduser().resolve()
    if output.exists() or output.is_symlink():
        loaded = load_audio_event_composition(output)
        if loaded.review_id == load_audio_event_review(review_root).review_id:
            return loaded
        raise AudioEventCompositionError(
            f"Audio-event composition output exists: {output}"
        )
    try:
        review = load_audio_event_review(review_root)
        if review.decision != "accept":
            raise AudioEventCompositionError(
                "Audio-event composition requires an accepted exact review"
            )
        review_snapshot = capture_authority_file(
            review_root / "review.json", "audio-event review", root=review_root
        )
        decision_snapshot = capture_authority_file(
            review_root / "decision.json", "audio-event decision", root=review_root
        )
        queue_snapshot = capture_authority_file(
            review_root / "queue.jsonl", "audio-event queue", root=review_root
        )
        audio_snapshot = capture_authority_file(
            review.audio, "accepted audio-event WAV", root=review_root
        )
        review_document = review_snapshot.json_document("audio-event review")
        decision_document = decision_snapshot.json_document("audio-event decision")
    except (AudioEventReviewError, AuthoringAuthorityError) as error:
        raise AudioEventCompositionError(str(error)) from error
    if (
        decision_document.get("decision") != "accept"
        or decision_document.get("review_id") != review.review_id
        or decision_document.get("review_sha256") != review_snapshot.sha256
        or decision_document.get("candidate_audio_sha256") != audio_snapshot.sha256
    ):
        raise AudioEventCompositionError("Accepted audio-event decision changed")
    candidate = review_document["candidate"]
    identity = {
        "schema": AUDIO_EVENT_COMPOSITION_SCHEMA,
        "schema_version": AUDIO_EVENT_COMPOSITION_VERSION,
        "review_id": review.review_id,
        "review_sha256": review_snapshot.sha256,
        "review_decision_sha256": decision_snapshot.sha256,
        "queue_sha256": queue_snapshot.sha256,
        "queue_id": review_document["queue_id"],
        "line_id": review_document["line_id"],
        "text": review_document["text"],
        "text_sha256": review_document["text_sha256"],
        "audio_event_plan": review_document["audio_event_plan"],
        "audio_event_plan_sha256": review_document["audio_event_plan_sha256"],
        "candidate_id": candidate["candidate_id"],
        "source": candidate["source"],
        "final_audio_sha256": audio_snapshot.sha256,
        "composition": {
            "kind": "source-event-only",
            "sample_offset": 0,
            "gain": 1.0,
            "fade_in_samples": 0,
            "fade_out_samples": 0,
            "byte_transform": "exact-copy",
            "speaker_identity_claim": False,
            "synthesis_provider": None,
            "synthesis_voice_character": None,
        },
    }
    composition_id = canonical_document_sha256(identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    ).resolve()
    try:
        review_target = staging / "review"
        _copy_review_snapshot(review_root, review_target)
        audio_target = staging / "audio/final.wav"
        audio_target.parent.mkdir(parents=True)
        audio_target.write_bytes(audio_snapshot.payload)
        document = {
            **identity,
            "composition_id": composition_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "review": "review/review.json",
            "review_decision": "review/decision.json",
            "queue": "review/queue.jsonl",
            "final_audio": "audio/final.wav",
            "sample_rate": candidate["sample_rate"],
            "sample_count": candidate["sample_count"],
            "duration_seconds": candidate["duration_seconds"],
            "peak": candidate["peak"],
        }
        atomic_write_json(staging / "composition.json", document, sort_keys=True)
        load_audio_event_composition(staging)
        for snapshot, label in (
            (review_snapshot, "audio-event review"),
            (decision_snapshot, "audio-event decision"),
            (queue_snapshot, "audio-event queue"),
            (audio_snapshot, "accepted audio-event WAV"),
        ):
            assert_authority_snapshot(snapshot, label)
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise AudioEventCompositionError(str(error)) from error
        result = load_audio_event_composition(output)
        return AudioEventComposition(**{**result.__dict__, "created": True})
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def load_audio_event_composition(directory):
    """Load and revalidate every byte in one event-only composition."""
    root = _safe_directory(directory, "audio-event composition")
    try:
        composition_snapshot = capture_authority_file(
            root / "composition.json", "audio-event composition", root=root
        )
        document = composition_snapshot.json_document("audio-event composition")
        review_root = _contained_directory(root, "review", "copied review")
        review = load_audio_event_review(review_root)
        review_snapshot = capture_authority_file(
            _contained_file(root, document.get("review"), "copied review"),
            "copied audio-event review",
            root=root,
        )
        review_document = review_snapshot.json_document("copied audio-event review")
        decision_snapshot = capture_authority_file(
            _contained_file(
                root, document.get("review_decision"), "copied review decision"
            ),
            "copied audio-event decision",
            root=root,
        )
        queue_snapshot = capture_authority_file(
            _contained_file(root, document.get("queue"), "copied queue"),
            "copied audio-event queue",
            root=root,
        )
        audio_snapshot = capture_authority_file(
            _contained_file(root, document.get("final_audio"), "final event audio"),
            "final event audio",
            root=root,
        )
        decision_document = decision_snapshot.json_document(
            "copied audio-event decision"
        )
    except (AudioEventReviewError, AuthoringAuthorityError) as error:
        raise AudioEventCompositionError(str(error)) from error
    _validate_composition_document(
        document,
        review,
        review_document,
        review_snapshot,
        decision_snapshot,
        decision_document,
        queue_snapshot,
        audio_snapshot,
    )
    decision = None
    terminal = root / "composition-decision.json"
    if terminal.exists() or terminal.is_symlink():
        try:
            terminal_snapshot = capture_authority_file(
                terminal, "audio-event composition decision", root=root
            )
            terminal_document = terminal_snapshot.json_document(
                "audio-event composition decision"
            )
        except AuthoringAuthorityError as error:
            raise AudioEventCompositionError(str(error)) from error
        decision = _validate_composition_decision(
            terminal_document, document, composition_snapshot.sha256
        )
        assert_authority_snapshot(terminal_snapshot, "audio-event composition decision")
    for snapshot, label in (
        (composition_snapshot, "audio-event composition"),
        (review_snapshot, "copied audio-event review"),
        (decision_snapshot, "copied audio-event decision"),
        (queue_snapshot, "copied audio-event queue"),
        (audio_snapshot, "final event audio"),
    ):
        assert_authority_snapshot(snapshot, label)
    return AudioEventComposition(
        root,
        document["composition_id"],
        document["review_id"],
        document["queue_id"],
        audio_snapshot.path,
        audio_snapshot.sha256,
        decision,
    )


def record_audio_event_composition_decision(directory, decision):
    """Record the final exact production-composition approval or rejection."""
    if decision not in AUDIO_EVENT_COMPOSITION_DECISIONS:
        raise AudioEventCompositionError(
            "Audio-event composition decision must be approved or rejected"
        )
    current = load_audio_event_composition(directory)
    path = current.directory / "composition-decision.json"
    if path.exists() or path.is_symlink():
        if current.decision == decision:
            return current
        raise AudioEventCompositionError(
            f"Audio-event composition is already decided: {current.decision}"
        )
    try:
        composition_snapshot = capture_authority_file(
            current.directory / "composition.json",
            "audio-event composition",
            root=current.directory,
        )
        document = composition_snapshot.json_document("audio-event composition")
        audio_snapshot = capture_authority_file(
            current.audio, "final event audio", root=current.directory
        )
        terminal = {
            "schema": AUDIO_EVENT_COMPOSITION_DECISION_SCHEMA,
            "schema_version": AUDIO_EVENT_COMPOSITION_DECISION_VERSION,
            "composition_id": current.composition_id,
            "composition_sha256": composition_snapshot.sha256,
            "final_audio_sha256": audio_snapshot.sha256,
            "decision": decision,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        _validate_composition_decision(terminal, document, composition_snapshot.sha256)
        assert_authority_snapshot(composition_snapshot, "audio-event composition")
        assert_authority_snapshot(audio_snapshot, "final event audio")
        write_json_document_no_replace(path, terminal, "composition decision")
    except AuthoringAuthorityError as error:
        raise AudioEventCompositionError(str(error)) from error
    return load_audio_event_composition(current.directory)


def _validate_composition_document(
    document,
    review,
    review_document,
    review_snapshot,
    decision_snapshot,
    decision_document,
    queue_snapshot,
    audio_snapshot,
):
    required = {
        "schema",
        "schema_version",
        "composition_id",
        "created_at",
        "review",
        "review_id",
        "review_sha256",
        "review_decision",
        "review_decision_sha256",
        "queue",
        "queue_sha256",
        "queue_id",
        "line_id",
        "text",
        "text_sha256",
        "audio_event_plan",
        "audio_event_plan_sha256",
        "candidate_id",
        "source",
        "final_audio",
        "final_audio_sha256",
        "sample_rate",
        "sample_count",
        "duration_seconds",
        "peak",
        "composition",
    }
    candidate = review_document.get("candidate")
    if (
        not isinstance(document, dict)
        or set(document) != required
        or document.get("schema") != AUDIO_EVENT_COMPOSITION_SCHEMA
        or document.get("schema_version") != AUDIO_EVENT_COMPOSITION_VERSION
        or review.decision != "accept"
        or not isinstance(candidate, dict)
        or document.get("review_id") != review.review_id
        or document.get("review_sha256") != review_snapshot.sha256
        or document.get("review_decision_sha256") != decision_snapshot.sha256
        or decision_document.get("decision") != "accept"
        or document.get("queue_sha256") != queue_snapshot.sha256
        or document.get("queue_id") != review.queue_id
        or document.get("final_audio_sha256") != audio_snapshot.sha256
        or review.audio_sha256 != audio_snapshot.sha256
        or any(
            document.get(field) != review_document.get(field)
            for field in (
                "queue_id",
                "line_id",
                "text",
                "text_sha256",
                "audio_event_plan",
                "audio_event_plan_sha256",
            )
        )
        or document.get("candidate_id") != candidate.get("candidate_id")
        or document.get("source") != candidate.get("source")
    ):
        raise AudioEventCompositionError("Audio-event composition authority changed")
    composition = document.get("composition")
    if composition != {
        "kind": "source-event-only",
        "sample_offset": 0,
        "gain": 1.0,
        "fade_in_samples": 0,
        "fade_out_samples": 0,
        "byte_transform": "exact-copy",
        "speaker_identity_claim": False,
        "synthesis_provider": None,
        "synthesis_voice_character": None,
    }:
        raise AudioEventCompositionError("Audio-event composition ledger changed")
    try:
        info = probe_pcm16_mono_wav(audio_snapshot.path)
    except (OSError, Pcm16MonoWavError) as error:
        raise AudioEventCompositionError(str(error)) from error
    if any(
        document.get(field) != value
        for field, value in (
            ("sample_rate", info.sample_rate),
            ("sample_count", info.sample_count),
            ("duration_seconds", info.duration_seconds),
            ("peak", info.peak),
        )
    ):
        raise AudioEventCompositionError("Final event audio metadata changed")
    identity = {
        key: value
        for key, value in document.items()
        if key
        not in {
            "composition_id",
            "created_at",
            "review",
            "review_decision",
            "queue",
            "final_audio",
            "sample_rate",
            "sample_count",
            "duration_seconds",
            "peak",
        }
    }
    if document["composition_id"] != canonical_document_sha256(identity):
        raise AudioEventCompositionError("Audio-event composition ID changed")


def _validate_composition_decision(value, composition, composition_sha256):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "schema_version",
            "composition_id",
            "composition_sha256",
            "final_audio_sha256",
            "decision",
            "reviewed_at",
        }
        or value.get("schema") != AUDIO_EVENT_COMPOSITION_DECISION_SCHEMA
        or value.get("schema_version") != AUDIO_EVENT_COMPOSITION_DECISION_VERSION
        or value.get("composition_id") != composition.get("composition_id")
        or value.get("composition_sha256") != composition_sha256
        or value.get("final_audio_sha256") != composition.get("final_audio_sha256")
        or value.get("decision") not in AUDIO_EVENT_COMPOSITION_DECISIONS
    ):
        raise AudioEventCompositionError("Audio-event composition decision changed")
    try:
        parsed = datetime.fromisoformat(value["reviewed_at"])
    except (TypeError, ValueError) as error:
        raise AudioEventCompositionError(
            "Audio-event composition decision timestamp is invalid"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AudioEventCompositionError(
            "Audio-event composition decision timestamp needs a timezone"
        )
    return value["decision"]


def _copy_review_snapshot(source, target):
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise AudioEventCompositionError("Audio-event review contains a symlink")
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())


def _safe_directory(value, label):
    supplied = Path(value).expanduser()
    if supplied.is_symlink():
        raise AudioEventCompositionError(f"{label.capitalize()} is a symlink")
    resolved = supplied.resolve()
    if not resolved.is_dir():
        raise AudioEventCompositionError(f"{label.capitalize()} is missing")
    return resolved


def _contained_directory(root, relative, label):
    path = Path(root) / relative
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != Path(root):
        raise AudioEventCompositionError(f"{label.capitalize()} leaves its root")
    return path.resolve()


def _contained_file(root, value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise AudioEventCompositionError(f"{label.capitalize()} path is invalid")
    return contained_regular_file(
        root, value, label, error_type=AudioEventCompositionError
    )


__all__ = [
    "AUDIO_EVENT_COMPOSITION_DECISIONS",
    "AUDIO_EVENT_COMPOSITION_DECISION_SCHEMA",
    "AUDIO_EVENT_COMPOSITION_DECISION_VERSION",
    "AUDIO_EVENT_COMPOSITION_SCHEMA",
    "AUDIO_EVENT_COMPOSITION_VERSION",
    "AudioEventComposition",
    "AudioEventCompositionError",
    "load_audio_event_composition",
    "publish_audio_event_composition",
    "record_audio_event_composition_decision",
]
