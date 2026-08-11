import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

module_path = (
    Path(__file__).resolve().parents[1] / "examples" / "provision_reverse1999_voices.py"
)
module_spec = importlib.util.spec_from_file_location(
    "vntts_reverse1999_provisioner",
    module_path,
)
provisioner = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(provisioner)


class Reverse1999VoiceProvisioningTest(unittest.TestCase):
    def test_video_ogg_is_skipped_in_favor_of_audio_references(self):
        media = {
            "video.ogg": {
                "url": "https://example.invalid/video.ogg",
                "descriptionurl": "https://example.invalid/video",
                "mime": "video/ogg",
            },
            "voice.ogg": {
                "url": "https://example.invalid/voice.ogg",
                "descriptionurl": "https://example.invalid/voice",
                "mime": "audio/ogg",
            },
        }

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(
                    provisioner,
                    "voice_line_images",
                    return_value=list(media),
                ),
                patch.object(
                    provisioner,
                    "order_references",
                    side_effect=lambda values: values,
                ),
                patch.object(
                    provisioner,
                    "resolve_media",
                    side_effect=lambda image: media[image],
                ),
                patch.object(provisioner, "download") as download,
            ):
                voice = provisioner.provision_character(
                    "Pioneer",
                    Path(temporary_directory),
                    1,
                )

        self.assertEqual(voice["sources"], ["https://example.invalid/voice"])
        download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
