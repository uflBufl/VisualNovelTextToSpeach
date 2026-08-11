import io
import struct
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.game_audio_cli import convert_main, extract_main


class GameAudioCLITest(unittest.TestCase):
    def test_extract_command_creates_embedded_wem(self):
        content = b"voice"
        didx = struct.pack("<III", 42, 0, len(content))
        bank_data = (
            b"BKHD"
            + struct.pack("<I", 0)
            + b"DIDX"
            + struct.pack("<I", len(didx))
            + didx
            + b"DATA"
            + struct.pack("<I", len(content))
            + content
        )
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bank = directory / "voice.bnk"
            bank.write_bytes(bank_data)
            output = directory / "output"

            with redirect_stdout(io.StringIO()):
                status = extract_main([str(bank), str(output)])

            self.assertEqual(status, 0)
            self.assertEqual((output / "42.wem").read_bytes(), content)

    def test_convert_command_delegates_to_converter(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "voice.wem"
            output = directory / "voice.wav"
            source.write_bytes(b"voice")

            with patch(
                "vntts.game_audio_cli.convert_audio", return_value=output
            ) as convert:
                with redirect_stdout(io.StringIO()):
                    status = convert_main([str(source), str(output)])

        self.assertEqual(status, 0)
        convert.assert_called_once_with(
            source,
            output,
            decoder="vgmstream-cli",
            overwrite=False,
        )
