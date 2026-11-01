from collections.abc import Callable, Iterable
from typing import TypeVar
from unicodedata import category, normalize

MAX_CHARACTER_NAME_LENGTH = 40
MAX_CHARACTER_NAME_WORDS = 4
NAME_PUNCTUATION = "-'’"


Image = TypeVar("Image")


def bounded_levenshtein(left: str, right: str, limit: int) -> int:
    """Return the edit distance, or limit + 1 when it exceeds the cutoff."""
    overflow = limit + 1
    if abs(len(left) - len(right)) > limit:
        return overflow
    previous = {column: column for column in range(min(len(right), limit) + 1)}
    for row, character in enumerate(left, 1):
        current = {0: row} if row <= limit else {}
        for column in range(max(1, row - limit), min(len(right), row + limit) + 1):
            current[column] = min(
                current.get(column - 1, overflow) + 1,
                previous.get(column, overflow) + 1,
                previous.get(column - 1, overflow) + (character != right[column - 1]),
            )
        if min(current.values(), default=overflow) > limit:
            return overflow
        previous = current
    return min(previous.get(len(right), overflow), overflow)


def _one_edit_text_match(left: str, right: str) -> bool:
    return left == right or (
        min(sum(c.isalnum() for c in left), sum(c.isalnum() for c in right)) >= 3
        and bounded_levenshtein(left, right, 1) <= 1
    )


def ocr_texts_equivalent(
    left: str, right: str, *, punctuation_fallback: bool = False
) -> bool:
    left, right = (
        " ".join(normalize("NFKC", value).casefold().split()) for value in (left, right)
    )
    if _one_edit_text_match(left, right):
        return True
    if not punctuation_fallback or not all(
        any(c.isalnum() for c in value) for value in (left, right)
    ):
        return False
    # Compare identity keys only; symbol-only replies and spoken text stay intact.
    left, right = (
        " ".join(
            "".join(" " if category(c).startswith("P") else c for c in value).split()
        )
        for value in (left, right)
    )
    return _one_edit_text_match(left, right)


def is_empty(text: str | None) -> bool:
    return text is None or text == "" or text.isspace()


def is_symbol_only_text(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    return all(
        character.isspace()
        or (category(character)[0] in "PS" and character not in "\ufffc\ufffd")
        for character in value
    )


def symbol_text_key(value: str) -> str:
    return "".join(normalize("NFKC", value).split())


def is_name_word(word: str) -> bool:
    return all(
        character.isalnum() or character in NAME_PUNCTUATION for character in word
    )


def is_probable_character_name(text: str) -> bool:
    candidate = text.strip()
    if is_empty(candidate) or len(candidate) > MAX_CHARACTER_NAME_LENGTH:
        return False

    words = candidate.split()
    if len(words) > MAX_CHARACTER_NAME_WORDS:
        return False

    if any(not is_name_word(word) for word in words):
        return False

    return candidate.istitle() or candidate.isupper() or candidate.isdecimal()


def join_dialog_lines(lines: Iterable[str]) -> str:
    return " ".join(line.strip() for line in lines if not is_empty(line))


def parse_dialog(text: str | None) -> tuple[str, str]:
    if text is None or is_empty(text):
        return "Narrator", ""

    lines = text.split("\n")
    character = "Narrator"
    if (
        len(lines) >= 3
        and is_empty(lines[1])
        and is_probable_character_name(lines[0])
        and any(not is_empty(line) for line in lines[2:])
    ):
        character = lines[0].strip()
        lines = lines[2:]

    return character, join_dialog_lines(lines)


def recognize_dialog(
    image: Image, recognize_text: Callable[[Image], str]
) -> tuple[str, str]:
    return parse_dialog(recognize_text(image))


def speak_dialog(text: str | None, speak_text: Callable[[str], object]) -> None:
    if text is not None and any(character.isalnum() for character in text):
        speak_text(text)
