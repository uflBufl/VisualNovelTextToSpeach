import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

            with (
                patch("vntts.release_backends.sys.platform", "linux"),
                patch("vntts.release_backends.platform.machine", return_value="x86_64"),
                patch.dict(
                    os.environ,
                    {"VNTTS_MOSS_CPP_EXECUTABLE": "", "VNTTS_MOSS_GGUF": ""},
                ),
            ):
                options = speech_backend_options("pocket-tts", bundle_root)
                self.assertEqual(
                    [backend for _label, backend, _available in options],
                    ["pocket-tts", "coqui-xtts"],
                )
                self.assertTrue(
                    packaged_speech_backend_available("pocket-tts", bundle_root)
                )
                self.assertFalse(
                    packaged_speech_backend_available("moss-tts", bundle_root)
                )

    def test_frozen_options_preserve_unavailable_existing_selection(self):
        with TemporaryDirectory() as directory:
            with (
                patch("vntts.release_backends.sys.platform", "linux"),
                patch("vntts.release_backends.platform.machine", return_value="x86_64"),
                patch.dict(
                    os.environ,
                    {"VNTTS_MOSS_CPP_EXECUTABLE": "", "VNTTS_MOSS_GGUF": ""},
                ),
            ):
                options = speech_backend_options("moss-tts", Path(directory))

            self.assertEqual(options[-1][1:], ("moss-tts", False))
            self.assertIn("not included", options[-1][0])

    def test_frozen_windows_x64_advertises_moss_for_automatic_setup(self):
        with TemporaryDirectory() as directory:
            with (
                patch("vntts.release_backends.sys.platform", "win32"),
                patch("vntts.release_backends.platform.machine", return_value="AMD64"),
                patch.dict(
                    os.environ,
                    {"VNTTS_MOSS_CPP_EXECUTABLE": "", "VNTTS_MOSS_GGUF": ""},
                ),
            ):
                options = speech_backend_options("pocket-tts", Path(directory))

            self.assertIn(("MOSS-TTS Local v1.5", "moss-tts", True), options)

    def test_frozen_moss_requires_supported_platform_or_explicit_native_server(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "moss-tts-server"
            unsupported_platforms = (
                ("linux", "x86_64"),
                ("darwin", "arm64"),
                ("win32", "arm64"),
            )
            for platform_name, machine in unsupported_platforms:
                with self.subTest(platform=platform_name, machine=machine):
                    with (
                        patch("vntts.release_backends.sys.platform", platform_name),
                        patch(
                            "vntts.release_backends.platform.machine",
                            return_value=machine,
                        ),
                        patch.dict(
                            os.environ,
                            {
                                "VNTTS_MOSS_CPP_EXECUTABLE": "",
                                "VNTTS_MOSS_GGUF": "",
                            },
                        ),
                    ):
                        self.assertFalse(
                            packaged_speech_backend_available("moss-tts", root)
                        )

            with (
                patch("vntts.release_backends.sys.platform", "linux"),
                patch("vntts.release_backends.platform.machine", return_value="x86_64"),
                patch.dict(
                    os.environ,
                    {"VNTTS_MOSS_CPP_EXECUTABLE": str(server), "VNTTS_MOSS_GGUF": ""},
                ),
            ):
                self.assertTrue(packaged_speech_backend_available("moss-tts", root))


if __name__ == "__main__":
    unittest.main()
