"""Deterministic live-mode replay from saved game frames."""

from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from time import monotonic

from PIL import Image
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.generated_audio import GeneratedAudioIndex

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
from vntts.versioned_json import read_versioned_json

LIVE_REPLAY_CORPUS_VERSION = 1


@dataclass(frozen=True)
class ReplayDialogue:
    frames: tuple[CapturedDialogFrame, ...]
    frame_sha256s: tuple[str, ...]
    character: str
    text: str
    expected_source: str | None


@dataclass(frozen=True)
class LiveReplayCorpus:
    name: str
    dialogue: tuple[ReplayDialogue, ...]
    story_document: dict
    generated_audio_manifest: Path | None = None


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
        self.lock = Lock()

    def capture(self):
        with self.lock:
            current = self.dialogue[self.dialogue_index]
            return current.frames[self.frame_index]

    def acknowledge(self, frame):
        """Advance only after OCR consumed this exact frame, never on capture drop."""
        with self.lock:
            current = self.dialogue[self.dialogue_index]
            if frame.image is not current.frames[self.frame_index].image:
                return False
            if self.frame_index + 1 < len(current.frames):
                self.frame_index += 1
                return True
            return False

    def advance(self):
        with self.lock:
            self.advance_requests += 1
            if self.dialogue_index + 1 >= len(self.dialogue):
                self.completed.set()
                return False
            self.dialogue_index += 1
            self.frame_index = 0
            return True


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
        frame_source = ReplayFrameSource(self.corpus.dialogue)
        resolver = ChapterVoicePreloader.from_document(self.corpus.story_document)
        library = (
            GeneratedAudioLibrary(
                GeneratedAudioIndex.load(self.corpus.generated_audio_manifest)
            )
            if self.corpus.generated_audio_manifest is not None
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
            recognized_frames.append(
                {"character": str(result[0]), "text": str(result[1])}
            )
            frame_source.acknowledge(frame)
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
            reader.stop()
            reader.wait()
        finally:
            for executor in executors:
                executor.shutdown(wait=True, cancel_futures=True)
            router.stop()

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
            and not errors
            and observed == expected
            and actual_expected_sources == expected_sources
            and frame_source.advance_requests == len(self.corpus.dialogue)
        )
        return {
            "schema_version": 1,
            "corpus": self.corpus.name,
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
            },
            "timelines": timelines.snapshot(),
        }


def load_live_replay_corpus(path):
    path = Path(path).expanduser().resolve()
    document = read_versioned_json(
        path,
        schema_version=LIVE_REPLAY_CORPUS_VERSION,
        document_name="live replay corpus",
    )
    name = str(document.get("name") or path.stem).strip()
    region = _decode_region(document.get("dialog_region"))
    dialogue = []
    story_rows = []
    generated_audio_manifest = _optional_document_path(
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
        loaded_frames = tuple(
            _load_frame(path.parent, frame_spec, region) for frame_spec in frame_paths
        )
        frames = tuple(frame for frame, _digest in loaded_frames)
        frame_sha256s = tuple(digest for _frame, digest in loaded_frames)
        expected_source = str(item.get("expected_source") or "").strip() or None
        dialogue.append(
            ReplayDialogue(frames, frame_sha256s, character, text, expected_source)
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
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("Live replay frame path must be non-empty")
    path = (Path(root) / relative_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Live replay frame does not exist: {path}")
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise ValueError(f"Live replay frame checksum does not match: {path}")
    with Image.open(path) as source:
        image = source.convert("RGB")
    if region is not None:
        image = region.crop(image)
    if observation is not None:
        image.info["vntts_replay_observation"] = observation
    return CapturedDialogFrame(image, 0.0), digest


def _recognize_replay_frame(frame):
    observation = frame.image.info.get("vntts_replay_observation")
    if observation is not None:
        return observation
    return recognize_live_frame(frame, minimum_confidence=0)


def _fingerprint_replay_frame(frame):
    fingerprint = fingerprint_dialog_frame(frame)
    observation = frame.image.info.get("vntts_replay_observation")
    return (fingerprint, observation) if observation is not None else fingerprint


def _optional_document_path(document_path, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("generated_audio_manifest must be a non-empty path")
    path = (document_path.parent / value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Generated audio manifest does not exist: {path}")
    return path


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
