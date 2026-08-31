import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts.release_backends import (
    packaged_speech_backend_available,
    speech_backend_options,
)


class ReleaseBackendsTest(unittest.TestCase):
    def test_source_build_exposes_development_backends(self):
        options = speech_backend_options("pocket-tts", bundle_root=None)

        self.assertEqual(
            [backend for _label, backend, _available in options],
            ["pocket-tts", "coqui-xtts", "chatterbox-nano", "moss-tts"],
        )

    def test_frozen_options_only_advertise_supplied_backends(self):
        with TemporaryDirectory() as directory:
            bundle_root = Path(directory)
            (bundle_root / "speech-runtimes/pocket-tts").mkdir(parents=True)

            options = speech_backend_options("pocket-tts", bundle_root)

            self.assertEqual(
                [backend for _label, backend, _available in options],
                ["pocket-tts", "coqui-xtts"],
            )
            self.assertTrue(
                packaged_speech_backend_available("pocket-tts", bundle_root)
            )
            self.assertFalse(packaged_speech_backend_available("moss-tts", bundle_root))

    def test_frozen_options_preserve_unavailable_existing_selection(self):
        with TemporaryDirectory() as directory:
            options = speech_backend_options("moss-tts", Path(directory))

            self.assertEqual(options[-1][1:], ("moss-tts", False))
            self.assertIn("not included", options[-1][0])


if __name__ == "__main__":
    unittest.main()
