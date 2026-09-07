import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Timer
from unittest.mock import Mock, patch
from zipfile import ZipFile

from vntts import game_audio_decoder as decoder


def archive_bytes(name="vgmstream-cli", payload=b"decoder"):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


class GameAudioDecoderTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_download_verified_cache_and_corruption_repair(self):
        payload = archive_bytes()
        opener = Mock(side_effect=lambda *_a, **_kw: io.BytesIO(payload))
        progress = Mock()
        with (
            patch.object(decoder, "find_game_decoder", return_value=None),
            patch.object(decoder, "get_bundle_root", return_value=None),
            patch.object(decoder.sys, "platform", "linux"),
            patch.object(decoder.platform, "machine", return_value="x86_64"),
            patch.dict(
                decoder.ARCHIVES,
                linux=("fixture.zip", hashlib.sha256(payload).hexdigest()),
            ),
            patch.object(decoder, "urlopen", opener),
            patch.object(decoder, "probe_game_decoder") as probe,
        ):
            path = decoder.ensure_game_decoder(
                storage_root=self.root, progress=progress
            )
            self.assertEqual(path.read_bytes(), b"decoder")
            self.assertEqual(decoder.ensure_game_decoder(storage_root=self.root), path)
            self.assertEqual(opener.call_count, 1)
            path.write_bytes(b"corrupt")
            decoder.ensure_game_decoder(storage_root=self.root)
            self.assertEqual(opener.call_count, 2)
            self.assertEqual(path.read_bytes(), b"decoder")
            self.assertEqual(probe.call_count, 3)
            self.assertTrue(
                any("Downloading" in call.args[0] for call in progress.call_args_list)
            )

    def test_bad_checksum_and_unsafe_archive_never_publish(self):
        for payload, digest, message in (
            (archive_bytes(), "0" * 64, "checksum"),
            (archive_bytes("../escape"), None, "unexpected path"),
        ):
            with (
                self.subTest(message=message),
                patch.object(decoder, "find_game_decoder", return_value=None),
                patch.object(decoder, "get_bundle_root", return_value=None),
                patch.object(decoder.sys, "platform", "linux"),
                patch.object(decoder.platform, "machine", return_value="x86_64"),
                patch.dict(
                    decoder.ARCHIVES,
                    linux=(
                        "fixture.zip",
                        digest or hashlib.sha256(payload).hexdigest(),
                    ),
                ),
                patch.object(decoder, "urlopen", return_value=io.BytesIO(payload)),
                patch.object(decoder, "probe_game_decoder") as probe,
            ):
                with self.assertRaisesRegex(decoder.DecoderSetupError, message):
                    decoder.ensure_game_decoder(storage_root=self.root)
                probe.assert_not_called()
                self.assertFalse(list(self.root.glob("*/verified.json")))

    def test_cancelled_setup_does_not_download_or_launch(self):
        cancellation = Event()
        cancellation.set()
        with (
            patch.object(decoder, "urlopen") as download,
            patch.object(decoder, "_run") as run,
        ):
            with self.assertRaises(decoder.DecoderSetupCancelled):
                decoder.ensure_game_decoder(
                    cancellation=cancellation, storage_root=self.root
                )
        download.assert_not_called()
        run.assert_not_called()

    def test_staging_readonly_system_files_is_repeatable(self):
        source = self.root / "system-tool"
        source.write_bytes(b"tool")
        source.chmod(0o555)
        target = self.root / "staging" / "tool"
        try:
            decoder._stage_file(source, target)
            decoder._stage_file(source, target)
            self.assertEqual(target.read_bytes(), b"tool")
        finally:
            source.chmod(0o755)

    def test_cancellation_stops_a_running_setup_process(self):
        cancellation = Event()
        timer = Timer(0.2, cancellation.set)
        timer.start()
        try:
            with self.assertRaises(decoder.DecoderSetupCancelled):
                decoder._run(
                    [sys.executable, "-c", "import time; time.sleep(30)"], cancellation
                )
        finally:
            timer.cancel()

    def test_macos_install_requires_consent_then_checks_the_result(self):
        with (
            patch.object(
                decoder,
                "find_game_decoder",
                side_effect=[None, None, Path("/opt/homebrew/bin/vgmstream-cli")],
            ),
            patch.object(decoder, "get_bundle_root", return_value=None),
            patch.object(decoder.sys, "platform", "darwin"),
            patch.object(
                decoder.shutil, "which", return_value="/opt/homebrew/bin/brew"
            ),
            patch.object(decoder, "_run") as run,
            patch.object(decoder, "probe_game_decoder") as probe,
        ):
            with self.assertRaises(decoder.DecoderSetupRequired):
                decoder.ensure_game_decoder(storage_root=self.root)
            run.assert_not_called()
            result = decoder.ensure_game_decoder(
                storage_root=self.root, allow_homebrew=True
            )
            run.assert_called_once_with(
                ["/opt/homebrew/bin/brew", "install", "vgmstream"], None
            )
            probe.assert_called_once_with(result, None)

    def test_bundle_never_falls_back_to_host_or_download(self):
        with (
            patch.object(decoder, "get_bundle_root", return_value=self.root),
            patch.object(decoder.shutil, "which") as which,
        ):
            self.assertIsNone(decoder.find_game_decoder())
            with self.assertRaisesRegex(decoder.DecoderSetupError, "bundle is missing"):
                decoder.ensure_game_decoder()
            which.assert_not_called()
            name = (
                "vgmstream-cli.exe"
                if decoder.sys.platform == "win32"
                else "vgmstream-cli"
            )
            path = self.root / "vgmstream" / name
            path.parent.mkdir()
            path.touch()
            self.assertEqual(decoder.find_game_decoder(), path)

    def test_cached_manifest_is_checksum_bound(self):
        # A manifest cannot redirect file checks outside the managed directory.
        folder = self.root / "r2117-linux"
        folder.mkdir()
        (folder / "verified.json").write_text(
            json.dumps({"vgmstream-cli": "bad", "../outside": "bad"})
        )
        with (
            patch.object(decoder, "find_game_decoder", return_value=None),
            patch.object(decoder, "get_bundle_root", return_value=None),
            patch.object(decoder.sys, "platform", "linux"),
            patch.object(decoder.platform, "machine", return_value="x86_64"),
            patch.object(decoder, "_download", side_effect=OSError("offline")),
        ):
            with self.assertRaisesRegex(decoder.DecoderSetupError, "offline"):
                decoder.ensure_game_decoder(storage_root=self.root)
