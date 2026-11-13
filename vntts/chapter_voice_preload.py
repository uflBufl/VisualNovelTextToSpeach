import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path
from typing import TypeAlias

from durable_file import atomic_output_path
from vntts_artifacts.story_index import (
    StoryIndexDocument,
    StoryIndexError,
    StoryIndexRecord,
    load_story_index,
    load_story_index_document,
)

from vntts.cleanup import temporary_directory
from vntts.dialog import is_symbol_only_text, symbol_text_key
from vntts.document_identity import is_lowercase_sha256
from vntts.json_types import decode_json
from vntts.path_safety import copy_regular_file, open_regular_text, read_regular_bytes
from vntts.source_audio_semantics import (
    SourceAudioSemanticEvidenceError,
    load_source_audio_semantic_evidence,
)
from vntts.story_text_matching import (
    MatchingText,
    TextEvidence,
    compare_text,
    prepare_text,
)

VERIFIED_SOURCE_AUDIO_COMPLETION = "verified-media-duration-seconds"
SourceAudioExtension: TypeAlias = tuple[str, str | None, float | None, str, bool]
ResolutionDiagnostics: TypeAlias = dict[str, int | float | str]


def _normalize(value: object) -> str:
    if is_symbol_only_text(value):
        return ""
    # OCR engines disagree on whether an apostrophe is straight, curly, or a
    # word boundary. Treat every form as a boundary so captured ``it's`` and
    # ``it’s`` compare identically.
    return " ".join(re.findall(r"\w+", str(value).casefold()))


def _normalize_exact_text(value: object) -> str:
    if is_symbol_only_text(value):
        return symbol_text_key(str(value))
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
    story_order: int | None = None

    @property
    def has_full_source_audio(self) -> bool:
        return (
            self.source_audio_status == "available"
            and self.source_audio_authoritative
            and self.source_audio_completeness == "full"
            and self.source_audio_duration_seconds is not None
        )


@dataclass(frozen=True)
class StoryObservationMatch:
    line: ChapterDialogue | None
    method: str
    complete: bool = False
    prefix: bool = False


@dataclass(frozen=True)
class _CandidateEvidence:
    line: ChapterDialogue
    observed: str
    evidence: TextEvidence
    noise: bool = False
    speaker_in_crop: bool = False
    suffix_trimmed: bool = False


@dataclass(frozen=True)
class ChapterMatch:
    chapter: str
    sequence: int
    confidence: float
    story_order: int | None = None


def _story_order(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _story_position(row: ChapterDialogue | ChapterMatch) -> int:
    return row.story_order if row.story_order is not None else row.sequence


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
        self.by_exact_dialogue: defaultdict[
            tuple[str, bool, str], list[ChapterDialogue]
        ] = defaultdict(list)
        self.by_normalized_dialogue: defaultdict[
            tuple[str, str], list[ChapterDialogue]
        ] = defaultdict(list)
        self.normalized_text: dict[ChapterDialogue, str] = {}
        self.matching_text: dict[ChapterDialogue, MatchingText] = {}
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
            self.matching_text[row] = prepare_text(row.text)
            self.by_exact_dialogue[
                (
                    speaker_key,
                    is_symbol_only_text(row.text),
                    _normalize_exact_text(row.text),
                )
            ].append(row)
            self.by_normalized_dialogue[
                (speaker_key, self.normalized_text[row])
            ].append(row)
        for rows in self.by_chapter.values():
            rows.sort(key=_story_position)
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
            (
                source_audio_status,
                source_audio_id,
                source_audio_duration_seconds,
                source_audio_completeness,
                source_audio_authoritative,
            ) = _source_audio_extension(
                entry,
                completion_contract=completion_contract or None,
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
                    source_audio_authoritative=source_audio_authoritative,
                    story_order=_story_order(entry.get("story_order")),
                )
            )
        return cls(rows, lookahead_rows=lookahead_rows)

    @classmethod
    def load_snapshot(
        cls, path: str | Path, payload: bytes, *, lookahead_rows: int = 80
    ) -> ChapterVoicePreloader:
        """Load captured story bytes with their optional checksum-bound evidence."""
        with temporary_directory(prefix="vntts-story-snapshot-") as directory:
            snapshot = Path(directory) / "story-index.jsonl"
            snapshot.write_bytes(payload)
            evidence = Path(path).with_name("source-audio-semantic-evidence.json")
            try:
                if evidence.is_file() and not evidence.is_symlink():
                    with atomic_output_path(
                        snapshot.with_name(evidence.name)
                    ) as staged:
                        copy_regular_file(evidence, staged)
            except OSError:
                # Unavailable optional evidence cannot authorise source audio.
                pass
            return cls.load_optional(snapshot, lookahead_rows=lookahead_rows)

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

        def rows() -> Iterable[ChapterDialogue]:
            for line in lines:
                (
                    source_audio_status,
                    source_audio_id,
                    source_audio_duration_seconds,
                    source_audio_completeness,
                    source_audio_authoritative,
                ) = source_audio_by_line_id.get(
                    _line_text(line, "line_id"),
                    ("unknown", None, None, "unknown", False),
                )
                yield ChapterDialogue(
                    _line_optional_text(line, "line_id"),
                    _line_text(line, "chapter"),
                    _line_integer(line, "sequence"),
                    _line_text(line, "speaker"),
                    _line_text(line, "text"),
                    _line_optional_text(line, "text_sha256"),
                    _line_text(line, "source_audio_status", source_audio_status),
                    _line_optional_text(line, "source_audio_id", source_audio_id),
                    source_audio_duration_seconds,
                    source_audio_completeness,
                    story_titles.get(_line_text(line, "collection_id")),
                    source_audio_authoritative,
                    _story_order(
                        _string_mapping(_line_value(line, "document")).get(
                            "story_order"
                        )
                    ),
                )

        return cls(rows(), lookahead_rows=lookahead_rows)

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
        self.current_match = ChapterMatch(
            line.chapter, line.sequence, 1.0, line.story_order
        )
        return line

    def resolve_exact_with_result(
        self, character: object, text: object
    ) -> tuple[ChapterDialogue | None, str]:
        """Return an exact line plus an explicit match result for diagnostics."""
        speaker_key = _normalize(character)
        exact_text = _normalize_exact_text(text)
        if not exact_text:
            return None, "no-match"
        all_candidates = self.by_exact_dialogue.get(
            (speaker_key, is_symbol_only_text(text), exact_text),
            (),
        )
        match_result = "exact"
        normalized_text = _normalize(text)
        if not all_candidates and normalized_text:
            all_candidates = self.by_normalized_dialogue.get(
                (speaker_key, normalized_text),
                (),
            )
            match_result = "normalized-exact"
        if match_result == "normalized-exact":
            observed = prepare_text(text)
            all_candidates = tuple(
                row
                for row in all_candidates
                if compare_text(observed, self.matching_text[row], limit=0).complete
            )
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
                (abs(_story_position(row) - _story_position(self.current_match)), row)
                for row in nearby
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
        self.current_match = ChapterMatch(
            selected.chapter, selected.sequence, 1.0, selected.story_order
        )
        return selected, match_result

    def equivalent_speech_candidates(
        self,
        character: object,
        text: object,
        *,
        line_id: str | None = None,
    ) -> tuple[ChapterDialogue, ...]:
        """Return exact speech equivalents without assigning canonical ownership."""
        speaker = _normalize(character)
        exact_text = _normalize_exact_text(text)
        if not speaker or not exact_text:
            return ()
        symbol_only = is_symbol_only_text(text)
        observed = prepare_text(text)
        candidates = tuple(
            row
            for row in self.by_exact_dialogue.get(
                (speaker, symbol_only, exact_text), ()
            )
            if row.line_id
            and row.text_sha256
            and not row.has_full_source_audio
            and (
                symbol_only
                or compare_text(observed, self.matching_text[row], limit=0).complete
            )
        )
        if not candidates:
            return ()
        if line_id is not None:
            return candidates if self.line_for_id(line_id) in candidates else ()

        scope = (
            self.dialogue
            if self.current_match is None
            else self.by_chapter.get(self.current_match.chapter, ())
        )
        representatives: dict[tuple[str, bool, str], ChapterDialogue] = {}
        for row in scope:
            if row.line_id and row.text_sha256 and not row.has_full_source_audio:
                representatives.setdefault(
                    (
                        _normalize(row.speaker),
                        is_symbol_only_text(row.text),
                        _normalize_exact_text(row.text),
                    ),
                    row,
                )
        # Duplicate positions share speech content, while continuations still
        # compete through the ordinary completeness guards.
        diagnostics = self.last_resolution_diagnostics
        try:
            match = self.resolve_observation_among(
                character,
                text,
                (row.line_id for row in representatives.values()),
                update_context=False,
            )
        finally:
            self.last_resolution_diagnostics = diagnostics
        return (
            candidates
            if match.complete
            and match.method == "expected-exact"
            and match.line in candidates
            else ()
        )

    def resolve_exact_among(
        self,
        character: object,
        text: object,
        line_ids: Iterable[object],
        *,
        update_context: bool = True,
    ) -> tuple[ChapterDialogue | None, str]:
        """Resolve one OCR observation only among explicit cursor candidates."""
        allowed = {str(line_id) for line_id in line_ids if line_id}
        if not allowed:
            return None, "no-expected-candidates"
        exact_text = _normalize_exact_text(text)
        normalized_text = _normalize(text)
        speaker_key = _normalize(character)
        valid = tuple(
            row
            for line_id in allowed
            if (row := self.by_line_id.get(line_id)) is not None
            and row.text_sha256
            and is_symbol_only_text(row.text) == is_symbol_only_text(text)
        )
        candidates = []
        shape = prepare_text(text)
        match_result = "expected-no-match"
        for exact, observed, require_speaker, result in (
            (True, exact_text, True, "expected-exact"),
            (True, exact_text, False, "expected-text-only"),
            (False, normalized_text, True, "expected-normalized-exact"),
            (False, normalized_text, False, "expected-normalized-text-only"),
        ):
            if not observed:
                continue
            candidates = [
                row
                for row in valid
                if (
                    _normalize_exact_text(row.text)
                    if exact
                    else self.normalized_text[row]
                )
                == observed
                and (not require_speaker or _normalize(row.speaker) == speaker_key)
                and (
                    exact
                    or compare_text(shape, self.matching_text[row], limit=0).complete
                )
            ]
            if candidates:
                match_result = result
                break
        if len(candidates) != 1:
            return None, "expected-ambiguous" if candidates else "expected-no-match"
        selected = candidates[0]
        if update_context:
            self.current_match = ChapterMatch(
                selected.chapter, selected.sequence, 1.0, selected.story_order
            )
        return selected, match_result

    def resolve_complete_among(
        self,
        character: object,
        text: object,
        line_ids: Iterable[object],
        *,
        update_context: bool = True,
        prefer_synthesis: bool = False,
    ) -> tuple[ChapterDialogue | None, str]:
        match = self.resolve_observation_among(
            character,
            text,
            line_ids,
            update_context=update_context,
            prefer_synthesis=prefer_synthesis,
        )
        return match.line, match.method

    def resolve_bounded_among(
        self,
        character: object,
        text: object,
        line_ids: Iterable[object],
        *,
        allow_speaker_evidence: bool = True,
    ) -> tuple[ChapterDialogue | None, str]:
        match = self.resolve_observation_among(
            character,
            text,
            line_ids,
            allow_prefix=True,
            recover_crop_noise=True,
            allow_speaker_evidence=allow_speaker_evidence,
        )
        return match.line, match.method

    def resolve_observation_among(
        self,
        character: object,
        text: object,
        line_ids: Iterable[object],
        *,
        allow_prefix: bool = False,
        recover_crop_noise: bool = False,
        allow_speaker_evidence: bool = True,
        update_context: bool = True,
        prefer_synthesis: bool = False,
    ) -> StoryObservationMatch:
        """Assess the same source coverage for canonical text and cursor ownership."""
        allowed = tuple(dict.fromkeys(str(value) for value in line_ids if value))
        valid = tuple(
            row
            for line_id in allowed
            if (row := self.by_line_id.get(line_id)) is not None and row.text_sha256
        )
        if prefer_synthesis:
            # Originals remain trackable, but cannot compete for synthesis.
            eligible = tuple(row for row in valid if not row.has_full_source_audio)
            originals = tuple(row for row in valid if row.has_full_source_audio)

            def resolve(rows: Iterable[ChapterDialogue]) -> StoryObservationMatch:
                return self.resolve_observation_among(
                    character,
                    text,
                    (row.line_id for row in rows),
                    allow_prefix=allow_prefix,
                    recover_crop_noise=recover_crop_noise,
                    allow_speaker_evidence=allow_speaker_evidence,
                    update_context=False,
                )

            match = resolve(eligible)
            speaker = _normalize(character) if allow_speaker_evidence else ""
            if match.method == "expected-no-match" and originals:
                match = resolve(originals)
            elif (
                match.line is not None
                and speaker
                and _normalize(match.line.speaker) != speaker
            ):
                named_originals = tuple(
                    row for row in originals if _normalize(row.speaker) == speaker
                )
                if named_originals:
                    # Filtering must not turn a known nameplate into another role.
                    diagnostics = self.last_resolution_diagnostics.copy()
                    canonical = resolve((*eligible, *named_originals))
                    if canonical.line is None or canonical.line.has_full_source_audio:
                        match = canonical
                    else:
                        self.last_resolution_diagnostics = diagnostics
            if update_context and match.line is not None:
                self.select_line_id(match.line.line_id)
            return match
        speaker = _normalize(character) if allow_speaker_evidence else ""
        normalized = _normalize(text)
        observed = prepare_text(text)
        self.last_resolution_diagnostics = {
            "indexed_line_count": len(self.dialogue),
            "indexed_chapter_count": len(self.by_chapter),
            "allowed_line_count": len(allowed),
            "eligible_line_count": len(valid),
            "normalized_text_characters": len(normalized),
            "normalized_text_tokens": len(normalized.split()),
            "normalized_text_sha256": sha256(normalized.encode()).hexdigest(),
            "normalized_speaker_sha256": sha256(speaker.encode()).hexdigest(),
            "speaker_candidate_count": sum(
                _normalize(row.speaker) == speaker for row in valid
            ),
            "missing_identity_candidate_count": len(allowed) - len(valid),
        }

        def finish(match: StoryObservationMatch) -> StoryObservationMatch:
            self.last_resolution_diagnostics["match_result"] = match.method
            if match.line is None:
                self.last_resolution_diagnostics["candidate_rejection_reason"] = (
                    "competing-candidates"
                    if match.method == "expected-ambiguous"
                    else "incomplete-typewriter-text"
                    if match.method == "expected-incomplete"
                    else "bounded-threshold-not-met"
                    if allowed
                    else "no-expected-candidates"
                )
            elif update_context:
                row = match.line
                self.current_match = ChapterMatch(
                    row.chapter, row.sequence, 1.0, row.story_order
                )
            return match

        if is_symbol_only_text(text):
            row, method = self.resolve_exact_among(
                speaker, text, (row.line_id for row in valid), update_context=False
            )
            return finish(StoryObservationMatch(row, method, complete=row is not None))
        valid = tuple(row for row in valid if not is_symbol_only_text(row.text))
        if not valid or not observed.alphanumeric_length:
            return finish(StoryObservationMatch(None, "expected-no-match"))

        def budget(row: ChapterDialogue) -> int:
            return sum(
                self.matching_text[row].alphanumeric_length >= boundary
                for boundary in (3, 60, 120)
            )

        prepared = {str(text): observed}
        comparison_cache: dict[tuple[str, ChapterDialogue], TextEvidence] = {}
        exact_shape = any(observed == self.matching_text[row] for row in valid)

        def compare(span: str, row: ChapterDialogue) -> TextEvidence:
            key = span, row
            evidence = comparison_cache.get(key)
            if evidence is None:
                shape = prepared.get(span)
                if shape is None:
                    shape = prepared[span] = prepare_text(span)
                evidence = comparison_cache[key] = compare_text(
                    shape, self.matching_text[row], limit=0 if exact_shape else 4
                )
            return evidence

        comparisons = [
            _CandidateEvidence(row, str(text), compare(str(text), row)) for row in valid
        ]
        raw = tuple(comparisons)
        # Trimming is crop recovery only. Every remaining span goes through the
        # same alignment and protected-content checks as an ordinary full crop.
        if recover_crop_noise and not any(
            (item.evidence.complete and item.evidence.distance <= budget(item.line))
            or (
                item.evidence.prefix_distance is not None
                and item.evidence.prefix_distance <= budget(item.line)
            )
            for item in raw
        ):
            crops: list[tuple[ChapterDialogue, str, bool, bool]] = []
            for row in valid:
                for crop, speaker_in_crop, suffix_trimmed in _observation_crops(
                    str(text), row, allow_speaker_evidence
                ):
                    shape = prepared.get(crop)
                    if shape is None:
                        shape = prepared[crop] = prepare_text(crop)
                    if shape.protected_content == observed.protected_content:
                        crops.append((row, crop, speaker_in_crop, suffix_trimmed))
            crop_keys = {crop: _normalize_exact_text(crop) for crop in prepared}
            source_keys = {row: _normalize_exact_text(row.text) for row in valid}
            exact_crops = [
                item for item in crops if crop_keys[item[1]] == source_keys[item[0]]
            ]
            if exact_crops:
                # Strict recovered text outranks every fuzzy crop; continuations
                # still compare against all authorized source candidates below.
                crops = exact_crops
                exact_shape = True
            comparisons.extend(
                _CandidateEvidence(
                    row, crop, compare(crop, row), True, named, suffix_trimmed
                )
                for row, crop, named, suffix_trimmed in crops
            )

        full = [
            item
            for item in comparisons
            if item.evidence.complete and item.evidence.distance <= budget(item.line)
        ]
        self.last_resolution_diagnostics["bounded_candidate_count"] = len(
            {item.line for item in full}
        )
        nearest = min(raw, key=lambda item: item.evidence.distance)
        self.last_resolution_diagnostics.update(
            best_candidate_line_id=nearest.line.line_id or "",
            best_candidate_edit_distance=nearest.evidence.distance,
            best_candidate_covered_characters=nearest.evidence.covered_characters,
        )

        def priority(item: _CandidateEvidence) -> tuple[int, int, int]:
            strict = _normalize_exact_text(item.observed) == _normalize_exact_text(
                item.line.text
            )
            normalized_equal = (
                _normalize(item.observed) == self.normalized_text[item.line]
            )
            level = (
                0
                if strict
                else 1
                if normalized_equal
                else 2
                if item.evidence.distance == 0
                else 3
            )
            return int(item.noise), level, item.evidence.distance

        if full:
            full.sort(key=priority)
            best = full[0]
            peers = {item.line for item in full if priority(item) == priority(best)}
            if speaker:
                speaker_rows = {
                    row for row in valid if _normalize(row.speaker) == speaker
                }
                named = peers & speaker_rows
                if named:
                    peers = named
                    valid = tuple(row for row in valid if row in speaker_rows)
                    comparisons = [
                        item for item in comparisons if item.line in speaker_rows
                    ]
            if len(peers) != 1:
                return finish(StoryObservationMatch(None, "expected-ambiguous"))
            row = next(iter(peers))
            best = next(
                item
                for item in full
                if item.line == row and priority(item) == priority(best)
            )
            # Within the supported speaker scope, failed guards still compete.
            if best.evidence.distance and any(
                other != row
                and compare(best.observed, other).distance < best.evidence.distance + 2
                for other in valid
            ):
                return finish(StoryObservationMatch(None, "expected-ambiguous"))
            continuation = False
            for other in valid:
                if (
                    other == row
                    or self.matching_text[other].alphanumeric_length
                    <= self.matching_text[row].alphanumeric_length
                ):
                    continue
                evidence = compare(best.observed, other)
                if (
                    evidence.prefix_distance is not None
                    and evidence.prefix_distance <= best.evidence.distance
                    and evidence.prefix_ends_at_boundary
                ):
                    continuation = True
                    break
            if not continuation:
                if best.noise:
                    method = (
                        "expected-bounded-speaker-text"
                        if best.speaker_in_crop
                        else "expected-bounded-ocr-suffix"
                    )
                elif _normalize_exact_text(text) == _normalize_exact_text(row.text):
                    method = (
                        "expected-exact"
                        if _normalize(row.speaker) == speaker
                        else "expected-text-only"
                    )
                elif normalized == self.normalized_text[row]:
                    method = (
                        "expected-normalized-exact"
                        if _normalize(row.speaker) == speaker
                        else "expected-normalized-text-only"
                    )
                elif best.evidence.distance == 0:
                    method = "expected-complete-diacritic-fold"
                else:
                    method = (
                        "expected-bounded-similarity"
                        if recover_crop_noise
                        else "expected-complete-edit-distance"
                    )
                return finish(StoryObservationMatch(row, method, complete=True))
            if not allow_prefix:
                return finish(StoryObservationMatch(None, "expected-incomplete"))
        if allow_prefix:
            prefixes = []
            for item in comparisons:
                # A trimmed observed tail cannot establish a growing source tail.
                if item.suffix_trimmed:
                    continue
                distance = item.evidence.prefix_distance
                length = prepared[item.observed].alphanumeric_length
                named = allow_speaker_evidence and (
                    item.speaker_in_crop or speaker == _normalize(item.line.speaker)
                )
                canonical_length = self.matching_text[item.line].alphanumeric_length
                enough = (
                    length >= 20
                    or (named and length >= 12)
                    or (
                        named
                        and canonical_length <= 12
                        and length >= 3
                        and length / canonical_length >= 0.6
                    )
                )
                if distance is not None and distance <= budget(item.line) and enough:
                    prefixes.append(item)
            if prefixes:
                prefixes.sort(
                    key=lambda item: (item.evidence.prefix_distance, item.noise)
                )
                best = prefixes[0]
                if speaker and any(
                    _normalize(item.line.speaker) == speaker
                    and (item.evidence.prefix_distance, item.noise)
                    == (best.evidence.prefix_distance, best.noise)
                    for item in prefixes
                ):
                    prefixes = [
                        item
                        for item in prefixes
                        if _normalize(item.line.speaker) == speaker
                    ]
                    best = prefixes[0]
                best_distance = best.evidence.prefix_distance
                peers = {
                    item.line
                    for item in prefixes
                    if item.evidence.prefix_distance is not None
                    and best_distance is not None
                    and item.evidence.prefix_distance <= best_distance + 1
                }
                if len(peers) != 1:
                    return finish(StoryObservationMatch(None, "expected-ambiguous"))
                method = (
                    "expected-bounded-speaker-prefix"
                    if best.evidence.prefix_distance
                    else "expected-bounded-prefix"
                )
                return finish(StoryObservationMatch(best.line, method, prefix=True))
        incomplete = any(
            item.evidence.prefix_distance is not None
            and not item.suffix_trimmed
            and item.evidence.prefix_distance <= budget(item.line)
            for item in comparisons
        )
        return finish(
            StoryObservationMatch(
                None,
                "expected-incomplete"
                if incomplete and not allow_prefix
                else "expected-no-match",
            )
        )

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
        if not prefix or len(prefix) < minimum_characters:
            return None
        candidates = self._prefix_candidates(
            speaker_key,
            prepare_text(text),
            candidate_filter=candidate_filter,
        )
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        self.current_match = ChapterMatch(
            selected.chapter, selected.sequence, 1.0, selected.story_order
        )
        return selected

    def resolve_unique_prefix_by_text(
        self, text: object, *, minimum_characters: int = 20
    ) -> ChapterDialogue | None:
        """Resolve one indexed line when OCR lost or corrupted its nameplate."""
        prefix = _normalize(text)
        if not prefix or len(prefix) < minimum_characters:
            return None

        observed = prepare_text(text)

        def matches(rows: Iterable[ChapterDialogue]) -> list[ChapterDialogue]:
            return [row for row in rows if self._matches_prefix(row, observed)]

        if self.current_match is not None:
            nearby = matches(self.by_chapter.get(self.current_match.chapter, ()))
            if len(nearby) == 1:
                selected = nearby[0]
                self.current_match = ChapterMatch(
                    selected.chapter,
                    selected.sequence,
                    1.0,
                    selected.story_order,
                )
                return selected
            return None

        candidates = matches(self.dialogue)
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        self.current_match = ChapterMatch(
            selected.chapter, selected.sequence, 1.0, selected.story_order
        )
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
        if not prefix or len(prefix) < minimum_characters:
            return False
        observed = prepare_text(text)
        candidates = self._prefix_candidates(speaker_key, observed)
        return (
            len(candidates) == 1
            and not compare_text(
                observed, self.matching_text[candidates[0]], limit=0
            ).complete
        )

    def _prefix_candidates(
        self,
        speaker_key: str,
        observed: MatchingText,
        *,
        candidate_filter: Callable[[ChapterDialogue], bool] | None = None,
    ) -> list[ChapterDialogue]:
        def matches(rows: Iterable[ChapterDialogue]) -> list[ChapterDialogue]:
            return [row for row in rows if self._matches_prefix(row, observed)]

        def eligible(rows: Iterable[ChapterDialogue]) -> list[ChapterDialogue]:
            return [
                row for row in rows if candidate_filter is None or candidate_filter(row)
            ]

        if self.current_match is not None:
            nearby = matches(
                self.by_chapter_speaker.get(
                    (self.current_match.chapter, speaker_key),
                    (),
                )
            )
            if nearby:
                # A filtered local owner must not make another chapter unique.
                return eligible(nearby)
        return eligible(matches(self.by_speaker.get(speaker_key, ())))

    def _matches_prefix(self, row: ChapterDialogue, observed: MatchingText) -> bool:
        if not row.line_id or not row.text_sha256 or not observed.alphanumeric_length:
            return False
        evidence = compare_text(observed, self.matching_text[row], limit=0)
        return evidence.complete or evidence.prefix_distance == 0

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
        # Voice warmup must not change the story scope used by OCR resolution.
        match = self.current_match or self._match(character, text)
        if match is None:
            return ()

        current_speaker = _normalize(character)
        chapter_rows = self.by_chapter.get(match.chapter, ())
        nearby = [
            row
            for row in chapter_rows
            if _story_position(row) >= _story_position(match)
        ][: self.lookahead_rows]
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
            row
            for row in rows
            if _story_position(row) >= _story_position(self.current_match)
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
                return ChapterMatch(row.chapter, row.sequence, 0.5, row.story_order)
            return None

        best_row = None
        best_score = 0.0
        for row in candidates:
            candidate_text = self.normalized_text[row]
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
        return ChapterMatch(
            best_row.chapter, best_row.sequence, best_score, best_row.story_order
        )


def _observation_crops(
    text: str, row: ChapterDialogue, allow_speaker_evidence: bool
) -> Iterable[tuple[str, bool, bool]]:
    tokens = tuple(re.finditer(r"\S+", text))
    length = sum(character.isalnum() for character in row.text)
    name = _normalize(row.speaker)
    for start in range(min(8, len(tokens))):
        leading = text[: tokens[start].start()]
        name_in_crop = bool(
            allow_speaker_evidence
            and name
            and name != "narrator"
            and f" {name} " in f" {_normalize(leading)} "
        )
        if length < 7 and not name_in_crop:
            continue
        for dropped in range(min(4, len(tokens) - start)):
            end = len(tokens) - dropped
            if not start and not dropped:
                continue
            trailing = text[tokens[end - 1].end() :]
            if len(trailing.strip()) > max(16, round(len(row.text) * 0.5)):
                continue
            if not name_in_crop and len(leading.strip()) > max(
                16, round(len(row.text) * 0.5)
            ):
                continue
            crop = text[tokens[start].start() : tokens[end - 1].end()]
            yield crop, name_in_crop, bool(dropped)


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
    if not 0 < value <= 600:
        return None
    value = float(value)
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
        or not is_lowercase_sha256(media_sha256)
        or not decoder
    ):
        return None
    if entry.get("source_media_ids") != [media_id] or entry.get(
        "available_media_ids"
    ) != [media_id]:
        return None
    try:
        measured = sample_count / sample_rate
    except OverflowError:
        return None
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


def _source_audio_evidence_payload(path: str | Path) -> bytes:
    evidence_path = Path(path).expanduser().resolve().parent / (
        "source-audio-semantic-evidence.json"
    )
    try:
        return read_regular_bytes(evidence_path)
    except OSError:
        return b""


def _validated_source_audio_line_ids(
    path: str | Path,
    document: StoryIndexDocument | None = None,
    *,
    evidence_payload: bytes | None = None,
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
        load_source_audio_semantic_evidence(
            evidence_path, document, payload=evidence_payload
        )
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


def _has_authoritative_source_audio(
    record: StoryIndexRecord,
    completion_contract: str | None,
    authorized_line_ids: Iterable[str],
) -> bool:
    return record.line_id in authorized_line_ids and _source_audio_covers_full_line(
        record.document,
        completion_contract=completion_contract,
        semantic_authorized=True,
    )


def _load_source_audio_extensions(
    path: str | Path, *, completion_contract: str | None = None
) -> dict[str, SourceAudioExtension]:
    """Retain optional source-audio fields omitted by older contract readers."""
    result: dict[str, SourceAudioExtension] = {}
    try:
        with open_regular_text(path, encoding="utf-8") as stream:
            next(stream, None)
            for row in stream:
                record = _string_mapping(decode_json(row))
                line_id = str(record.get("line_id") or "").strip()
                if not line_id:
                    continue
                result[line_id] = _source_audio_extension(
                    record,
                    completion_contract=completion_contract,
                    semantic_authorized=False,
                )
    except OSError, TypeError, ValueError:
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
