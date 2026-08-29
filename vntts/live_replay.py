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
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Condition, Event, Lock, RLock
from time import monotonic

from PIL import Image
from vntts_artifacts.atomic_io import atomic_write_json
from vntts_artifacts.generated_audio import (
    GeneratedAudioIndex,
    GeneratedAudioManifestError,
)

from vntts.chapter_voice_preload import ChapterVoicePreloader
from vntts.cli import cli_error, cli_messages
from vntts.controller import AppController
from vntts.dialog_capture import (
    CapturedDialogFrame,
    dialog_glyphs_visible,
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
from vntts.live_sequence import LiveSequencePlan
from vntts.ocr import DialogRegion
from vntts.playback import PreparedPlayback, outcome_for_prepared
from vntts.settings import AppSettings
from vntts.speech_backend import SpeechBackendCapabilities
from vntts.support import GenerationTimelineLog, generation_timeline_stages
from vntts.voices import CharacterVoice, CharacterVoiceRegistry

LIVE_REPLAY_CORPUS_VERSION = 2
LIVE_REPLAY_CORPUS_VERSIONS = frozenset({1, LIVE_REPLAY_CORPUS_VERSION})
LIVE_REPLAY_SEQUENCE_MODES = frozenset({"shadow", "audio-manual", "audio-auto"})


@dataclass(frozen=True)
class ReplayDialogue:
    frames: tuple[CapturedDialogFrame, ...]
    frame_paths: tuple[str, ...]
    frame_sha256s: tuple[str, ...]
    frame_recognition_sources: tuple[str, ...]
    character: str
    text: str
    event_id: str | None
    line_id: str | None
    expect_playback: bool
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
class ReplayFileBinding:
    root: Path
    relative_path: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class LiveReplaySequenceExpectation:
    event_ids: tuple[str, ...]
    line_ids: tuple[str | None, ...]
    ocr_calls: int
    bounded_recoveries: int
    key_dispatch_attempts: int
    confirmed_key_dispatches: int

    def to_dict(self):
        return {
            "event_ids": list(self.event_ids),
            "line_ids": list(self.line_ids),
            "ocr_calls": self.ocr_calls,
            "bounded_recoveries": self.bounded_recoveries,
            "key_dispatch_attempts": self.key_dispatch_attempts,
            "confirmed_key_dispatches": self.confirmed_key_dispatches,
        }


@dataclass(frozen=True)
class LiveReplaySequenceBinding:
    mode: str
    story_index: ReplayFileBinding
    plan: ReplayFileBinding
    expectation: LiveReplaySequenceExpectation
    focus_probes: tuple[bool, ...] = ()


@dataclass(frozen=True)
class LiveReplayCorpus:
    schema_version: int
    name: str
    fixture_kind: str
    source_sha256: str
    dialogue: tuple[ReplayDialogue, ...]
    story_document: dict
    generated_audio_manifest: GeneratedAudioManifestBinding | None = None
    live_sequence: LiveReplaySequenceBinding | None = None


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

    def __init__(self, characters=()):
        unique_characters = tuple(
            dict.fromkeys(
                str(character or "Narrator").strip() or "Narrator"
                for character in characters
            )
        )
        self.registry = CharacterVoiceRegistry(
            CharacterVoice(character, f"replay:{character.casefold()}")
            for character in unique_characters
            if character.casefold() != "narrator"
        )
        self.narrator_speaker = "Replay Narrator"

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
    def __init__(self, dialogue, *, focus_probes=()):
        self.dialogue = tuple(dialogue)
        self.dialogue_index = 0
        self.frame_index = 0
        self.advance_requests = 0
        self.manual_advance_requests = 0
        self.focus_probes = list(focus_probes)
        self.focus_probe_calls = 0
        self.completed = Event()
        self.condition = Condition(Lock())
        self.stopped = False
        self.consumed = [[False for _frame in item.frames] for item in self.dialogue]
        self.route_kinds = [[None for _frame in item.frames] for item in self.dialogue]
        self.skipped = [0 for _item in self.dialogue]
        self.unmapped_skipped = 0

    def capture(self):
        with self.condition:
            current = self.dialogue[self.dialogue_index]
            return current.frames[self.frame_index]

    def acknowledge(self, frame, *, route_kind="ocr"):
        """Consume one exact declared frame and return its ledger event."""
        identity = frame.image.info.get("vntts_replay_declared_identity")
        with self.condition:
            event = self._event_for_identity(identity)
            expected = self.dialogue_index, self.frame_index
            if identity == expected and not self.consumed[identity[0]][identity[1]]:
                self.consumed[identity[0]][identity[1]] = True
                self.route_kinds[identity[0]][identity[1]] = str(route_kind)
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
        return self._advance(manual=False)

    def manual_advance(self):
        return self._advance(manual=True)

    def complete_terminal(self):
        with self.condition:
            if (
                self.dialogue_index + 1 == len(self.dialogue)
                and self._current_dialogue_consumed()
            ):
                self.completed.set()
                self.condition.notify_all()
                return True
            return False

    def _advance(self, *, manual):
        with self.condition:
            if self.completed.is_set():
                return False
            while not self._current_dialogue_consumed() and not self.stopped:
                self.condition.wait()
            if self.stopped or self.completed.is_set():
                return False
            if manual:
                self.manual_advance_requests += 1
            else:
                self.advance_requests += 1
            if self.dialogue_index + 1 >= len(self.dialogue):
                self.completed.set()
                return False
            self.dialogue_index += 1
            self.frame_index = 0
            return True

    def focus_probe(self):
        with self.condition:
            self.focus_probe_calls += 1
            return self.focus_probes.pop(0) if self.focus_probes else True

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
                        "route_kind": self.route_kinds[dialogue_index][frame_index],
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
                "automatic_advance_requests": self.advance_requests,
                "manual_advance_requests": self.manual_advance_requests,
                "focus_probe_calls": self.focus_probe_calls,
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


class ReplayPipelineRecorder:
    """Split production timelines from sequence-specific replay evidence."""

    def __init__(self, maximum_entries):
        self.timelines = GenerationTimelineLog(maximum_entries=maximum_entries)
        self.events = []
        self.lock = RLock()

    def record(self, stage, generation, occurred_at, **details):
        if stage in generation_timeline_stages:
            return self.timelines.record(stage, generation, occurred_at, **details)
        event = {
            "stage": str(stage),
            "generation": int(generation),
            **{key: _json_safe(value) for key, value in details.items()},
        }
        with self.lock:
            self.events.append(event)
        return True

    def sequence_snapshot(self):
        with self.lock:
            return list(self.events)


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
        self.uses_default_recognizer = recognizer is None
        self.recognizer = recognizer or _recognize_replay_frame
        self.interval_seconds = float(interval_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.audio_source_policy = audio_source_policy

    def run(self):
        with _generated_audio_index_snapshot(
            self.corpus.generated_audio_manifest
        ) as generated_audio_index:
            with _live_sequence_snapshot(self.corpus.live_sequence) as sequence:
                if sequence is not None:
                    return self._run_sequence(generated_audio_index, *sequence)
                return self._run_legacy(generated_audio_index)

    def _run_legacy(self, generated_audio_index):
        frame_source = ReplayFrameSource(self.corpus.dialogue)
        resolver = ChapterVoicePreloader.from_document(self.corpus.story_document)
        library = (
            GeneratedAudioLibrary(generated_audio_index)
            if generated_audio_index is not None
            else None
        )
        live_backend = ReplayLiveSpeechBackend(
            _replay_voice_characters(self.corpus.dialogue, library)
        )
        audio_output = ReplayAudioOutput()
        router = GeneratedAudioFallbackBackend(
            live_backend,
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
            if item.expect_playback
        ]
        route_sources = [route["effective_source"] for route in routes]
        expected_sources = [
            item.expected_source
            for item in self.corpus.dialogue
            if item.expect_playback and item.expected_source is not None
        ]
        actual_expected_sources = route_sources[: len(expected_sources)]
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
            "routes": routes,
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

    def _run_sequence(
        self,
        generated_audio_index,
        plan,
        resolver,
        story_index_path,
        plan_path,
    ):
        binding = self.corpus.live_sequence
        mode = binding.mode
        frame_source = ReplayFrameSource(
            self.corpus.dialogue,
            focus_probes=binding.focus_probes,
        )
        library = (
            GeneratedAudioLibrary(generated_audio_index)
            if generated_audio_index is not None
            else None
        )
        live_backend = ReplayLiveSpeechBackend(
            _replay_voice_characters(self.corpus.dialogue, library)
        )
        audio_output = ReplayAudioOutput()
        router = GeneratedAudioFallbackBackend(
            live_backend,
            library,
            resolver,
            audio_source_policy=self.audio_source_policy,
            audio_output=audio_output,
            require_source_audio_completion=mode == "shadow",
        )
        router.set_live_mode_active(True)
        pipeline = ReplayPipelineRecorder(len(self.corpus.dialogue) + 1)
        routes = []
        errors = []
        statuses = []
        sequence_statuses = []
        advance_states = []
        recognized_frames = []
        routed_frames = []
        played = []

        def recognize(frame):
            result = (
                _recognize_replay_frame(frame, ellipsis_speaker_resolver=resolver)
                if self.uses_default_recognizer
                else self.recognizer(frame)
            )
            recognized_frames.append(
                {
                    "character": str(result[0]),
                    "text": str(result[1]),
                    "source": frame.image.info.get(
                        "vntts_replay_recognition_source", "ocr"
                    ),
                    **frame_source._event_for_identity(
                        frame.image.info.get("vntts_replay_declared_identity")
                    ),
                }
            )
            return result

        def frame_routed(frame, _fingerprint, route_kind, character, text):
            ledger_event = frame_source.acknowledge(
                frame,
                route_kind=route_kind,
            )
            routed_frames.append(
                {
                    "character": str(character or "Narrator"),
                    "text": str(text),
                    "route_kind": route_kind,
                    **ledger_event,
                }
            )
            if mode == "audio-manual":
                with controller.story_cursor_lock:
                    event = controller.story_cursor.current_event
                    silent_event = event is not None and event.kind == "silent"
                if silent_event:
                    frame_source.manual_advance()
            elif mode == "audio-auto":
                with controller.story_cursor_lock:
                    event = controller.story_cursor.current_event
                    terminal_silent = bool(
                        event is not None
                        and event.kind == "silent"
                        and not event.successors
                    )
                if terminal_silent:
                    frame_source.complete_terminal()

        def record_route(trace):
            routes.append(trace.support_fields())

        settings = AppSettings(
            story_index=str(story_index_path),
            live_sequence_plan=str(plan_path),
            live_sequence_mode=mode,
            auto_advance_enabled=mode in {"shadow", "audio-auto"},
            audio_source_policy=self.audio_source_policy,
            live_interval_ms=max(1, round(self.interval_seconds * 1000)),
            live_idle_flush_ms=max(1, round(self.interval_seconds * 10_000)),
            live_min_chunk_characters=1,
            warm_up_voices=False,
        )
        controller = AppController(
            settings,
            status_handler=statuses.append,
            dialog_handler=lambda _character, _text: None,
            sequence_status_handler=sequence_statuses.append,
            unknown_speaker_handler=lambda speaker: errors.append(
                RuntimeError(f"Replay unexpectedly requires a voice for {speaker}")
            ),
            error_handler=errors.append,
            chapter_voice_preloader=resolver,
            route_trace_handler=record_route,
            pipeline_event_handler=pipeline.record,
            live_sequence_plan_factory=lambda _plan, _story: plan,
        )
        controller.tts = live_backend
        controller.voice_router = live_backend
        controller.speech_backend = router
        controller._auto_advance_dialog = frame_source.advance

        executors = [
            ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"replay-{name}")
            for name in ("capture", "ocr", "speech", "playback")
        ]
        controller.capture_executor = executors[0]
        controller.ocr_executor = executors[1]
        controller.speech_executor = executors[2]
        controller.playback_executor = executors[3]
        live_configuration = controller._get_live_configuration()

        def play(chunk, prepared):
            result = controller._play_live_chunk(chunk, prepared)
            if result:
                played.append(
                    {
                        "generation": chunk.generation,
                        "character": chunk.character,
                        "text": chunk.text,
                        "line_id": chunk.line_id,
                    }
                )
                if mode == "audio-manual":
                    frame_source.manual_advance()
                elif mode == "audio-auto":
                    with controller.story_cursor_lock:
                        event = controller.story_cursor.current_event
                        terminal = bool(event is not None and not event.successors)
                    if terminal:
                        frame_source.complete_terminal()
            return result

        def auto_advance_state_changed(state, generation, attempt):
            advance_states.append(
                {
                    "state": state,
                    "generation": generation,
                    "attempt": attempt,
                }
            )
            controller._auto_advance_state_changed(state, generation, attempt)

        reader = LiveDialogReader(
            capture_executor=executors[0],
            ocr_executor=executors[1],
            speech_executor=executors[2],
            playback_executor=executors[3],
            read_snapshot=lambda: (None, ""),
            capture_frame=frame_source.capture,
            recognize_frame=recognize,
            frame_fingerprint=_fingerprint_replay_frame,
            frame_presence=dialog_glyphs_visible,
            stable_frame_route=controller._stable_live_frame_route,
            stable_frame_owner=controller._stable_live_frame_owner,
            frame_routed=frame_routed,
            line_id_resolver=controller._live_sequence_line_id,
            speak_chunk=lambda _chunk: None,
            prepare_chunk=controller._prepare_live_chunk,
            play_prepared=play,
            report_error=errors.append,
            interrupt_speech=controller._interrupt_speech,
            dialog_observed=controller._dialog_observed,
            focus_probe=frame_source.focus_probe,
            capture_state_changed=controller._capture_state_changed,
            auto_advance=controller._live_auto_advance_callback(),
            require_visible_auto_advance=mode == "audio-auto",
            auto_advance_delay_seconds=self.interval_seconds,
            auto_advance_confirmation_timeout_seconds=max(
                5.0,
                self.interval_seconds * 10,
            ),
            auto_advance_state_changed=auto_advance_state_changed,
            pipeline_event_handler=pipeline.record,
            max_speech_jobs=1,
            first_pcm_on_prepare=False,
            **live_configuration,
        )
        controller.live_reader = reader
        controller._set_backend_live_mode(True)
        try:
            reader.start()
            completed = frame_source.completed.wait(self.timeout_seconds)
            frame_source.stop()
            reader.stop()
            reader.wait()
            metrics = reader.get_pipeline_metrics()
            final_cursor = controller.story_cursor.snapshot()
        finally:
            frame_source.stop()
            controller.shutdown()

        frame_consumption = frame_source.snapshot()
        observed = _group_played_dialogue(played)
        expected = [
            {"character": item.character, "text": item.text}
            for item in self.corpus.dialogue
            if item.expect_playback
        ]
        route_sources = [route["effective_source"] for route in routes]
        expected_sources = [
            item.expected_source
            for item in self.corpus.dialogue
            if item.expect_playback
        ]
        sequence_events = pipeline.sequence_snapshot()
        observed_sequence = _sequence_replay_metrics(
            mode,
            sequence_events,
            recognized_frames,
            advance_states,
        )
        expected_sequence = binding.expectation.to_dict()
        sequence_successful = observed_sequence == expected_sequence
        successful = bool(
            completed
            and frame_consumption["complete"]
            and not errors
            and observed == expected
            and route_sources == expected_sources
            and sequence_successful
            and (
                frame_source.manual_advance_requests == len(self.corpus.dialogue)
                if mode == "audio-manual"
                else (
                    frame_source.advance_requests
                    == expected_sequence["key_dispatch_attempts"]
                    if mode == "audio-auto"
                    else frame_source.advance_requests == len(self.corpus.dialogue)
                )
            )
        )
        return {
            "schema_version": 2,
            "corpus": self.corpus.name,
            "fixture_kind": self.corpus.fixture_kind,
            "successful": successful,
            "expected_dialogue": expected,
            "observed_dialogue": observed,
            "route_sources": route_sources,
            "advance_requests": frame_source.advance_requests,
            "manual_advance_requests": frame_source.manual_advance_requests,
            "advance_states": advance_states,
            "errors": [str(error) for error in errors],
            "sequence": {
                "mode": mode,
                "successful": sequence_successful,
                "expected": expected_sequence,
                "observed": observed_sequence,
                "ocr_invocations": metrics.recognized_frames,
                "final_cursor": {
                    **asdict(final_cursor),
                    "state": final_cursor.state.value,
                },
                "events": sequence_events,
                "statuses": [asdict(status) for status in sequence_statuses],
            },
            "routes": routes,
            "media_integrity": {
                "frame_sha256s": [
                    digest
                    for dialogue in self.corpus.dialogue
                    for digest in dialogue.frame_sha256s
                ],
                "generated_playback": audio_output.played,
                "recognized_frames": recognized_frames,
                "routed_frames": routed_frames,
                "frame_consumption": frame_consumption,
            },
            "provenance": {
                "corpus_sha256": self.corpus.source_sha256,
                "story_index_sha256": binding.story_index.sha256,
                "live_sequence_plan_sha256": binding.plan.sha256,
                "generated_audio_manifest_sha256": (
                    self.corpus.generated_audio_manifest.sha256
                    if self.corpus.generated_audio_manifest is not None
                    else None
                ),
                "generated_audio_artifacts": (
                    [
                        {"path": artifact.relative_path, "sha256": artifact.sha256}
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
            "statuses": statuses,
            "timelines": pipeline.timelines.snapshot(),
        }


def load_live_replay_corpus(path):
    path, payload, document = _read_replay_document(path)
    schema_version = document["schema_version"]
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
        expect_playback = item.get("expect_playback", True)
        if not isinstance(expect_playback, bool):
            raise ValueError(
                f"Live replay dialogue {index} expect_playback must be a boolean"
            )
        if not expect_playback and expected_source is not None:
            raise ValueError(
                f"Live replay dialogue {index} cannot expect an audio source when "
                "playback is disabled"
            )
        if schema_version == 2 and expect_playback and expected_source is None:
            raise ValueError(
                f"Live replay dialogue {index} must declare expected_source when "
                "playback is enabled in schema version 2"
            )
        raw_line_id = item.get("line_id")
        if schema_version == 1:
            line_id = str(raw_line_id or f"replay:{index}").strip()
            event_id = None
        else:
            line_id = None if raw_line_id is None else str(raw_line_id).strip() or None
            event_id = str(item.get("event_id") or "").strip() or None
            if event_id is None:
                raise ValueError(
                    f"Live replay dialogue {index} has no sequence event_id"
                )
        if schema_version == 1 and not line_id:
            raise ValueError(f"Live replay dialogue {index} has no line_id")
        dialogue.append(
            ReplayDialogue(
                frames,
                frame_paths,
                frame_sha256s,
                frame_recognition_sources,
                character,
                text,
                event_id,
                line_id,
                expect_playback,
                expected_source,
            )
        )
        story_rows.append(
            {
                "line_id": line_id,
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
    live_sequence = _live_sequence_binding(
        path,
        document.get("live_sequence"),
        schema_version=schema_version,
        dialogue=tuple(dialogue),
    )
    return LiveReplayCorpus(
        schema_version,
        name,
        fixture_kind,
        hashlib.sha256(payload).hexdigest(),
        tuple(dialogue),
        {"dialogue": story_rows},
        generated_audio_manifest,
        live_sequence,
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


def _recognize_replay_frame(frame, *, ellipsis_speaker_resolver=None):
    observation = frame.image.info.get("vntts_replay_observation")
    if observation is not None:
        return observation
    return recognize_live_frame(
        frame,
        ellipsis_speaker_resolver=ellipsis_speaker_resolver,
    )


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
    if isinstance(version, bool) or version not in LIVE_REPLAY_CORPUS_VERSIONS:
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


def _live_sequence_binding(
    document_path,
    value,
    *,
    schema_version,
    dialogue,
):
    if schema_version == 1:
        if value is not None:
            raise ValueError("live_sequence requires live replay schema version 2")
        return None
    if not isinstance(value, dict) or set(value) != {
        "mode",
        "story_index",
        "plan",
        "expected",
        "focus_probes",
    }:
        raise ValueError(
            "schema v2 live_sequence must contain mode, story_index, plan, "
            "expected and focus_probes"
        )
    mode = value.get("mode")
    if mode not in LIVE_REPLAY_SEQUENCE_MODES:
        raise ValueError(f"Unsupported live replay sequence mode: {mode!r}")
    story_index = _replay_file_binding(
        document_path,
        value.get("story_index"),
        "Live replay story index",
    )
    plan_binding = _replay_file_binding(
        document_path,
        value.get("plan"),
        "Live replay sequence plan",
    )
    expectation = _live_sequence_expectation(value.get("expected"), len(dialogue))
    focus_probes = value.get("focus_probes")
    if not isinstance(focus_probes, list) or any(
        not isinstance(item, bool) for item in focus_probes
    ):
        raise ValueError("Live replay focus_probes must be a list of booleans")
    try:
        plan = LiveSequencePlan.load(plan_binding.path, story_index.path)
    except Exception as error:
        raise ValueError(f"Live replay sequence binding is invalid: {error}") from error
    resolver = ChapterVoicePreloader.load_optional(story_index.path)
    dialogue_event_ids = tuple(item.event_id for item in dialogue)
    dialogue_line_ids = tuple(item.line_id for item in dialogue)
    if expectation.event_ids != dialogue_event_ids:
        raise ValueError(
            "Live replay expected event_ids must exactly match dialogue event_id order"
        )
    if expectation.line_ids != dialogue_line_ids:
        raise ValueError(
            "Live replay expected line_ids must exactly match dialogue line_id order"
        )
    mapped_event_ids = []
    for index, item in enumerate(dialogue, start=1):
        event = plan.events.get(item.event_id)
        if event is None:
            raise ValueError(
                f"Live replay dialogue {index} event_id is not bound by the exact "
                "sequence plan"
            )
        if item.line_id is None:
            if event.line_id is not None or event.kind != "silent":
                raise ValueError(
                    f"Live replay dialogue {index} without line_id must bind a "
                    "line-less silent event"
                )
            if item.expect_playback:
                raise ValueError(
                    f"Live replay dialogue {index} silent event cannot expect playback"
                )
        else:
            line = resolver.line_for_id(item.line_id)
            if line is None or event.line_id != item.line_id:
                raise ValueError(
                    f"Live replay dialogue {index} line_id is not bound by its exact "
                    "story index and sequence event"
                )
            if (line.speaker, line.text) != (item.character, item.text):
                raise ValueError(
                    f"Live replay dialogue {index} disagrees with its canonical story "
                    "line"
                )
        if item.line_id is None and item.expected_source is not None:
            raise ValueError(
                f"Live replay dialogue {index} silent event cannot expect an audio "
                "source"
            )
        mapped_event_ids.append(event.event_id)
    if expectation.event_ids != tuple(mapped_event_ids):
        raise ValueError(
            "Live replay expected event_ids do not match the sequence-plan bindings"
        )
    return LiveReplaySequenceBinding(
        mode,
        story_index,
        plan_binding,
        expectation,
        tuple(focus_probes),
    )


def _replay_file_binding(document_path, value, label):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{label} must bind exactly path and sha256")
    path, relative, payload = _read_contained_file(
        document_path.parent,
        value.get("path"),
        label,
    )
    digest = _required_sha256(value.get("sha256"), f"{label} sha256")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError(f"{label} checksum does not match")
    return ReplayFileBinding(document_path.parent.resolve(), relative, path, digest)


def _live_sequence_expectation(value, dialogue_count):
    fields = {
        "event_ids",
        "line_ids",
        "ocr_calls",
        "bounded_recoveries",
        "key_dispatch_attempts",
        "confirmed_key_dispatches",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(
            "Live replay sequence expected metrics must declare exact identities and "
            "all counters"
        )

    def identities(name, *, nullable=False):
        raw = value.get(name)
        if (
            not isinstance(raw, list)
            or len(raw) != dialogue_count
            or any(
                item is not None and (not isinstance(item, str) or not item.strip())
                for item in raw
            )
            or (not nullable and any(item is None for item in raw))
        ):
            raise ValueError(
                f"Live replay sequence expected {name} must contain one identity per "
                "dialogue"
            )
        return tuple(None if item is None else item.strip() for item in raw)

    def count(name):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError(
                f"Live replay sequence expected {name} must be a non-negative integer"
            )
        return raw

    return LiveReplaySequenceExpectation(
        identities("event_ids"),
        identities("line_ids", nullable=True),
        count("ocr_calls"),
        count("bounded_recoveries"),
        count("key_dispatch_attempts"),
        count("confirmed_key_dispatches"),
    )


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


@contextmanager
def _live_sequence_snapshot(binding):
    if binding is None:
        yield None
        return
    story_path, _story_relative, story_payload = _read_contained_file(
        binding.story_index.root,
        binding.story_index.relative_path,
        "Live replay story index",
    )
    plan_path, _plan_relative, plan_payload = _read_contained_file(
        binding.plan.root,
        binding.plan.relative_path,
        "Live replay sequence plan",
    )
    if hashlib.sha256(story_payload).hexdigest() != binding.story_index.sha256:
        raise ValueError("Live replay story index changed after corpus validation")
    if hashlib.sha256(plan_payload).hexdigest() != binding.plan.sha256:
        raise ValueError("Live replay sequence plan changed after corpus validation")
    if story_path != binding.story_index.path or plan_path != binding.plan.path:
        raise ValueError("Live replay sequence authority changed after validation")
    with TemporaryDirectory(prefix="vntts-live-replay-sequence-") as directory:
        root = Path(directory)
        snapshot_story = root / "story-index.jsonl"
        snapshot_plan = root / "live-sequence.json"
        snapshot_story.write_bytes(story_payload)
        snapshot_plan.write_bytes(plan_payload)
        try:
            plan = LiveSequencePlan.load(snapshot_plan, snapshot_story)
        except Exception as error:
            raise ValueError(
                f"Live replay sequence snapshot is invalid: {error}"
            ) from error
        resolver = ChapterVoicePreloader.load_optional(snapshot_story)
        yield plan, resolver, snapshot_story, snapshot_plan


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


def _replay_voice_characters(dialogue, library):
    characters = [item.character for item in dialogue]
    if library is not None:
        characters.extend(
            decision.requested_voice_character
            for decision in library.live_fallbacks.values()
        )
    return tuple(dict.fromkeys(characters))


def _sequence_replay_metrics(mode, events, recognized_frames, advance_states):
    if mode == "shadow":
        identity_events = [
            event
            for event in events
            if event["stage"] in {"sequence-shadow", "sequence-visual-transition"}
        ]
    else:
        identity_events = [
            event
            for event in events
            if (
                event["stage"] == "sequence-playback-state"
                and event.get("outcome") == "completed"
            )
            or (
                event["stage"] == "sequence-visual-transition"
                and event.get("route") == "silent"
            )
            or (
                event["stage"] in {"sequence-audio-manual", "sequence-audio-auto"}
                and event.get("line_id") is None
                and event.get("match_result") == "expected-silent-ellipsis"
            )
        ]
    identities = []
    for event in identity_events:
        identity = event.get("event_id"), event.get("line_id")
        if identity[0] and (not identities or identities[-1] != identity):
            identities.append(identity)
    recovered_event_ids = {
        event.get("event_id")
        for event in events
        if event["stage"] in {"sequence-audio-manual", "sequence-audio-auto"}
        and event.get("reason") == "observation-bounded-lookahead"
        and event.get("event_id")
    }
    ocr_frame_identities = {
        (frame.get("dialogue_index"), frame.get("frame_index"))
        for frame in recognized_frames
        if frame.get("dialogue_index") is not None
        and frame.get("frame_index") is not None
    }
    return {
        "event_ids": [event_id for event_id, _line_id in identities],
        "line_ids": [line_id for _event_id, line_id in identities],
        "ocr_calls": len(ocr_frame_identities),
        "bounded_recoveries": len(recovered_event_ids),
        "key_dispatch_attempts": sum(
            state["state"] == "dispatched" for state in advance_states
        ),
        "confirmed_key_dispatches": sum(
            state["state"] == "confirmed" for state in advance_states
        ),
    }


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    enum_value = getattr(value, "value", None)
    return _json_safe(enum_value) if enum_value is not None else str(value)


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
