MAX_CHARACTER_NAME_LENGTH = 40
MAX_CHARACTER_NAME_WORDS = 4
NAME_PUNCTUATION = "-'’"


def is_empty(text):
    return text is None or text == "" or text.isspace()


def is_name_word(word):
    return all(
        character.isalpha() or character in NAME_PUNCTUATION for character in word
    )


def is_probable_character_name(text):
    candidate = text.strip()
    if is_empty(candidate) or len(candidate) > MAX_CHARACTER_NAME_LENGTH:
        return False

    words = candidate.split()
    if len(words) > MAX_CHARACTER_NAME_WORDS:
        return False

    if any(not is_name_word(word) for word in words):
        return False

    return candidate.istitle() or candidate.isupper()


def join_dialog_lines(lines):
    return " ".join(line.strip() for line in lines if not is_empty(line))


def parse_dialog(text):
    if is_empty(text):
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


def recognize_dialog(image, recognize_text):
    return parse_dialog(recognize_text(image))


def speak_dialog(text, speak_text):
    if not is_empty(text):
        speak_text(text)
