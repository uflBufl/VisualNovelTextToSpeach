import struct
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from vntts.wwise import (
    AudioConversionError,
    EmbeddedMedia,
    WwiseActionReference,
    WwiseBankError,
    WwiseEventRoute,
    convert_audio,
    extract_bank,
    extract_embedded_media,
    inspect_bank_data,
)


def make_bank(*media):
    data = b""
    entries = []
    for media_id, content in media:
        entries.append(struct.pack("<III", media_id, len(data), len(content)))
        data += content
    didx = b"".join(entries)
    return (
        b"BKHD"
        + struct.pack("<I", 0)
        + b"DIDX"
        + struct.pack("<I", len(didx))
        + didx
        + b"DATA"
        + struct.pack("<I", len(data))
        + data
    )


def make_hirc_object(object_type, object_id, payload=b""):
    body = struct.pack("<I", object_id) + payload
    return bytes([object_type]) + struct.pack("<I", len(body)) + body


def make_routed_bank():
    sound_id = 101
    media_id = 202
    action_id = 303
    event_id = 404
    sound = make_hirc_object(
        0x02,
        sound_id,
        struct.pack("<IBI", 0x00040001, 0, media_id),
    )
    action = make_hirc_object(
        0x03,
        action_id,
        struct.pack("<HIB", 0x0403, sound_id, 0),
    )
    event = make_hirc_object(0x04, event_id, b"\x01" + struct.pack("<I", action_id))
    hirc = struct.pack("<I", 3) + sound + action + event
    return (
        b"BKHD"
        + struct.pack("<I", 4)
        + struct.pack("<I", 154)
        + b"HIRC"
        + struct.pack("<I", len(hirc))
        + hirc
    )


class WwiseBankTest(unittest.TestCase):
    def test_extracts_embedded_media_with_ids_and_bytes(self):
        first = b"RIFF-first"
        second = b"RIFF-second"

        self.assertEqual(
            extract_embedded_media(make_bank((10, first), (20, second))),
            [EmbeddedMedia(10, first), EmbeddedMedia(20, second)],
        )

    def test_invalid_or_truncated_bank_is_rejected(self):
        with self.assertRaisesRegex(WwiseBankError, "DIDX"):
            extract_embedded_media(b"BKHD")

        truncated = make_bank((10, b"voice"))[:-1]
        with self.assertRaisesRegex(WwiseBankError, "truncated"):
            extract_embedded_media(truncated)

    def test_inspects_bank_sections_without_copying_embedded_media(self):
        bank = make_bank((10, b"one"), (20, b"two"))
        bank += b"HIRC" + struct.pack("<I", 4) + struct.pack("<I", 0)

        summary = inspect_bank_data(bank)

        self.assertEqual(summary.sections, ("BKHD", "DIDX", "DATA", "HIRC"))
        self.assertEqual(summary.media_ids, (10, 20))
        self.assertEqual(summary.media_count, 2)
        self.assertEqual(summary.embedded_media_bytes, 6)
        self.assertEqual(summary.hirc_object_count, 0)
        self.assertEqual(summary.event_count, 0)

    def test_maps_event_actions_to_sound_and_media_ids(self):
        summary = inspect_bank_data(make_routed_bank())

        self.assertEqual(summary.hirc_object_count, 3)
        self.assertEqual(
            summary.event_routes,
            (
                WwiseEventRoute(
                    event_id=404,
                    action_ids=(303,),
                    actions=(WwiseActionReference(303, 0x0403, 101),),
                    sound_ids=(101,),
                    media_ids=(202,),
                ),
            ),
        )

    def test_maps_container_target_to_descendant_sound(self):
        sound_id = 101
        media_id = 202
        container_id = 505
        action_id = 303
        event_id = 404
        node_base = b"\0\0\0\0" + struct.pack("<II", 0, container_id)
        sound = make_hirc_object(
            0x02,
            sound_id,
            struct.pack("<IBII", 0x00040001, 0, media_id, 1) + b"\x01" + node_base,
        )
        container = make_hirc_object(
            0x05,
            container_id,
            b"\0\0\0\0" + struct.pack("<II", 0, 0),
        )
        action = make_hirc_object(
            0x03,
            action_id,
            struct.pack("<HIB", 0x0403, container_id, 0),
        )
        event = make_hirc_object(
            0x04,
            event_id,
            b"\x01" + struct.pack("<I", action_id),
        )
        hirc = struct.pack("<I", 4) + sound + container + action + event

        routes = inspect_bank_data(
            b"BKHD"
            + struct.pack("<II", 4, 150)
            + b"HIRC"
            + struct.pack("<I", len(hirc))
            + hirc
        ).event_routes

        self.assertEqual(routes[0].sound_ids, (sound_id,))
        self.assertEqual(routes[0].media_ids, (media_id,))

    def test_rejects_truncated_hirc_object(self):
        hirc = struct.pack("<I", 1) + b"\x04" + struct.pack("<I", 9) + b"short"
        bank = (
            b"BKHD"
            + struct.pack("<I", 4)
            + struct.pack("<I", 154)
            + b"HIRC"
            + struct.pack("<I", len(hirc))
            + hirc
        )

        with self.assertRaisesRegex(WwiseBankError, "HIRC object 0 is truncated"):
            inspect_bank_data(bank)

    def test_extract_bank_writes_wem_files_and_honors_limit(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bank = directory / "voice.bnk"
            bank.write_bytes(make_bank((10, b"one"), (20, b"two")))

            outputs = extract_bank(bank, directory / "output", limit=1)

            self.assertEqual([path.name for path in outputs], ["10.wem"])
            self.assertEqual(outputs[0].read_bytes(), b"one")

    def test_extract_bank_does_not_overwrite_without_permission(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            bank = directory / "voice.bnk"
            bank.write_bytes(make_bank((10, b"new")))
            output = directory / "output"
            output.mkdir()
            (output / "10.wem").write_bytes(b"existing")

            with self.assertRaisesRegex(WwiseBankError, "--overwrite"):
                extract_bank(bank, output)

            self.assertEqual((output / "10.wem").read_bytes(), b"existing")


class AudioConversionTest(unittest.TestCase):
    def test_conversion_invokes_decoder_and_requires_created_output(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "voice.wem"
            output = directory / "voice.wav"
            source.write_bytes(b"voice")

            def run(command, **_options):
                Path(command[2]).write_bytes(b"wav")
                return CompletedProcess(command, 0, "", "")

            with patch("vntts.wwise.resolve_decoder", return_value="decoder"):
                result = convert_audio(source, output, runner=run)

        self.assertEqual(result, output.resolve())

    def test_decoder_failure_includes_reported_error(self):
        with TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source = directory / "voice.wem"
            source.write_bytes(b"voice")
            runner = Mock(return_value=CompletedProcess([], 1, "", "unsupported codec"))

            with patch("vntts.wwise.resolve_decoder", return_value="decoder"):
                with self.assertRaisesRegex(AudioConversionError, "unsupported codec"):
                    convert_audio(source, directory / "voice.wav", runner=runner)
