"""Recover a fail-closed sequence segment from immutable live capture evidence."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.cli import cli_error, cli_messages
from vntts.dialog_capture import (
    detect_standalone_ellipsis_frame,
    ellipsis_speaker_hint,
)
from vntts.live_replay_sequence_seal import (
    SequenceReplaySealError,
    _decode_json,
    _next_visible_events,
    _read_contained,
    _read_regular_file,
    _required_sha256,
    _standalone_ellipsis,
    _validate_capture_report,
    _write_bytes,
    _write_json,
)
from vntts.live_sequence import LiveSequencePlan
from vntts.settings import load_app_settings

CAPTURE_RECOVERY_VERSION = 1
_BLOCKED_OBSERVATION = object()


class LiveReplayCaptureRecoveryError(RuntimeError):
    """Capture evidence cannot yield one unambiguous sequence segment."""


@dataclass(frozen=True)
class CaptureRecoveryResult:
    directory: Path
    report: Path
    corpus: Path | None
    event_count: int
    contains_silent: bool
    sufficient: bool
    recommended_follow_up: dict | None


@dataclass(frozen=True)
class _Observation:
    observation_index: int
    frames: tuple[dict, ...]
    character: str | None
    text: str | None
    line_id: str | None
    status: str


@dataclass
class _MappedEvent:
    event: object
    frames: list[dict]
    observation_indices: list[int]
    mapping_method: str
    observed_character: str
    observed_text: str
    absorbed_observation_indices: list[int]


def recover_live_replay_capture(
    capture_corpus,
    output_directory,
    *,
    story_index,
    sequence_plan,
    minimum_events=20,
    require_silent=True,
):
    """Publish a new raw corpus only when one explicit capture path meets its gate."""
    if isinstance(minimum_events, bool) or minimum_events < 1:
        raise LiveReplayCaptureRecoveryError("minimum_events must be positive")
    capture_path, capture_payload = _read_regular_file(
        capture_corpus, "Raw replay corpus"
    )
    capture = _decode_json(capture_payload, "Raw replay corpus")
    if (
        capture.get("schema_version") != 1
        or capture.get("fixture_kind") != "saved-frame-ocr-replay-capture"
        or not isinstance(capture.get("capture"), dict)
    ):
        raise LiveReplayCaptureRecoveryError(
            "Capture recovery requires raw vntts-capture-live-replay output"
        )
    raw_dialogue = capture.get("dialogue")
    if (
        not isinstance(raw_dialogue, list)
        or not raw_dialogue
        or any(not isinstance(record, dict) for record in raw_dialogue)
    ):
        raise LiveReplayCaptureRecoveryError("Raw replay corpus has no dialogue")
    report_path, report_payload = _read_regular_file(
        capture_path.with_name("capture-report.json"), "Capture review report"
    )
    report_document = _decode_json(report_payload, "Capture review report")
    try:
        _validate_capture_report(capture, report_document)
    except SequenceReplaySealError as error:
        raise LiveReplayCaptureRecoveryError(str(error)) from error

    _story_path, story_payload = _read_regular_file(story_index, "Story index")
    _plan_path, plan_payload = _read_regular_file(sequence_plan, "Sequence plan")
    story_sha256 = hashlib.sha256(story_payload).hexdigest()
    plan_sha256 = hashlib.sha256(plan_payload).hexdigest()
    if capture["capture"].get("story_index_sha256") != story_sha256:
        raise LiveReplayCaptureRecoveryError(
            "Raw capture is not bound to the selected story-index bytes"
        )

    selected_output = Path(output_directory).expanduser()
    if selected_output.exists() or selected_output.is_symlink():
        raise LiveReplayCaptureRecoveryError(
            f"Capture recovery output already exists: {selected_output}"
        )
    parent = selected_output.parent.resolve()
    if not parent.is_dir():
        raise LiveReplayCaptureRecoveryError(
            f"Capture recovery parent does not exist: {parent}"
        )
    output = parent / selected_output.name
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=parent))
    try:
        authority = staging / "authority"
        authority.mkdir()
        story_copy = authority / "story-index.jsonl"
        plan_copy = authority / "live-sequence.json"
        _write_bytes(story_copy, story_payload)
        _write_bytes(plan_copy, plan_payload)
        try:
            resolver = ChapterVoicePreloader.load_optional(story_copy)
            plan = LiveSequencePlan.load(plan_copy, story_copy)
        except Exception as error:
            raise LiveReplayCaptureRecoveryError(
                f"Story index and sequence plan are incompatible: {error}"
            ) from error

        observations, ledger_authority, ledger_payload, visual_ellipses = (
            _load_observations(
                capture_path,
                capture,
                raw_dialogue,
                resolver,
            )
        )
        candidates = _candidate_events(observations, resolver, plan)
        selected = _longest_explicit_run(candidates, plan)
        contains_silent = any(item.event.kind == "silent" for item in selected)
        sufficient = len(selected) >= minimum_events and (
            contains_silent or not require_silent
        )
        follow_up = None
        if not sufficient:
            follow_up = _recommended_capture_segment(
                plan,
                minimum_events,
                require_silent=require_silent,
            )
        analysis = {
            "schema": "vntts.live-replay-capture-recovery-report",
            "schema_version": CAPTURE_RECOVERY_VERSION,
            "authority": {
                "raw_corpus_sha256": hashlib.sha256(capture_payload).hexdigest(),
                "capture_report_sha256": hashlib.sha256(report_payload).hexdigest(),
                "observation_ledger_sha256": ledger_authority,
                "story_index_sha256": story_sha256,
                "sequence_plan_sha256": plan_sha256,
            },
            "raw_dialogue_count": len(raw_dialogue),
            "raw_observation_count": len(observations),
            "candidate_count": sum(
                event is not _BLOCKED_OBSERVATION
                for _observation, event, _method in candidates
            ),
            "blocked_observation_count": sum(
                event is _BLOCKED_OBSERVATION
                for _observation, event, _method in candidates
            ),
            "visually_classified_ellipsis_observations": visual_ellipses,
            "absorbed_transient_observations": [
                {
                    "observation_index": observation_index,
                    "event_id": item.event.event_id,
                    "reason": "intervening-noise-before-explicit-successor",
                }
                for item in selected
                for observation_index in item.absorbed_observation_indices
            ],
            "minimum_event_count": minimum_events,
            "silent_event_required": bool(require_silent),
            "selected_event_count": len(selected),
            "selected_contains_silent": contains_silent,
            "sufficient": sufficient,
            "selected": [_mapped_wire(item) for item in selected],
            "recommended_follow_up_capture": follow_up,
            "note": (
                "Unselected observations remain immutable raw evidence. They are "
                "not silently promoted to sequence events."
            ),
        }
        recovery_report = staging / "recovery-report.json"
        _write_json(recovery_report, analysis)
        corpus_path = None
        if sufficient:
            corpus_path = _publish_recovered_corpus(
                staging,
                capture_path.parent,
                capture,
                capture_payload,
                report_payload,
                ledger_payload,
                selected,
                resolver,
                story_sha256=story_sha256,
                plan_sha256=plan_sha256,
            )
        os.replace(staging, output)
        return CaptureRecoveryResult(
            output,
            output / recovery_report.name,
            None if corpus_path is None else output / corpus_path.name,
            len(selected),
            contains_silent,
            sufficient,
            follow_up,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _load_observations(capture_path, capture, raw_dialogue, resolver):
    binding = capture["capture"].get("observation_ledger")
    if binding is None:
        observations = []
        for index, record in enumerate(raw_dialogue, start=1):
            frames = record.get("frames")
            if not isinstance(frames, list) or not frames:
                raise LiveReplayCaptureRecoveryError(
                    f"Raw replay dialogue {index} has no exact frames"
                )
            _validate_frames(capture_path.parent, frames)
            observations.append(
                _Observation(
                    index,
                    tuple(frames),
                    str(record.get("character") or "Narrator").strip() or "Narrator",
                    " ".join(str(record.get("text") or "").split()) or None,
                    str(record.get("line_id") or "").strip() or None,
                    "legacy-dialogue",
                )
            )
        return tuple(observations), None, None, []
    if not isinstance(binding, dict):
        raise LiveReplayCaptureRecoveryError(
            "Raw capture observation ledger binding is invalid"
        )
    _relative, payload = _read_contained(
        capture_path.parent,
        binding.get("path"),
        "Capture observation ledger",
    )
    digest = _required_sha256(
        binding.get("sha256"), "Capture observation ledger sha256"
    )
    if hashlib.sha256(payload).hexdigest() != digest:
        raise LiveReplayCaptureRecoveryError(
            "Capture observation ledger checksum changed"
        )
    document = _decode_json(payload, "Capture observation ledger")
    entries = document.get("observations")
    if (
        document.get("schema") != "vntts.live-replay-capture-observations"
        or document.get("schema_version") != 1
        or document.get("story_index_sha256")
        != capture["capture"].get("story_index_sha256")
        or not isinstance(entries, list)
        or len(entries) != binding.get("observation_count")
        or document.get("observation_count") != len(entries)
    ):
        raise LiveReplayCaptureRecoveryError(
            "Capture observation ledger authority is invalid"
        )
    observations = []
    visual_ellipses = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or entry.get("observation_index") != index:
            raise LiveReplayCaptureRecoveryError(
                "Capture observation ledger order is invalid"
            )
        frame = entry.get("frame")
        frame_payload = _validated_frame_payload(capture_path.parent, frame)
        character = _optional_text(entry.get("observed_character"))
        text = _optional_text(entry.get("observed_text"))
        status = str(entry.get("status") or "unknown")
        if status in {"unresolved", "uncertain"}:
            try:
                with Image.open(io.BytesIO(frame_payload)) as image:
                    visual_ellipsis = detect_standalone_ellipsis_frame(image)
            except OSError as error:
                raise LiveReplayCaptureRecoveryError(
                    "Capture observation frame is not a valid image"
                ) from error
            if visual_ellipsis:
                character = ellipsis_speaker_hint(character, text, resolver)
                text = "..."
                status = "visual-ellipsis"
                visual_ellipses.append(
                    {
                        "observation_index": index,
                        "speaker_hint": character,
                        "method": "isolated-three-dot-glyph",
                    }
                )
        observations.append(
            _Observation(
                index,
                (frame,),
                character,
                text,
                _optional_text(entry.get("story_line_id")),
                status,
            )
        )
    return tuple(observations), digest, payload, visual_ellipses


def _validate_frames(root, frames):
    for frame in frames:
        _validated_frame_payload(root, frame)


def _validated_frame_payload(root, frame):
    if not isinstance(frame, dict) or set(frame) != {"path", "sha256"}:
        raise LiveReplayCaptureRecoveryError(
            "Capture observation frame must bind only path and sha256"
        )
    _relative, payload = _read_contained(
        root, frame.get("path"), "Capture observation frame"
    )
    digest = _required_sha256(frame.get("sha256"), "Capture observation frame sha256")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise LiveReplayCaptureRecoveryError(
            "Capture observation frame checksum changed"
        )
    return payload


def _candidate_events(observations, resolver, plan):
    candidates = []
    for observation in observations:
        if (
            observation.status
            in {"punctuation-only", "visual-ellipsis", "legacy-dialogue"}
            and observation.text
            and _standalone_ellipsis(observation.text)
        ):
            candidates.append((observation, None, observation.status))
            continue
        if observation.status in {"canonical", "legacy-dialogue"}:
            line = resolver.line_for_id(observation.line_id)
            event = plan.event_for_line(observation.line_id)
            if (
                line is not None
                and event is not None
                and observation.text
                and _normalized_text(observation.text) == _normalized_text(line.text)
                and _normalized_text(observation.character)
                == _normalized_text(line.speaker)
            ):
                candidates.append((observation, event, "exact-canonical-observation"))
                continue
        bounded = _bounded_plan_match(observation, resolver, plan)
        if bounded is None:
            candidates.append(
                (observation, _BLOCKED_OBSERVATION, "unresolved-observation")
            )
        else:
            event, method = bounded
            candidates.append((observation, event, method))
    return tuple(candidates)


def _bounded_plan_match(observation, resolver, plan):
    if not observation.text or observation.status == "uncertain":
        return None
    line, method = resolver.resolve_bounded_among(
        observation.character,
        observation.text,
        (
            event.line_id
            for event in plan.events.values()
            if event.kind == "speech" and event.line_id
        ),
    )
    if line is None:
        return None
    event = plan.event_for_line(line.line_id)
    if event is None:
        return None
    return event, method


def _longest_explicit_run(candidates, plan):
    best = []
    for start, (observation, event, method) in enumerate(candidates):
        if event is None or event is _BLOCKED_OBSERVATION:
            continue
        run = [_mapped_event(observation, event, method)]
        current = event
        pending = []
        for next_observation, next_event, next_method in candidates[start + 1 :]:
            if next_event is _BLOCKED_OBSERVATION:
                pending.append(next_observation.observation_index)
                continue
            if next_event is not None and next_event.event_id == current.event_id:
                _merge_mapped_observation(
                    run[-1],
                    next_observation,
                    next_method,
                    pending,
                )
                pending = []
                continue
            if (
                next_event is None
                and current.kind == "silent"
                and _same_silent_observation(run[-1], next_observation)
            ):
                run[-1].absorbed_observation_indices.extend(
                    [*pending, next_observation.observation_index]
                )
                pending = []
                continue
            visible = _next_visible_events(plan, current)
            if len(visible) != 1:
                break
            expected = visible[0]
            if next_event is not None and next_event.event_id == expected.event_id:
                run.append(
                    _mapped_event(
                        next_observation,
                        next_event,
                        next_method,
                        absorbed=pending,
                    )
                )
                pending = []
                current = next_event
                continue
            if next_event is None and expected.kind == "silent":
                run.append(
                    _mapped_event(
                        next_observation,
                        expected,
                        "unique-punctuation-frontier",
                        absorbed=pending,
                    )
                )
                pending = []
                current = expected
                continue
            break
        if len(run) > len(best):
            best = run
    return best


def _same_silent_observation(current, observation):
    current_speaker = _normalized_text(current.observed_character)
    observed_speaker = _normalized_text(observation.character)
    unknown = {"", "narrator", "unknown"}
    if current_speaker in unknown or observed_speaker in unknown:
        return True
    return current_speaker == observed_speaker


def _merge_mapped_observation(current, observation, method, pending):
    incoming_rank = _mapping_rank(method)
    current_rank = _mapping_rank(current.mapping_method)
    if incoming_rank >= current_rank:
        current.absorbed_observation_indices.extend(
            [*current.observation_indices, *pending]
        )
        current.frames = list(observation.frames)
        current.observation_indices = [observation.observation_index]
        current.mapping_method = method
        current.observed_character = observation.character or "Narrator"
        current.observed_text = observation.text or "..."
    else:
        current.absorbed_observation_indices.extend(
            [*pending, observation.observation_index]
        )


def _mapping_rank(method):
    return {
        "exact-canonical-observation": 4,
        "expected-exact": 4,
        "expected-normalized-exact": 4,
        "expected-text-only": 4,
        "expected-bounded-similarity": 3,
        "expected-bounded-ocr-suffix": 3,
        "expected-bounded-prefix": 2,
    }.get(str(method), 1)


def _recommended_capture_segment(plan, minimum_events, *, require_silent):
    """Return one shortest explicit visible run that can satisfy the gate."""
    visible_events = sorted(
        (event for event in plan.events.values() if event.kind in {"speech", "silent"}),
        key=lambda event: (str(event.chapter), event.sequence, event.event_id),
    )
    for start in visible_events:
        segment = [start]
        current = start
        while len(segment) < minimum_events:
            successors = _next_visible_events(plan, current)
            if len(successors) != 1:
                break
            current = successors[0]
            if current.event_id in {event.event_id for event in segment}:
                break
            segment.append(current)
        if len(segment) != minimum_events:
            continue
        if require_silent and not any(event.kind == "silent" for event in segment):
            continue
        return {
            "minimum_visible_events": minimum_events,
            "must_include_silent": bool(require_silent),
            "start": _event_wire(segment[0]),
            "end": _event_wire(segment[-1]),
            "events": [_event_wire(event) for event in segment],
            "instruction": (
                "Start capture with the first listed event fully visible and continue "
                "without skipping until the last listed event is fully visible."
            ),
        }
    return {
        "minimum_visible_events": minimum_events,
        "must_include_silent": bool(require_silent),
        "events": [],
        "instruction": (
            "No branch-free explicit segment in this plan can satisfy the requested "
            "gate; revise the plan or acceptance gate before another capture."
        ),
    }


def _event_wire(event):
    return {
        "event_id": event.event_id,
        "chapter": event.chapter,
        "sequence": event.sequence,
        "kind": event.kind,
        "line_id": event.line_id,
    }


def _mapped_event(observation, event, method, *, absorbed=()):
    return _MappedEvent(
        event,
        list(observation.frames),
        [observation.observation_index],
        method,
        observation.character or "Narrator",
        observation.text or "...",
        list(absorbed),
    )


def _mapped_wire(item):
    return {
        "event_id": item.event.event_id,
        "event_kind": item.event.kind,
        "line_id": item.event.line_id,
        "mapping_method": item.mapping_method,
        "observation_indices": item.observation_indices,
        "absorbed_observation_indices": item.absorbed_observation_indices,
        "frame_count": len(item.frames),
    }


def _publish_recovered_corpus(
    staging,
    capture_root,
    capture,
    capture_payload,
    report_payload,
    raw_observation_payload,
    selected,
    resolver,
    *,
    story_sha256,
    plan_sha256,
):
    copied = {}
    dialogue = []
    ledger = []
    for dialogue_index, item in enumerate(selected, start=1):
        frames = []
        for source in item.frames:
            key = (source["path"], source["sha256"])
            frame = copied.get(key)
            if frame is None:
                _relative, payload = _read_contained(
                    capture_root,
                    source["path"],
                    "Recovered capture frame",
                )
                if hashlib.sha256(payload).hexdigest() != source["sha256"]:
                    raise LiveReplayCaptureRecoveryError(
                        "Recovered capture frame checksum changed"
                    )
                relative = f"frames/frame-{len(copied) + 1:06d}.png"
                destination = staging / relative
                _write_bytes(destination, payload)
                frame = {"path": relative, "sha256": source["sha256"]}
                copied[key] = frame
            frames.append(frame)
        dialogue.append(
            _recovered_dialogue_record(dialogue_index, item, frames, resolver)
        )
        for frame in frames:
            ledger.append(
                {
                    "observation_index": len(ledger) + 1,
                    "frame": frame,
                    "status": (
                        "punctuation-only"
                        if item.event.kind == "silent"
                        else "canonical"
                    ),
                    "observed_character": item.observed_character,
                    "observed_text": item.observed_text,
                    "story_line_id": item.event.line_id,
                    "story_match": item.mapping_method,
                }
            )
    observation_document = {
        "schema": "vntts.live-replay-capture-observations",
        "schema_version": 1,
        "story_index_sha256": story_sha256,
        "observation_count": len(ledger),
        "observations": ledger,
    }
    observation_path = staging / "observation-ledger.json"
    _write_json(observation_path, observation_document)
    observation_binding = {
        "path": observation_path.name,
        "sha256": hashlib.sha256(observation_path.read_bytes()).hexdigest(),
        "observation_count": len(ledger),
    }
    capture_authority = {
        "schema_version": 1,
        "frame_count": len(copied),
        "dialogue_count": len(dialogue),
        "boundary_review_required": False,
        "boundary_review_count": 0,
        "story_index_sha256": story_sha256,
        "observation_ledger": observation_binding,
        "unresolved_observation_count": 0,
        "recovery": {
            "schema_version": CAPTURE_RECOVERY_VERSION,
            "raw_corpus_sha256": hashlib.sha256(capture_payload).hexdigest(),
            "capture_report_sha256": hashlib.sha256(report_payload).hexdigest(),
            "sequence_plan_sha256": plan_sha256,
            "mapping_policy": "exact-explicit-sequence-run",
        },
    }
    corpus_document = {
        "schema_version": 1,
        "name": f"{capture.get('name') or 'Captured live replay'} recovered run",
        "fixture_kind": "saved-frame-ocr-replay-capture",
        "capture": capture_authority,
        "dialogue": dialogue,
    }
    report_document = {
        "schema": "vntts.live-replay-capture-report",
        "schema_version": 1,
        **capture_authority,
        "duplicate_fingerprints_skipped": 0,
        "uncertain_observations_skipped": 0,
        "unresolved_observation_count": 0,
        "observation_ledger": observation_binding,
        "boundaries": [],
        "dialogue": [
            {
                "dialogue_index": index,
                "character": record["character"],
                "text": record["text"],
                "line_id": record["line_id"],
                "story_match": record["story_match"],
                "frame_count": len(record["frames"]),
                "boundary_reason": record["capture_boundary"],
            }
            for index, record in enumerate(dialogue, start=1)
        ],
    }
    corpus_path = staging / "corpus.json"
    capture_report = staging / "capture-report.json"
    _write_json(corpus_path, corpus_document)
    _write_json(capture_report, report_document)
    _validate_capture_report(corpus_document, report_document)
    provenance = staging / "provenance"
    _write_bytes(provenance / "raw-corpus.json", capture_payload)
    _write_bytes(provenance / "capture-report.json", report_payload)
    if raw_observation_payload is not None:
        _write_bytes(provenance / "observation-ledger.json", raw_observation_payload)
    return corpus_path


def _recovered_dialogue_record(index, item, frames, resolver):
    if item.event.kind == "silent":
        return {
            "frames": frames,
            "character": item.observed_character,
            "text": item.observed_text,
            "line_id": f"capture:{index}",
            "source_audio_status": "unknown",
            "expected_source": None,
            "capture_boundary": "recovered-unique-silent-frontier",
            "story_match": "punctuation-only",
        }
    line = resolver.line_for_id(item.event.line_id)
    return {
        "frames": frames,
        "character": line.speaker,
        "text": line.text,
        "line_id": line.line_id,
        "source_audio_status": line.source_audio_status,
        "source_audio_id": line.source_audio_id,
        "source_audio_duration_seconds": line.source_audio_duration_seconds,
        "expected_source": (
            "game" if line.source_audio_status == "available" else None
        ),
        "capture_boundary": "recovered-exact-sequence",
        "story_match": "exact-recovery",
    }


def _optional_text(value):
    if value is None:
        return None
    return str(value).strip() or None


def _normalized_text(value):
    return " ".join(re.findall(r"\w+", str(value or "").casefold()))


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Recover one explicit sequence segment from immutable live capture evidence"
        )
    )
    parser.add_argument("capture_corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--story-index", type=Path)
    parser.add_argument("--sequence-plan", type=Path)
    parser.add_argument("--minimum-events", type=int, default=20)
    parser.add_argument("--allow-no-silent", action="store_true")
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    settings = load_app_settings()
    story_index = arguments.story_index or settings.story_index
    sequence_plan = arguments.sequence_plan or settings.live_sequence_plan
    if not story_index:
        return cli_error("Configure or pass --story-index")
    if not sequence_plan:
        return cli_error("Configure or pass --sequence-plan")
    try:
        result = recover_live_replay_capture(
            arguments.capture_corpus,
            arguments.output,
            story_index=story_index,
            sequence_plan=sequence_plan,
            minimum_events=arguments.minimum_events,
            require_silent=not arguments.allow_no_silent,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return cli_error(error)
    messages = [
        f"Longest explicit recovered run: {result.event_count} events",
        (
            "Recovered run includes a silent event"
            if result.contains_silent
            else "Recovered run has no silent event"
        ),
        result.report,
    ]
    if result.corpus is not None:
        messages.append(result.corpus)
    elif result.recommended_follow_up:
        start = result.recommended_follow_up.get("start")
        end = result.recommended_follow_up.get("end")
        if start and end:
            messages.append(
                "Next capture: chapter "
                f"{start['chapter']}, visible sequence {start['sequence']} through "
                f"{end['sequence']} ({result.recommended_follow_up['minimum_visible_events']} "
                "events, without skipping)"
            )
        else:
            messages.append(result.recommended_follow_up["instruction"])
    cli_messages(messages)
    return 0 if result.sufficient else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAPTURE_RECOVERY_VERSION",
    "CaptureRecoveryResult",
    "LiveReplayCaptureRecoveryError",
    "build_parser",
    "main",
    "recover_live_replay_capture",
]
