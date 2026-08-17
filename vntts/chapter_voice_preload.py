import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from vntts_artifacts.story_index import StoryIndexError, load_story_index


def _normalize(value):
    return " ".join(re.findall(r"[\w']+", str(value).casefold()))


def _normalize_exact_text(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


@dataclass(frozen=True)
class ChapterDialogue:
    line_id: str | None
    chapter: str
    sequence: int
    speaker: str
    text: str
    text_sha256: str | None
    source_audio_status: str = "unknown"
    source_audio_id: str | None = None
    source_audio_duration_seconds: float | None = None


@dataclass(frozen=True)
class ChapterMatch:
    chapter: str
    sequence: int
    confidence: float


class ChapterVoicePreloader:
    """Infer the current story chapter and rank voices likely to speak next."""

    def __init__(self, dialogue=(), *, lookahead_rows=80):
        self.dialogue = tuple(dialogue)
        self.lookahead_rows = max(1, int(lookahead_rows))
        self.by_speaker = defaultdict(list)
        self.by_chapter = defaultdict(list)
        self.by_exact_dialogue = defaultdict(list)
        self.by_normalized_dialogue = defaultdict(list)
        self.speaker_names = {}
        for row in self.dialogue:
            speaker_key = _normalize(row.speaker)
            self.by_speaker[speaker_key].append(row)
            self.speaker_names.setdefault(speaker_key, row.speaker)
            self.by_chapter[row.chapter].append(row)
            self.by_exact_dialogue[
                (speaker_key, _normalize_exact_text(row.text))
            ].append(row)
            self.by_normalized_dialogue[(speaker_key, _normalize(row.text))].append(row)
        for rows in self.by_chapter.values():
            rows.sort(key=lambda row: row.sequence)
        self.current_match = None

    @classmethod
    def from_document(cls, document, *, lookahead_rows=80):
        rows = []
        for entry in document.get("dialogue", ()) if isinstance(document, dict) else ():
            if not isinstance(entry, dict):
                continue
            chapter = str(entry.get("chapter", "")).strip()
            speaker = str(entry.get("speaker_name") or "").strip()
            text = str(entry.get("text") or "").strip()
            if not chapter or not speaker or not text:
                continue
            try:
                sequence = int(entry.get("sequence", 0))
            except (TypeError, ValueError):
                sequence = 0
            line_id = str(entry.get("line_id") or "").strip() or None
            text_hash = str(entry.get("text_sha256") or "").strip() or None
            source_audio_status = _source_audio_status(entry)
            source_audio_id = (
                str(
                    entry.get("source_audio_id") or entry.get("source_voice_id") or ""
                ).strip()
                or None
            )
            source_audio_duration_seconds = _source_audio_duration_seconds(entry)
            rows.append(
                ChapterDialogue(
                    line_id,
                    chapter,
                    sequence,
                    speaker,
                    text,
                    text_hash,
                    source_audio_status,
                    source_audio_id,
                    source_audio_duration_seconds,
                )
            )
        return cls(rows, lookahead_rows=lookahead_rows)

    @classmethod
    def load_optional(cls, path=None, *, lookahead_rows=80):
        if not path:
            return cls(lookahead_rows=lookahead_rows)
        try:
            metadata, indexed_lines = load_story_index(path)
        except StoryIndexError:
            return cls(lookahead_rows=lookahead_rows)
        needs_source_audio_bridge = bool(
            indexed_lines and not hasattr(indexed_lines[0], "source_audio_status")
        )
        completion_declared = (
            metadata.get("source_audio_completion") == "duration-seconds"
        )
        source_audio_by_line_id = (
            _load_source_audio_extensions(path)
            if needs_source_audio_bridge or completion_declared
            else {}
        )

        def source_audio(line):
            return source_audio_by_line_id.get(
                line.line_id,
                ("unknown", None, None),
            )

        rows = (
            ChapterDialogue(
                line.line_id,
                line.chapter,
                line.sequence,
                line.speaker,
                line.text,
                line.text_sha256,
                getattr(line, "source_audio_status", source_audio(line)[0]),
                getattr(line, "source_audio_id", source_audio(line)[1]),
                getattr(
                    line,
                    "source_audio_duration_seconds",
                    source_audio(line)[2],
                ),
            )
            for line in indexed_lines
        )
        return cls(rows, lookahead_rows=lookahead_rows)

    def resolve_exact(self, character, text):
        """Resolve an OCR line without fuzzy text substitution."""
        line, _result = self.resolve_exact_with_result(character, text)
        return line

    def resolve_exact_with_result(self, character, text):
        """Return an exact line plus an explicit match result for diagnostics."""
        speaker_key = _normalize(character)
        all_candidates = self.by_exact_dialogue.get(
            (speaker_key, _normalize_exact_text(text)),
            (),
        )
        match_result = "exact"
        if not all_candidates:
            all_candidates = self.by_normalized_dialogue.get(
                (speaker_key, _normalize(text)),
                (),
            )
            match_result = "normalized-exact"
        if not all_candidates:
            return None, "no-match"
        candidates = [row for row in all_candidates if row.line_id and row.text_sha256]
        if not candidates:
            return None, "incomplete-identity"
        if len(candidates) == 1:
            selected = candidates[0]
        elif self.current_match is not None:
            nearby = [
                row for row in candidates if row.chapter == self.current_match.chapter
            ]
            if not nearby:
                return None, "ambiguous"
            distances = [
                (abs(row.sequence - self.current_match.sequence), row) for row in nearby
            ]
            closest_distance = min(distance for distance, _row in distances)
            closest = [
                row for distance, row in distances if distance == closest_distance
            ]
            if len(closest) != 1:
                return None, "ambiguous"
            selected = closest[0]
        else:
            return None, "ambiguous"
        self.current_match = ChapterMatch(selected.chapter, selected.sequence, 1.0)
        return selected, match_result

    def resolve_unique_prefix(
        self,
        character,
        text,
        *,
        minimum_characters=20,
        candidate_filter=None,
    ):
        """Resolve one full indexed line from a sufficiently long OCR prefix."""
        speaker_key = _normalize(character)
        prefix = _normalize(text)
        if len(prefix) < minimum_characters:
            return None
        candidates = [
            row
            for row in self.by_speaker.get(speaker_key, ())
            if row.line_id
            and row.text_sha256
            and _normalize(row.text).startswith(prefix)
            and (candidate_filter is None or candidate_filter(row))
        ]
        if self.current_match is not None:
            nearby = [
                row for row in candidates if row.chapter == self.current_match.chapter
            ]
            if nearby:
                candidates = nearby
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        self.current_match = ChapterMatch(selected.chapter, selected.sequence, 1.0)
        return selected

    def is_unique_incomplete_prefix(
        self,
        character,
        text,
        *,
        minimum_characters=10,
    ):
        """Return whether OCR has one known line prefix but not its full text."""
        speaker_key = _normalize(character)
        prefix = _normalize(text)
        if len(prefix) < minimum_characters:
            return False
        candidates = [
            row
            for row in self.by_speaker.get(speaker_key, ())
            if row.line_id
            and row.text_sha256
            and _normalize(row.text).startswith(prefix)
            and _normalize(row.text) != prefix
        ]
        if self.current_match is not None:
            nearby = [
                row for row in candidates if row.chapter == self.current_match.chapter
            ]
            if nearby:
                candidates = nearby
        return len(candidates) == 1

    def canonical_speaker(self, character, *, minimum_similarity=0.86, margin=0.08):
        """Correct a unique, high-confidence OCR drift to a story speaker name."""
        original = str(character or "").strip()
        normalized = _normalize(original)
        if not normalized or normalized == "narrator":
            return original or "Narrator"
        exact = self.speaker_names.get(normalized)
        if exact is not None:
            return exact
        if len(normalized) < 5 or not self.speaker_names:
            return original

        ranked = sorted(
            (
                SequenceMatcher(None, normalized, candidate).ratio(),
                candidate,
            )
            for candidate in self.speaker_names
            if len(candidate) >= 5
        )
        if not ranked:
            return original
        best_score, best_key = ranked[-1]
        second_score = ranked[-2][0] if len(ranked) > 1 else 0.0
        if best_score < minimum_similarity or best_score - second_score < margin:
            return original
        return self.speaker_names[best_key]

    def recommend(self, character, text, *, limit=3):
        if limit <= 0 or not self.dialogue:
            return ()
        match = self._match(character, text)
        if match is not None:
            self.current_match = match
        else:
            match = self.current_match
        if match is None:
            return ()

        current_speaker = _normalize(character)
        chapter_rows = self.by_chapter.get(match.chapter, ())
        nearby = [row for row in chapter_rows if row.sequence >= match.sequence][
            : self.lookahead_rows
        ]
        ranked = []
        seen = {current_speaker, "narrator", ""}
        for row in nearby:
            key = _normalize(row.speaker)
            if key in seen:
                continue
            seen.add(key)
            ranked.append(row.speaker)
            if len(ranked) >= limit:
                return tuple(ranked)

        frequency = Counter(
            row.speaker for row in chapter_rows if _normalize(row.speaker) not in seen
        )
        ranked.extend(speaker for speaker, _count in frequency.most_common())
        return tuple(ranked[:limit])

    def live_voice_preflight_rows(self):
        """Return the current chapter lookahead, or ``None`` before it is known."""
        if not self.dialogue:
            return ()
        if self.current_match is None:
            return None
        rows = self.by_chapter.get(self.current_match.chapter, ())
        return tuple(
            row for row in rows if row.sequence >= self.current_match.sequence
        )[: self.lookahead_rows]

    def _match(self, character, text):
        speaker = _normalize(character)
        normalized_text = _normalize(text)
        candidates = self.by_speaker.get(speaker, ())
        speaker_matched = bool(candidates)
        if not candidates:
            candidates = self.dialogue

        if len(normalized_text) < 8:
            chapters = {row.chapter for row in candidates}
            if speaker_matched and len(chapters) == 1:
                row = candidates[0]
                return ChapterMatch(row.chapter, row.sequence, 0.5)
            return None

        best_row = None
        best_score = 0.0
        for row in candidates:
            candidate_text = _normalize(row.text)
            if not candidate_text:
                continue
            if normalized_text in candidate_text or candidate_text in normalized_text:
                score = min(len(normalized_text), len(candidate_text)) / max(
                    len(normalized_text), len(candidate_text)
                )
                score = max(score, 0.9)
            else:
                score = SequenceMatcher(None, normalized_text, candidate_text).ratio()
            if score > best_score:
                best_row = row
                best_score = score

        minimum = 0.62 if speaker_matched else 0.88
        if best_row is None or best_score < minimum:
            return None
        return ChapterMatch(best_row.chapter, best_row.sequence, best_score)


def _source_audio_status(entry):
    status = str(entry.get("source_audio_status") or "").strip()
    if status:
        return status
    return {
        "configured_unavailable": "unavailable",
        "installed": "available",
        "no_audio": "absent",
        "unchecked": "unknown",
        "unresolved": "unknown",
    }.get(str(entry.get("audio_status") or "").strip(), "unknown")


def _source_audio_duration_seconds(entry):
    value = entry.get("source_audio_duration_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 < value <= 600 else None


def _load_source_audio_extensions(path):
    """Retain optional source-audio fields omitted by older contract readers."""
    result = {}
    try:
        with open(path, encoding="utf-8") as stream:
            next(stream, None)
            for row in stream:
                record = json.loads(row)
                line_id = str(record.get("line_id") or "").strip()
                if not line_id:
                    continue
                source_audio_id = (
                    str(
                        record.get("source_audio_id")
                        or record.get("source_voice_id")
                        or ""
                    ).strip()
                    or None
                )
                result[line_id] = (
                    _source_audio_status(record),
                    source_audio_id,
                    _source_audio_duration_seconds(record),
                )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return result
