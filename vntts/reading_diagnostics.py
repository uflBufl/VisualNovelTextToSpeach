"""Repeatable, bounded Reading diagnostics shared by UI and support export."""

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING
from uuid import uuid4

from vntts_artifacts.atomic_io import atomic_output_path

from vntts.diagnostic_recording import NativeDiagnosticRecording
from vntts.path_safety import open_regular_binary, safe_relative_path
from vntts.support_timeline_acquisition import TimelineAcquisition
from vntts.window_capture import WindowCaptureTarget

if TYPE_CHECKING:
    from PIL.Image import Image

    from vntts.support import GenerationTimelineLog

_active: "ReadingDiagnosticSession | None" = None


def record_diagnostic_event(category: str, operation: str, **details: object) -> None:
    session = _active
    if session is None:
        return
    event = {"category": category, "operation": operation, "occurred_at": monotonic()}
    for key, value in details.items():
        if key in {"callback_timing", "dac_timing"} and isinstance(value, dict):
            event.update({f"{key}_{name}": number for name, number in value.items()})
        else:
            event[key] = value
    try:
        session.runtime.record(event)
    except Exception:
        # Observability cannot cancel playback or generation.
        pass


def arm_audio_worker_recording(
    identity: dict[str, object], interrupted: Callable[[], bool]
) -> bool:
    session = _active
    if session is None or session.recording is None or session.phase != "recording":
        return True
    started = monotonic()
    try:
        ready, reason = session.recording.wait_for_worker(identity, interrupted)
    except Exception as error:
        ready, reason = False, "capture-failed"
        try:
            session.recording._fail_capture("rust-arming", error)
        except Exception:
            # Recorder diagnostics must not interrupt the audio owner.
            pass
    record_diagnostic_event(
        "audio",
        "rust-recording-arm",
        **identity,
        ready=ready,
        reason=reason,
        wait_ms=round((monotonic() - started) * 1000, 3),
    )
    return reason != "cancelled" or not interrupted()


def retain_diagnostic_source(
    line_id: str, path: Path | None, digest: str | None
) -> None:
    session = _active
    if session is None or session.recording is None or path is None or digest is None:
        return
    if re.fullmatch(r"[0-9a-f]{64}", digest) and len(session.sources) < 200:
        session.sources[(line_id, digest)] = path


def retain_visual_wait_frames(
    routed: object, latest: object, *, run_id: str, generation: int, frame_version: int
) -> None:
    session = _active
    if session is None or session.recording is None:
        return
    from vntts.dialog_capture import CapturedDialogFrame

    if not isinstance(routed, CapturedDialogFrame) or not isinstance(
        latest, CapturedDialogFrame
    ):
        return
    target = session.recording.target
    frames = (routed, latest)
    for frame in frames:
        provenance = frame.provenance
        if (
            provenance is None
            or not provenance.success
            or provenance.run_id != run_id
            or provenance.window_id != target.get("window_id")
            or provenance.owner_pid != target.get("pid")
            or frame.image.width * frame.image.height > 2_000_000
        ):
            return
    try:
        with session.visual_wait_lock:
            key = (run_id, generation)
            if session.phase != "recording" or key in session.visual_wait_frames:
                return
            pixels = sum(frame.image.width * frame.image.height for frame in frames)
            if (
                len(session.visual_wait_frames) >= 12
                or session.visual_wait_pixels + pixels > 8_000_000
            ):
                session.visual_wait_dropped += 1
                return
            images = tuple(frame.image.convert("RGB") for frame in frames)
            metadata: dict[str, object] = {
                "run_id": run_id,
                "generation": generation,
                "frame_version": frame_version,
                "observed_monotonic": monotonic(),
                "frames": [
                    {
                        "capture": frame.provenance.as_dict()
                        if frame.provenance
                        else None,
                        "ocr_regions": asdict(frame.ocr_regions)
                        if frame.ocr_regions
                        else None,
                    }
                    for frame in frames
                ],
            }
            session.visual_wait_frames[key] = (images, metadata)
            session.visual_wait_pixels += pixels
    except Exception:
        # Optional evidence must never interfere with continuation or Stop.
        pass


def _write_json(path: Path, document: dict[str, object]) -> None:
    with atomic_output_path(path) as temporary:
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def selected_recording_target(
    target: object,
) -> dict[str, object]:
    from vntts.diagnostic_recording import process_executable

    if not isinstance(target, WindowCaptureTarget):
        raise RuntimeError("Select a game window in capture settings before recording")
    window = target.last_window
    if window is not None:
        current = target.backend.get_window(window.handle)
        if current is None or current.process_id != window.process_id:
            raise RuntimeError(
                "Selected game window changed; recalibrate before recording"
            )
        window = current
    else:
        matches = [
            w
            for w in target.list_windows()
            if w.title.casefold() == target.window_title.casefold()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Recording needs one exact game window; select it in capture settings"
            )
        window = matches[0]
    if window.minimized:
        raise RuntimeError("Restore the selected game window before recording")
    bounds = target.backend.get_client_geometry(window.handle)
    return {
        "pid": window.process_id,
        "window_id": window.handle,
        "executable": process_executable(window.process_id),
        "title": window.title,
        "bounds": bounds.as_monitor(),
    }


def _diagnostic_export_entries(directory: Path) -> list[tuple[str, Path]]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Diagnostic session directory is unsafe")
    entries: list[tuple[str, Path]] = []
    total = 0
    for index, path in enumerate(directory.rglob("*")):
        if index >= 2048:
            raise ValueError("Diagnostic export exceeds its bounded file budget")
        if path.is_symlink():
            raise ValueError("Diagnostic evidence contains a symlink")
        if not path.is_file():
            continue
        total += path.stat().st_size
        if total > 2 * 1024**3 or len(entries) >= 1024:
            raise ValueError("Diagnostic export exceeds its bounded evidence budget")
        filename = "reading-session/" + path.relative_to(directory).as_posix()
        safe_relative_path(filename, "Diagnostic archive entry")
        entries.append((filename, path))
    return sorted(entries)


def _journal_has_cutoff(path: Path, identity: object) -> bool:
    if path.is_symlink():
        return False
    with open_regular_binary(path) as source:
        source.seek(0, 2)
        source.seek(max(0, source.tell() - 8192))
        tail = source.read(8192)
    if not tail.endswith(b"\n"):
        return False
    final = json.loads(tail.splitlines()[-1])
    return bool(
        isinstance(final, dict)
        and final.get("kind") == "stop"
        and final.get("status") == "cutoff-written"
        and final.get("acquisition_id") == identity
    )


@dataclass(frozen=True)
class SavedDiagnosticSession:
    directory: Path
    session_id: str
    report: dict[str, object]

    def status(self) -> dict[str, object]:
        return dict(self.report)

    def export_entries(self) -> list[tuple[str, Path]]:
        return _diagnostic_export_entries(self.directory)


@dataclass(frozen=True)
class DiagnosticSessionEntry:
    directory: Path
    session_id: str
    report: dict[str, object] | None
    size_bytes: int | None
    saved: SavedDiagnosticSession | None
    error: str = ""
    file_counts: dict[str, int] | None = None


def _session_directory(root: Path, session_id: str) -> Path:
    if (
        root.is_symlink()
        or not root.is_dir()
        or re.fullmatch(r"[0-9a-f]{32}", session_id) is None
    ):
        raise ValueError("Diagnostic session directory is unsafe")
    directory = root / session_id
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Diagnostic session directory is unsafe")
    return directory


def _session_report(directory: Path) -> dict[str, object]:
    metadata = directory / "session.json"
    if metadata.is_symlink():
        raise ValueError("Diagnostic metadata contains a symlink")
    with open_regular_binary(metadata) as source:
        payload = source.read(65537)
    if len(payload) > 65536:
        raise ValueError("Diagnostic metadata exceeds its size limit")
    report = json.loads(payload)
    if (
        not isinstance(report, dict)
        or report.get("schema") != "vntts.reading-diagnostics"
        or type(report.get("version")) is not int
        or report["version"] != 1
        or report.get("session_id") != directory.name
        or not isinstance(report.get("started_at"), str)
    ):
        raise ValueError("Diagnostic metadata is invalid")
    if datetime.fromisoformat(report["started_at"]).tzinfo is None:
        raise ValueError("Diagnostic start date has no timezone")
    return report


def _finished_session(
    directory: Path, report: dict[str, object]
) -> SavedDiagnosticSession:
    if report.get("status") != "finished":
        raise ValueError("Diagnostic session is not finished")
    if (
        _active is not None
        and not _active.finished.is_set()
        and _active.directory.resolve() == directory.resolve()
    ):
        raise ValueError("Diagnostic session is still active or finalizing")
    for name in ("timeline", "runtime"):
        journal = report.get(name)
        if (
            not isinstance(journal, dict)
            or not isinstance(journal.get("acquisition_id"), str)
            or not _journal_has_cutoff(
                directory / (name + ".jsonl"), journal["acquisition_id"]
            )
        ):
            raise ValueError("Diagnostic journals are incomplete")
    return SavedDiagnosticSession(directory, directory.name, report)


def load_saved_diagnostic_session(
    root: Path, session_id: str
) -> SavedDiagnosticSession | None:
    try:
        directory = _session_directory(root, session_id)
        return _finished_session(directory, _session_report(directory))
    except OSError, ValueError, TypeError, IndexError:
        return None


def _session_inventory(directory: Path) -> tuple[int, dict[str, int]]:
    if not stat.S_ISDIR(directory.stat(follow_symlinks=False).st_mode):
        raise ValueError("Diagnostic session directory is unsafe")
    total = 0
    file_counts: dict[str, int] = {}

    def fail(error: OSError) -> None:
        raise error

    for parent, directories, files in os.walk(directory, onerror=fail):
        for name in directories + files:
            path = Path(parent) / name
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("Diagnostic evidence contains a symlink")
            if stat.S_ISREG(metadata.st_mode):
                total += metadata.st_size
                suffix = path.suffix.lower()
                file_counts[suffix] = file_counts.get(suffix, 0) + 1
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("Diagnostic evidence contains an unsafe file")
    return total, file_counts


def _history_entry(directory: Path) -> DiagnosticSessionEntry:
    report = None
    saved = None
    size = None
    file_counts = None
    errors = []
    try:
        _session_directory(directory.parent, directory.name)
        size, file_counts = _session_inventory(directory)
    except (OSError, ValueError) as error:
        errors.append(str(error))
    try:
        _session_directory(directory.parent, directory.name)
        report = _session_report(directory)
        if not errors:
            saved = _finished_session(directory, report)
    except (OSError, ValueError, TypeError, IndexError) as error:
        errors.append(str(error))
    return DiagnosticSessionEntry(
        directory, directory.name, report, size, saved, "; ".join(errors), file_counts
    )


def _session_started(entry: DiagnosticSessionEntry) -> datetime:
    if entry.report is not None:
        return datetime.fromisoformat(str(entry.report["started_at"]))
    return datetime.min.replace(tzinfo=timezone.utc)


def list_diagnostic_sessions(root: Path) -> list[DiagnosticSessionEntry]:
    try:
        metadata = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Diagnostic sessions root is unsafe")
    entries = [
        _history_entry(directory)
        for directory in root.iterdir()
        if re.fullmatch(r"[0-9a-f]{32}", directory.name)
    ]
    return sorted(entries, key=_session_started, reverse=True)


def latest_finished_diagnostic_session(root: Path) -> SavedDiagnosticSession | None:
    if root.is_symlink() or not root.is_dir():
        return None
    latest: SavedDiagnosticSession | None = None
    try:
        for directory in root.iterdir():
            saved = load_saved_diagnostic_session(root, directory.name)
            if saved is not None and (
                latest is None
                or datetime.fromisoformat(str(saved.report["started_at"]))
                > datetime.fromisoformat(str(latest.report["started_at"]))
            ):
                latest = saved
    except OSError:
        return None
    return latest


def delete_saved_diagnostic_session(root: Path, session_id: str) -> None:
    directory = _session_directory(root, session_id)
    _finished_session(directory, _session_report(directory))
    # Inspect all evidence before rmtree; no symlinks or special files are owned data.
    _session_inventory(directory)
    shutil.rmtree(directory)


class ReadingDiagnosticSession:
    def __init__(
        self,
        root: Path,
        timelines: "GenerationTimelineLog",
        *,
        seconds: int = 600,
        target: dict[str, object] | None = None,
    ) -> None:
        global _active
        if not 1 <= seconds <= 600:
            raise ValueError(
                "Diagnostic session duration must be between 1 and 600 seconds"
            )
        if _active is not None:
            raise RuntimeError("A diagnostic session is already active")
        root.mkdir(parents=True, exist_ok=True)
        self.session_id = uuid4().hex
        self.directory = root / self.session_id
        self.directory.mkdir(parents=True)
        self.timelines, self.seconds = timelines, seconds
        self.sources: dict[tuple[str, str], Path] = {}
        self.visual_wait_lock = Lock()
        self.visual_wait_frames: dict[
            tuple[str, int], tuple[tuple[Image, ...], dict[str, object]]
        ] = {}
        self.visual_wait_pixels = 0
        self.visual_wait_dropped = 0
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started_monotonic = monotonic()
        self.stop_requested = Event()
        self.finished = Event()
        self.analysis_cancelled = Event()
        self.phase = "recording"
        self.failure: str | None = None
        self.recording: NativeDiagnosticRecording | None = None
        self.analysis_status = "not-requested"
        self.timeline_status: dict[str, object] = {}
        self.runtime = TimelineAcquisition(self.directory / "runtime.jsonl")
        try:
            timelines.start_acquisition(self.directory / "timeline.jsonl")
            acquisition = timelines.acquisition
            assert acquisition is not None
            self.timeline = acquisition
        except Exception:
            self.runtime.stop()
            raise
        if target is not None:
            from vntts.rust_audio import active_audio_workers

            self.recording = NativeDiagnosticRecording(
                self.directory,
                target,
                seconds,
                active_audio_workers,
                root.parent / "diagnostic-tools",
                deadline=self.started_monotonic + self.seconds,
            )
        try:
            self._persist()
            self.thread = Thread(
                target=self._run, name="reading-diagnostics", daemon=True
            )
            _active = self
            self.thread.start()
        except Exception:
            _active = None
            timelines.stop_acquisition()
            self.runtime.stop()
            raise

    def _run_recording(self) -> None:
        try:
            assert self.recording is not None
            self.recording.run()
        except Exception as error:
            self.failure = type(error).__name__

    def _run(self) -> None:
        global _active
        recording_thread = None
        try:
            if self.recording is not None:
                recording_thread = Thread(
                    target=self._run_recording, name="reading-media", daemon=True
                )
                recording_thread.start()
            self.stop_requested.wait(
                max(0, self.started_monotonic + self.seconds - monotonic())
            )
        except Exception as error:
            self.failure = type(error).__name__
            self.stop_requested.wait(
                max(0, self.started_monotonic + self.seconds - monotonic())
            )
        finally:
            self.phase = "finalizing"
            # Close admission on both journals before waiting for any writer or recorder.
            self.timeline.stop(timeout=0)
            self.runtime.stop(timeout=0)
            if self.recording is not None:
                self.recording.cancelled.set()
            self.timeline_status = self.timelines.stop_acquisition()
            self.runtime.stop()
            try:
                if self.recording is not None:
                    if recording_thread is not None and recording_thread.is_alive():
                        # Native teardown shares one budget across all owned processes.
                        # Retain ownership until release; Stop's caller has its own bound.
                        recording_thread.join()
                    self._retain_visual_wait_frames()
                    self._retain_sources()
                    self.analysis_status = "running"
                    self._persist()
                    from vntts.reading_diagnostics_analysis import analyze_session

                    analyze_session(self.directory, cancelled=self.analysis_cancelled)
                    self.analysis_status = "complete"
            except Exception as error:
                self.analysis_status = "unavailable"
                self.failure = type(error).__name__
            self.phase = "finished"
            try:
                self._persist()
            except OSError as error:
                self.failure = type(error).__name__
            finally:
                if _active is self:
                    _active = None
                self.finished.set()

    def stop(self, timeout: float = 0) -> None:
        self.stop_requested.set()
        if self.recording is not None:
            self.recording.cancelled.set()
        if timeout:
            self.analysis_cancelled.set()
            self.thread.join(max(0, min(timeout, 15)))

    def status(self) -> dict[str, object]:
        return {
            "schema": "vntts.reading-diagnostics",
            "version": 1,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "started_monotonic": self.started_monotonic,
            "duration_limit_seconds": self.seconds,
            "status": "stopping"
            if self.stop_requested.is_set() and self.phase == "recording"
            else self.phase,
            "media_status": self.recording.status if self.recording else "disabled",
            "target": self.recording.target if self.recording else None,
            "media_error": self.recording.error_message if self.recording else None,
            "analysis_status": self.analysis_status,
            "visual_wait_pairs": len(self.visual_wait_frames),
            "visual_wait_dropped": self.visual_wait_dropped,
            "error_type": self.failure,
            "timeline": self.timeline.status(),
            "runtime": self.runtime.status(),
            "privacy": "Media and source WAVs included only when recording was selected. Logs exclude recognized text.",
        }

    def _persist(self) -> None:
        report = self.status()
        if self.recording:
            report["recording"] = self.recording.report()
        _write_json(self.directory / "session.json", report)

    def _retain_sources(self) -> None:
        sources: list[dict[str, object]] = []
        directory = self.directory / "sources"
        directory.mkdir(exist_ok=True)
        total = 0
        for (line_id, digest), audio in list(self.sources.items()):
            with open_regular_binary(audio) as source:
                data = source.read(64 * 1024**2 + 1)
            total += len(data)
            if len(data) > 64 * 1024**2 or total > 256 * 1024**2:
                raise ValueError("Source WAV evidence exceeds diagnostic budget")
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("Source WAV changed since playback admission")
            output = directory / (digest + ".wav")
            if not output.exists():
                output.write_bytes(data)
            sources.append(
                {
                    "line_id": line_id,
                    "audio_sha256": digest,
                    "audio": str(output.relative_to(self.directory)),
                }
            )
        _write_json(self.directory / "sources.json", {"entries": sources})

    def _retain_visual_wait_frames(self) -> None:
        with self.visual_wait_lock:
            snapshots = tuple(self.visual_wait_frames.values())
        if not snapshots:
            return
        directory = self.directory / "visual-waits"
        directory.mkdir(exist_ok=True)
        entries = []
        for index, (images, metadata) in enumerate(snapshots):
            paths = []
            for role, image in zip(("routed", "latest"), images, strict=True):
                path = directory / f"{index:02d}-{role}.png"
                image.save(path)
                paths.append(path.name)
            entries.append({**metadata, "images": paths})
        _write_json(
            directory / "frames.json",
            {"entries": entries, "dropped": self.visual_wait_dropped},
        )

    def export_entries(self) -> list[tuple[str, Path]]:
        if not self.finished.is_set():
            raise RuntimeError(
                "Stop the diagnostic session and wait for finalization before exporting"
            )
        if self.runtime.status().get("writer_running") or self.timeline.status().get(
            "writer_running"
        ):
            raise RuntimeError("Diagnostic journal is still finalizing")
        self._persist()
        return _diagnostic_export_entries(self.directory)
