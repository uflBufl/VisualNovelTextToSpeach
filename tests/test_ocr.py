import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from vntts.ocr import (
    DialogRegion,
    default_dialog_region,
    load_dialog_region,
    parse_dialog_region,
    parse_recognized_dialog,
    recognize_dialog_image,
    save_dialog_region,
)
from vntts.voices import CharacterVoice, CharacterVoiceRegistry


class DialogRegionTest(unittest.TestCase):
    def test_region_converts_normalized_values_to_monitor_coordinates(self):
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)

        self.assertEqual(
            region.capture_box(
                {
                    "left": 100,
                    "top": 50,
                    "width": 2000,
                    "height": 1000,
                }
            ),
            {"left": 300, "top": 650, "width": 1600, "height": 300},
        )

    def test_region_can_be_parsed_from_environment_format(self):
        self.assertEqual(
            parse_dialog_region("0.1, 0.6, 0.8, 0.3"),
            DialogRegion(0.1, 0.6, 0.8, 0.3),
        )

    def test_invalid_region_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "fit inside"):
            DialogRegion(0.5, 0.5, 0.6, 0.6)

    def test_region_round_trips_through_json_file(self):
        region = DialogRegion(0.1, 0.6, 0.8, 0.3)
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "nested" / "region.json"

            save_dialog_region(region, path)

            self.assertEqual(load_dialog_region(path), region)
            self.assertEqual(json.loads(path.read_text()), region.to_json())


class RecognizedDialogTest(unittest.TestCase):
    def setUp(self):
        self.registry = CharacterVoiceRegistry(
            [
                CharacterVoice("Marcus", "marcus"),
                CharacterVoice("X", "x"),
            ]
        )

    def test_fuzzy_speaker_name_ignores_ui_noise(self):
        character, text = parse_recognized_dialog(
            "AUTO SKIP\nMareus\nHello from the suitcase.\nv\n",
            self.registry,
        )

        self.assertEqual(character, "Marcus")
        self.assertEqual(text, "Hello from the suitcase.")

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract is not installed")
    def test_real_sample_screenshots_resolve_speaker_and_dialog(self):
        samples = Path(__file__).resolve().parents[1] / "samples"
        expected = {
            "01.jpeg": (
                "Marcus",
                "And me, I just wanted to go home for a visit. "
                "Why would you take away even that?!",
            ),
            "02.png": (
                "X",
                "You were the perfect fulcrum! "
                "I don't know what I would have done without you.",
            ),
        }

        for filename, dialog in expected.items():
            with self.subTest(filename=filename):
                screenshot = Image.open(samples / filename)
                cropped_dialog = default_dialog_region.crop(screenshot)

                self.assertEqual(
                    recognize_dialog_image(cropped_dialog, self.registry),
                    dialog,
                )


if __name__ == "__main__":
    unittest.main()
