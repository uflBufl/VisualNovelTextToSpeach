"""Bounded offline source/output correlation after native recording has stopped."""

import hashlib
import json
import math
import re
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, NamedTuple

import numpy as np
import soundfile as sf

from vntts.diagnostic_caf import MAX_CAPTURE_SECONDS, diagnostic_audio_path
from vntts.path_safety import open_regular_binary

RATE = 16000
MAX_SOURCE_SECONDS = 60
MATCH_STEP = RATE * 30
MAX_MATCH_SAMPLES = 1 << 22


class _CaptureAudio(NamedTuple):
    path: Path
    offset: float
    residual_ms: float
    frames: int
    pid: int


class _RouteCapture(NamedTuple):
    source: dict
    dispatch: float
    source_lead_ms: float | None
    identity_verified: bool


def normalized_match(audio: np.ndarray, source: np.ndarray) -> tuple[int, float] | None:
    if (
        audio.ndim != 1
        or source.ndim != 1
        or not np.isfinite(audio).all()
        or not np.isfinite(source).all()
    ):
        raise ValueError("Correlation requires finite mono PCM")
    if len(source) < 16 or len(audio) < len(source):
        return None
    size = 1 << (len(audio) + len(source) - 2).bit_length()
    if size > MAX_MATCH_SAMPLES:
        raise ValueError("Correlation exceeds its memory budget")
    # Float32 FFT cancellation can overwhelm near-silent candidate energy.
    audio = audio.astype(np.float64, copy=False)
    source = source.astype(np.float64, copy=False)
    source = source - source.mean()
    energy = float(np.dot(source, source))
    if energy < 1e-10:
        return None
    convolution = np.fft.irfft(
        np.fft.rfft(audio, size) * np.fft.rfft(source[::-1], size), size
    )
    correlation = convolution[len(source) - 1 : len(audio)]
    sums = np.r_[0.0, np.cumsum(audio, dtype=np.float64)]
    squares = np.r_[0.0, np.cumsum(audio**2)]
    n = len(source)
    variance = np.maximum(
        squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n, 0
    )
    norm = np.sqrt(variance * energy)
    valid = norm > 1e-10
    if not valid.any():
        return None
    scores = np.divide(
        correlation, norm, out=np.full_like(correlation, -np.inf), where=valid
    )
    index = int(np.argmax(scores))
    # Verify the winner without FFT or cumulative-sum cancellation.
    candidate = audio[index : index + n]
    candidate = candidate - candidate.mean()
    direct_norm = math.sqrt(float(np.dot(candidate, candidate)) * energy)
    if direct_norm <= 1e-10:
        return None
    score = float(np.dot(candidate, source)) / direct_norm
    return index, min(1.0, max(-1.0, score))


def _check(started: float, cancelled: Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("Source analysis cancelled")
    if monotonic() - started > 120:
        raise TimeoutError("Source analysis reached its 120-second budget")


def _audio(
    path: Path,
    *,
    seconds: int = MAX_CAPTURE_SECONDS,
    first: int = 0,
    last: int | None = None,
    cancelled: Event | None = None,
    started: float | None = None,
) -> np.ndarray:
    # Absolute resampling positions preserve the capture clock across window boundaries.
    with open_regular_binary(path) as source, sf.SoundFile(source) as audio:
        if (
            not 8000 <= audio.samplerate <= 192000
            or audio.channels not in (1, 2)
            or audio.frames > seconds * audio.samplerate
        ):
            raise ValueError("Unsupported or oversized diagnostic audio")
        rate, frames = audio.samplerate, audio.frames
        total = round(frames * RATE / rate)
        last = total if last is None else min(total, last)
        if first < 0 or last < first or last - first > RATE * (30 + MAX_SOURCE_SECONDS):
            raise ValueError("Diagnostic audio window exceeds its memory budget")
        result = np.empty(last - first, dtype=np.float32)
        for begin in range(first, last, RATE):
            if started is not None:
                _check(started, cancelled)
            elif cancelled is not None and cancelled.is_set():
                raise InterruptedError("Source analysis cancelled")
            end = min(begin + RATE, last)
            positions = np.arange(begin, end, dtype=np.float64) * rate / RATE
            start = int(positions[0])
            stop = min(frames, math.ceil(positions[-1]) + 1)
            audio.seek(start)
            chunk = audio.read(stop - start, dtype="float32", always_2d=True).mean(
                axis=1
            )
            if not len(chunk) or not np.isfinite(chunk).all():
                raise ValueError("Nonfinite or incomplete diagnostic audio")
            result[begin - first : end - first] = np.interp(
                positions - start, np.arange(len(chunk)), chunk
            )
    return result


def _match_audio(
    path: Path,
    left: int,
    right: int,
    template: np.ndarray,
    started: float,
    cancelled: Event | None,
) -> tuple[int, float] | None:
    best = None
    for first in range(left, max(left, right - len(template) + 1), MATCH_STEP):
        _check(started, cancelled)
        last = min(right, first + MATCH_STEP + len(template) - 1)
        window = _audio(
            path, first=first, last=last, started=started, cancelled=cancelled
        )
        match = normalized_match(window, template)
        if match is not None and (best is None or match[1] > best[1]):
            best = first - left + match[0], match[1]
    return best


def _json(path: Path, limit: int = 65536) -> dict[str, Any]:
    with open_regular_binary(path) as source:
        payload = source.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Diagnostic metadata exceeds analysis budget")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Diagnostic metadata must be an object")
    return value


def _journal(
    path: Path,
    limit: int = 512 * 1024**2,
    *,
    started: float | None = None,
    cancelled: Event | None = None,
) -> Iterator[dict[str, Any]]:
    consumed = 0
    with open_regular_binary(path) as source:
        for count in range(2_000_001):
            if started is not None:
                _check(started, cancelled)
            line = source.readline(65537)
            if not line:
                return
            consumed += len(line)
            if len(line) > 65536 or consumed > limit or count == 2_000_000:
                raise ValueError("Diagnostic journal exceeds analysis budget")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("Diagnostic journal entry must be an object")
            yield value


def _events(
    path: Path, field: str, wanted: set[str], started: float, cancelled: Event | None
) -> list[dict]:
    result: list[dict] = []
    for event in _journal(path, started=started, cancelled=cancelled):
        if event.get(field) in wanted:
            if len(result) >= 20000:
                raise ValueError(
                    "Relevant diagnostic event count exceeds analysis budget"
                )
            result.append(event)
    return result


def _number(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
    ):
        raise ValueError("Invalid finite diagnostic number")
    return float(value)


def _capture_metadata(directory: Path) -> tuple[dict[str, Any], float]:
    metadata = _json(directory / "session.json")
    if (
        metadata.get("status") != "captured_unqualified"
        or metadata.get("errors")
        or metadata.get("mechanism") != "CoreAudio explicit process tap"
        or metadata.get("microphone") is not False
        or metadata.get("global_fallback") is not False
    ):
        raise ValueError("Process capture is incomplete or has an unexpected scope")
    rate = _number(metadata.get("sample_rate"))
    if not 8000 <= rate <= 192000:
        raise ValueError("Invalid capture rate")
    identity = re.fullmatch(r"rust-([1-9][0-9]*)(?:-[1-9][0-9]*)?", directory.name)
    if identity is None or metadata.get("target", {}).get("pid") != int(identity[1]):
        raise ValueError(
            "Captured process identity differs from its evidence directory"
        )
    return metadata, rate


def _capture_clock(
    directory: Path,
    *,
    started: float | None = None,
    cancelled: Event | None = None,
    audio_path: Path | None = None,
) -> tuple[float, float]:
    metadata, rate = _capture_metadata(directory)
    frames = 0
    previous_sample: float | None = None
    previous_host = float("-inf")
    offsets: list[float] = []
    minimum_offset, maximum_offset = float("inf"), float("-inf")
    callback_count = 0
    for event in _journal(
        directory / "audio.jsonl", started=started, cancelled=cancelled
    ):
        if event.get("kind") != "audio":
            continue
        count, start = event.get("frame_count"), event.get("file_frame_start")
        host, sample = (
            _number(event.get("monotonic_s")),
            _number(event.get("sample_time")),
        )
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or type(start) is not int
            or start != frames
        ):
            raise ValueError("Invalid capture callback timeline")
        if previous_sample is not None and (
            abs(sample - previous_sample) >= 0.5 or host <= previous_host
        ):
            raise ValueError("Discontinuous capture callback timeline")
        offset = host - frames / rate
        minimum_offset = min(minimum_offset, offset)
        maximum_offset = max(maximum_offset, offset)
        # Bound clock samples while still validating every callback, including the tail.
        if callback_count % 256 == 0:
            offsets.append(offset)
        callback_count += 1
        frames += count
        previous_sample, previous_host = sample + count, host
    if (
        not offsets
        or metadata.get("frames") != frames
        or type(metadata.get("peak")) not in (int, float)
        or not 0 < metadata["peak"] < float("inf")
    ):
        raise ValueError("No complete nonzero process capture")
    with (
        open_regular_binary(
            audio_path
            or diagnostic_audio_path(
                directory / "process.caf",
                cancelled=cancelled,
                deadline=started + 120 if started is not None else None,
            )
        ) as source,
        sf.SoundFile(source) as audio,
    ):
        if (
            audio.frames != frames
            or audio.samplerate != rate
            or audio.channels != metadata.get("channels")
        ):
            raise ValueError("Recorded PCM differs from the callback timeline")
    return float(np.median(offsets)), float((maximum_offset - minimum_offset) * 1000)


def _source_path(
    directory: Path,
    entry: dict,
    *,
    started: float | None = None,
    cancelled: Event | None = None,
) -> Path:
    if not isinstance(entry.get("audio"), str):
        raise ValueError("Invalid source evidence path")
    path: Path = directory / entry["audio"]
    if not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink():
        raise ValueError("Source evidence escapes its session")
    digest, total = hashlib.sha256(), 0
    with open_regular_binary(path) as source:
        while chunk := source.read(1024**2):
            if started is not None:
                _check(started, cancelled)
            total += len(chunk)
            if total > 64 * 1024**2:
                raise ValueError("Source evidence exceeds its read budget")
            digest.update(chunk)
    if digest.hexdigest() != entry.get("audio_sha256"):
        raise ValueError("Source evidence identity changed")
    return path


def _playback_key(event: dict) -> tuple[str | None, str | None]:
    run_id, playback_id = event.get("run_id"), event.get("playback_id")
    if any(
        value is not None and not isinstance(value, str)
        for value in (run_id, playback_id)
    ):
        raise ValueError("Invalid diagnostic playback identity")
    return run_id, playback_id


def _timestamp(value: object) -> float | None:
    try:
        return _number(value)
    except ValueError:
        return None


def _playback_lifetime(
    route: dict, dispatch: float, events: list[dict], runtime: list[dict]
) -> tuple[float | None, bool, str]:
    key = _playback_key(route)
    terminals = [
        event
        for event in events + runtime
        if _playback_key(event) == key
        and (
            event.get("stage") in {"playback-completion", "playback-outcome"}
            or event.get("operation") in {"rust-worker-finished", "cancel", "stop"}
        )
    ]
    ends = [_timestamp(event.get("occurred_at")) for event in terminals]
    valid_ends = [end for end in ends if end is not None and end >= dispatch]
    if len(valid_ends) != len(ends):
        return None, False, "ambiguous-playback-lifetime"
    boundaries = valid_ends + [
        timestamp
        for event in events + runtime
        if (timestamp := _timestamp(event.get("occurred_at"))) is not None
        and timestamp > dispatch
        and (
            (
                event.get("operation") == "prepared-source"
                and _playback_key(event) != key
            )
            or (
                event.get("stage") == "reading-control"
                and event.get("run_id") == route.get("run_id")
                and event.get("action") in {"pause", "stop-capture", "emergency-stop"}
            )
        )
    ]
    complete = (
        bool(valid_ends)
        and any(
            event.get("outcome") in {"complete", "completed"}
            or (
                event.get("stage") == "playback-completion"
                and event.get("outcome") is None
            )
            for event in terminals
        )
        and len(valid_ends) == len(ends)
        and not any(
            event.get("outcome") not in {None, "complete", "completed", "requested"}
            or event.get("operation") == "cancel"
            for event in terminals
        )
    )
    end = min(boundaries) if boundaries else None
    complete = complete and end == min(valid_ends)
    reason = "missing-playback-lifetime"
    if boundaries:
        reason = "complete" if complete else "partial-or-ambiguous-playback-lifetime"
    return end, complete, reason


def _source_entries(directory: Path) -> dict[tuple[str, str], dict]:
    entries = _json(directory / "sources.json", 1024**2).get("entries", [])
    if not isinstance(entries, list) or len(entries) > 200:
        raise ValueError("Source manifest exceeds analysis budget")
    sources: dict[tuple[str, str], dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("line_id"), str):
            raise ValueError("Invalid diagnostic source entry")
        digest = entry.get("audio_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Invalid source evidence digest")
        source_key = (entry["line_id"], digest)
        if source_key in sources and sources[source_key] != entry:
            raise ValueError("Ambiguous source evidence identity")
        sources[source_key] = entry
    return sources


def _runtime_playback_evidence(
    runtime: list[dict],
) -> tuple[
    dict[tuple, set[int]],
    dict[tuple, set[str]],
    dict[tuple, set[str]],
    dict[tuple, set[float | None]],
    dict[tuple, list[object]],
]:
    owners: dict[tuple, set[int]] = {}
    owner_tokens: dict[tuple, set[str]] = {}
    admitted: dict[tuple, set[str]] = {}
    source_leads: dict[tuple, set[float | None]] = {}
    admission_times: dict[tuple, list[object]] = {}
    for event in runtime:
        if (
            event.get("operation") == "prepared-source"
            and isinstance(event.get("playback_id"), str)
            and isinstance(event.get("source_sha256"), str)
        ):
            admitted.setdefault(_playback_key(event), set()).add(event["source_sha256"])
            admission_times.setdefault(
                (_playback_key(event), event["source_sha256"]), []
            ).append(event.get("occurred_at"))
            value = event.get("source_audio_lead_ms")
            lead = (
                float(value)
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
                else None
            )
            source_leads.setdefault(
                (_playback_key(event), event["source_sha256"]), set()
            ).add(lead)
        if (
            event.get("operation") == "rust-worker-finished"
            and event.get("playback_id")
            and type(event.get("worker_pid")) is int
        ):
            owners.setdefault(_playback_key(event), set()).add(event["worker_pid"])
            if isinstance(event.get("worker_id"), str):
                owner_tokens.setdefault(_playback_key(event), set()).add(
                    event["worker_id"]
                )
    return owners, owner_tokens, admitted, source_leads, admission_times


def _recording_evidence(directory: Path) -> tuple[dict, list, dict[str, dict]]:
    session_path = directory / "session.json"
    recording = (
        _json(session_path).get("recording", {}) if session_path.exists() else {}
    )
    if not isinstance(recording, dict):
        raise ValueError("Invalid recording evidence")
    gaps = recording.get("acquisition_gaps", [])
    if not isinstance(gaps, list) or len(gaps) > 16:
        raise ValueError("Acquisition gap count exceeds analysis budget")
    recorded_captures = recording.get("captures", [])
    if not isinstance(recorded_captures, list) or len(recorded_captures) > 10:
        raise ValueError("Capture count exceeds analysis budget")
    capture_records = {
        value["directory"]: value
        for value in recorded_captures
        if isinstance(value, dict) and isinstance(value.get("directory"), str)
    }
    return recording, gaps, capture_records


def _capture_audio_evidence(
    directory: Path,
    capture: Path,
    record: dict,
    started: float,
    cancelled: Event | None,
) -> _CaptureAudio:
    if capture.is_symlink() or not capture.resolve().is_relative_to(
        directory.resolve()
    ):
        raise ValueError("Process capture escapes its session")
    if record.get("acquisition_failure"):
        raise ValueError("Process capture has an acquisition failure")
    audio_path = diagnostic_audio_path(
        capture / "process.caf", cancelled=cancelled, deadline=started + 120
    )
    offset, residual = _capture_clock(
        capture, started=started, cancelled=cancelled, audio_path=audio_path
    )
    with (
        open_regular_binary(audio_path) as source_file,
        sf.SoundFile(source_file) as captured_audio,
    ):
        if captured_audio.frames > MAX_CAPTURE_SECONDS * captured_audio.samplerate:
            raise ValueError("Unsupported or oversized diagnostic audio")
        audio_frames = round(captured_audio.frames * RATE / captured_audio.samplerate)
    capture_pid = int(capture.name.split("-")[1])
    return _CaptureAudio(audio_path, offset, residual, audio_frames, capture_pid)


def _route_capture_evidence(
    route: dict,
    sources: dict[tuple[str, str], dict],
    dispatches: dict[tuple, float],
    owners: dict[tuple, set[int]],
    owner_tokens: dict[tuple, set[str]],
    admitted: dict[tuple, set[str]],
    source_leads: dict[tuple, set[float | None]],
    record: dict,
    capture_paths: list[Path],
    audio: _CaptureAudio,
) -> _RouteCapture | None:
    _audio_path, offset, _residual, audio_frames, capture_pid = audio
    playback_id = route.get("playback_id")
    line_id = route.get("line_id")
    if not isinstance(playback_id, str) or not isinstance(line_id, str):
        return None
    key = _playback_key(route)
    digests = admitted.get(key, set())
    entry = sources.get((line_id, next(iter(digests)))) if len(digests) == 1 else None
    dispatch = dispatches.get(key)
    worker_pids = owners.get(key, set())
    tokens = owner_tokens.get(key, set())
    captured_token = record.get("worker_identity", {}) or {}
    identity_verified = bool(worker_pids) and (
        tokens == {captured_token.get("worker_id")}
        if tokens
        else sum(path.name.split("-")[1] == str(capture_pid) for path in capture_paths)
        == 1
    )
    if (
        entry is None
        or dispatch is None
        or (worker_pids and capture_pid not in worker_pids)
        or (tokens and not identity_verified)
    ):
        return None
    leads = source_leads.get((key, entry["audio_sha256"]), set())
    source_lead = next(iter(leads)) if len(leads) == 1 else None
    expected_start = dispatch + (source_lead / 1000 if source_lead is not None else 0)
    if not offset - 0.25 <= expected_start < offset + audio_frames / RATE:
        return None
    return _RouteCapture(entry, dispatch, source_lead, identity_verified)


def _correlate_route_capture(
    directory: Path,
    capture: Path,
    audio: _CaptureAudio,
    evidence: _RouteCapture,
    route: dict,
    end: float,
    lifetime_complete: bool,
    lifetime_reason: str,
    phase_dispatches: list[object],
    admissions: list[object],
    started: float,
    cancelled: Event | None,
) -> dict[str, object] | None:
    audio_path, offset, residual, audio_frames, _capture_pid = audio
    entry, dispatch, source_lead, identity_verified = evidence
    source = _audio(
        _source_path(directory, entry, started=started, cancelled=cancelled),
        seconds=MAX_SOURCE_SECONDS,
        cancelled=cancelled,
        started=started,
    )
    left = max(0, math.ceil((dispatch - offset) * RATE))
    right = min(
        audio_frames,
        math.floor((end - offset) * RATE),
    )
    if right <= left:
        return None
    full_source = lifetime_complete and right - left >= len(source)
    template_source = (
        source
        if full_source
        else source[: min(len(source), RATE // 2, max(0, (right - left) // 2))]
    )
    match = _match_audio(audio_path, left, right, template_source, started, cancelled)
    if match is None or (not full_source and match[1] < 0.9):
        return None
    position, score = match
    onset = offset + (left + position) / RATE
    dispatch_to_admission = admission_to_onset = None
    if len(phase_dispatches) == len(admissions) == 1:
        try:
            phase_dispatch = _number(phase_dispatches[0])
            admission = _number(admissions[0])
        except ValueError:
            pass
        else:
            if phase_dispatch <= admission <= onset:
                dispatch_to_admission = (admission - phase_dispatch) * 1000
                admission_to_onset = (onset - admission) * 1000
    windows = []
    for frame in range(0, len(template_source) - RATE // 10 + 1, RATE // 5):
        _check(started, cancelled)
        template = source[frame : frame + RATE // 10]
        if float(np.sqrt(np.mean(template**2))) < 0.004:
            return None
        predicted = left + position + frame
        lo, hi = (
            max(left, predicted - RATE // 5),
            min(right, predicted + RATE * 3 // 10),
        )
        local = normalized_match(
            _audio(
                audio_path,
                first=lo,
                last=hi,
                started=started,
                cancelled=cancelled,
            ),
            template,
        )
        if local is not None and local[1] >= 0.9:
            windows.append(
                {
                    "source_seconds": frame / RATE,
                    "offset_ms": (lo + local[0] - frame - left - position)
                    / RATE
                    * 1000,
                    "correlation": local[1],
                }
            )
    shifts = [w["offset_ms"] for w in windows]
    qualified = (
        full_source
        and len(phase_dispatches) == 1
        and score >= 0.9
        and residual < 0.1
        and identity_verified
        and bool(windows)
    )
    return {
        "line_id": route["line_id"],
        "run_id": route.get("run_id"),
        "playback_id": route.get("playback_id"),
        "capture": capture.name,
        "source_sha256": entry["audio_sha256"],
        "correlation": score,
        "playback_lifetime": lifetime_reason,
        "matched_source_seconds": len(template_source) / RATE,
        "candidate_onset_monotonic": onset,
        "candidate_onset_provenance": "source-frame-zero-alignment",
        "candidate_dispatch_to_onset_ms": (onset - dispatch) * 1000,
        "candidate_dispatch_to_prepared_source_admission_ms": dispatch_to_admission,
        "candidate_prepared_source_admission_to_captured_onset_ms": admission_to_onset,
        "source_audio_lead_ms": source_lead,
        "candidate_latency_after_source_wait_ms": (onset - dispatch) * 1000
        - source_lead
        if source_lead is not None
        else None,
        "window_shift_range_ms": max(shifts) - min(shifts) if shifts else None,
        "energetic_windows": windows,
        "worker_identity_verified": identity_verified,
        "status": "correlated" if qualified else "review-required",
    }


def _playback_dispatches(
    events: list[dict],
) -> tuple[dict[tuple, list[object]], dict[tuple, float]]:
    dispatch_times: dict[tuple, list[object]] = {}
    for event in events:
        if event.get("stage") == "playback-start" and isinstance(
            event.get("playback_id"), str
        ):
            dispatch_times.setdefault(_playback_key(event), []).append(
                event.get("occurred_at")
            )
    dispatches = {
        _playback_key(e): e["occurred_at"]
        for e in events
        if e.get("stage") == "playback-start"
        and e.get("playback_id")
        and _timestamp(e.get("occurred_at")) is not None
        and math.isfinite(e["occurred_at"])
    }
    return dispatch_times, dispatches


def _runtime_events(
    directory: Path, started: float, cancelled: Event | None
) -> list[dict]:
    return (
        _events(
            directory / "runtime.jsonl",
            "operation",
            {
                "prepared-source",
                "rust-worker-finished",
                "cancel",
                "stop",
            },
            started,
            cancelled,
        )
        if (directory / "runtime.jsonl").exists()
        else []
    )


def _capture_failure_report(
    capture: Path, record: dict, error: Exception
) -> dict[str, object]:
    try:
        native = _json(capture / "session.json")
    except OSError, ValueError:
        native = {"status": "unavailable"}
    return {
        "capture": capture.name,
        "status": "unavailable",
        "error_type": type(error).__name__,
        "error": str(error),
        "native_status": native.get("status"),
        "native_errors": native.get("errors"),
        "target": native.get("target"),
        "acquisition_failure": record.get("acquisition_failure"),
    }


def _generated_routes(events: list[dict]) -> list[dict]:
    routes = [
        e
        for e in events
        if e.get("stage") == "route-decision"
        and e.get("effective_source") == "generated"
    ]
    if len(routes) > 200:
        raise ValueError("Generated route count exceeds analysis budget")
    return routes


def _capture_paths(directory: Path) -> list[Path]:
    capture_paths = sorted(path for path in directory.glob("rust-*") if path.is_dir())
    if len(capture_paths) > 8:
        raise ValueError("Process capture count exceeds analysis budget")
    return capture_paths


def analyze_session(
    directory: Path, *, cancelled: Event | None = None, interrupted: bool = False
) -> dict[str, object]:
    """A match qualifies digital source evidence, never physical speaker quality."""
    started = monotonic()
    _check(started, cancelled)
    events = _events(
        directory / "timeline.jsonl",
        "stage",
        {
            "route-decision",
            "playback-start",
            "playback-completion",
            "playback-outcome",
            "reading-control",
        },
        started,
        cancelled,
    )
    sources = _source_entries(directory)
    routes = _generated_routes(events)
    dispatch_times, dispatches = _playback_dispatches(events)
    runtime = _runtime_events(directory, started, cancelled)
    owners, owner_tokens, admitted, source_leads, admission_times = (
        _runtime_playback_evidence(runtime)
    )
    recording, gaps, capture_records = _recording_evidence(directory)
    results, captures, attempted, matched = [], [], set(), set()
    unmatched_reasons = {}
    capture_paths = _capture_paths(directory)
    analysis_interruption: str | None = None
    for capture in capture_paths:
        try:
            _check(started, cancelled)
            record = capture_records.get(capture.name, {})
            audio = _capture_audio_evidence(
                directory, capture, record, started, cancelled
            )
            _audio_path, offset, residual, audio_frames, _capture_pid = audio
            captures.append(
                {
                    "capture": capture.name,
                    "status": "captured-unqualified",
                    "clock_residual_ms": residual,
                    "monotonic_start": offset,
                    "duration_seconds": audio_frames / RATE,
                }
            )
            for index, route in enumerate(routes):
                _check(started, cancelled)
                evidence = _route_capture_evidence(
                    route,
                    sources,
                    dispatches,
                    owners,
                    owner_tokens,
                    admitted,
                    source_leads,
                    record,
                    capture_paths,
                    audio,
                )
                if evidence is None:
                    continue
                entry, dispatch, _source_lead, _identity_verified = evidence
                key = _playback_key(route)
                attempted.add(index)
                end, lifetime_complete, lifetime_reason = _playback_lifetime(
                    route, dispatch, events, runtime
                )
                unmatched_reasons[index] = lifetime_reason
                if end is None:
                    continue
                result = _correlate_route_capture(
                    directory,
                    capture,
                    audio,
                    evidence,
                    route,
                    end,
                    lifetime_complete,
                    lifetime_reason,
                    dispatch_times.get(key, []),
                    admission_times.get((key, entry["audio_sha256"]), []),
                    started,
                    cancelled,
                )
                if result is not None:
                    if result["status"] == "correlated":
                        matched.add(index)
                    results.append(result)
        except (InterruptedError, TimeoutError) as error:
            analysis_interruption = type(error).__name__
            break
        except Exception as error:
            captures.append(
                _capture_failure_report(
                    capture, capture_records.get(capture.name, {}), error
                )
            )
    unmatched = [
        {
            "line_id": route.get("line_id"),
            "playback_id": route.get("playback_id"),
            "status": "review-required" if index in attempted else "unmatched",
            "reason": unmatched_reasons.get(index, "missing-source-or-worker-evidence"),
        }
        for index, route in enumerate(routes)
        if index not in matched
    ]
    report: dict[str, object] = {
        "schema_version": 1,
        "qualification": "digital-source-correlated"
        if results
        and not unmatched
        and not interrupted
        and not analysis_interruption
        and not gaps
        and not recording.get("error_type")
        and all(capture["status"] != "unavailable" for capture in captures)
        else "unqualified",
        "analysis_rate": RATE,
        "status": "partial"
        if interrupted
        else "cancelled"
        if analysis_interruption == "InterruptedError"
        else "budget-exhausted"
        if analysis_interruption
        else "complete",
        "captures": captures,
        "coverage": {
            "generated_routes": len(routes),
            "attempted_routes": len(attempted),
            "correlated_routes": len(matched),
        },
        "clips": results,
        "acquisition_gaps": gaps,
        "unmatched_routes": unmatched,
        "elapsed_seconds": monotonic() - started,
        "limits": {
            "source_seconds": MAX_SOURCE_SECONDS,
            "capture_seconds": MAX_CAPTURE_SECONDS,
            "audio_window_seconds": 30 + MAX_SOURCE_SECONDS,
            "routes": 200,
            "captures": 8,
            "seconds": 120,
        },
        "limitations": [
            "Digital process output only; no acoustic or DAC measurement.",
            "Linear-resampled correlation is diagnostic evidence, not an automatic audio-quality verdict.",
            "Source silence, announcements, original-source waits and unmatched/partial recordings cannot qualify continuity.",
            "Correlated windows can reveal timing shifts; inspect the actual recording and window coverage.",
        ],
    }
    if interrupted:
        report["scope"] = "interrupted"
        report["analysis_status"] = (
            "cancelled"
            if analysis_interruption == "InterruptedError"
            else "budget-exhausted"
            if analysis_interruption
            else "complete"
        )
    output = "recovery-analysis.json" if interrupted else "analysis.json"
    (directory / output).write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n"
    )
    return report
