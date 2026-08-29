"""Seal a raw real-game replay capture into a sequence-bound replay corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vntts_artifacts.generated_audio import GeneratedAudioDocument

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.cli import cli_error, cli_messages
from vntts.live_replay import LiveReplayRunner, load_live_replay_corpus
from vntts.live_sequence import LiveSequencePlan
from vntts.settings import audio_source_policies, load_app_settings

SEQUENCE_REPLAY_SEAL_VERSION = 1


class SequenceReplaySealError(RuntimeError):
    """A raw capture cannot be safely bound to one exact sequence."""


@dataclass(frozen=True)
class SealedSequenceReplayResult:
    directory: Path
    corpus: Path
    review: Path
    replay_report: Path
    dialogue_count: int
    operator_review_required: bool


def seal_sequence_replay(
    capture_corpus,
    output_directory,
    *,
    story_index,
    sequence_plan,
    mode="audio-manual",
    generated_audio_manifest=None,
    audio_source_policy="prefer-game-audio",
    recognizer=None,
    interval_seconds=0.01,
    timeout_seconds=30.0,
):
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
        or not isinstance(capture.get("capture"), dict)
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

    _story_path, story_payload = _read_regular_file(story_index, "Story index")
    _plan_path, plan_payload = _read_regular_file(sequence_plan, "Sequence plan")
    story_sha256 = hashlib.sha256(story_payload).hexdigest()
    plan_sha256 = hashlib.sha256(plan_payload).hexdigest()
    captured_story_sha256 = capture["capture"].get("story_index_sha256")
    if captured_story_sha256 != story_sha256:
        raise SequenceReplaySealError(
            "Raw capture is not bound to the selected story-index bytes"
        )
    recovery = capture["capture"].get("recovery")
    if recovery is not None and recovery["sequence_plan_sha256"] != plan_sha256:
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
            raise SequenceReplaySealError(
                f"Story index and sequence plan are incompatible: {error}"
            ) from error
        mappings = _map_dialogue(raw_dialogue, resolver, plan)
        frame_records = _copy_frames(capture_path.parent, staging, raw_dialogue)
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
            raw_dialogue,
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
        corpus = {
            "schema_version": 2,
            "name": f"{capture.get('name') or capture_path.stem} sequence replay",
            "fixture_kind": "sealed-real-capture-production-controller",
            "capture": {
                **capture["capture"],
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
        route_sources = iter(probe["route_sources"])
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
        corpus["live_sequence"]["expected"] = probe["sequence"]["observed"]
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
            capture["capture"].get("boundary_review_required")
        )
        inferred_mapping = any(
            mapping["mapping_method"] != "exact-line-id" for mapping in mappings
        )
        review = {
            "schema": "vntts.sequence-replay-seal-review",
            "schema_version": SEQUENCE_REPLAY_SEAL_VERSION,
            "operator_review_required": boundary_review_required or inferred_mapping,
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
            "capture_boundary_review_count": capture["capture"].get(
                "boundary_review_count", 0
            ),
            "capture_report_boundary_count": len(
                capture_report_document.get("boundaries", ())
            ),
            "mappings": mappings,
            "measured_baseline": {
                "route_sources": final_report["route_sources"],
                **final_report["sequence"]["observed"],
            },
            "sealed_replay_successful": True,
        }
        review_path = staging / "sequence-review.json"
        _write_json(review_path, review)
        os.replace(staging, output)
        return SealedSequenceReplayResult(
            output,
            output / corpus_path.name,
            output / review_path.name,
            output / replay_report.name,
            len(dialogue),
            review["operator_review_required"],
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _map_dialogue(raw_dialogue, resolver, plan):
    mappings = []
    previous_event = None
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
        frontier = (
            None
            if previous_event is None
            else _next_visible_events(plan, previous_event)
        )
        raw_line_id = str(record.get("line_id") or "").strip() or None
        line = resolver.line_for_id(raw_line_id) if raw_line_id else None
        event = plan.event_for_line(line.line_id) if line is not None else None
        method = "exact-line-id"
        if event is not None:
            if (line.speaker, line.text) != (character, text):
                raise SequenceReplaySealError(
                    f"Raw replay dialogue {index} disagrees with canonical line "
                    f"{line.line_id!r}"
                )
        else:
            if previous_event is None:
                candidates = tuple(
                    event
                    for event in plan.events.values()
                    if event.is_speech
                    and _normalized_exact(resolver.line_for_id(event.line_id).text)
                    == _normalized_exact(text)
                )
            else:
                candidates = frontier
            if previous_event is not None and len(candidates) != 1:
                raise SequenceReplaySealError(
                    f"Raw replay dialogue {index} reaches an ambiguous or skipped "
                    "visible sequence frontier"
                )
            if _standalone_ellipsis(text):
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
                speech = []
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


def _next_visible_events(plan, event):
    pending = list(event.successors)
    visited = set()
    visible = []
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


def _copy_frames(capture_root, staging, raw_dialogue):
    copied = []
    seen = {}
    for dialogue_index, record in enumerate(raw_dialogue, start=1):
        frames = record.get("frames") if isinstance(record, dict) else None
        if not isinstance(frames, list) or not frames:
            raise SequenceReplaySealError(
                f"Raw replay dialogue {dialogue_index} has no exact frames"
            )
        copied_frames = []
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


def _snapshot_generated_audio(manifest, staging, mappings, resolver):
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
    identities = {
        (mapping["line_id"], resolver.line_for_id(mapping["line_id"]).text_sha256)
        for mapping in mappings
        if mapping["line_id"] is not None
    }
    selected = [
        record
        for record in document.records
        if (record.line_id, record.text_sha256) in identities
        and document.find(record.line_id, record.text_sha256) is not None
    ]
    if not selected:
        return None, set()
    raw_document = _decode_json(manifest_payload, "Generated audio manifest")
    records = []
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
        wire = record.to_record()
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


def _sealed_dialogue(
    raw_dialogue,
    frame_records,
    mappings,
    resolver,
    generated_lines,
    *,
    mode,
    audio_source_policy,
):
    dialogue = []
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
                and (mode != "shadow" or line.source_audio_duration_seconds is not None)
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
                "expected_source": expected_source,
            }
        dialogue.append(record)
    return dialogue


def _validate_probe(report, mappings, dialogue):
    expected_dialogue = [
        {"character": record["character"], "text": record["text"]}
        for record in dialogue
        if record["expect_playback"]
    ]
    expected_event_ids = [mapping["event_id"] for mapping in mappings]
    expected_line_ids = [mapping["line_id"] for mapping in mappings]
    if report["errors"]:
        raise SequenceReplaySealError(
            f"Production replay probe failed: {report['errors'][0]}"
        )
    if not report["media_integrity"]["frame_consumption"]["complete"]:
        consumption = report["media_integrity"]["frame_consumption"]
        raise SequenceReplaySealError(
            "Production replay probe did not consume all frames: "
            f"{consumption['consumed_count']}/{consumption['declared_count']}"
        )
    if report["observed_dialogue"] != expected_dialogue:
        raise SequenceReplaySealError(
            "Production replay probe did not reproduce canonical captured speech"
        )
    observed = report["sequence"]["observed"]
    if (
        observed["event_ids"] != expected_event_ids
        or observed["line_ids"] != expected_line_ids
    ):
        raise SequenceReplaySealError(
            "Production replay probe did not reproduce canonical sequence identities"
        )
    expected_routes = sum(record["expect_playback"] for record in dialogue)
    if len(report["route_sources"]) != expected_routes:
        raise SequenceReplaySealError(
            "Production replay probe did not produce one route per speech record"
        )


def _validate_capture_report(capture, report):
    if (
        report.get("schema") != "vntts.live-replay-capture-report"
        or report.get("schema_version") != 1
    ):
        raise SequenceReplaySealError("Capture review report has an unsupported schema")
    authority = capture["capture"]
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
    raw_dialogue = capture.get("dialogue")
    expected_dialogue = [
        {
            "dialogue_index": index,
            "character": record.get("character"),
            "text": record.get("text"),
            "line_id": record.get("line_id"),
            "story_match": record.get("story_match"),
            "frame_count": len(record.get("frames", ())),
            "boundary_reason": record.get("capture_boundary"),
        }
        for index, record in enumerate(raw_dialogue, start=1)
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
        for index, record in enumerate(raw_dialogue, start=1)
        if record.get("capture_boundary") == "inferred-observation-replacement"
    ]
    if boundaries != expected_boundaries:
        raise SequenceReplaySealError(
            "Capture review report boundary ledger disagrees with the raw corpus"
        )


def _validate_capture_observation_ledger(capture_path, capture, report):
    authority = capture["capture"]
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
    ledger_frames = set()
    statuses = []
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
    dialogue_frames = {
        (frame.get("path"), frame.get("sha256"))
        for record in capture["dialogue"]
        for frame in record.get("frames", ())
        if isinstance(frame, dict)
    }
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
    _validate_recovery_authority(capture_path.parent, recovery, report)


def _validate_recovery_authority(root, recovery, report):
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


def _normalized_exact(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _standalone_ellipsis(value):
    text = "".join(str(value).split())
    return text.replace("…", "...") == "..."


def _decode_json(payload, label):
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SequenceReplaySealError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise SequenceReplaySealError(f"{label} root must be an object")
    return document


def _read_regular_file(value, label):
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


def _read_contained(root, value, label):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        raise SequenceReplaySealError(f"{label} must use a contained relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SequenceReplaySealError(f"{label} must use a contained relative path")
    root = Path(root).resolve()
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SequenceReplaySealError(f"{label} path must not contain symlinks")
    try:
        path = current.resolve()
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise SequenceReplaySealError(
            f"{label} leaves its capture directory"
        ) from error
    _path, payload = _read_regular_file(path, label)
    return relative.as_posix(), payload


def _required_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SequenceReplaySealError(f"{label} must be a lowercase SHA-256")
    return value


def _write_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _write_json(path, document):
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if path.exists():
        path.unlink()
    _write_bytes(path, payload)


def build_parser():
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


def _generated_audio_manifest_for_run(arguments, settings):
    if arguments.no_generated_audio_manifest:
        return None
    return arguments.generated_audio_manifest or settings.generated_audio_manifest


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    settings = load_app_settings()
    story_index = arguments.story_index or settings.story_index
    sequence_plan = arguments.sequence_plan or settings.live_sequence_plan
    generated_manifest = _generated_audio_manifest_for_run(arguments, settings)
    audio_policy = arguments.audio_source_policy or settings.audio_source_policy
    if not story_index:
        return cli_error("Configure or pass --story-index")
    if not sequence_plan:
        return cli_error("Configure or pass --sequence-plan")
    if arguments.timeout <= 0:
        return cli_error("timeout must be positive")
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
        return cli_error(error)
    return cli_messages(
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
