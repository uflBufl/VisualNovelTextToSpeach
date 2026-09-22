import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from os.path import commonprefix
from pathlib import Path
from typing import TypeAlias

from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    load_story_index,
    load_story_index_document,
)

from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
)

VERIFIED_SOURCE_AUDIO_COMPLETION = "verified-media-duration-seconds"
SourceAudioExtension: TypeAlias = tuple[str, str | None, float | None, str, bool]
ResolutionDiagnostics: TypeAlias = dict[str, int | float | str]


def _normalize(value: object) -> str:
    # OCR engines disagree on whether an apostrophe is straight, curly, or a
    # word boundary. Treat every form as a boundary so captured ``it's`` and
    # ``it’s`` compare identically.
    return " ".join(re.findall(r"\w+", str(value).casefold()))


def _normalize_exact_text(value: object) -> str:
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
    source_audio_completeness: str = "unknown"
    story_title: str | None = None
    source_audio_authoritative: bool = False


@dataclass(frozen=True)
class ChapterMatch:
    chapter: str
    sequence: int
    confidence: float


class ChapterVoicePreloader:
    """Infer the current story chapter and rank voices likely to speak next."""

    def __init__(
        self, dialogue: Iterable[ChapterDialogue] = (), *, lookahead_rows: int = 80
    ) -> None:
        self.dialogue = tuple(dialogue)
        self.last_resolution_diagnostics: ResolutionDiagnostics = {}
        self.lookahead_rows = max(1, int(lookahead_rows))
        self.by_speaker: defaultdict[str, list[ChapterDialogue]] = defaultdict(list)
        self.by_line_id: dict[str, ChapterDialogue] = {}
        self.by_chapter: defaultdict[str, list[ChapterDialogue]] = defaultdict(list)
        self.by_chapter_speaker: defaultdict[tuple[str, str], list[ChapterDialogue]] = (
            defaultdict(list)
        )
        self.by_exact_dialogue: defaultdict[tuple[str, str], list[ChapterDialogue]] = (
            defaultdict(list)
        )
        self.by_normalized_dialogue: defaultdict[
            tuple[str, str], list[ChapterDialogue]
        ] = defaultdict(list)
        self.normalized_text: dict[ChapterDialogue, str] = {}
        self.speaker_names: dict[str, str] = {}
        for row in self.dialogue:
            if row.line_id:
                self.by_line_id[row.line_id] = row
            speaker_key = _normalize(row.speaker)
            self.by_speaker[speaker_key].append(row)
            self.speaker_names.setdefault(speaker_key, row.speaker)
            self.by_chapter[row.chapter].append(row)
            self.by_chapter_speaker[(row.chapter, speaker_key)].append(row)
            self.normalized_text[row] = _normalize(row.text)
            self.by_exact_dialogue[
                (speaker_key, _normalize_exact_text(row.text))
            ].append(row)
            self.by_normalized_dialogue[(speaker_key, _normalize(row.text))].append(row)
        for rows in self.by_chapter.values():
            rows.sort(key=lambda row: row.sequence)
        self.current_match: ChapterMatch | None = None

    @classmethod
    def from_document(
        cls, document: Mapping[str, object], *, lookahead_rows: int = 80
    ) -> ChapterVoicePreloader:
        rows: list[ChapterDialogue] = []
        completion_contract = str(document.get("source_audio_completion") or "").strip()
        dialogue = document.get("dialogue", ())
        for raw_entry in dialogue if isinstance(dialogue, Sequence) else ():
            if not isinstance(raw_entry, Mapping):
                continue
            entry = _string_mapping(raw_entry)
            chapter = str(entry.get("chapter", "")).strip()
            speaker = str(entry.get("speaker_name") or "").strip()
            text = str(entry.get("text") or "").strip()
            if not chapter or not speaker or not text:
                continue
            sequence_value = entry.get("sequence", 0)
            try:
                sequence = (
                    int(sequence_value)
                    if isinstance(sequence_value, int | float | str | bytes | bytearray)
                    else 0
                )
            except ValueError:
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
            source_audio_duration_seconds = _source_audio_duration_seconds(
                entry,
                completion_contract=completion_contract or None,
            )
            source_audio_completeness = _source_audio_completeness(
                entry,
                completion_contract=completion_contract or None,
                duration_seconds=source_audio_duration_seconds,
                semantic_authorized=False,
            )
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
                    source_audio_completeness,
                )
            )
        return cls(rows, lookahead_rows=lookahead_rows)

    @classmethod
    def load_optional(
        cls, path: str | Path | None = None, *, lookahead_rows: int = 80
    ) -> ChapterVoicePreloader:
        if not path:
            return cls(lookahead_rows=lookahead_rows)
        document: StoryIndexDocument | None
        try:
            document = load_story_index_document(path)
        except StoryIndexError, ValueError:
            document = None
            try:
                metadata, indexed_lines = load_story_index(path)
            except StoryIndexError:
                return cls(lookahead_rows=lookahead_rows)
        else:
            metadata, indexed_lines = document.metadata, document.records
        lines: Sequence[object] = indexed_lines
        needs_source_audio_bridge = bool(
            lines and not hasattr(lines[0], "source_audio_status")
        )
        completion_contract = str(metadata.get("source_audio_completion") or "").strip()
        story_titles = {
            str(collection.get("collection_id") or "").strip(): str(
                collection.get("title") or ""
            ).strip()
            for raw_collection in metadata.get("collections") or ()
            if isinstance(raw_collection, Mapping)
            for collection in (_string_mapping(raw_collection),)
        }
        completion_declared = completion_contract == VERIFIED_SOURCE_AUDIO_COMPLETION
        authoritative_line_ids = _validated_source_audio_line_ids(path, document)
        if document is not None and completion_declared:
            source_audio_by_line_id = {
                line.line_id: _source_audio_extension(
                    line.document,
                    completion_contract=completion_contract,
                    semantic_authorized=line.line_id in authoritative_line_ids,
                )
                for line in document.records
            }
        elif needs_source_audio_bridge or completion_declared:
            source_audio_by_line_id = _load_source_audio_extensions(
                path,
                completion_contract=completion_contract or None,
            )
        else:
            source_audio_by_line_id = {}

        def source_audio(line: object) -> SourceAudioExtension:
            return source_audio_by_line_id.get(
                _line_text(line, "line_id"),
                ("unknown", None, None, "unknown", False),
            )

        rows = (
            ChapterDialogue(
                _line_optional_text(line, "line_id"),
                _line_text(line, "chapter"),
                _line_integer(line, "sequence"),
                _line_text(line, "speaker"),
                _line_text(line, "text"),
                _line_optional_text(line, "text_sha256"),
                _line_text(line, "source_audio_status", source_audio(line)[0]),
                _line_optional_text(line, "source_audio_id", source_audio(line)[1]),
                source_audio(line)[2],
                source_audio(line)[3],
                story_titles.get(_line_text(line, "collection_id")),
                source_audio(line)[4],
            )
            for line in lines
        )
        return cls(rows, lookahead_rows=lookahead_rows)

    def resolve_exact(self, character: object, text: object) -> ChapterDialogue | None:
        """Resolve an OCR line without fuzzy text substitution."""
        line, _result = self.resolve_exact_with_result(character, text)
        return line

    def line_for_id(self, line_id: object) -> ChapterDialogue | None:
        return self.by_line_id.get(str(line_id))

    def story_title_for(self, chapter: str, line_id: object = None) -> str | None:
        line = self.line_for_id(line_id)
        if line is not None:
            return line.story_title
        # Silent events have no line ID. Only an unambiguous chapter identifies
        # their story; a chapter may contain several distinct collections.
        titles = {row.story_title for row in self.by_chapter.get(chapter, ())}
        return next(iter(titles)) if len(titles) == 1 else None

    def select_line_id(self, line_id: object) -> ChapterDialogue | None:
        """Select one checksum-bound canonical line without text re-resolution."""
        line = self.line_for_id(line_id)
        if line is None or not line.line_id or not line.text_sha256:
            return None
        self.current_match = ChapterMatch(line.chapter, line.sequence, 1.0)
        return line

    def resolve_exact_with_result(
        self, character: object, text: object
    ) -> tuple[ChapterDialogue | None, str]:
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

    def resolve_exact_among(
        self, character: object, text: object, line_ids: Iterable[object]
    ) -> tuple[ChapterDialogue | None, str]:
        """Resolve one OCR observation only among explicit cursor candidates."""
        allowed = {str(line_id) for line_id in line_ids if line_id}
        if not allowed:
            return None, "no-expected-candidates"
        exact_text = _normalize_exact_text(text)
        normalized_text = _normalize(text)
        speaker_key = _normalize(character)
        candidates = [
            row
            for line_id in allowed
            if (row := self.by_line_id.get(line_id)) is not None
            and row.text_sha256
            and _normalize_exact_text(row.text) == exact_text
            and _normalize(row.speaker) == speaker_key
        ]
        match_result = "expected-exact"
        if not candidates:
            candidates = [
                row
                for line_id in allowed
                if (row := self.by_line_id.get(line_id)) is not None
                and row.text_sha256
                and _normalize(row.text) == normalized_text
                and _normalize(row.speaker) == speaker_key
            ]
            match_result = "expected-normalized-exact"
        if not candidates:
            candidates = [
                row
                for line_id in allowed
                if (row := self.by_line_id.get(line_id)) is not None
                and row.text_sha256
                and _normalize_exact_text(row.text) == exact_text
            ]
            match_result = "expected-text-only"
        if len(candidates) != 1:
            return None, "expected-ambiguous" if candidates else "expected-no-match"
        selected = candidates[0]
        self.current_match = ChapterMatch(selected.chapter, selected.sequence, 1.0)
        return selected, match_result

    def resolve_bounded_among(
        self,
        character: object,
        text: object,
        line_ids: Iterable[object],
        *,
        allow_speaker_evidence: bool = True,
    ) -> tuple[ChapterDialogue | None, str]:
        """Resolve OCR drift only among explicit cursor-authorized line IDs."""
        allowed = tuple(dict.fromkeys(str(line_id) for line_id in line_ids if line_id))
        allowed_set = set(allowed)
        valid = tuple(
            row
            for row in self.dialogue
            if row.line_id in allowed_set and row.text_sha256
        )
        exact_text = _normalize_exact_text(text)
        normalized_text = _normalize(text)
        speaker_key = _normalize(character)
        self.last_resolution_diagnostics = {
            "indexed_line_count": len(self.dialogue),
            "indexed_chapter_count": len(self.by_chapter),
            "allowed_line_count": len(allowed),
            "eligible_line_count": len(valid),
            "normalized_text_characters": len(normalized_text),
            "normalized_text_tokens": len(normalized_text.split()),
            "normalized_text_sha256": sha256(normalized_text.encode()).hexdigest(),
            "normalized_speaker_sha256": sha256(speaker_key.encode()).hexdigest(),
            "speaker_candidate_count": sum(
                _normalize(candidate.speaker) == speaker_key for candidate in valid
            ),
            "missing_identity_candidate_count": len(allowed) - len(valid),
            "exact_speaker_candidate_count": sum(
                _normalize(candidate.speaker) == speaker_key
                and _normalize_exact_text(candidate.text) == exact_text
                for candidate in valid
            ),
            "normalized_speaker_candidate_count": sum(
                _normalize(candidate.speaker) == speaker_key
                and self.normalized_text[candidate] == normalized_text
                for candidate in valid
            ),
            "text_only_candidate_count": sum(
                _normalize_exact_text(candidate.text) == exact_text
                for candidate in valid
            ),
        }
        line, match_result = self.resolve_exact_among(character, text, allowed)
        if line is not None or not allowed:
            self.last_resolution_diagnostics["match_result"] = match_result
            if line is None:
                self.last_resolution_diagnostics["candidate_rejection_reason"] = (
                    "no-expected-candidates"
                )
            return line, match_result
        ranked: list[tuple[float, str, ChapterDialogue, str]] = []
        best_evidence: tuple[dict[str, float], str] | None = None
        for line_id in allowed:
            candidate = self.by_line_id.get(line_id)
            if candidate is None or not candidate.text_sha256:
                continue
            evidence: dict[str, float] = {}
            match = _bounded_text_match(text, candidate.text, evidence=evidence)
            if evidence and (
                best_evidence is None
                or evidence["similarity"] > best_evidence[0]["similarity"]
            ):
                best_evidence = (evidence, line_id)
            if match is None and allow_speaker_evidence:
                match = _speaker_bounded_text_match(
                    character,
                    text,
                    candidate.speaker,
                    candidate.text,
                )
            if match is not None:
                score, method = match
                if method == "expected-bounded-ocr-suffix":
                    normalized = _normalize(candidate.text)
                    if any(
                        other_id != line_id
                        and (other := self.by_line_id.get(other_id)) is not None
                        and _normalize(other.text).startswith(f"{normalized} ")
                        for other_id in allowed
                    ):
                        continue
                ranked.append((score, line_id, candidate, method))
        self.last_resolution_diagnostics["bounded_candidate_count"] = len(ranked)
        if best_evidence is not None:
            evidence, line_id = best_evidence
            self.last_resolution_diagnostics.update(
                best_candidate_line_id=line_id,
                best_bounded_similarity=round(evidence["similarity"], 4),
                best_bounded_coverage=round(evidence["coverage"], 4),
            )
        if not ranked:
            self.last_resolution_diagnostics.update(
                match_result="expected-no-match",
                candidate_rejection_reason="bounded-threshold-not-met",
            )
            return None, "expected-no-match"
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        best = ranked[0]
        if len(ranked) > 1 and best[0] - ranked[1][0] < 0.08:
            self.last_resolution_diagnostics.update(
                match_result="expected-ambiguous",
                candidate_rejection_reason="competing-candidates",
            )
            return None, "expected-ambiguous"
        selected = best[2]
        self.current_match = ChapterMatch(selected.chapter, selected.sequence, 1.0)
        self.last_resolution_diagnostics["match_result"] = best[3]
        return selected, best[3]

    def resolve_unique_prefix(
        self,
        character: object,
        text: object,
        *,
        minimum_characters: int = 20,
        candidate_filter: Callable[[ChapterDialogue], bool] | None = None,
    ) -> ChapterDialogue | None:
        """Resolve one full indexed line from a sufficiently long OCR prefix."""
        speaker_key = _normalize(character)
        prefix = _normalize(text)
        if len(prefix) < minimum_characters:
            return None
        candidates = self._prefix_candidates(
            speaker_key,
            prefix,
            candidate_filter=candidate_filter,
        )
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        self.current_match = ChapterMatch(selected.chapter, selected.sequence, 1.0)
        return selected

    def resolve_unique_prefix_by_text(
        self, text: object, *, minimum_characters: int = 20
    ) -> ChapterDialogue | None:
        """Resolve one indexed line when OCR lost or corrupted its nameplate."""
        prefix = _normalize(text)
        if len(prefix) < minimum_characters:
            return None

        def matches(rows: Iterable[ChapterDialogue]) -> list[ChapterDialogue]:
            return [
                row
                for row in rows
                if row.line_id
                and row.text_sha256
                and self.normalized_text[row].startswith(prefix)
            ]

        if self.current_match is not None:
            nearby = matches(self.by_chapter.get(self.current_match.chapter, ()))
            if len(nearby) == 1:
                selected = nearby[0]
                self.current_match = ChapterMatch(
                    selected.chapter,
                    selected.sequence,
                    1.0,
                )
                return selected
            return None

        candidates = matches(self.dialogue)
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        self.current_match = ChapterMatch(selected.chapter, selected.sequence, 1.0)
        return selected

    def is_unique_incomplete_prefix(
        self,
        character: object,
        text: object,
        *,
        minimum_characters: int = 10,
    ) -> bool:
        """Return whether OCR has one known line prefix but not its full text."""
        speaker_key = _normalize(character)
        prefix = _normalize(text)
        if len(prefix) < minimum_characters:
            return False
        candidates = self._prefix_candidates(
            speaker_key,
            prefix,
            incomplete_only=True,
        )
        return len(candidates) == 1

    def _prefix_candidates(
        self,
        speaker_key: str,
        prefix: str,
        *,
        incomplete_only: bool = False,
        candidate_filter: Callable[[ChapterDialogue], bool] | None = None,
    ) -> list[ChapterDialogue]:
        def matches(rows: Iterable[ChapterDialogue]) -> list[ChapterDialogue]:
            return [
                row
                for row in rows
                if row.line_id
                and row.text_sha256
                and self.normalized_text[row].startswith(prefix)
                and (not incomplete_only or self.normalized_text[row] != prefix)
                and (candidate_filter is None or candidate_filter(row))
            ]

        if self.current_match is not None:
            nearby = matches(
                self.by_chapter_speaker.get(
                    (self.current_match.chapter, speaker_key),
                    (),
                )
            )
            if nearby:
                return nearby
        return matches(self.by_speaker.get(speaker_key, ()))

    def canonical_speaker(
        self,
        character: object,
        *,
        minimum_similarity: float = 0.86,
        margin: float = 0.08,
    ) -> str:
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

    def recommend(
        self, character: object, text: object, *, limit: int = 3
    ) -> tuple[str, ...]:
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

    def live_voice_preflight_rows(self) -> tuple[ChapterDialogue, ...] | None:
        """Return the current chapter lookahead, or ``None`` before it is known."""
        if not self.dialogue:
            return ()
        if self.current_match is None:
            return None
        rows = self.by_chapter.get(self.current_match.chapter, ())
        return tuple(
            row for row in rows if row.sequence >= self.current_match.sequence
        )[: self.lookahead_rows]

    def _match(self, character: object, text: object) -> ChapterMatch | None:
        speaker = _normalize(character)
        normalized_text = _normalize(text)
        candidates: Sequence[ChapterDialogue] = self.by_speaker.get(speaker, ())
        speaker_matched = bool(candidates)
        if not candidates:
            candidates = self.dialogue

        if len(normalized_text) < 8:
            chapters = {row.chapter for row in candidates}
            if speaker_matched and len(chapters) == 1:
                row = next(iter(candidates))
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


def _bounded_text_match(
    observed: object,
    canonical: object,
    *,
    evidence: dict[str, float] | None = None,
) -> tuple[float, str] | None:
    canonical_text = _normalize(str(canonical).replace("_", " "))
    tokens = _normalize(str(observed).replace("_", " ")).split()
    if not canonical_text or not tokens:
        return None
    best = None
    # A dialogue crop occasionally includes the nameplate plus a few decorative
    # OCR fragments before the actual line. Long canonical prefixes remain
    # strong evidence after discarding that bounded leading noise.
    for dropped in range(min(8, len(tokens))):
        candidate = " ".join(tokens[dropped:])
        if canonical_text.startswith(candidate) and len(candidate) >= 20:
            coverage = min(1.0, len(candidate) / len(canonical_text))
            match = (1.0 + coverage / 10, "expected-bounded-prefix")
        elif (
            candidate.startswith(canonical_text)
            and len(canonical_text) >= 7
            and len(candidate) - len(canonical_text)
            <= max(16, round(len(canonical_text) * 0.5))
        ):
            match = (1.05, "expected-bounded-ocr-suffix")
        else:
            coverage = min(len(candidate), len(canonical_text)) / max(
                len(candidate), len(canonical_text)
            )
            similarity = SequenceMatcher(None, candidate, canonical_text).ratio()
            if evidence is not None and similarity > evidence.get("similarity", 0.0):
                evidence.update(similarity=similarity, coverage=coverage)
            if len(candidate) < 20 or coverage < 0.65 or similarity < 0.88:
                continue
            match = (similarity, "expected-bounded-similarity")
        if best is None or match[0] > best[0]:
            best = match
    return best


def _speaker_bounded_text_match(
    observed_character: object,
    observed_text: object,
    canonical_speaker: object,
    canonical_text: object,
) -> tuple[float, str] | None:
    """Match nameplate-contaminated or truncated OCR in a bounded frontier."""
    observed = _normalize(observed_text)
    canonical = _normalize(canonical_text)
    speaker = _normalize(canonical_speaker)
    observed_speaker = _normalize(observed_character)
    if not observed or not canonical or not speaker:
        return None
    speaker_in_text = f" {speaker} " in f" {observed} "
    speaker_matches = observed_speaker == speaker
    if not (speaker_in_text or speaker_matches):
        return None
    specific_speaker = speaker != "narrator" and (speaker_in_text or speaker_matches)
    if speaker_in_text:
        observed = " ".join(observed.replace(speaker, " ", 1).split())
    best = None
    tokens = observed.split()
    for dropped in range(min(8, len(tokens))):
        candidate = " ".join(tokens[dropped:])
        if not candidate:
            continue
        if canonical in candidate and (len(canonical) >= 7 or specific_speaker):
            match = (1.2, "expected-bounded-speaker-text")
        else:
            prefix_length = len(commonprefix((candidate, canonical)))
            if (
                specific_speaker
                and len(canonical) <= 12
                and prefix_length >= 3
                and prefix_length / len(canonical) >= 0.6
            ):
                match = (
                    1.1 + prefix_length / len(canonical) / 100,
                    "expected-bounded-speaker-prefix",
                )
            elif len(canonical) > 12 and prefix_length >= 12:
                match = (
                    1.05 + min(1.0, prefix_length / len(canonical)) / 100,
                    "expected-bounded-speaker-prefix",
                )
            else:
                coverage = min(len(candidate), len(canonical)) / max(
                    len(candidate), len(canonical)
                )
                similarity = SequenceMatcher(None, candidate, canonical).ratio()
                if len(candidate) < 20 or coverage < 0.65 or similarity < 0.8:
                    continue
                match = (similarity, "expected-bounded-speaker-similarity")
        if best is None or match[0] > best[0]:
            best = match
    return best


def _source_audio_status(entry: Mapping[str, object]) -> str:
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


def _source_audio_duration_seconds(
    entry: Mapping[str, object], *, completion_contract: str | None = None
) -> float | None:
    value = entry.get("source_audio_duration_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 600:
        return None
    if completion_contract != VERIFIED_SOURCE_AUDIO_COMPLETION:
        return None
    media_id = entry.get("source_audio_duration_media_id")
    media_sha256 = str(entry.get("source_audio_duration_media_sha256") or "").strip()
    sample_rate = entry.get("source_audio_duration_sample_rate")
    sample_count = entry.get("source_audio_duration_sample_count")
    decoder = str(entry.get("source_audio_duration_decoder") or "").strip()
    if (
        isinstance(media_id, bool)
        or not isinstance(media_id, int)
        or isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or sample_rate <= 0
        or isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count <= 0
        or re.fullmatch(r"[0-9a-f]{64}", media_sha256) is None
        or not decoder
    ):
        return None
    if entry.get("source_media_ids") != [media_id] or entry.get(
        "available_media_ids"
    ) != [media_id]:
        return None
    measured = sample_count / sample_rate
    return value if math.isclose(value, measured, rel_tol=0, abs_tol=0.000001) else None


def _source_audio_completeness(
    entry: Mapping[str, object],
    *,
    completion_contract: str | None = None,
    duration_seconds: float | None = None,
    semantic_authorized: bool = False,
) -> str:
    if (
        duration_seconds is None
        or completion_contract != VERIFIED_SOURCE_AUDIO_COMPLETION
        or not semantic_authorized
    ):
        return "unknown"
    value = str(entry.get("source_audio_completeness") or "unknown").strip()
    expected_reason = {
        "full": "exact-normalized-asr-transcript",
        "partial": "asr-transcript-mismatch",
    }.get(value)
    return (
        value
        if expected_reason is not None
        and entry.get("source_audio_completeness_reason") == expected_reason
        else "unknown"
    )


def _source_audio_extension(
    entry: Mapping[str, object],
    *,
    completion_contract: str | None = None,
    semantic_authorized: bool = False,
) -> SourceAudioExtension:
    source_audio_id = (
        str(entry.get("source_audio_id") or entry.get("source_voice_id") or "").strip()
        or None
    )
    duration_seconds = _source_audio_duration_seconds(
        entry,
        completion_contract=completion_contract,
    )
    completeness = _source_audio_completeness(
        entry,
        completion_contract=completion_contract,
        duration_seconds=duration_seconds,
        semantic_authorized=semantic_authorized,
    )
    return (
        _source_audio_status(entry),
        source_audio_id,
        duration_seconds,
        completeness,
        completeness in {"full", "partial"},
    )


def _validated_source_audio_line_ids(
    path: str | Path, document: StoryIndexDocument | None = None
) -> frozenset[str]:
    if document is None:
        return frozenset()
    if (
        document.metadata.get("source_audio_completion")
        != VERIFIED_SOURCE_AUDIO_COMPLETION
    ):
        return frozenset()
    evidence_path = Path(path).expanduser().resolve().parent / (
        "source-audio-semantic-evidence.json"
    )
    try:
        load_source_audio_semantic_evidence(evidence_path, document)
    except OSError, SourceAudioSemanticEvidenceError:
        return frozenset()
    return frozenset(
        record.line_id
        for record in document.records
        if record.document.get("source_audio_semantic_evidence_entry_id") is not None
    )


def _source_audio_covers_full_line(
    entry: Mapping[str, object],
    *,
    completion_contract: str | None,
    semantic_authorized: bool,
) -> bool:
    return (
        _source_audio_status(entry) == "available"
        and _source_audio_completeness(
            entry,
            completion_contract=completion_contract,
            duration_seconds=_source_audio_duration_seconds(
                entry,
                completion_contract=completion_contract,
            ),
            semantic_authorized=semantic_authorized,
        )
        == "full"
    )


def _load_source_audio_extensions(
    path: str | Path, *, completion_contract: str | None = None
) -> dict[str, SourceAudioExtension]:
    """Retain optional source-audio fields omitted by older contract readers."""
    result: dict[str, SourceAudioExtension] = {}
    try:
        with open(path, encoding="utf-8") as stream:
            next(stream, None)
            for row in stream:
                record = _string_mapping(json.loads(row))
                line_id = str(record.get("line_id") or "").strip()
                if not line_id:
                    continue
                result[line_id] = _source_audio_extension(
                    record,
                    completion_contract=completion_contract,
                    semantic_authorized=False,
                )
    except OSError, TypeError, ValueError, json.JSONDecodeError:
        return {}
    return result


def _string_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _line_value(line: object, name: str, default: object = None) -> object:
    value: object = getattr(line, name, default)
    return value


def _line_text(line: object, name: str, default: str = "") -> str:
    value = _line_value(line, name, default)
    return value if isinstance(value, str) else default


def _line_optional_text(
    line: object, name: str, default: str | None = None
) -> str | None:
    value = _line_value(line, name, default)
    return value if isinstance(value, str) and value else default


def _line_integer(line: object, name: str) -> int:
    value = _line_value(line, name, 0)
    return int(value) if isinstance(value, int | float | str | bytes | bytearray) else 0
