"""Seal a raw real-game replay capture into a sequence-bound replay corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, TypeAlias

from vntts_artifacts.generated_audio import GeneratedAudioDocument

from vntts.authoring.publication import (
    AtomicPublicationError,
    rename_directory_no_replace,
    staged_directory,
)
from vntts.chapter_voice_preload import ChapterDialogue, ChapterVoicePreloader
from vntts.cli import cli_error, cli_messages
from vntts.dialog_capture import is_standalone_ellipsis_text
from vntts.document_identity import is_lowercase_sha256
from vntts.live_replay import (
    LiveReplayRunner,
    ReplayRecognizer,
    _read_contained_file,
    load_live_replay_corpus,
)
from vntts.live_sequence import LiveSequenceEvent, LiveSequencePlan
from vntts.settings import AppSettings, audio_source_policies, load_app_settings

SEQUENCE_REPLAY_SEAL_VERSION = 1
PathInput: TypeAlias = str | os.PathLike[str]
JSONDocument: TypeAlias = dict[str, object]


class SequenceReplaySealError(RuntimeError):
    """A raw capture cannot be safely bound to one exact sequence."""


class _SealArguments(Protocol):
    capture_corpus: Path
    output: Path
    story_index: Path | None
    sequence_plan: Path | None
    generated_audio_manifest: Path | None
    no_generated_audio_manifest: bool
    mode: str
    audio_source_policy: str | None
    timeout: float


@dataclass(frozen=True)
class SealedSequenceReplayResult:
    directory: Path
    corpus: Path
    review: Path
    replay_report: Path
    dialogue_count: int
    operator_review_required: bool


def seal_sequence_replay(
    capture_corpus: PathInput,
    output_directory: PathInput,
    *,
    story_index: PathInput,
    sequence_plan: PathInput,
    mode: str = "audio-manual",
    generated_audio_manifest: PathInput | None = None,
    audio_source_policy: str = "prefer-game-audio",
    recognizer: ReplayRecognizer | None = None,
    interval_seconds: float = 0.01,
    timeout_seconds: float = 30.0,
) -> SealedSequenceReplayResult:
    """Publish a contained v2 corpus only after its production replay passes."""
    if mode not in {"shadow", "audio-manual", "audio-auto"}:
        raise SequenceReplaySealError(f"Unsupported sequence replay mode: {mode!r}")
    if audio_source_policy not in audio_source_policies:
        raise SequenceReplaySealError(
            f"Unsupported audio source policy: {audio_source_policy!r}"
        )
    capture_path, capture_payload = _read_regular_file(
        capture_corpus, "Raw replay corpus"
    )
    capture = _decode_json(capture_payload, "Raw replay corpus")
    if (
        capture.get("schema_version") != 1
        or capture.get("fixture_kind") != "saved-frame-ocr-replay-capture"
        or not isinstance(capture_binding := capture.get("capture"), dict)
    ):
        raise SequenceReplaySealError(
            "Sequence sealing requires raw schema-v1 vntts-capture-live-replay output"
        )
    raw_dialogue = capture.get("dialogue")
    if (
        not isinstance(raw_dialogue, list)
        or not raw_dialogue
        or any(not isinstance(record, dict) for record in raw_dialogue)
    ):
        raise SequenceReplaySealError("Raw replay corpus has no dialogue records")
    capture_authority: JSONDocument = capture_binding
    raw_dialogue_records: tuple[JSONDocument, ...] = tuple(
        record for record in raw_dialogue if isinstance(record, dict)
    )
    capture_report_path = capture_path.with_name("capture-report.json")
    _capture_report_path, capture_report_payload = _read_regular_file(
        capture_report_path, "Capture review report"
    )
    capture_report_document = _decode_json(
        capture_report_payload, "Capture review report"
    )
    _validate_capture_report(capture, capture_report_document)
    _validate_capture_observation_ledger(
        capture_path,
        capture,
        capture_report_document,
    )
    capture_report_sha256 = hashlib.sha256(capture_report_payload).hexdigest()

    source_story_path, story_payload = _read_regular_file(story_index, "Story index")
    _plan_path, plan_payload = _read_regular_file(sequence_plan, "Sequence plan")
    story_sha256 = hashlib.sha256(story_payload).hexdigest()
    plan_sha256 = hashlib.sha256(plan_payload).hexdigest()
    captured_story_sha256 = capture_authority.get("story_index_sha256")
    if captured_story_sha256 != story_sha256:
        raise SequenceReplaySealError(
            "Raw capture is not bound to the selected story-index bytes"
        )
    recovery = capture_authority.get("recovery")
    if recovery is not None:
        recovery_document = _required_document(recovery, "Capture recovery authority")
        if recovery_document["sequence_plan_sha256"] != plan_sha256:
            raise SequenceReplaySealError(
                "Recovered capture is not bound to the selected sequence-plan bytes"
            )
    selected_output = Path(output_directory).expanduser()
    if selected_output.exists() or selected_output.is_symlink():
        raise SequenceReplaySealError(
            f"Replay seal output already exists: {selected_output}"
        )
    parent = selected_output.parent.resolve()
    if not parent.is_dir():
        raise SequenceReplaySealError(f"Replay seal parent does not exist: {parent}")
    output = parent / selected_output.name
    with staged_directory(parent, prefix=f".{output.name}.") as staging:
        authority = staging / "authority"
        authority.mkdir()
        story_copy = authority / "story-index.jsonl"
        plan_copy = authority / "live-sequence.json"
        _write_bytes(story_copy, story_payload)
        _write_bytes(plan_copy, plan_payload)
        source_semantic_path = (
            source_story_path.parent / "source-audio-semantic-evidence.json"
        )
        if source_semantic_path.is_file() and not source_semantic_path.is_symlink():
            _, semantic_payload = _read_regular_file(
                source_semantic_path,
                "Source audio semantic evidence",
            )
            _write_bytes(
                authority / "source-audio-semantic-evidence.json",
                semantic_payload,
            )
        try:
            resolver = ChapterVoicePreloader.load_optional(story_copy)
            plan = LiveSequencePlan.load(plan_copy, story_copy)
        except Exception as error:
            raise SequenceReplaySealError(
                f"Story index and sequence plan are incompatible: {error}"
            ) from error
        mappings = _map_dialogue(raw_dialogue_records, resolver, plan)
        frame_records = _copy_frames(capture_path.parent, staging, raw_dialogue_records)
        raw_copy = staging / "provenance" / "raw-corpus.json"
        _write_bytes(raw_copy, capture_payload)
        _write_bytes(
            staging / "provenance" / "capture-report.json",
            capture_report_payload,
        )

        generated_binding, generated_lines = _snapshot_generated_audio(
            generated_audio_manifest,
            staging,
            mappings,
            resolver,
        )
        dialogue = _sealed_dialogue(
            raw_dialogue_records,
            frame_records,
            mappings,
            resolver,
            generated_lines,
            mode=mode,
            audio_source_policy=audio_source_policy,
        )
        expected = {
            "event_ids": [mapping["event_id"] for mapping in mappings],
            "line_ids": [mapping["line_id"] for mapping in mappings],
            "ocr_calls": 0,
            "bounded_recoveries": 0,
            "key_dispatch_attempts": 0,
            "confirmed_key_dispatches": 0,
        }
        corpus: JSONDocument = {
            "schema_version": 2,
            "name": f"{capture.get('name') or capture_path.stem} sequence replay",
            "fixture_kind": "sealed-real-capture-production-controller",
            "capture": {
                **capture_authority,
                "raw_corpus_sha256": hashlib.sha256(capture_payload).hexdigest(),
                "sequence_seal_version": SEQUENCE_REPLAY_SEAL_VERSION,
            },
            "live_sequence": {
                "mode": mode,
                "story_index": {
                    "path": story_copy.relative_to(staging).as_posix(),
                    "sha256": story_sha256,
                },
                "plan": {
                    "path": plan_copy.relative_to(staging).as_posix(),
                    "sha256": plan_sha256,
                },
                "focus_probes": [],
                "expected": expected,
            },
            "dialogue": dialogue,
        }
        if generated_binding is not None:
            corpus["generated_audio_manifest"] = generated_binding
        corpus_path = staging / "corpus.json"
        _write_json(corpus_path, corpus)

        probe = LiveReplayRunner(
            load_live_replay_corpus(corpus_path),
            recognizer=recognizer,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            audio_source_policy=audio_source_policy,
        ).run()
        _validate_probe(probe, mappings, dialogue)
        probe_sequence = _required_document(probe.get("sequence"), "Probe sequence")
        probe_observed = _required_document(
            probe_sequence.get("observed"), "Probe sequence observations"
        )
        probe_route_sources = _required_list(probe.get("route_sources"), "Probe routes")
        route_sources = iter(probe_route_sources)
        for record in dialogue:
            if record["expect_playback"]:
                record["expected_source"] = next(route_sources)
        try:
            next(route_sources)
        except StopIteration:
            pass
        else:
            raise SequenceReplaySealError(
                "Probe produced more audio routes than captured speech records"
            )
        live_sequence = _required_document(corpus.get("live_sequence"), "Live sequence")
        live_sequence["expected"] = probe_observed
        corpus["dialogue"] = dialogue
        _write_json(corpus_path, corpus)

        final_report = LiveReplayRunner(
            load_live_replay_corpus(corpus_path),
            recognizer=recognizer,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            audio_source_policy=audio_source_policy,
        ).run()
        if not final_report["successful"]:
            raise SequenceReplaySealError(
                "Sealed sequence replay did not reproduce its measured baseline"
            )
        replay_report = staging / "replay-report.json"
        _write_json(replay_report, final_report)

        boundary_review_required = bool(
            capture_authority.get("boundary_review_required")
        )
        inferred_mapping = any(
            mapping["mapping_method"] != "exact-line-id" for mapping in mappings
        )
        operator_review_required = boundary_review_required or inferred_mapping
        review = {
            "schema": "vntts.sequence-replay-seal-review",
            "schema_version": SEQUENCE_REPLAY_SEAL_VERSION,
            "operator_review_required": operator_review_required,
            "human_acceptance_recorded": False,
            "note": (
                "Measured counters and routes are reproducible baseline evidence, "
                "not a human gameplay acceptance decision."
            ),
            "authority": {
                "raw_corpus_sha256": hashlib.sha256(capture_payload).hexdigest(),
                "capture_report_sha256": capture_report_sha256,
                "story_index_sha256": story_sha256,
                "sequence_plan_sha256": plan_sha256,
                "generated_audio_manifest_sha256": (
                    generated_binding["sha256"]
                    if generated_binding is not None
                    else None
                ),
            },
            "capture_boundary_review_required": boundary_review_required,
            "capture_boundary_review_count": capture_authority.get(
                "boundary_review_count", 0
            ),
            "capture_report_boundary_count": len(
                _required_list(
                    capture_report_document.get("boundaries"),
                    "Capture report boundaries",
                )
            ),
            "mappings": mappings,
            "measured_baseline": {
                "route_sources": _required_list(
                    final_report.get("route_sources"), "Final replay routes"
                ),
                **_required_document(
                    _required_document(
                        final_report.get("sequence"), "Final replay sequence"
                    ).get("observed"),
                    "Final replay sequence observations",
                ),
            },
            "sealed_replay_successful": True,
        }
        review_path = staging / "sequence-review.json"
        _write_json(review_path, review)
        try:
            rename_directory_no_replace(staging, output)
        except (AtomicPublicationError, OSError) as error:
            raise SequenceReplaySealError(
                f"Unable to publish replay seal: {error}"
            ) from error
        return SealedSequenceReplayResult(
            output,
            output / corpus_path.name,
            output / review_path.name,
            output / replay_report.name,
            len(dialogue),
            operator_review_required,
        )


def _map_dialogue(
    raw_dialogue: Sequence[JSONDocument],
    resolver: ChapterVoicePreloader,
    plan: LiveSequencePlan,
) -> list[JSONDocument]:
    mappings: list[JSONDocument] = []
    previous_event: LiveSequenceEvent | None = None
    for index, record in enumerate(raw_dialogue, start=1):
        if not isinstance(record, dict):
            raise SequenceReplaySealError(
                f"Raw replay dialogue {index} must be an object"
            )
        character = str(record.get("character") or "Narrator").strip() or "Narrator"
        text = " ".join(str(record.get("text") or "").split())
        if not text:
            raise SequenceReplaySealError(
                f"Raw replay dialogue {index} has no observed text"
            )
        frontier: tuple[LiveSequenceEvent, ...] = ()
        if previous_event is not None:
            frontier = _next_visible_events(plan, previous_event)
        raw_line_id = str(record.get("line_id") or "").strip() or None
        line: ChapterDialogue | None = (
            resolver.line_for_id(raw_line_id) if raw_line_id else None
        )
        event = plan.event_for_line(line.line_id) if line is not None else None
        method = "exact-line-id"
        if event is not None:
            assert line is not None
            if (line.speaker, line.text) != (character, text):
                raise SequenceReplaySealError(
                    f"Raw replay dialogue {index} disagrees with canonical line "
                    f"{line.line_id!r}"
                )
        else:
            if previous_event is None:
                candidates = _initial_text_candidates(plan, resolver, text)
            else:
                candidates = frontier
            if previous_event is not None and len(candidates) != 1:
                raise SequenceReplaySealError(
                    f"Raw replay dialogue {index} reaches an ambiguous or skipped "
                    "visible sequence frontier"
                )
            if is_standalone_ellipsis_text(text):
                silent = tuple(event for event in candidates if event.kind == "silent")
                if len(silent) != 1 or len(candidates) != 1:
                    raise SequenceReplaySealError(
                        f"Raw replay dialogue {index} cannot uniquely bind a silent "
                        "sequence event"
                    )
                event = silent[0]
                line = None
                method = "unique-silent-frontier"
            else:
                speech: list[tuple[LiveSequenceEvent, ChapterDialogue]] = []
                for candidate in candidates:
                    if not candidate.is_speech or candidate.line_id is None:
                        continue
                    candidate_line = resolver.line_for_id(candidate.line_id)
                    if candidate_line is not None and _normalized_exact(
                        candidate_line.text
                    ) == _normalized_exact(text):
                        speech.append((candidate, candidate_line))
                if len(speech) != 1:
                    raise SequenceReplaySealError(
                        f"Raw replay dialogue {index} has no unique canonical text "
                        "match on the explicit sequence path"
                    )
                event, line = speech[0]
                method = "unique-text-frontier"
        if previous_event is not None:
            if len(frontier) != 1 or frontier[0].event_id != event.event_id:
                raise SequenceReplaySealError(
                    f"Raw replay dialogue {index} is not the unique next visible "
                    "sequence event"
                )
        mappings.append(
            {
                "dialogue_index": index,
                "event_id": event.event_id,
                "line_id": None if line is None else line.line_id,
                "event_kind": event.kind,
                "mapping_method": method,
                "capture_story_match": record.get("story_match"),
                "capture_boundary": record.get("capture_boundary"),
                "observed_character": character,
                "observed_text": text,
                "canonical_character": None if line is None else line.speaker,
                "canonical_text": None if line is None else line.text,
            }
        )
        previous_event = event
    return mappings


def _ordered_visible_events(plan: LiveSequencePlan) -> tuple[LiveSequenceEvent, ...]:
    return tuple(
        sorted(
            (
                event
                for event in plan.events.values()
                if event.kind in {"speech", "silent"}
            ),
            key=lambda event: (str(event.chapter), event.sequence, event.event_id),
        )
    )


def _next_visible_events(
    plan: LiveSequencePlan, event: LiveSequenceEvent
) -> tuple[LiveSequenceEvent, ...]:
    pending = list(event.successors)
    visited: set[str] = set()
    visible: list[LiveSequenceEvent] = []
    while pending:
        event_id = pending.pop(0)
        if event_id in visited:
            continue
        visited.add(event_id)
        candidate = plan.events[event_id]
        if candidate.kind in {"speech", "silent"}:
            visible.append(candidate)
            continue
        if candidate.kind == "wait" or candidate.control == "manual":
            continue
        pending.extend(candidate.successors)
    return tuple(visible)


def _initial_text_candidates(
    plan: LiveSequencePlan, resolver: ChapterVoicePreloader, text: str
) -> tuple[LiveSequenceEvent, ...]:
    return tuple(
        event
        for event in plan.events.values()
        if event.is_speech
        and _normalized_exact(_canonical_line(resolver, event.line_id).text)
        == _normalized_exact(text)
    )


def _canonical_line(
    resolver: ChapterVoicePreloader, line_id: str | None
) -> ChapterDialogue:
    line: ChapterDialogue | None = resolver.line_for_id(line_id)
    assert line is not None
    return line


def _copy_frames(
    capture_root: Path, staging: Path, raw_dialogue: Sequence[JSONDocument]
) -> list[list[JSONDocument]]:
    copied: list[list[JSONDocument]] = []
    seen: dict[str, str] = {}
    for dialogue_index, record in enumerate(raw_dialogue, start=1):
        frames = record.get("frames") if isinstance(record, dict) else None
        if not isinstance(frames, list) or not frames:
            raise SequenceReplaySealError(
                f"Raw replay dialogue {dialogue_index} has no exact frames"
            )
        copied_frames: list[JSONDocument] = []
        for frame_index, frame in enumerate(frames, start=1):
            if not isinstance(frame, dict) or set(frame) != {"path", "sha256"}:
                raise SequenceReplaySealError(
                    f"Raw replay frame {dialogue_index}:{frame_index} must bind only "
                    "path and sha256"
                )
            relative, payload = _read_contained(
                capture_root, frame.get("path"), "Raw replay frame"
            )
            digest = _required_sha256(frame.get("sha256"), "Raw replay frame sha256")
            if hashlib.sha256(payload).hexdigest() != digest:
                raise SequenceReplaySealError("Raw replay frame checksum changed")
            previous = seen.get(relative)
            if previous is not None and previous != digest:
                raise SequenceReplaySealError(
                    f"Raw replay frame {relative!r} has conflicting checksums"
                )
            seen[relative] = digest
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            if not destination.exists():
                _write_bytes(destination, payload)
            copied_frames.append({"path": relative, "sha256": digest})
        copied.append(copied_frames)
    return copied


def _snapshot_generated_audio(
    manifest: PathInput | None,
    staging: Path,
    mappings: Sequence[JSONDocument],
    resolver: ChapterVoicePreloader,
) -> tuple[JSONDocument | None, set[str]]:
    if manifest is None:
        return None, set()
    manifest_path, manifest_payload = _read_regular_file(
        manifest, "Generated audio manifest"
    )
    try:
        document = GeneratedAudioDocument.load(manifest_path)
    except Exception as error:
        raise SequenceReplaySealError(
            f"Generated audio manifest is invalid: {error}"
        ) from error
    _current_manifest_path, current_manifest_payload = _read_regular_file(
        manifest_path, "Generated audio manifest"
    )
    if current_manifest_payload != manifest_payload:
        raise SequenceReplaySealError(
            "Generated audio manifest changed while it was being loaded"
        )
    identities = _generated_audio_identities(mappings, resolver)
    selected = [
        record
        for record in document.records
        if (record.line_id, record.text_sha256) in identities
        and document.find(record.line_id, record.text_sha256) is not None
    ]
    if not selected:
        return None, set()
    raw_document = _decode_json(manifest_payload, "Generated audio manifest")
    records: list[JSONDocument] = []
    for record in selected:
        if record.audio.is_symlink():
            raise SequenceReplaySealError(
                f"Generated audio must not be a symlink: {record.audio}"
            )
        _audio_path, payload = _read_regular_file(record.audio, "Generated audio")
        if hashlib.sha256(payload).hexdigest() != record.audio_sha256:
            raise SequenceReplaySealError(
                f"Generated audio changed while sealing: {record.audio}"
            )
        relative = f"audio/{record.audio_sha256}.wav"
        destination = staging / "generated" / relative
        if destination.exists():
            if destination.read_bytes() != payload:
                raise SequenceReplaySealError(
                    "Generated audio digest collision while sealing"
                )
        else:
            _write_bytes(destination, payload)
        wire: JSONDocument = record.to_record()
        wire["audio"] = relative
        records.append(wire)
    sealed_manifest = {
        key: value
        for key, value in raw_document.items()
        if key not in {"entries", "entry_count"}
    }
    sealed_manifest["entry_count"] = len(records)
    sealed_manifest["entries"] = records
    path = staging / "generated" / "manifest.json"
    _write_json(path, sealed_manifest)
    try:
        GeneratedAudioDocument.load(path)
    except Exception as error:
        raise SequenceReplaySealError(
            f"Sealed generated audio is invalid: {error}"
        ) from error
    payload = path.read_bytes()
    return (
        {
            "path": path.relative_to(staging).as_posix(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
        {record.line_id for record in selected},
    )


def _generated_audio_identities(
    mappings: Sequence[JSONDocument], resolver: ChapterVoicePreloader
) -> set[tuple[str, str | None]]:
    identities: set[tuple[str, str | None]] = set()
    for mapping in mappings:
        line_id = mapping["line_id"]
        if line_id is None:
            continue
        assert isinstance(line_id, str)
        identities.add((line_id, _canonical_line(resolver, line_id).text_sha256))
    return identities


def _sealed_dialogue(
    raw_dialogue: Sequence[JSONDocument],
    frame_records: Sequence[list[JSONDocument]],
    mappings: Sequence[JSONDocument],
    resolver: ChapterVoicePreloader,
    generated_lines: set[str],
    *,
    mode: str,
    audio_source_policy: str,
) -> list[JSONDocument]:
    dialogue: list[JSONDocument] = []
    for raw, frames, mapping in zip(raw_dialogue, frame_records, mappings, strict=True):
        line_id = mapping["line_id"]
        if line_id is None:
            character = str(raw.get("character") or "Narrator").strip() or "Narrator"
            text = " ".join(str(raw.get("text") or "").split())
            record = {
                "frames": frames,
                "character": character,
                "text": text,
                "event_id": mapping["event_id"],
                "line_id": None,
                "expect_playback": False,
                "source_audio_status": "not-applicable",
                "expected_source": None,
            }
        else:
            line = resolver.line_for_id(line_id)
            if line is None:
                raise SequenceReplaySealError(
                    f"Canonical line disappeared while sealing: {line_id}"
                )
            if (
                audio_source_policy == "prefer-game-audio"
                and line.source_audio_status == "available"
                and line.source_audio_authoritative
                and line.source_audio_completeness == "full"
            ):
                expected_source = "game"
            elif (
                audio_source_policy in {"prefer-generated", "prefer-game-audio"}
                and line_id in generated_lines
            ):
                expected_source = "generated"
            else:
                expected_source = "live:replay-live-tts"
            record = {
                "frames": frames,
                "character": line.speaker,
                "text": line.text,
                "event_id": mapping["event_id"],
                "line_id": line.line_id,
                "expect_playback": True,
                "source_audio_status": line.source_audio_status,
                "source_audio_id": line.source_audio_id,
                "source_audio_duration_seconds": line.source_audio_duration_seconds,
                "source_audio_completeness": line.source_audio_completeness,
                "expected_source": expected_source,
            }
        dialogue.append(record)
    return dialogue


def _validate_probe(
    report: JSONDocument,
    mappings: Sequence[JSONDocument],
    dialogue: Sequence[JSONDocument],
) -> None:
    expected_dialogue = [
        {"character": record["character"], "text": record["text"]}
        for record in dialogue
        if record["expect_playback"]
    ]
    expected_event_ids = [mapping["event_id"] for mapping in mappings]
    expected_line_ids = [mapping["line_id"] for mapping in mappings]
    errors = _required_list(report.get("errors"), "Production replay errors")
    if errors:
        raise SequenceReplaySealError(f"Production replay probe failed: {errors[0]}")
    media_integrity = _required_document(
        report.get("media_integrity"), "Production replay media integrity"
    )
    consumption = _required_document(
        media_integrity.get("frame_consumption"), "Production replay frame consumption"
    )
    if not consumption.get("complete"):
        raise SequenceReplaySealError(
            "Production replay probe did not consume all frames: "
            f"{consumption['consumed_count']}/{consumption['declared_count']}"
        )
    if report.get("observed_dialogue") != expected_dialogue:
        raise SequenceReplaySealError(
            "Production replay probe did not reproduce canonical captured speech"
        )
    observed = _required_document(
        _required_document(report.get("sequence"), "Production replay sequence").get(
            "observed"
        ),
        "Production replay sequence observations",
    )
    if (
        observed["event_ids"] != expected_event_ids
        or observed["line_ids"] != expected_line_ids
    ):
        raise SequenceReplaySealError(
            "Production replay probe did not reproduce canonical sequence identities"
        )
    expected_routes = sum(1 for record in dialogue if record["expect_playback"])
    if (
        len(_required_list(report.get("route_sources"), "Production replay routes"))
        != expected_routes
    ):
        raise SequenceReplaySealError(
            "Production replay probe did not produce one route per speech record"
        )


def _validate_capture_report(capture: JSONDocument, report: JSONDocument) -> None:
    if (
        report.get("schema") != "vntts.live-replay-capture-report"
        or report.get("schema_version") != 1
    ):
        raise SequenceReplaySealError("Capture review report has an unsupported schema")
    authority = _required_document(capture.get("capture"), "Raw replay corpus capture")
    for field in (
        "frame_count",
        "dialogue_count",
        "boundary_review_required",
        "boundary_review_count",
        "story_index_sha256",
    ):
        if report.get(field) != authority.get(field):
            raise SequenceReplaySealError(
                f"Capture review report disagrees with raw corpus field {field!r}"
            )
    boundaries = report.get("boundaries")
    dialogue = report.get("dialogue")
    if not isinstance(boundaries, list) or len(boundaries) != report.get(
        "boundary_review_count"
    ):
        raise SequenceReplaySealError(
            "Capture review report boundary ledger is invalid"
        )
    if not isinstance(dialogue, list) or len(dialogue) != report.get("dialogue_count"):
        raise SequenceReplaySealError(
            "Capture review report dialogue ledger is invalid"
        )
    raw_dialogue = _required_list(capture.get("dialogue"), "Raw replay dialogue")
    if any(not isinstance(record, dict) for record in raw_dialogue):
        raise SequenceReplaySealError("Raw replay corpus has no dialogue records")
    raw_dialogue_records: tuple[JSONDocument, ...] = tuple(
        record for record in raw_dialogue if isinstance(record, dict)
    )
    expected_dialogue = [
        {
            "dialogue_index": index,
            "character": record.get("character"),
            "text": record.get("text"),
            "line_id": record.get("line_id"),
            "story_match": record.get("story_match"),
            "frame_count": len(
                _required_list(record.get("frames", ()), "Raw replay frames")
            ),
            "boundary_reason": record.get("capture_boundary"),
        }
        for index, record in enumerate(raw_dialogue_records, start=1)
    ]
    if dialogue != expected_dialogue:
        raise SequenceReplaySealError(
            "Capture review report dialogue ledger disagrees with the raw corpus"
        )
    expected_boundaries = [
        {
            "after_dialogue": index,
            "reason": "inferred-observation-replacement",
            "requires_operator_review": True,
        }
        for index, record in enumerate(raw_dialogue_records, start=1)
        if record.get("capture_boundary") == "inferred-observation-replacement"
    ]
    if boundaries != expected_boundaries:
        raise SequenceReplaySealError(
            "Capture review report boundary ledger disagrees with the raw corpus"
        )


def _validate_capture_observation_ledger(
    capture_path: Path, capture: JSONDocument, report: JSONDocument
) -> None:
    authority = _required_document(capture.get("capture"), "Raw replay corpus capture")
    binding = authority.get("observation_ledger")
    if binding is None:
        return
    if not isinstance(binding, dict) or report.get("observation_ledger") != binding:
        raise SequenceReplaySealError(
            "Capture observation ledger binding disagrees with the review report"
        )
    unresolved = authority.get("unresolved_observation_count")
    if (
        isinstance(unresolved, bool)
        or not isinstance(unresolved, int)
        or unresolved < 0
        or report.get("unresolved_observation_count") != unresolved
    ):
        raise SequenceReplaySealError("Capture unresolved-observation count is invalid")
    _relative, payload = _read_contained(
        capture_path.parent,
        binding.get("path"),
        "Capture observation ledger",
    )
    digest = _required_sha256(
        binding.get("sha256"), "Capture observation ledger sha256"
    )
    if hashlib.sha256(payload).hexdigest() != digest:
        raise SequenceReplaySealError("Capture observation ledger checksum changed")
    document = _decode_json(payload, "Capture observation ledger")
    observations = document.get("observations")
    if (
        document.get("schema") != "vntts.live-replay-capture-observations"
        or document.get("schema_version") != 1
        or document.get("story_index_sha256") != authority.get("story_index_sha256")
        or not isinstance(observations, list)
        or document.get("observation_count") != len(observations)
        or binding.get("observation_count") != len(observations)
    ):
        raise SequenceReplaySealError("Capture observation ledger is invalid")
    ledger_frames: set[tuple[object, str]] = set()
    statuses: list[str] = []
    for index, observation in enumerate(observations, start=1):
        if (
            not isinstance(observation, dict)
            or observation.get("observation_index") != index
        ):
            raise SequenceReplaySealError("Capture observation ledger order is invalid")
        status = observation.get("status")
        if status not in {
            "canonical",
            "punctuation-only",
            "unresolved",
            "uncertain",
            "accepted-unbound",
        }:
            raise SequenceReplaySealError(
                "Capture observation ledger status is invalid"
            )
        assert isinstance(status, str)
        statuses.append(status)
        frame = observation.get("frame")
        if not isinstance(frame, dict) or set(frame) != {"path", "sha256"}:
            raise SequenceReplaySealError(
                "Capture observation ledger frame binding is invalid"
            )
        _frame_relative, frame_payload = _read_contained(
            capture_path.parent,
            frame.get("path"),
            "Capture observation frame",
        )
        frame_digest = _required_sha256(
            frame.get("sha256"), "Capture observation frame sha256"
        )
        if hashlib.sha256(frame_payload).hexdigest() != frame_digest:
            raise SequenceReplaySealError("Capture observation frame checksum changed")
        ledger_frames.add((frame["path"], frame_digest))
    raw_dialogue = _required_list(capture.get("dialogue"), "Raw replay dialogue")
    if any(not isinstance(record, dict) for record in raw_dialogue):
        raise SequenceReplaySealError("Raw replay corpus has no dialogue records")
    dialogue_records: tuple[JSONDocument, ...] = tuple(
        record for record in raw_dialogue if isinstance(record, dict)
    )
    dialogue_frames: set[tuple[object, object]] = set()
    for record in dialogue_records:
        for frame in _required_list(record.get("frames", ()), "Raw replay frames"):
            if isinstance(frame, dict):
                dialogue_frames.add((frame.get("path"), frame.get("sha256")))
    if not dialogue_frames.issubset(ledger_frames):
        raise SequenceReplaySealError(
            "Capture dialogue frames are not bound by the observation ledger"
        )
    uncertain = report.get("uncertain_observations_skipped")
    if (
        isinstance(uncertain, bool)
        or not isinstance(uncertain, int)
        or uncertain < 0
        or statuses.count("unresolved") != unresolved
        or statuses.count("uncertain") != uncertain
        or len(observations) != authority.get("frame_count")
    ):
        raise SequenceReplaySealError(
            "Capture observation ledger counts disagree with capture authority"
        )
    recovery = authority.get("recovery")
    if recovery is None:
        raise SequenceReplaySealError(
            "Raw observation-ledger capture must recover one explicit sequence "
            "segment before sealing"
        )
    _validate_recovery_authority(
        capture_path.parent,
        _required_document(recovery, "Capture recovery authority"),
        report,
    )


def _validate_recovery_authority(
    root: Path, recovery: JSONDocument, report: JSONDocument
) -> None:
    if (
        not isinstance(recovery, dict)
        or set(recovery)
        != {
            "schema_version",
            "raw_corpus_sha256",
            "capture_report_sha256",
            "sequence_plan_sha256",
            "mapping_policy",
        }
        or recovery.get("schema_version") != 1
        or recovery.get("mapping_policy") != "exact-explicit-sequence-run"
        or report.get("recovery") != recovery
    ):
        raise SequenceReplaySealError("Capture recovery authority is invalid")
    for field in (
        "raw_corpus_sha256",
        "capture_report_sha256",
        "sequence_plan_sha256",
    ):
        _required_sha256(recovery.get(field), f"Capture recovery {field}")
    for relative, field, label in (
        (
            "provenance/raw-corpus.json",
            "raw_corpus_sha256",
            "Recovered source corpus",
        ),
        (
            "provenance/capture-report.json",
            "capture_report_sha256",
            "Recovered source capture report",
        ),
    ):
        _path, payload = _read_contained(root, relative, label)
        if hashlib.sha256(payload).hexdigest() != recovery[field]:
            raise SequenceReplaySealError(f"{label} checksum changed")


def _normalized_exact(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _decode_json(payload: bytes, label: str) -> JSONDocument:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SequenceReplaySealError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise SequenceReplaySealError(f"{label} root must be an object")
    return document


def _required_document(value: object, label: str) -> JSONDocument:
    if not isinstance(value, dict):
        raise SequenceReplaySealError(f"{label} must be an object")
    return value


def _required_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise SequenceReplaySealError(f"{label} must be a list")
    return value


def _read_regular_file(value: PathInput, label: str) -> tuple[Path, bytes]:
    selected = Path(value).expanduser()
    if selected.is_symlink():
        raise SequenceReplaySealError(f"{label} must not be a symlink: {selected}")
    path = selected.resolve()
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise SequenceReplaySealError(f"{label} must be a regular file: {path}")
            payload = source.read()
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise SequenceReplaySealError(f"Unable to read {label}: {error}") from error
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SequenceReplaySealError(f"{label} changed while being read: {path}")
    return path, payload


def _read_contained(root: PathInput, value: object, label: str) -> tuple[str, bytes]:
    try:
        _path, relative, payload = _read_contained_file(Path(root), value, label)
    except (OSError, ValueError) as error:
        raise SequenceReplaySealError(str(error)) from error
    return relative, payload


def _required_sha256(value: object, label: str) -> str:
    if is_lowercase_sha256(value):
        assert isinstance(value, str)
        return value
    raise SequenceReplaySealError(f"{label} must be a lowercase SHA-256")


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _write_json(path: Path, document: JSONDocument) -> None:
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.exists():
        path.unlink()
    _write_bytes(path, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal raw vntts-capture-live-replay output into a checksum-bound "
            "sequence replay"
        )
    )
    parser.add_argument("capture_corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--story-index", type=Path)
    parser.add_argument("--sequence-plan", type=Path)
    generated_audio = parser.add_mutually_exclusive_group()
    generated_audio.add_argument("--generated-audio-manifest", type=Path)
    generated_audio.add_argument(
        "--no-generated-audio-manifest",
        action="store_true",
        help=(
            "Ignore the generated-audio manifest from app settings. This is "
            "required for an isolated live-tts-only acceptance run."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("shadow", "audio-manual", "audio-auto"),
        default="audio-manual",
    )
    parser.add_argument(
        "--audio-source-policy",
        choices=tuple(sorted(audio_source_policies)),
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def _generated_audio_manifest_for_run(
    arguments: _SealArguments, settings: AppSettings
) -> PathInput | None:
    if arguments.no_generated_audio_manifest:
        return None
    return arguments.generated_audio_manifest or settings.generated_audio_manifest


def main(argv: Sequence[str] | None = None) -> int:
    arguments: _SealArguments = build_parser().parse_args(argv)
    settings = load_app_settings()
    story_index = arguments.story_index or settings.story_index
    sequence_plan = arguments.sequence_plan or settings.live_sequence_plan
    generated_manifest = _generated_audio_manifest_for_run(arguments, settings)
    audio_policy = arguments.audio_source_policy or settings.audio_source_policy
    if not story_index:
        return int(cli_error("Configure or pass --story-index"))
    if not sequence_plan:
        return int(cli_error("Configure or pass --sequence-plan"))
    if arguments.timeout <= 0:
        return int(cli_error("timeout must be positive"))
    try:
        result = seal_sequence_replay(
            arguments.capture_corpus,
            arguments.output,
            story_index=story_index,
            sequence_plan=sequence_plan,
            mode=arguments.mode,
            generated_audio_manifest=generated_manifest,
            audio_source_policy=audio_policy,
            timeout_seconds=arguments.timeout,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return int(cli_error(error))
    return int(
        cli_messages(
            (
                f"Sealed {result.dialogue_count} sequence-bound dialogue events",
                (
                    "Operator boundary/mapping review required"
                    if result.operator_review_required
                    else "No inferred boundary or mapping review flags"
                ),
                result.corpus,
                result.review,
                result.replay_report,
            )
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SEQUENCE_REPLAY_SEAL_VERSION",
    "SealedSequenceReplayResult",
    "SequenceReplaySealError",
    "build_parser",
    "main",
    "seal_sequence_replay",
]
