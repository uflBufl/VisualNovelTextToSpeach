"""Incremental OCR dialogue tracking for live reading."""

import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from hashlib import sha256
from time import monotonic


@dataclass(frozen=True)
class SpeechChunk:
    generation: int
    character: str
    text: str
    ordinal: int | None = field(default=None, compare=False)
    line_id: str | None = field(default=None, compare=False)
    explicit_replay: bool = False

    @property
    def chunk_id(self) -> str | None:
        if self.ordinal is None:
            return None
        character = " ".join((self.character or "Narrator").casefold().split())
        text = " ".join((self.text or "").casefold().split())
        payload = (
            f"{self.generation}\0{self.ordinal}\0{character}\0{text}\0"
            f"{self.line_id or ''}"
        )
        return sha256(payload.encode("utf-8")).hexdigest()[:16]


TrackerResolver = Callable[[str, str], str | None]
TrackerProbe = Callable[[str, str], bool]


class IncrementalDialogTracker:
    def __init__(
        self,
        *,
        stability_frames: int = 2,
        idle_flush_seconds: float = 0.7,
        min_chunk_characters: int = 20,
        complete_sentences_only: bool = True,
        complete_dialogue_only: bool = False,
        early_dialogue_resolver: TrackerResolver | None = None,
        incomplete_dialogue_probe: TrackerProbe | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if stability_frames < 2:
            raise ValueError("stability_frames must be at least 2")
        if idle_flush_seconds <= 0:
            raise ValueError("idle_flush_seconds must be positive")
        if min_chunk_characters <= 0:
            raise ValueError("min_chunk_characters must be positive")

        self.stability_frames = stability_frames
        self.idle_flush_seconds = idle_flush_seconds
        self.min_chunk_characters = min_chunk_characters
        self.complete_sentences_only = complete_sentences_only
        self.complete_dialogue_only = bool(complete_dialogue_only)
        self.early_dialogue_resolver = early_dialogue_resolver
        self.incomplete_dialogue_probe = incomplete_dialogue_probe
        self.clock = clock
        self.generation = 0
        self.character: str | None = None
        self.latest_text = ""
        self.committed_text = ""
        self.conflicting_observation = False
        self.transition_expected = False
        self.last_change_at: float | None = None
        self.stable_text = ""
        self.last_stable_change_at: float | None = None
        self.history: deque[str] = deque(maxlen=stability_frames)
        self.pending_character: str | None = None
        self.pending_history: deque[str] = deque(maxlen=stability_frames)
        self.next_chunk_ordinal = 1
        self.silent_event_id: str | None = None
        self.canonical_line_id: str | None = None

    @property
    def committed_position(self) -> int:
        return len(self.committed_text)

    def expect_new_dialog(self) -> None:
        if self.latest_text:
            self.transition_expected = True

    def observe(self, character: str | None, text: str | None) -> list[SpeechChunk]:
        now = self.clock()
        character = (character or "Narrator").strip() or "Narrator"
        text = self._normalize(text)

        if not text:
            if self.latest_text:
                self._clear_dialog()
            return []

        if self._observation_is_blocked(character, text):
            return []

        if self._is_new_dialog(character, text):
            if self.latest_text:
                return self._observe_new_dialog_candidate(character, text, now)
            self._start_dialog(character, text, now)
            return []

        self._clear_pending_dialog()
        self.conflicting_observation = False
        if text != self.latest_text:
            self.last_change_at = now
        self.character = character
        self.latest_text = text
        self.history.append(text)

        if len(self.history) < self.stability_frames:
            return []

        stable_text = os.path.commonprefix(list(self.history))
        if stable_text != self.stable_text:
            self.stable_text = stable_text
            self.last_stable_change_at = now
        last_change_at = self.last_change_at
        assert last_change_at is not None
        idle = now - last_change_at >= self.idle_flush_seconds
        if (
            self.complete_dialogue_only
            and not idle
            and self.committed_position == 0
            and self.early_dialogue_resolver is not None
        ):
            resolved_text = self.early_dialogue_resolver(character, stable_text)
            if resolved_text:
                return self._emit(resolved_text, flush=True)
        if (
            self.complete_dialogue_only
            and idle
            and self.committed_position == 0
            and self.incomplete_dialogue_probe is not None
            and self.incomplete_dialogue_probe(character, stable_text)
        ):
            return []
        return self._emit(stable_text, flush=idle)

    def _observation_is_blocked(self, character: str, text: str) -> bool:
        if (
            self.canonical_line_id is not None
            and character == self.character
            and not self.transition_expected
        ):
            return True
        if self._is_speaker_noise_over_committed_text(character, text):
            self._clear_pending_dialog()
            return True
        if character != self.character or self.transition_expected:
            return False
        if self.committed_text.startswith(text) and text != self.committed_text:
            self._clear_pending_dialog()
            return True
        if self.committed_text and not text.startswith(self.committed_text):
            self.conflicting_observation = True
            self._clear_pending_dialog()
            return True
        return False

    def observe_silent(self, event_id: str) -> bool:
        event_id = str(event_id).strip()
        if not event_id:
            raise ValueError("silent event_id must be non-empty")
        if self.silent_event_id == event_id:
            return False
        self.generation += 1
        self.character = None
        self.latest_text = ""
        self.committed_text = ""
        self.conflicting_observation = False
        self.transition_expected = False
        self.last_change_at = None
        self.stable_text = ""
        self.last_stable_change_at = self.clock()
        self.history.clear()
        self.next_chunk_ordinal = 1
        self._clear_pending_dialog()
        self.silent_event_id = event_id
        self.canonical_line_id = None
        return True

    def observe_canonical(
        self, character: str | None, text: str | None, line_id: str
    ) -> list[SpeechChunk]:
        character = (character or "Narrator").strip() or "Narrator"
        text = self._normalize(text)
        line_id = str(line_id).strip()
        if not text or not line_id:
            raise ValueError("canonical dialogue requires text and line_id")
        if self.canonical_line_id == line_id:
            return []
        now = self.clock()
        self.generation += 1
        self.character = character
        self.latest_text = text
        self.committed_text = text
        self.conflicting_observation = False
        self.transition_expected = False
        self.last_change_at = now
        self.stable_text = text
        self.last_stable_change_at = now
        self.history.clear()
        self.history.extend([text] * self.stability_frames)
        self.next_chunk_ordinal = 2
        self._clear_pending_dialog()
        self.silent_event_id = None
        self.canonical_line_id = line_id
        return [
            SpeechChunk(
                self.generation,
                character,
                text,
                ordinal=1,
                line_id=line_id,
            )
        ]

    def flush(self) -> list[SpeechChunk]:
        if not self.latest_text or self.conflicting_observation:
            return []
        return self._emit(self.latest_text, flush=True)

    def is_idle_complete(self) -> bool:
        if self.silent_event_id is not None:
            return True
        if self.canonical_line_id is not None:
            return True
        if (
            self.conflicting_observation
            or not self.latest_text
            or len(self.history) < self.stability_frames
            or self.last_stable_change_at is None
            or not self.stable_text.strip()
        ):
            return False
        stable_length = len(self.stable_text.rstrip())
        return (
            self.committed_position >= stable_length
            and self.clock() - self.last_stable_change_at >= self.idle_flush_seconds
        )

    def _is_new_dialog(self, character: str, text: str) -> bool:
        if not self.latest_text:
            return True
        if self.transition_expected and (
            character != self.character or text != self.latest_text
        ):
            return True
        if character != self.character:
            return True
        if text == self.latest_text or text.startswith(self.latest_text):
            return False

        common_prefix = os.path.commonprefix([self.latest_text, text])
        if len(text) < self.committed_position and len(common_prefix) < len(text):
            return True

        similarity = SequenceMatcher(None, self.latest_text, text).ratio()
        meaningful_prefix = min(8, max(1, len(self.latest_text) // 3))
        return len(common_prefix) < meaningful_prefix and similarity < 0.5

    def _is_speaker_noise_over_committed_text(self, character: str, text: str) -> bool:
        """Ignore a speaker wobble that still contains the spoken dialogue.

        The nameplate is a small OCR target and can temporarily be recognized
        as Narrator while the dialogue crop also picks up background glyphs.
        Once the stable dialogue has been committed, that must not create a new
        speech generation for the same words.
        """
        if character == self.character or not self.committed_text:
            return False
        committed_key = self._comparison_key(self.committed_text)
        text_key = self._comparison_key(text)
        if len(committed_key) < 4 or not text_key:
            return False
        return committed_key in text_key

    def _observe_new_dialog_candidate(
        self, character: str, text: str, now: float
    ) -> list[SpeechChunk]:
        if not self._matches_pending_dialog(character, text):
            self.pending_character = character
            self.pending_history.clear()
        self.pending_history.append(text)

        if len(self.pending_history) < self.stability_frames:
            return []

        candidate_history = list(self.pending_history)
        self._start_dialog(character, text, now)
        self.history.clear()
        self.history.extend(candidate_history)
        stable_text = os.path.commonprefix(candidate_history)
        self.stable_text = stable_text
        self.last_stable_change_at = now
        self._clear_pending_dialog()
        return self._emit(stable_text, flush=False)

    def _matches_pending_dialog(self, character: str, text: str) -> bool:
        if character != self.pending_character or not self.pending_history:
            return False
        previous = self.pending_history[-1]
        if text == previous or text.startswith(previous) or previous.startswith(text):
            return True
        common_prefix = os.path.commonprefix([previous, text])
        meaningful_prefix = min(8, max(1, len(previous) // 3))
        similarity = SequenceMatcher(None, previous, text).ratio()
        return len(common_prefix) >= meaningful_prefix or similarity >= 0.5

    def _clear_pending_dialog(self) -> None:
        self.pending_character = None
        self.pending_history.clear()

    @staticmethod
    def _comparison_key(text: str) -> str:
        return "".join(
            character.casefold() for character in text if character.isalnum()
        )

    def _start_dialog(self, character: str, text: str, now: float) -> None:
        self.generation += 1
        self.character = character
        self.latest_text = text
        self.committed_text = ""
        self.conflicting_observation = False
        self.transition_expected = False
        self.last_change_at = now
        self.stable_text = ""
        self.last_stable_change_at = now
        self.history.clear()
        self.history.append(text)
        self.next_chunk_ordinal = 1
        self.silent_event_id = None
        self.canonical_line_id = None
        self._clear_pending_dialog()

    def _clear_dialog(self) -> None:
        self.generation += 1
        self.character = None
        self.latest_text = ""
        self.committed_text = ""
        self.conflicting_observation = False
        self.transition_expected = False
        self.last_change_at = None
        self.stable_text = ""
        self.last_stable_change_at = None
        self.history.clear()
        self.next_chunk_ordinal = 1
        self.silent_event_id = None
        self.canonical_line_id = None
        self._clear_pending_dialog()

    def _emit(self, stable_text: str, *, flush: bool) -> list[SpeechChunk]:
        if self.complete_dialogue_only and not flush:
            return []
        if not stable_text.startswith(self.committed_text):
            self.conflicting_observation = True
            return []
        if len(stable_text) <= len(self.committed_text):
            return []

        unspoken_text = stable_text[len(self.committed_text) :]
        boundary = len(unspoken_text) if flush else self._find_boundary(unspoken_text)
        if boundary == 0:
            return []

        text = unspoken_text[:boundary].strip()
        self.committed_text += unspoken_text[:boundary]
        if not text or not any(character.isalnum() for character in text):
            return []

        sentences = (
            [text] if self.complete_dialogue_only else self._split_sentences(text)
        )
        chunks = []
        for sentence in sentences:
            if not any(character.isalnum() for character in sentence):
                continue
            chunks.append(
                SpeechChunk(
                    self.generation,
                    self.character or "Narrator",
                    sentence,
                    ordinal=self.next_chunk_ordinal,
                )
            )
            self.next_chunk_ordinal += 1
        return chunks

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        sentences = []
        start = 0
        for position, character in enumerate(text):
            if character not in ".!?":
                continue
            next_position = position + 1
            if next_position != len(text) and not text[next_position].isspace():
                continue
            sentence = text[start:next_position].strip()
            if sentence:
                sentences.append(sentence)
            start = next_position
        remainder = text[start:].strip()
        if remainder:
            sentences.append(remainder)
        return sentences

    def _find_boundary(self, text: str) -> int:
        sentence_boundary = self._last_punctuation_boundary(text, ".!?")
        if sentence_boundary:
            return sentence_boundary

        # Short clause fragments make XTTS try to continue from a comma or
        # semicolon and can produce buzzing, repeated syllables, or unstable
        # prosody. Live reading therefore waits for a complete sentence or an
        # idle flush by default. The old clause behavior remains opt-in for
        # integrations that explicitly prioritize latency over voice quality.
        if self.complete_sentences_only:
            return 0

        if len(text.strip()) < self.min_chunk_characters:
            return 0
        return self._last_punctuation_boundary(text, ",;:")

    @staticmethod
    def _last_punctuation_boundary(text: str, punctuation: str) -> int:
        boundary = 0
        for position, character in enumerate(text):
            if character not in punctuation:
                continue
            next_position = position + 1
            if next_position == len(text) or text[next_position].isspace():
                boundary = next_position
        return boundary

    @staticmethod
    def _normalize(text: str | None) -> str:
        return " ".join((text or "").split())
