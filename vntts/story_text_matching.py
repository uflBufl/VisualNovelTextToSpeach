"""Directional OCR evidence; matching does not grant authority to a story line."""

import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

_HARD = "\x00"
_SOFT = "\x01"
_BOUNDARIES = {_HARD, _SOFT}
_NEGATIVE_AND_NUMBER_WORDS = frozenset(
    "no not never neither nor cannot zero one two three four five six seven eight "
    "nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen "
    "nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred "
    "thousand million billion".split()
)
_NUMBERS = re.compile(r"[+-]?(?:\d+(?:[.,:/]\d+)*|[.,]\d+)(?:e[+-]?\d+)?(?:[%‰])?")
_WORDS = re.compile(r"\w+(?:'\w+)?")
_TYPOGRAPHY = str.maketrans("’‘ʼ−", "'''-")
_Protection = tuple[str, int] | None


@dataclass(frozen=True)
class MatchingText:
    key: str
    alphanumeric_length: int
    protected_content: tuple[str, ...]
    _protected: tuple[_Protection, ...]
    _lengths: tuple[int, ...]


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
    original = (
        unicodedata.normalize("NFKC", str(value)).casefold().translate(_TYPOGRAPHY)
    )
    spans = list(_NUMBERS.finditer(original))
    spans.extend(
        match
        for match in _WORDS.finditer(original)
        if match[0] in _NEGATIVE_AND_NUMBER_WORDS or match[0].endswith("n't")
    )
    protected: dict[int, _Protection] = {}
    for match in spans:
        for offset, position in enumerate(range(match.start(), match.end())):
            protected[position] = (match[0], offset)

    units: list[str] = []
    protections: list[_Protection] = []
    latin = False
    for position, character in enumerate(original):
        if unicodedata.category(character).startswith("M"):
            if latin:
                continue
        else:
            latin = "LATIN" in unicodedata.name(character, "")
        protection = protected.get(position)
        for folded in fold_latin_diacritics(character):
            if (
                protection is not None
                or folded.isalnum()
                or unicodedata.category(folded).startswith("M")
                or folded == "_"
            ):
                units.append(folded)
                protections.append(protection)
                continue
            boundary = _HARD if folded.isspace() else _SOFT
            if units and units[-1] in _BOUNDARIES:
                if boundary == _SOFT:
                    units[-1] = _SOFT
            else:
                units.append(boundary)
                protections.append(None)
    while units and units[-1] in _BOUNDARIES:
        units.pop()
        protections.pop()
    if units and units[0] in _BOUNDARIES:
        del units[0]
        del protections[0]
    lengths = [0]
    for unit in units:
        lengths.append(lengths[-1] + (unit not in _BOUNDARIES))
    key = "".join(units)
    return MatchingText(
        key,
        sum(character.isalnum() for character in key),
        tuple(tag[0] for tag in protections if tag is not None and tag[1] == 0),
        tuple(protections),
        tuple(lengths),
    )


def _safe_pair(observed: MatchingText, canonical: MatchingText, i: int, j: int) -> bool:
    left, right = observed.key[i], canonical.key[j]
    if left in _BOUNDARIES or right in _BOUNDARIES:
        return left in _BOUNDARIES and right in _BOUNDARIES
    return observed._protected[i] == canonical._protected[j]


def compare_text(
    observed: MatchingText, canonical: MatchingText, limit: int = 4
) -> TextEvidence:
    if limit < 0:
        raise ValueError("The edit limit must be nonnegative")
    overflow = limit + 1
    if not observed.key or not canonical.key:
        distance = min(
            overflow,
            sum(unit != _SOFT for unit in observed.key or canonical.key),
        )
        return TextEvidence(distance, False, None, 0)
    if observed == canonical:
        return TextEvidence(0, True, None, canonical.alphanumeric_length)
    if observed._lengths[-1] > canonical._lengths[-1] + limit:
        return TextEvidence(overflow, False, None, 0)

    # Safety flags aggregate every cheapest alignment, including uncertain ends.
    # 1: missing source beginning; 2: missing source end; 4: unaligned last letter.
    previous: dict[int, int] = {}
    safe_previous: dict[int, tuple[int, int]] = {}
    source, actual = canonical.key, observed.key
    for i in range(len(actual) + 1):
        length = observed._lengths[i]
        start = bisect_left(canonical._lengths, max(0, length - limit))
        stop = bisect_right(canonical._lengths, length + limit)
        current: dict[int, int] = {}
        safe_current: dict[int, tuple[int, int]] = {}
        for j in range(start, stop):
            costs: list[int] = []
            safe_costs: list[tuple[int, int]] = []
            if i == j == 0:
                costs.append(0)
                safe_costs.append((0, 0))
            if i:
                unit = actual[i - 1]
                cost = unit != _SOFT
                costs.append(previous.get(j, overflow) + cost)
                if unit != _HARD and observed._protected[i - 1] is None:
                    if (state := safe_previous.get(j)) is not None:
                        safe_costs.append(
                            (state[0] + cost, state[1] | (4 if cost else 0))
                        )
            if j:
                unit = source[j - 1]
                cost = unit != _SOFT
                costs.append(current.get(j - 1, overflow) + cost)
                if unit != _HARD and canonical._protected[j - 1] is None:
                    if (state := safe_current.get(j - 1)) is not None:
                        flags = state[1]
                        if cost:
                            flags |= (
                                4
                                | (1 if i == 0 else 0)
                                | (2 if i == len(actual) else 0)
                            )
                        safe_costs.append((state[0] + cost, flags))
            if i and j:
                left, right = actual[i - 1], source[j - 1]
                cost = not (
                    left == right or left in _BOUNDARIES and right in _BOUNDARIES
                )
                costs.append(previous.get(j - 1, overflow) + cost)
                if _safe_pair(observed, canonical, i - 1, j - 1):
                    if (state := safe_previous.get(j - 1)) is not None:
                        flags = state[1] if left in _BOUNDARIES else state[1] & ~4
                        safe_costs.append((state[0] + cost, flags))
            distance = min(costs, default=overflow)
            if distance <= limit:
                current[j] = distance
            safe_distance = min((item[0] for item in safe_costs), default=overflow)
            if safe_distance <= limit:
                flags = 0
                for cost, path_flags in safe_costs:
                    if cost == safe_distance:
                        flags |= path_flags
                safe_current[j] = (safe_distance, flags)
        previous, safe_previous = current, safe_current
        if not current:
            return TextEvidence(overflow, False, None, 0)

    distance = previous.get(len(source), overflow)
    safe_full = safe_previous.get(len(source))
    complete = (
        safe_full is not None and safe_full[0] == distance and not safe_full[1] & 3
    )
    prefixes = []
    for end, (cost, flags) in safe_previous.items():
        if not end or end == len(source) or flags:
            continue
        before, after = canonical._protected[end - 1 : end + 1]
        if before is not None and after is not None and after[1] != 0:
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
        source[end] in _BOUNDARIES or source[end - 1] in _BOUNDARIES,
    )
