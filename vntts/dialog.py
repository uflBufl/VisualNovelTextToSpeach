from collections.abc import Callable, Iterable
from typing import TypeVar
from unicodedata import category, normalize

MAX_CHARACTER_NAME_LENGTH = 40
MAX_CHARACTER_NAME_WORDS = 4
NAME_PUNCTUATION = "-'’"


Image = TypeVar("Image")


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
