import json
import struct
import unittest
import wave
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
                    side_effect=lambda _source, destination, **_options: write_wav(
                        destination
                    ),
                ),
            ):
                references = importer.decode_references(
                    bank_path,
                    output,
                    "Kamuta",
                    1,
                    "decoder",
                )

        self.assertEqual(
            [reference.path.name for reference in references],
            ["kamuta-game-01.wav"],
        )
        self.assertEqual(references[0].media_id, 20)
        self.assertEqual(len(references[0].source_sha256), 64)

    def test_known_kamuta_bank_is_resolved_from_game_audio_directory(self):
        with TemporaryDirectory() as temporary_directory:
            audio_directory = Path(temporary_directory)
            bank = audio_directory / "activitystory_yuzhou2_7_yishi_npc520301_voc.bnk"
            bank.write_bytes(b"bank")

            resolved = importer.resolve_bank(
                "Kamuta", game_audio_directory=audio_directory
            )

        self.assertEqual(resolved, bank.resolve())

    def test_reviewed_media_ids_are_imported_in_requested_order(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bank = directory / "voice.bnk"
            bank.write_bytes(b"bank")
            output = directory / "pack"
            with (
                patch.object(
                    importer,
                    "read_embedded_media",
                    return_value=[
                        EmbeddedMedia(10, b"short"),
                        EmbeddedMedia(20, b"much-longer"),
                    ],
                ),
                patch.object(
                    importer,
                    "convert_audio",
                    side_effect=lambda _source, destination, **_options: write_wav(
                        destination
                    ),
                ),
            ):
                references = importer.decode_references(
                    bank,
                    output,
                    "Tang Ji",
                    3,
                    "decoder",
                    media_ids=[10, 20],
                )

        self.assertEqual([reference.media_id for reference in references], [10, 20])

    def test_missing_reviewed_media_id_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bank = directory / "voice.bnk"
            bank.write_bytes(b"bank")
            with patch.object(
                importer,
                "read_embedded_media",
                return_value=[EmbeddedMedia(10, b"voice")],
            ):
                with self.assertRaisesRegex(importer.GameVoiceImportError, "media ID"):
                    importer.decode_references(
                        bank,
                        directory / "pack",
                        "Tang Ji",
                        3,
                        "decoder",
                        media_ids=[99],
                    )

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

    def test_manifest_records_reference_provenance_idempotently(self):
        with TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            references = output / "references"
            references.mkdir()
            path = references / "selone-game-01.wav"
            path.write_bytes(b"voice")
            reference = importer.ImportedReference(
                path=path,
                media_id=42,
                source_sha256="a" * 64,
                reference_sha256="b" * 64,
            )

            importer.update_manifest(output, "Selone", [reference], Path("selone.bnk"))
            manifest_path = importer.update_manifest(
                output, "Selone", [reference], Path("selone.bnk")
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(len(manifest["voices"]), 1)
        self.assertEqual(
            manifest["voices"][0]["reference_metadata"],
            [
                {
                    "bank": "selone.bnk",
                    "media_id": 42,
                    "source_sha256": "a" * 64,
                    "reference_sha256": "b" * 64,
                }
            ],
        )


def write_wav(path):
    path = Path(path)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes((b"\x00\x10" * 32000))


if __name__ == "__main__":
    unittest.main()
