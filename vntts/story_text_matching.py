"""OCR similarity and source coverage; neither grants story-position authority."""

import unicodedata
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein


@dataclass(frozen=True)
class MatchingText:
    key: str
    alphanumeric_length: int


@dataclass(frozen=True)
class TextEvidence:
    distance: int
    complete: bool
    prefix_distance: int | None
    covered_characters: int
    prefix_ends_at_boundary: bool = False


def fold_latin_diacritics(value: str) -> str:
    result = []
    latin = False
    for character in unicodedata.normalize("NFD", value):
        if unicodedata.category(character).startswith("M"):
            if not latin:
                result.append(character)
        else:
            latin = "LATIN" in unicodedata.name(character, "")
            result.append(character)
    return unicodedata.normalize("NFC", "".join(result))


def prepare_text(value: object) -> MatchingText:
    folded = fold_latin_diacritics(unicodedata.normalize("NFKC", str(value)).casefold())
    key = " ".join(
        "".join(
            character
            if character.isalnum() or unicodedata.category(character).startswith("M")
            else " "
            for character in folded
        ).split()
    )
    return MatchingText(key, sum(character.isalnum() for character in key))


def edit_budget(text: MatchingText) -> int:
    return max(int(text.alphanumeric_length >= 3), len(text.key) // 20)


def retrieval_distance(observed: MatchingText) -> int:
    # n-m <= floor(n/20) bounds accepted source edits by floor(m/19).
    # Include short-text tolerance and the competing-candidate margin.
    return max(4, (len(observed.key) + 1) // 19 + 2)


def _covers_source(actual: str, source: str, distance: int) -> bool:
    if not distance:
        return True
    # Reject if ANY optimal alignment can omit a source endpoint.
    return all(
        Levenshtein.distance(actual, end_removed, score_cutoff=distance - 1) >= distance
        for end_removed in (source[1:], source[:-1])
    )


def compare_text(
    observed: MatchingText, canonical: MatchingText, limit: int | None = None
) -> TextEvidence:
    if limit is None:
        limit = max(4, edit_budget(canonical) + 1)
    if limit < 0:
        raise ValueError("The edit limit must be nonnegative")
    actual, source = observed.key, canonical.key
    distance = Levenshtein.distance(actual, source, score_cutoff=limit)
    if not actual or not source:
        return TextEvidence(distance, False, None, 0)
    complete = distance <= limit and _covers_source(actual, source, distance)
    if distance == 0:
        return TextEvidence(distance, True, None, canonical.alphanumeric_length)

    prefix_limit = min(limit, edit_budget(observed))
    prefixes = []
    for end in range(
        max(1, len(actual) - prefix_limit),
        min(len(source) - 1, len(actual) + prefix_limit) + 1,
    ):
        if source[end - 1].isspace():
            continue
        prefix = source[:end]
        cost = Levenshtein.distance(actual, prefix, score_cutoff=prefix_limit)
        if cost > prefix_limit or not _covers_source(actual, prefix, cost):
            continue
        # An extra observed tail is noise, not evidence of a growing source tail.
        if (
            cost
            and Levenshtein.distance(actual[:-1], prefix, score_cutoff=cost - 1) < cost
        ):
            continue
        prefixes.append((cost, -end))
    if not prefixes:
        return TextEvidence(
            distance, complete, None, canonical.alphanumeric_length if complete else 0
        )
    prefix_distance, negative_end = min(prefixes)
    end = -negative_end
    return TextEvidence(
        distance,
        complete,
        prefix_distance,
        canonical.alphanumeric_length
        if complete
        else sum(character.isalnum() for character in source[:end]),
        source[end].isspace(),
    )
