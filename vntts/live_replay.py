"""Deterministic live-mode replay from saved game frames."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Condition, Event, Lock
from time import monotonic

from PIL import Image
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.cli import cli_error, cli_messages
from vntts.dialog_capture import (
    CapturedDialogFrame,
    fingerprint_dialog_frame,
    recognize_live_frame,
)
from vntts.generated_audio import (
    GeneratedAudioFallbackBackend,
    GeneratedAudioLibrary,
    PlaybackStatus,
    SourceAudioRoute,
)
from vntts.live import LiveDialogReader
from vntts.ocr import DialogRegion
from vntts.playback import PreparedPlayback, outcome_for_prepared
from vntts.speech_backend import SpeechBackendCapabilities
from vntts.support import GenerationTimelineLog

LIVE_REPLAY_CORPUS_VERSION = 1


@dataclass(frozen=True)
class ReplayDialogue:
    frames: tuple[CapturedDialogFrame, ...]
    frame_paths: tuple[str, ...]
    frame_sha256s: tuple[str, ...]
    frame_recognition_sources: tuple[str, ...]
    character: str
    text: str
    expected_source: str | None


@dataclass(frozen=True)
class GeneratedAudioArtifactBinding:
    relative_path: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class GeneratedAudioManifestBinding:
    root: Path
    relative_path: str
    path: Path
    sha256: str
    artifacts: tuple[GeneratedAudioArtifactBinding, ...]


@dataclass(frozen=True)
class LiveReplayCorpus:
    name: str
    fixture_kind: str
    source_sha256: str
    dialogue: tuple[ReplayDialogue, ...]
    story_document: dict
    generated_audio_manifest: GeneratedAudioManifestBinding | None = None


class ReplayAudioOutput:
    """Device-free output that still consumes exact generated PCM."""

    def __init__(self):
        self.played = []

    def query_devices(self, _device=None, _kind=None):
        return {"default_samplerate": 24_000}

    def play(self, samples, sample_rate, **_options):
        samples = samples.astype("<f4", copy=False)
        self.played.append(
            {
                "sample_rate": int(sample_rate),
                "sample_count": int(len(samples)),
                "pcm_sha256": hashlib.sha256(samples.tobytes()).hexdigest(),
            }
        )

    def wait(self):
        return type("ReplayStatus", (), {"output_underflow": False})()

    def stop(self):
        return None


class ReplayLiveSpeechBackend:
    """No-audio backend used after the production route decision is exercised."""

    name = "replay-live-tts"
    capabilities = SpeechBackendCapabilities(True, False, True)

    def prepare_playback(self, character, text):
        return PreparedPlayback(
            (character, text),
            0.0,
            0.0,
            "replay",
            "live:replay-live-tts",
        )

    def play_prepared(self, prepared, *, playback_guard=None):
        completed = playback_guard is None or bool(playback_guard())
        return outcome_for_prepared(
            prepared,
            PlaybackStatus.COMPLETED if completed else PlaybackStatus.INTERRUPTED,
            0.0,
            first_audio_ms=prepared.first_audio_ms if completed else None,
        )

    def stop(self):
        return False


class ReplayFrameSource:
    def __init__(self, dialogue):
        self.dialogue = tuple(dialogue)
        self.dialogue_index = 0
        self.frame_index = 0
        self.advance_requests = 0
        self.completed = Event()
        self.condition = Condition(Lock())
        self.stopped = False
        self.consumed = [[False for _frame in item.frames] for item in self.dialogue]
        self.skipped = [0 for _item in self.dialogue]
        self.unmapped_skipped = 0

    def capture(self):
        with self.condition:
            current = self.dialogue[self.dialogue_index]
            return current.frames[self.frame_index]

    def acknowledge(self, frame):
        """Consume one exact declared frame and return its ledger event."""
        identity = frame.image.info.get("vntts_replay_declared_identity")
        with self.condition:
            event = self._event_for_identity(identity)
            expected = self.dialogue_index, self.frame_index
            if identity == expected and not self.consumed[identity[0]][identity[1]]:
                self.consumed[identity[0]][identity[1]] = True
                event["consumed"] = True
                event["skip_reason"] = None
                if self.frame_index + 1 < len(
                    self.dialogue[self.dialogue_index].frames
                ):
                    self.frame_index += 1
                self.condition.notify_all()
                return event
            event["consumed"] = False
            if identity is None or event["dialogue_index"] is None:
                event["skip_reason"] = "not-declared"
            elif self.consumed[identity[0]][identity[1]]:
                event["skip_reason"] = "already-consumed"
            elif identity[0] != self.dialogue_index:
                event["skip_reason"] = "different-dialogue"
            else:
                event["skip_reason"] = "out-of-order"
            if event["dialogue_index"] is not None:
                self.skipped[event["dialogue_index"] - 1] += 1
            else:
                self.unmapped_skipped += 1
            return event

    def advance(self):
        with self.condition:
            if self.completed.is_set():
                return False
            while not self._current_dialogue_consumed() and not self.stopped:
                self.condition.wait()
            if self.stopped or self.completed.is_set():
                return False
            self.advance_requests += 1
            if self.dialogue_index + 1 >= len(self.dialogue):
                self.completed.set()
                return False
            self.dialogue_index += 1
            self.frame_index = 0
            return True

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            dialogues = []
            for dialogue_index, dialogue in enumerate(self.dialogue):
                frames = [
                    {
                        "frame_index": frame_index + 1,
                        "path": dialogue.frame_paths[frame_index],
                        "sha256": dialogue.frame_sha256s[frame_index],
                        "consumed": self.consumed[dialogue_index][frame_index],
                    }
                    for frame_index in range(len(dialogue.frames))
                ]
                dialogues.append(
                    {
                        "dialogue_index": dialogue_index + 1,
                        "declared_count": len(frames),
                        "consumed_count": sum(frame["consumed"] for frame in frames),
                        "skipped_count": self.skipped[dialogue_index],
                        "frames": frames,
                    }
                )
            declared_count = sum(item["declared_count"] for item in dialogues)
            consumed_count = sum(item["consumed_count"] for item in dialogues)
            return {
                "complete": consumed_count == declared_count,
                "declared_count": declared_count,
                "consumed_count": consumed_count,
                "skipped_count": sum(item["skipped_count"] for item in dialogues)
                + self.unmapped_skipped,
                "unmapped_skipped_count": self.unmapped_skipped,
                "dialogues": dialogues,
            }

    def _current_dialogue_consumed(self):
        return all(self.consumed[self.dialogue_index])

    def _event_for_identity(self, identity):
        if (
            not isinstance(identity, tuple)
            or len(identity) != 2
            or not all(isinstance(value, int) for value in identity)
            or identity[0] < 0
            or identity[0] >= len(self.dialogue)
            or identity[1] < 0
            or identity[1] >= len(self.dialogue[identity[0]].frames)
        ):
            return {
                "dialogue_index": None,
                "frame_index": None,
                "path": None,
                "sha256": None,
            }
        dialogue = self.dialogue[identity[0]]
        return {
            "dialogue_index": identity[0] + 1,
            "frame_index": identity[1] + 1,
            "path": dialogue.frame_paths[identity[1]],
            "sha256": dialogue.frame_sha256s[identity[1]],
        }


class LiveReplayRunner:
    def __init__(
        self,
        corpus,
        *,
        recognizer=None,
        interval_seconds=0.01,
        timeout_seconds=30.0,
        audio_source_policy="prefer-game-audio",
    ):
        if not corpus.dialogue:
            raise ValueError("Live replay corpus has no dialogue frames")
        self.corpus = corpus
        self.recognizer = recognizer or _recognize_replay_frame
        self.interval_seconds = float(interval_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.audio_source_policy = audio_source_policy

    def run(self):
        with _generated_audio_index_snapshot(
            self.corpus.generated_audio_manifest
        ) as generated_audio_index:
            return self._run(generated_audio_index)

    def _run(self, generated_audio_index):
        frame_source = ReplayFrameSource(self.corpus.dialogue)
        resolver = ChapterVoicePreloader.from_document(self.corpus.story_document)
        library = (
            GeneratedAudioLibrary(generated_audio_index)
            if generated_audio_index is not None
            else None
        )
        audio_output = ReplayAudioOutput()
        router = GeneratedAudioFallbackBackend(
            ReplayLiveSpeechBackend(),
            library,
            resolver,
            audio_source_policy=self.audio_source_policy,
            audio_output=audio_output,
        )
        router.set_live_mode_active(True)
        timelines = GenerationTimelineLog(maximum_entries=len(self.corpus.dialogue) + 1)
        played = []
        routes = []
        errors = []
        advance_states = []
        recognized_frames = []

        def recognize(frame):
            result = self.recognizer(frame)
            ledger_event = frame_source.acknowledge(frame)
            recognized_frames.append(
                {
                    "character": str(result[0]),
                    "text": str(result[1]),
                    "source": frame.image.info.get(
                        "vntts_replay_recognition_source", "ocr"
                    ),
                    **ledger_event,
                }
            )
            return result

        def prepare(chunk):
            prepared = router.prepare_route(chunk.character, chunk.text)
            trace = prepared.trace
            routes.append(trace.support_fields() | {"generation": chunk.generation})
            occurred_at = monotonic()
            route_details = trace.support_fields()
            route_details.pop("generation", None)
            timelines.record(
                "route-decision",
                chunk.generation,
                occurred_at,
                **route_details,
            )
            timelines.record(
                "voice-resolution",
                chunk.generation,
                occurred_at,
                voice_reference_id=f"replay:{chunk.character.casefold()}",
            )
            return prepared

        def play(chunk, prepared):
            playback_started = monotonic()
            outcome = router.play_route(
                prepared,
                playback_guard=lambda: reader.wait_until_playable(chunk),
            )
            if (
                isinstance(prepared, SourceAudioRoute)
                and outcome.status is PlaybackStatus.PASSTHROUGH_UNOBSERVED
            ):
                reader.block_auto_advance_for_generation(
                    chunk.generation,
                    "Replay cannot observe original game-audio completion",
                )
            elif not outcome.successful:
                reader.block_auto_advance_for_generation(
                    chunk.generation,
                    "Replay playback was interrupted or failed",
                )
            if outcome.successful and isinstance(prepared, SourceAudioRoute):
                reader.seal_generation(chunk.generation)
            chunk_details = {
                "chunk_id": chunk.chunk_id,
                "chunk_ordinal": chunk.ordinal,
                "chunk_characters": len(chunk.text),
            }
            if outcome.first_audio_ms is not None:
                reader.record_first_pcm(
                    playback_started + outcome.first_audio_ms / 1000
                )
            timelines.record(
                "playback-outcome",
                chunk.generation,
                monotonic(),
                outcome=outcome.status.value,
                underflowed=outcome.underflowed,
                generation_limited=outcome.generation_limited,
                synthesis_ms=outcome.synthesis_ms,
                playback_ms=outcome.playback_ms,
                first_audio_ms=outcome.first_audio_ms,
                cache_source=outcome.cache_source,
                effective_source=outcome.audio_source,
                **chunk_details,
            )
            if outcome.status is PlaybackStatus.FAILED:
                raise RuntimeError(outcome.error or "Replay playback failed")
            played.append(
                {
                    "generation": chunk.generation,
                    "character": chunk.character,
                    "text": chunk.text,
                }
            )
            return outcome.successful

        executors = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"replay-{name}")
            for name in ("capture", "ocr", "speech", "playback")
        ]
        tracker_options = {
            "idle_flush_seconds": max(0.5, self.interval_seconds * 10),
            "min_chunk_characters": 1,
            "complete_dialogue_only": self.audio_source_policy != "live-tts-only",
        }
        if tracker_options["complete_dialogue_only"]:
            tracker_options["incomplete_dialogue_probe"] = (
                resolver.is_unique_incomplete_prefix
            )
        if library is not None:
            tracker_options["early_dialogue_resolver"] = lambda character, text: (
                line.text
                if (
                    line := resolver.resolve_unique_prefix(
                        character,
                        text,
                        candidate_filter=router.has_generated_line,
                    )
                )
                is not None
                else None
            )
        reader = LiveDialogReader(
            capture_executor=executors[0],
            ocr_executor=executors[1],
            speech_executor=executors[2],
            playback_executor=executors[3],
            read_snapshot=lambda: (None, ""),
            capture_frame=frame_source.capture,
            recognize_frame=recognize,
            frame_fingerprint=_fingerprint_replay_frame,
            speak_chunk=lambda _chunk: None,
            prepare_chunk=prepare,
            play_prepared=play,
            report_error=errors.append,
            interval_seconds=self.interval_seconds,
            tracker_options=tracker_options,
            adaptive_options={
                "fast_interval": self.interval_seconds,
                "idle_interval": self.interval_seconds,
                "unfocused_interval": self.interval_seconds,
            },
            auto_advance=frame_source.advance,
            auto_advance_delay_seconds=self.interval_seconds,
            auto_advance_confirmation_timeout_seconds=max(
                5.0,
                self.interval_seconds * 10,
            ),
            auto_advance_state_changed=lambda state, generation, attempt: (
                advance_states.append(
                    {
                        "state": state,
                        "generation": generation,
                        "attempt": attempt,
                    }
                )
            ),
            pipeline_event_handler=timelines.record,
            max_speech_jobs=1,
            first_pcm_on_prepare=True,
        )
        try:
            reader.start()
            completed = frame_source.completed.wait(self.timeout_seconds)
            frame_source.stop()
            reader.stop()
            reader.wait()
        finally:
            frame_source.stop()
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=True)
            router.stop()

        frame_consumption = frame_source.snapshot()
        observed = _group_played_dialogue(played)
        expected = [
            {"character": item.character, "text": item.text}
            for item in self.corpus.dialogue
        ]
        route_sources = [route["effective_source"] for route in routes]
        expected_sources = [
            item.expected_source
            for item in self.corpus.dialogue
            if item.expected_source is not None
        ]
        actual_expected_sources = [
            route["effective_source"]
            for route, item in zip(routes, self.corpus.dialogue, strict=False)
            if item.expected_source is not None
        ]
        successful = bool(
            completed
            and frame_consumption["complete"]
            and not errors
            and observed == expected
            and actual_expected_sources == expected_sources
            and frame_source.advance_requests == len(self.corpus.dialogue)
        )
        return {
            "schema_version": 1,
            "corpus": self.corpus.name,
            "fixture_kind": self.corpus.fixture_kind,
            "successful": successful,
            "expected_dialogue": expected,
            "observed_dialogue": observed,
            "route_sources": route_sources,
            "advance_requests": frame_source.advance_requests,
            "advance_states": advance_states,
            "errors": [str(error) for error in errors],
            "media_integrity": {
                "frame_sha256s": [
                    digest
                    for dialogue in self.corpus.dialogue
                    for digest in dialogue.frame_sha256s
                ],
                "generated_playback": audio_output.played,
                "recognized_frames": recognized_frames,
                "frame_consumption": frame_consumption,
            },
            "provenance": {
                "corpus_sha256": self.corpus.source_sha256,
                "generated_audio_manifest_sha256": (
                    self.corpus.generated_audio_manifest.sha256
                    if self.corpus.generated_audio_manifest is not None
                    else None
                ),
                "generated_audio_artifacts": (
                    [
                        {
                            "path": artifact.relative_path,
                            "sha256": artifact.sha256,
                        }
                        for artifact in self.corpus.generated_audio_manifest.artifacts
                    ]
                    if self.corpus.generated_audio_manifest is not None
                    else []
                ),
                "recognition_sources": sorted(
                    {
                        source
                        for dialogue in self.corpus.dialogue
                        for source in dialogue.frame_recognition_sources
                    }
                ),
            },
            "timelines": timelines.snapshot(),
        }


def load_live_replay_corpus(path):
    path, payload, document = _read_replay_document(path)
    name = str(document.get("name") or path.stem).strip()
    fixture_kind = str(document.get("fixture_kind") or "saved-frame-ocr-replay").strip()
    if not fixture_kind:
        raise ValueError("Live replay fixture_kind must be non-empty")
    region = _decode_region(document.get("dialog_region"))
    dialogue = []
    story_rows = []
    generated_audio_manifest = _generated_audio_manifest_binding(
        path, document.get("generated_audio_manifest")
    )
    for index, item in enumerate(document.get("dialogue", ()), start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Live replay dialogue {index} must be an object")
        character = str(item.get("character") or "Narrator").strip() or "Narrator"
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            raise ValueError(f"Live replay dialogue {index} has no expected text")
        frame_paths = item.get("frames")
        if not isinstance(frame_paths, list) or not frame_paths:
            raise ValueError(f"Live replay dialogue {index} has no frames")
        loaded_frames = []
        for frame_index, frame_spec in enumerate(frame_paths):
            loaded = _load_frame(path.parent, frame_spec, region)
            loaded[0].image.info["vntts_replay_declared_identity"] = (
                index - 1,
                frame_index,
            )
            loaded_frames.append(loaded)
        loaded_frames = tuple(loaded_frames)
        frames = tuple(frame for frame, _path, _digest, _source in loaded_frames)
        frame_paths = tuple(
            frame_path for _frame, frame_path, _digest, _source in loaded_frames
        )
        frame_sha256s = tuple(
            digest for _frame, _path, digest, _source in loaded_frames
        )
        frame_recognition_sources = tuple(
            source for _frame, _path, _digest, source in loaded_frames
        )
        expected_source = str(item.get("expected_source") or "").strip() or None
        dialogue.append(
            ReplayDialogue(
                frames,
                frame_paths,
                frame_sha256s,
                frame_recognition_sources,
                character,
                text,
                expected_source,
            )
        )
        story_rows.append(
            {
                "line_id": str(item.get("line_id") or f"replay:{index}"),
                "chapter": name,
                "sequence": index,
                "speaker_name": character,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_audio_status": str(
                    item.get("source_audio_status") or "missing"
                ),
                "source_audio_id": item.get("source_audio_id"),
                "source_audio_duration_seconds": item.get(
                    "source_audio_duration_seconds"
                ),
            }
        )
    if not dialogue:
        raise ValueError("Live replay corpus has no dialogue entries")
    return LiveReplayCorpus(
        name,
        fixture_kind,
        hashlib.sha256(payload).hexdigest(),
        tuple(dialogue),
        {"dialogue": story_rows},
        generated_audio_manifest,
    )


def _decode_region(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Live replay dialog_region must be an object")
    return DialogRegion(
        value["left"],
        value["top"],
        value["width"],
        value["height"],
    )


def _load_frame(root, frame_spec, region):
    expected_sha256 = None
    observation = None
    if isinstance(frame_spec, str):
        relative_path = frame_spec
    elif isinstance(frame_spec, dict):
        relative_path = frame_spec.get("path")
        expected_sha256 = frame_spec.get("sha256")
        observed_character = frame_spec.get("observed_character")
        observed_text = frame_spec.get("observed_text")
        if observed_character is not None or observed_text is not None:
            expected_sha256 = _required_sha256(
                expected_sha256,
                "Replay frame observation sha256",
            )
            if (
                not isinstance(observed_character, str)
                or not observed_character.strip()
            ):
                raise ValueError("Replay frame observed_character must be non-empty")
            if not isinstance(observed_text, str) or not observed_text.strip():
                raise ValueError("Replay frame observed_text must be non-empty")
            observation = observed_character.strip(), " ".join(observed_text.split())
    else:
        raise ValueError("Live replay frame must be a path or an object")
    if expected_sha256 is not None:
        expected_sha256 = _required_sha256(
            expected_sha256,
            "Replay frame sha256",
        )
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Live replay frame path must be non-empty")
    path, relative, payload = _read_contained_file(
        root,
        relative_path,
        "Live replay frame",
    )
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError(f"Live replay frame checksum does not match: {path}")
    with Image.open(io.BytesIO(payload)) as source:
        image = source.convert("RGB").copy()
    if region is not None:
        image = region.crop(image)
    if observation is not None:
        image.info["vntts_replay_observation"] = observation
    recognition_source = "declared-observation" if observation is not None else "ocr"
    image.info["vntts_replay_recognition_source"] = recognition_source
    return CapturedDialogFrame(image, 0.0), relative, digest, recognition_source


def _recognize_replay_frame(frame):
    observation = frame.image.info.get("vntts_replay_observation")
    if observation is not None:
        return observation
    return recognize_live_frame(frame, minimum_confidence=0)


def _fingerprint_replay_frame(frame):
    fingerprint = fingerprint_dialog_frame(frame)
    observation = frame.image.info.get("vntts_replay_observation")
    identity = frame.image.info.get("vntts_replay_declared_identity")
    return fingerprint, observation, identity


def _read_replay_document(value):
    selected_path = Path(value).expanduser()
    if selected_path.is_symlink():
        raise ValueError(f"Live replay corpus must not be a symlink: {selected_path}")
    path, _relative, payload = _read_contained_file(
        selected_path.parent,
        selected_path.name,
        "Live replay corpus",
    )
    document = _decode_json_object(payload, "live replay corpus")
    version = document.get("schema_version")
    if isinstance(version, bool) or version != LIVE_REPLAY_CORPUS_VERSION:
        raise ValueError(f"unsupported live replay corpus schema version: {version}")
    return path, payload, document


def _decode_json_object(payload, document_name):
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{document_name} root must be an object")
    return document


def _required_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _contained_regular_file(root, value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path must be non-empty")
    if "\\" in value:
        raise ValueError(f"{label} path must use a contained relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{label} path must use a contained relative path")
    root = Path(root).resolve()
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} path must not contain symlinks: {value}")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} path leaves the corpus directory") from error
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist: {resolved}")
    return resolved, relative.as_posix()


def _read_contained_file(root, value, label):
    path, relative = _contained_regular_file(root, value, label)
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{label} is not a regular file: {path}")
        payload = source.read()
    current, current_relative = _contained_regular_file(root, value, label)
    current_stat = current.stat(follow_symlinks=False)
    if (
        current_relative != relative
        or current != path
        or (opened.st_dev, opened.st_ino) != (current_stat.st_dev, current_stat.st_ino)
    ):
        raise ValueError(f"{label} changed while it was being read: {path}")
    return path, relative, payload


def _generated_audio_manifest_binding(document_path, value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            "generated_audio_manifest must bind a relative path and sha256"
        )
    path, relative, payload = _read_contained_file(
        document_path.parent,
        value.get("path"),
        "Generated audio manifest",
    )
    expected_sha256 = _required_sha256(
        value.get("sha256"),
        "Generated audio manifest sha256",
    )
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("Generated audio manifest checksum does not match")
    manifest_document = _decode_json_object(payload, "generated audio manifest")
    artifacts = _generated_audio_artifact_bindings(path, manifest_document)
    binding = GeneratedAudioManifestBinding(
        document_path.parent.resolve(),
        relative,
        path,
        expected_sha256,
        artifacts,
    )
    try:
        with _generated_audio_index_snapshot(binding):
            pass
    except GeneratedAudioManifestError as error:
        raise ValueError(f"Generated audio manifest is invalid: {error}") from error
    return binding


def _generated_audio_artifact_bindings(manifest_path, document):
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Generated audio manifest must contain an entries list")
    artifacts = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"Generated audio entry {index} must be an object")
        path, relative, payload = _read_contained_file(
            manifest_path.parent,
            entry.get("audio"),
            f"Generated audio entry {index}",
        )
        expected_sha256 = _required_sha256(
            entry.get("audio_sha256"),
            f"Generated audio entry {index} audio_sha256",
        )
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError(
                f"Generated audio entry {index} checksum does not match: {path}"
            )
        previous = artifacts.get(relative)
        if previous is not None and previous.sha256 != expected_sha256:
            raise ValueError(
                f"Generated audio path {relative!r} has conflicting checksums"
            )
        artifacts[relative] = GeneratedAudioArtifactBinding(
            relative,
            path,
            expected_sha256,
        )
    return tuple(artifacts[key] for key in sorted(artifacts))


@contextmanager
def _generated_audio_index_snapshot(binding):
    if binding is None:
        yield None
        return
    _manifest_path, _relative, manifest_payload = _read_contained_file(
        binding.root,
        binding.relative_path,
        "Generated audio manifest",
    )
    if hashlib.sha256(manifest_payload).hexdigest() != binding.sha256:
        raise ValueError("Generated audio manifest changed after corpus validation")
    manifest_document = _decode_json_object(
        manifest_payload,
        "generated audio manifest",
    )
    current_artifacts = _generated_audio_artifact_bindings(
        binding.path,
        manifest_document,
    )
    if current_artifacts != binding.artifacts:
        raise ValueError("Generated audio inventory changed after corpus validation")
    with TemporaryDirectory(prefix="vntts-live-replay-") as temporary_directory:
        snapshot_root = Path(temporary_directory)
        snapshot_manifest = snapshot_root / "generated-audio.json"
        for artifact in binding.artifacts:
            _path, _relative, payload = _read_contained_file(
                binding.path.parent,
                artifact.relative_path,
                "Generated audio",
            )
            if hashlib.sha256(payload).hexdigest() != artifact.sha256:
                raise ValueError(
                    f"Generated audio changed after corpus validation: {artifact.path}"
                )
            destination = snapshot_root.joinpath(
                *PurePosixPath(artifact.relative_path).parts
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        snapshot_manifest.write_bytes(manifest_payload)
        yield GeneratedAudioIndex.load(snapshot_manifest)


def _group_played_dialogue(played):
    grouped = []
    for item in played:
        if grouped and grouped[-1]["generation"] == item["generation"]:
            grouped[-1]["text"] = f"{grouped[-1]['text']} {item['text']}"
        else:
            grouped.append(dict(item))
    return [
        {"character": item["character"], "text": " ".join(item["text"].split())}
        for item in grouped
    ]


def build_parser():
    parser = argparse.ArgumentParser(
        description="Replay saved visual-novel frames through the live pipeline"
    )
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--audio-source-policy",
        choices=("live-tts-only", "prefer-generated", "prefer-game-audio"),
        default="prefer-game-audio",
    )
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        corpus = load_live_replay_corpus(arguments.corpus)
        report = LiveReplayRunner(
            corpus,
            timeout_seconds=arguments.timeout,
            audio_source_policy=arguments.audio_source_policy,
        ).run()
    except (OSError, TypeError, ValueError) as error:
        return cli_error(error)
    output = arguments.output or arguments.corpus.with_suffix(".report.json")
    atomic_write_json(output, report)
    return cli_messages(
        (
            f"Live replay {'passed' if report['successful'] else 'failed'}",
            output,
        ),
        exit_code=0 if report["successful"] else 1,
        error=not report["successful"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
