def is_empty(text):
    return text is None or text == "" or text.isspace()


def parse_dialog(text):
    lines = text.split('\n')
    character = 'Narrator'
    if len(lines) > 3 and is_empty(lines[1]):
        character = lines[0].strip()
        lines = lines[2:]

    return character, ' '.join(line.strip() for line in lines).strip()


def recognize_dialog(image, recognize_text):
    return parse_dialog(recognize_text(image))


def speak_dialog(text, speak_text):
    if not is_empty(text):
        speak_text(text)
