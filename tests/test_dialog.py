import unittest
from unittest.mock import Mock

from vntts.dialog import (
    is_probable_character_name,
    parse_dialog,
    recognize_dialog,
    speak_dialog,
)


class DialogTest(unittest.TestCase):
    def test_recognize_dialog_returns_speaker_and_text_from_mocked_ocr(self):
        image = object()
        recognize_text = Mock(return_value="Alice\n\nHello there.\n")

        result = recognize_dialog(image, recognize_text)

        self.assertEqual(result, ("Alice", "Hello there."))
        recognize_text.assert_called_once_with(image)

    def test_parse_dialog_without_speaker_uses_narrator(self):
        self.assertEqual(
            parse_dialog("The wind is rising.\n"),
            ("Narrator", "The wind is rising."),
        )

    def test_parse_dialog_preserves_ambiguous_first_paragraph(self):
        self.assertEqual(
            parse_dialog("A quiet morning\n\nThe birds begin to sing.\n"),
            ("Narrator", "A quiet morning The birds begin to sing."),
        )

    def test_parse_dialog_preserves_multiple_narration_paragraphs(self):
        self.assertEqual(
            parse_dialog("First paragraph.\n\nSecond paragraph.\n"),
            ("Narrator", "First paragraph. Second paragraph."),
        )

    def test_parse_dialog_recognizes_uppercase_multiword_speaker(self):
        self.assertEqual(
            parse_dialog("MARY JANE\n\nWelcome back."),
            ("MARY JANE", "Welcome back."),
        )

    def test_parse_dialog_ignores_trailing_newlines(self):
        self.assertEqual(
            parse_dialog("Alice\n\nHello there.\n\n"),
            ("Alice", "Hello there."),
        )

    def test_parse_dialog_returns_empty_narration_for_blank_ocr(self):
        for text in [None, "", " ", "\n\n"]:
            with self.subTest(text=text):
                self.assertEqual(parse_dialog(text), ("Narrator", ""))

    def test_character_name_rejects_sentence_punctuation_and_long_labels(self):
        self.assertFalse(is_probable_character_name("Chapter One."))
        self.assertFalse(is_probable_character_name("A" * 41))

    def test_speak_dialog_ignores_empty_text(self):
        for text in ["", " ", "\n\n"]:
            with self.subTest(text=text):
                speak_text = Mock()

                speak_dialog(text, speak_text)

                speak_text.assert_not_called()

    def test_speak_dialog_sends_non_empty_text_to_mocked_tts(self):
        speak_text = Mock()

        speak_dialog("Hello there.", speak_text)

        speak_text.assert_called_once_with("Hello there.")


if __name__ == "__main__":
    unittest.main()
