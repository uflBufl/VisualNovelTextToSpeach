"""Conservative text-only advice for a human reviewing an ASR transcript.

No status certifies audio quality or semantic equivalence. In particular, mixed
replacement spans can contain real added or missing content despite being uncertain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, TypeAlias

SPEECH_CONTENT_ADVICE_VERSION = 1

SpeechContentStatus: TypeAlias = Literal[
    "no_asr_mismatch",
    "review_possible_content_mismatch",
    "uncertain_recognition",
    "boundary_variation",
]
SpeechContentIssueKind: TypeAlias = Literal[
    "possible_added_content",
    "possible_missing_content",
    "uncertain_recognition",
    "boundary_variation",
    "uncertain_numeric_format",
    "empty_transcript",
]

_DIGIT_WORDS = dict(
    zip(
        "0123456789",
        "zero one two three four five six seven eight nine".split(),
        strict=True,
    )
)
# Keep this policy independent of the frozen authoring report comparisons.
_NUMERIC_FORMAT = re.compile(
    r"(?<![\w'’+\-−.,:/])(?:[+\-−]\s*|\.)?\(?\d+\)?"
    r"(?:\s*[.,:/\-]\s*\(?\d+\)?|\s+\(?\d+\)?)*(?![\w'’]|[.,:/+\-−]\w)"
)


@dataclass(frozen=True)
class SpeechContentIssue:
    kind: SpeechContentIssueKind
    expected_words: tuple[str, ...]
    observed_words: tuple[str, ...]


@dataclass(frozen=True)
class SpeechContentAdvice:
    expected_text: str
    observed_text: str
    status: SpeechContentStatus
    issues: tuple[SpeechContentIssue, ...]


def normalize_speech_text(text: str) -> tuple[str, ...]:
    """Normalize punctuation/case and isolated English digits, retaining contractions.

    Numeric punctuation still needs the protection in ``assess_speech_content``;
    tokens alone cannot distinguish a signed number from an unsigned one.
    """
    # shortcut: only isolated English digits; qualify other number readings separately.
    text = _NUMERIC_FORMAT.sub(
        lambda match: _DIGIT_WORDS.get(match[0].strip("()"), match[0]), text
    )
    return tuple(
        token.lower().replace("’", "'")
        for token in re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text)
    )


def _protected_numbers(text: str) -> tuple[str, ...]:
    return tuple(
        match[0]
        for match in _NUMERIC_FORMAT.finditer(text)
        if match[0].strip("()") not in _DIGIT_WORDS
    )


def assess_speech_content(
    expected_text: str, observed_text: str
) -> SpeechContentAdvice:
    """Suggest review categories without accepting, rejecting, or ranking audio.

    Boundary variations also require listening: ``apart`` and ``a part`` may
    carry different meanings. Empty text cannot establish an ASR match.
    """
    expected = normalize_speech_text(expected_text)
    observed = normalize_speech_text(observed_text)
    issues: list[SpeechContentIssue] = []
    if not expected or not observed:
        issues.append(SpeechContentIssue("empty_transcript", expected, observed))
    else:
        for tag, start, end, other_start, other_end in SequenceMatcher(
            None, expected, observed, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                continue
            before, after = expected[start:end], observed[other_start:other_end]
            kind: SpeechContentIssueKind
            if tag == "replace":
                boundary = all(token.isalpha() for token in (*before, *after)) and (
                    "".join(before) == "".join(after)
                )
                kind = "boundary_variation" if boundary else "uncertain_recognition"
            else:
                kind = (
                    "possible_added_content"
                    if tag == "insert"
                    else "possible_missing_content"
                )
            issues.append(SpeechContentIssue(kind, before, after))
    expected_numbers = _protected_numbers(expected_text)
    observed_numbers = _protected_numbers(observed_text)
    if expected_numbers != observed_numbers:
        issues.append(
            SpeechContentIssue(
                "uncertain_numeric_format", expected_numbers, observed_numbers
            )
        )
    kinds = {issue.kind for issue in issues}
    status: SpeechContentStatus
    if kinds & {"possible_added_content", "possible_missing_content"}:
        status = "review_possible_content_mismatch"
    elif kinds & {
        "uncertain_recognition",
        "uncertain_numeric_format",
        "empty_transcript",
    }:
        status = "uncertain_recognition"
    elif kinds:
        status = "boundary_variation"
    else:
        status = "no_asr_mismatch"
    return SpeechContentAdvice(expected_text, observed_text, status, tuple(issues))
