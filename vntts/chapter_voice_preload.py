import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from vntts_artifacts.story_index import StoryIndexError, load_story_index


def _normalize(value):
    return " ".join(re.findall(r"[\w']+", str(value).casefold()))


@dataclass(frozen=True)
class ChapterDialogue:
    chapter: str
    sequence: int
    speaker: str
    text: str


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
        for row in self.dialogue:
            self.by_speaker[_normalize(row.speaker)].append(row)
            self.by_chapter[row.chapter].append(row)
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
            rows.append(ChapterDialogue(chapter, sequence, speaker, text))
        return cls(rows, lookahead_rows=lookahead_rows)

    @classmethod
    def load_optional(cls, path=None, *, lookahead_rows=80):
        if not path:
            return cls(lookahead_rows=lookahead_rows)
        try:
            _metadata, indexed_lines = load_story_index(path)
        except StoryIndexError:
            return cls(lookahead_rows=lookahead_rows)
        rows = (
            ChapterDialogue(line.chapter, line.sequence, line.speaker, line.text)
            for line in indexed_lines
        )
        return cls(rows, lookahead_rows=lookahead_rows)

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
