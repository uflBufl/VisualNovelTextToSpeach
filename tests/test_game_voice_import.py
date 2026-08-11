import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts import reverse1999_voice_import as importer
from vntts.wwise import EmbeddedMedia


class Reverse1999GameVoiceImportTest(unittest.TestCase):
    def test_selects_longest_embedded_media_as_voice_references(self):
        first = b"RIFF-first"
        second = b"RIFF-second"
        didx = b"".join(
            (
                struct.pack("<III", 10, 0, len(first)),
                struct.pack("<III", 20, len(first), len(second)),
            )
        )
        data = first + second
        bank = (
            b"BKHD"
            + struct.pack("<I", 0)
            + b"DIDX"
            + struct.pack("<I", len(didx))
            + didx
            + b"DATA"
            + struct.pack("<I", len(data))
            + data
        )

        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bank_path = directory / "voice.bnk"
            bank_path.write_bytes(bank)
            output = directory / "pack"

            with (
                patch.object(
                    importer,
                    "read_embedded_media",
                    return_value=[
                        EmbeddedMedia(10, first),
                        EmbeddedMedia(20, second),
                    ],
                ),
                patch.object(
                    importer,
                    "convert_audio",
                    side_effect=lambda _source, destination, **_options: Path(
                        destination
                    ).write_bytes(b"wav"),
                ),
            ):
                references = importer.decode_references(
                    bank_path,
                    output,
                    "Kamuta",
                    1,
                    "decoder",
                )

        self.assertEqual([path.name for path in references], ["kamuta-game-01.wav"])

    def test_known_kamuta_bank_is_resolved_from_game_audio_directory(self):
        with TemporaryDirectory() as temporary_directory:
            audio_directory = Path(temporary_directory)
            bank = audio_directory / "activitystory_yuzhou2_7_yishi_npc520301_voc.bnk"
            bank.write_bytes(b"bank")

            resolved = importer.resolve_bank(
                "Kamuta", game_audio_directory=audio_directory
            )

        self.assertEqual(resolved, bank.resolve())

    def test_manifest_adds_story_npc_without_losing_existing_voices(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            references = output / "references"
            references.mkdir()
            kamuta = references / "kamuta-game-01.wav"
            kamuta.write_bytes(b"voice")
            (output / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "reference_count": 3,
                        "voices": [
                            {
                                "character": "Fatutu",
                                "speaker": "fatutu",
                                "references": ["references/fatutu.wav"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            manifest_path = importer.update_manifest(
                output,
                "Kamuta",
                [kamuta],
                Path("kamuta.bnk"),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [voice["character"] for voice in manifest["voices"]],
            ["Fatutu", "Kamuta"],
        )
        self.assertEqual(
            manifest["voices"][1]["references"],
            ["references/kamuta-game-01.wav"],
        )


if __name__ == "__main__":
    unittest.main()
