"""Capture checksum-bound real OCR frames for deterministic live replay."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NotRequired, Protocol, TypedDict, runtime_checkable

from PIL import Image

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.cli import cli_error, cli_messages
from vntts.dialog_capture import (
    capture_live_frame,
    detect_standalone_ellipsis_frame,
    ellipsis_speaker_hint,
    fingerprint_dialog_frame,
    get_screenshot_directory,
    is_standalone_ellipsis_text,
    recognize_screenshot_result,
)
from vntts.ocr_corrections import OCRCorrectionStore
from vntts.settings import load_app_settings
from vntts.window_capture import WindowCaptureTarget

LIVE_REPLAY_CAPTURE_VERSION = 1

PathInput = str | os.PathLike[str]


class StoryLine(Protocol):
    line_id: str | None
    chapter: str
    speaker: str
    text: str
    source_audio_status: str
    source_audio_id: str | None
    source_audio_duration_seconds: float | None


class StoryResolver(Protocol):
    @property
    def dialogue(self) -> Sequence[StoryLine]: ...

    @property
    def by_chapter(self) -> Mapping[str, Sequence[StoryLine]]: ...

    def resolve_exact_with_result(
        self, character: str, text: str
    ) -> tuple[StoryLine | None, str]: ...


@runtime_checkable
class ChapterBoundStoryResolver(Protocol):
    def resolve_exact_among(
        self, character: str, text: str, line_ids: Sequence[str]
    ) -> tuple[StoryLine | None, str]: ...


@runtime_checkable
class CapturedImageFrame(Protocol):
    image: Image.Image


class FrameSpecification(TypedDict):
    path: str
    sha256: str


class DialogueItem(TypedDict):
    observed_character: str
    observed_text: str
    frames: list[FrameSpecification]
    group_identity: NotRequired[str]
    story_line: NotRequired[StoryLine | None]
    story_match: NotRequired[str]
    boundary_reason: NotRequired[str]


class ObservationRecord(TypedDict):
    observation_index: int
    frame: FrameSpecification
    status: str
    observed_character: str | None
    observed_text: str | None
    story_line_id: str | None
    story_match: str


class CorpusRecord(TypedDict):
    frames: list[FrameSpecification]
    character: str
    text: str
    line_id: str | None
    source_audio_status: str
    source_audio_id: NotRequired[str | None]
    source_audio_duration_seconds: NotRequired[float | None]
    source_audio_completeness: NotRequired[str]
    expected_source: str | None
    capture_boundary: str
    story_match: str


@dataclass(frozen=True)
class CapturedReplayResult:
    directory: Path
    corpus: Path
    report: Path
    observation_ledger: Path
    dialogue_count: int
    frame_count: int
    boundary_review_count: int


class LiveReplayCaptureError(RuntimeError):
    """Real replay evidence could not be captured without guessing."""


class LiveReplayCaptureSession:
    """Persist accepted OCR frames and conservative dialogue groupings."""

    def __init__(
        self,
        output_directory: PathInput,
        *,
        name: object = "Captured live replay",
        story_resolver: StoryResolver | None = None,
        story_index_path: PathInput | None = None,
        story_index_sha256: str | None = None,
    ) -> None:
        selected_story: Path | None = None
        if story_index_path is not None:
            selected_story = Path(story_index_path).expanduser()
            if selected_story.is_symlink():
                raise LiveReplayCaptureError("Story index cannot be a symlink")
        selected = Path(output_directory).expanduser()
        if selected.exists() or selected.is_symlink():
            raise LiveReplayCaptureError(
                f"Replay capture output already exists: {selected}"
            )
        parent = selected.parent.resolve()
        self.directory = parent / selected.name
        try:
            self.directory.mkdir(mode=0o700)
            self.frames_directory = self.directory / "frames"
            self.frames_directory.mkdir(mode=0o700)
        except OSError as error:
            raise LiveReplayCaptureError(
                f"Unable to create replay capture output: {error}"
            ) from error
        self.name = str(name).strip() or "Captured live replay"
        self.story_resolver = story_resolver
        self.story_index_path = (
            selected_story.resolve() if selected_story is not None else None
        )
        self.story_index_sha256 = story_index_sha256
        self.story_chapter: str | None = None
        self.story_chapter_line_ids: tuple[str, ...] = ()
        self.dialogue: list[DialogueItem] = []
        self.active: DialogueItem | None = None
        self.frame_count: int = 0
        self.recognized_observation_count: int = 0
        self.duplicate_fingerprints: int = 0
        self.uncertain_observations: int = 0
        self.unresolved_observations: int = 0
        self.observations: list[ObservationRecord] = []
        self.boundaries: list[dict[str, object]] = []
        self.finished: bool = False

    def note_duplicate_fingerprint(self) -> None:
        self.duplicate_fingerprints += 1

    def note_uncertain_observation(
        self, frame: Image.Image | CapturedImageFrame | None = None
    ) -> None:
        self.uncertain_observations += 1
        if frame is not None:
            frame_spec = self._write_frame(frame)
            self._record_observation(
                frame_spec,
                status="uncertain",
                character=None,
                text=None,
                story_line=None,
                story_match="ocr-uncertain",
            )

    def observe(
        self,
        frame: Image.Image | CapturedImageFrame,
        character: object,
        text: object,
    ) -> bool:
        """Record one accepted OCR observation and its exact cropped pixels."""
        if self.finished:
            raise LiveReplayCaptureError("Replay capture is already finished")
        character = str(character or "Narrator").strip() or "Narrator"
        text = " ".join(str(text or "").split())
        frame_spec = self._write_frame(frame)
        if not text:
            self.uncertain_observations += 1
            self._record_observation(
                frame_spec,
                status="uncertain",
                character=character,
                text=None,
                story_line=None,
                story_match="observed-empty-dialogue",
            )
            if self.story_resolver is None:
                self._finalize_active("observed-empty-dialogue")
            return False
        self.recognized_observation_count += 1
        if self.story_resolver is not None:
            line, match_result = self._resolve_story_line(character, text)
            if line is not None:
                self._record_observation(
                    frame_spec,
                    status="canonical",
                    character=character,
                    text=text,
                    story_line=line,
                    story_match=match_result,
                )
                self._observe_resolved_group(
                    character,
                    text,
                    frame_spec,
                    story_line=line,
                    story_match=match_result,
                )
                return True
            if is_standalone_ellipsis_text(text):
                self._record_observation(
                    frame_spec,
                    status="punctuation-only",
                    character=character,
                    text=text,
                    story_line=None,
                    story_match="punctuation-only",
                )
                self._observe_resolved_group(
                    character,
                    text,
                    frame_spec,
                    story_line=None,
                    story_match="punctuation-only",
                )
                return True
            self.unresolved_observations += 1
            self._record_observation(
                frame_spec,
                status="unresolved",
                character=character,
                text=text,
                story_line=None,
                story_match=match_result,
            )
            return True
        self._record_observation(
            frame_spec,
            status="accepted-unbound",
            character=character,
            text=text,
            story_line=None,
            story_match="story-index-unavailable",
        )
        if self.active is None:
            self.active = self._new_dialogue(character, text, frame_spec)
            return True
        same_character = (
            self.active["observed_character"].casefold() == character.casefold()
        )
        previous = self.active["observed_text"]
        prefix_related = text.startswith(previous) or previous.startswith(text)
        if same_character and prefix_related:
            self.active["frames"].append(frame_spec)
            if len(text) > len(previous):
                self.active["observed_text"] = text
            return True
        self._finalize_active("inferred-observation-replacement")
        self.active = self._new_dialogue(character, text, frame_spec)
        return True

    def _resolve_story_line(
        self, character: str, text: str
    ) -> tuple[StoryLine | None, str]:
        assert self.story_resolver is not None
        if self.story_chapter_line_ids and isinstance(
            self.story_resolver, ChapterBoundStoryResolver
        ):
            line, match_result = self.story_resolver.resolve_exact_among(
                character,
                text,
                self.story_chapter_line_ids,
            )
        else:
            line, match_result = self.story_resolver.resolve_exact_with_result(
                character, text
            )
        if line is None:
            return None, match_result
        chapter = getattr(line, "chapter", None)
        if self.story_chapter is not None and chapter != self.story_chapter:
            return None, "outside-capture-chapter"
        if self.story_chapter is None and chapter is not None:
            self.story_chapter = chapter
            rows = self.story_resolver.by_chapter.get(chapter, ())
            self.story_chapter_line_ids = tuple(
                row.line_id for row in rows if row.line_id is not None
            )
        return line, match_result

    def _observe_resolved_group(
        self,
        character: str,
        text: str,
        frame_spec: FrameSpecification,
        *,
        story_line: StoryLine | None,
        story_match: str,
    ) -> None:
        identity = (
            f"line:{story_line.line_id}"
            if story_line is not None
            else f"punctuation:{''.join(text.split())}"
        )
        if self.active is not None and self.active.get("group_identity") == identity:
            self.active["frames"].append(frame_spec)
            return
        self._finalize_active("canonical-successor")
        self.active = {
            **self._new_dialogue(character, text, frame_spec),
            "group_identity": identity,
            "story_line": story_line,
            "story_match": story_match,
        }

    def _record_observation(
        self,
        frame_spec: FrameSpecification,
        *,
        status: str,
        character: str | None,
        text: str | None,
        story_line: StoryLine | None,
        story_match: str,
    ) -> None:
        self.observations.append(
            {
                "observation_index": len(self.observations) + 1,
                "frame": frame_spec,
                "status": status,
                "observed_character": character,
                "observed_text": text,
                "story_line_id": (None if story_line is None else story_line.line_id),
                "story_match": story_match,
            }
        )

    def finish(self) -> CapturedReplayResult:
        """Validate captured bytes and publish a replay corpus plus review report."""
        if self.finished:
            raise LiveReplayCaptureError("Replay capture is already finished")
        self._finalize_active("capture-finished")
        if not self.dialogue:
            raise LiveReplayCaptureError("Replay capture contains no accepted dialogue")
        self._validate_bound_inputs()
        records = [
            self._corpus_record(index, value)
            for index, value in enumerate(self.dialogue, 1)
        ]
        ledger_document = {
            "schema": "vntts.live-replay-capture-observations",
            "schema_version": LIVE_REPLAY_CAPTURE_VERSION,
            "story_index_sha256": self.story_index_sha256,
            "observation_count": len(self.observations),
            "observations": self.observations,
        }
        ledger_payload = _json_payload(ledger_document)
        ledger_binding = {
            "path": "observation-ledger.json",
            "sha256": hashlib.sha256(ledger_payload).hexdigest(),
            "observation_count": len(self.observations),
        }
        capture = {
            "schema_version": LIVE_REPLAY_CAPTURE_VERSION,
            "frame_count": self.frame_count,
            "dialogue_count": len(records),
            "boundary_review_required": bool(self.boundaries),
            "boundary_review_count": len(self.boundaries),
            "story_index_sha256": self.story_index_sha256,
            "observation_ledger": ledger_binding,
            "unresolved_observation_count": self.unresolved_observations,
        }
        corpus_document = {
            "schema_version": 1,
            "name": self.name,
            "fixture_kind": "saved-frame-ocr-replay-capture",
            "capture": capture,
            "dialogue": records,
        }
        report_document = {
            "schema": "vntts.live-replay-capture-report",
            "schema_version": LIVE_REPLAY_CAPTURE_VERSION,
            **capture,
            "duplicate_fingerprints_skipped": self.duplicate_fingerprints,
            "uncertain_observations_skipped": self.uncertain_observations,
            "unresolved_observation_count": self.unresolved_observations,
            "observation_ledger": ledger_binding,
            "boundaries": self.boundaries,
            "dialogue": [
                {
                    "dialogue_index": index,
                    "character": record["character"],
                    "text": record["text"],
                    "line_id": record["line_id"],
                    "story_match": value["story_match"],
                    "frame_count": len(record["frames"]),
                    "boundary_reason": value["boundary_reason"],
                }
                for index, (record, value) in enumerate(
                    zip(records, self.dialogue, strict=True), 1
                )
            ],
        }
        corpus = self.directory / "corpus.json"
        report = self.directory / "capture-report.json"
        observation_ledger = self.directory / "observation-ledger.json"
        if (
            corpus.exists()
            or corpus.is_symlink()
            or report.exists()
            or report.is_symlink()
            or observation_ledger.exists()
            or observation_ledger.is_symlink()
        ):
            raise LiveReplayCaptureError("Replay capture result already exists")
        _write_payload_no_replace(observation_ledger, ledger_payload)
        _write_json_no_replace(report, report_document)
        _write_json_no_replace(corpus, corpus_document)
        self.finished = True
        return CapturedReplayResult(
            self.directory,
            corpus,
            report,
            observation_ledger,
            len(records),
            self.frame_count,
            len(self.boundaries),
        )

    def _new_dialogue(
        self, character: str, text: str, frame_spec: FrameSpecification
    ) -> DialogueItem:
        return {
            "observed_character": character,
            "observed_text": text,
            "frames": [frame_spec],
        }

    def _finalize_active(self, reason: str) -> None:
        if self.active is None:
            return
        item = self.active.copy()
        item.pop("group_identity", None)
        item["boundary_reason"] = reason
        line = item.get("story_line")
        match_result = item.get("story_match", "story-index-unavailable")
        if self.story_resolver is not None and "story_line" not in item:
            line, match_result = self.story_resolver.resolve_exact_with_result(
                item["observed_character"], item["observed_text"]
            )
        item["story_line"] = line
        item["story_match"] = match_result
        self.dialogue.append(item)
        if reason == "inferred-observation-replacement":
            self.boundaries.append(
                {
                    "after_dialogue": len(self.dialogue),
                    "reason": reason,
                    "requires_operator_review": True,
                }
            )
        self.active = None

    def _write_frame(
        self, frame: Image.Image | CapturedImageFrame
    ) -> FrameSpecification:
        image = frame if isinstance(frame, Image.Image) else frame.image
        if not isinstance(image, Image.Image):
            raise LiveReplayCaptureError("Replay capture frame must be a PIL image")
        payload = io.BytesIO()
        image.convert("RGB").save(payload, format="PNG")
        content = payload.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        self.frame_count += 1
        relative = PurePosixPath("frames") / f"frame-{self.frame_count:06d}.png"
        path = self.directory.joinpath(*relative.parts)
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
        except OSError as error:
            raise LiveReplayCaptureError(
                f"Unable to save replay frame: {error}"
            ) from error
        return {"path": relative.as_posix(), "sha256": digest}

    def _corpus_record(self, index: int, item: DialogueItem) -> CorpusRecord:
        line = item["story_line"]
        if line is None:
            return {
                "frames": item["frames"],
                "character": item["observed_character"],
                "text": item["observed_text"],
                "line_id": f"capture:{index}",
                "source_audio_status": "unknown",
                "expected_source": None,
                "capture_boundary": item["boundary_reason"],
                "story_match": item["story_match"],
            }
        source_status = line.source_audio_status
        return {
            "frames": item["frames"],
            "character": line.speaker,
            "text": line.text,
            "line_id": line.line_id,
            "source_audio_status": source_status,
            "source_audio_id": line.source_audio_id,
            "source_audio_duration_seconds": line.source_audio_duration_seconds,
            "source_audio_completeness": getattr(
                line,
                "source_audio_completeness",
                "full" if line.source_audio_duration_seconds is not None else "unknown",
            ),
            "expected_source": "game" if source_status == "available" else None,
            "capture_boundary": item["boundary_reason"],
            "story_match": item["story_match"],
        }

    def _validate_bound_inputs(self) -> None:
        if (
            self.directory.is_symlink()
            or self.frames_directory.is_symlink()
            or self.frames_directory.resolve() != self.directory / "frames"
        ):
            raise LiveReplayCaptureError("Replay capture directory became unsafe")
        if self.story_index_path is not None:
            if (
                self.story_index_path.is_symlink()
                or not self.story_index_path.is_file()
            ):
                raise LiveReplayCaptureError("Story index became unavailable or unsafe")
            digest = hashlib.sha256(self.story_index_path.read_bytes()).hexdigest()
            if digest != self.story_index_sha256:
                raise LiveReplayCaptureError(
                    "Story index changed during replay capture"
                )
        for item in self.dialogue:
            for frame in item["frames"]:
                path = self.directory.joinpath(*PurePosixPath(frame["path"]).parts)
                if path.is_symlink() or not path.is_file():
                    raise LiveReplayCaptureError("Captured replay frame is unavailable")
                if hashlib.sha256(path.read_bytes()).hexdigest() != frame["sha256"]:
                    raise LiveReplayCaptureError("Captured replay frame changed")
        for observation in self.observations:
            frame = observation["frame"]
            path = self.directory.joinpath(*PurePosixPath(frame["path"]).parts)
            if path.is_symlink() or not path.is_file():
                raise LiveReplayCaptureError(
                    "Captured observation frame is unavailable"
                )
            if hashlib.sha256(path.read_bytes()).hexdigest() != frame["sha256"]:
                raise LiveReplayCaptureError("Captured observation frame changed")


def capture_replay_session(
    session: LiveReplayCaptureSession,
    *,
    capture_frame: Callable[[], CapturedImageFrame],
    recognize_frame: Callable[[CapturedImageFrame], tuple[object, object] | None],
    interval_seconds: float,
    maximum_frames: int | None = None,
    duration_seconds: float | None = None,
    fingerprint_frame: Callable[
        [CapturedImageFrame], object
    ] = fingerprint_dialog_frame,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    focused: Callable[[], bool] = lambda: True,
) -> CapturedReplayResult:
    """Capture distinct accepted observations until a bound software limit."""
    started = clock()
    last_fingerprint = object()
    while True:
        if duration_seconds is not None and clock() - started >= duration_seconds:
            break
        if not focused():
            sleep(interval_seconds)
            continue
        frame = capture_frame()
        fingerprint = fingerprint_frame(frame)
        if fingerprint == last_fingerprint:
            session.note_duplicate_fingerprint()
        else:
            last_fingerprint = fingerprint
            observation = recognize_frame(frame)
            if observation is None:
                session.note_uncertain_observation(frame)
            else:
                session.observe(frame, *observation)
                if (
                    maximum_frames is not None
                    and session.recognized_observation_count >= maximum_frames
                ):
                    break
        sleep(interval_seconds)
    return session.finish()


def _json_payload(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_payload_no_replace(path: Path, payload: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise LiveReplayCaptureError(
            f"Replay capture result already exists: {path}"
        ) from error
    except OSError as error:
        raise LiveReplayCaptureError(
            f"Unable to publish replay capture result {path}: {error}"
        ) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json_no_replace(path: Path, document: object) -> None:
    _write_payload_no_replace(path, _json_payload(document))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture exact real-game OCR frames for vntts-replay-live"
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--name", default="Captured live replay")
    parser.add_argument("--story-index", type=Path)
    parser.add_argument("--interval-ms", type=int)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--max-accepted-frames", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.interval_ms is not None and arguments.interval_ms < 1:
        return int(cli_error("interval-ms must be positive"))
    if arguments.duration_seconds is not None and arguments.duration_seconds <= 0:
        return int(cli_error("duration-seconds must be positive"))
    if arguments.max_accepted_frames is not None and arguments.max_accepted_frames < 1:
        return int(cli_error("max-accepted-frames must be positive"))
    settings = load_app_settings()
    story_index = arguments.story_index or settings.story_index
    resolver: StoryResolver | None = None
    story_path: Path | None = None
    story_sha256: str | None = None
    if story_index:
        selected_story = Path(story_index).expanduser()
        try:
            if selected_story.is_symlink():
                raise LiveReplayCaptureError("Story index cannot be a symlink")
            story_path = selected_story.resolve()
            if not story_path.is_file():
                raise LiveReplayCaptureError("Story index is unavailable or unsafe")
            story_sha256 = hashlib.sha256(story_path.read_bytes()).hexdigest()
            loaded_resolver: StoryResolver = ChapterVoicePreloader.load_optional(
                story_path
            )
            resolver = loaded_resolver
            if not resolver.dialogue:
                raise LiveReplayCaptureError("Story index has no usable dialogue")
            if hashlib.sha256(story_path.read_bytes()).hexdigest() != story_sha256:
                raise LiveReplayCaptureError("Story index changed while loading")
        except (OSError, RuntimeError, ValueError) as error:
            return int(cli_error(error))
    try:
        session = LiveReplayCaptureSession(
            arguments.output,
            name=arguments.name,
            story_resolver=resolver,
            story_index_path=story_path,
            story_index_sha256=story_sha256,
        )
        correction_store = OCRCorrectionStore.load()
        corrections = correction_store.dictionary_for(settings.active_profile_id)
        capture_target = (
            WindowCaptureTarget(settings.game_window_title)
            if settings.capture_mode == "window"
            else None
        )

        def recognize(frame: CapturedImageFrame) -> tuple[str, str]:
            result = recognize_screenshot_result(
                frame.image,
                minimum_confidence=settings.ocr_minimum_confidence,
                ocr_language=settings.ocr_language,
                correction_dictionary=corrections,
            )
            # Capture evidence is intentionally broader than live playback.
            # Keep low-confidence OCR in the immutable observation ledger so a
            # later exact story/sequence recovery can accept or reject it. The
            # capture session only promotes exact canonical text or standalone
            # punctuation to dialogue groups.
            if detect_standalone_ellipsis_frame(frame.image):
                return (
                    ellipsis_speaker_hint(result.character, result.text, resolver),
                    "...",
                )
            return result.character or "Narrator", result.text

        print("Capturing accepted OCR frames; press Ctrl+C to finish and validate")
        try:
            result = capture_replay_session(
                session,
                capture_frame=lambda: capture_live_frame(
                    get_screenshot_directory(settings), capture_target
                ),
                recognize_frame=recognize,
                interval_seconds=(arguments.interval_ms or settings.live_interval_ms)
                / 1000,
                maximum_frames=arguments.max_accepted_frames,
                duration_seconds=arguments.duration_seconds,
                focused=(
                    capture_target.is_focused
                    if capture_target is not None
                    else (lambda: True)
                ),
            )
        except KeyboardInterrupt:
            result = session.finish()
    except (OSError, RuntimeError, ValueError) as error:
        return int(cli_error(error))
    return int(
        cli_messages(
            (
                f"Captured {result.dialogue_count} dialogue groups and "
                f"{result.frame_count} exact frames",
                f"Boundary decisions requiring review: {result.boundary_review_count}",
                result.corpus,
                result.report,
            )
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CapturedReplayResult",
    "LIVE_REPLAY_CAPTURE_VERSION",
    "LiveReplayCaptureError",
    "LiveReplayCaptureSession",
    "build_parser",
    "capture_replay_session",
    "detect_standalone_ellipsis_frame",
    "ellipsis_speaker_hint",
    "main",
]
