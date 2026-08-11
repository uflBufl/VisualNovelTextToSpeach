import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WwiseBankError(RuntimeError):
    pass


class AudioConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddedMedia:
    media_id: int
    data: bytes

    @property
    def size(self):
        return len(self.data)


@dataclass(frozen=True)
class WwiseBankSummary:
    bank_version: int | None
    sections: tuple[str, ...]
    media_ids: tuple[int, ...]
    embedded_media_bytes: int
    hirc_object_count: int | None

    @property
    def media_count(self):
        return len(self.media_ids)


def inspect_bank_data(bank_data):
    sections = []
    section_locations = {}
    offset = 0
    while offset + 8 <= len(bank_data):
        tag_bytes = bank_data[offset : offset + 4]
        if tag_bytes == b"\0\0\0\0" and not any(bank_data[offset:]):
            break
        try:
            tag = tag_bytes.decode("ascii")
        except UnicodeDecodeError as error:
            raise WwiseBankError(f"Invalid Wwise section at offset {offset}") from error
        size = struct.unpack_from("<I", bank_data, offset + 4)[0]
        payload_offset = offset + 8
        end = payload_offset + size
        if end > len(bank_data):
            raise WwiseBankError(f"Wwise {tag} section is truncated")
        sections.append(tag)
        section_locations[tag] = (payload_offset, size)
        offset = end

    if not sections:
        raise WwiseBankError("Wwise bank contains no sections")

    bank_version = None
    bkhd = section_locations.get("BKHD")
    if bkhd is not None and bkhd[1] >= 4:
        bank_version = struct.unpack_from("<I", bank_data, bkhd[0])[0]

    hirc_object_count = None
    hirc = section_locations.get("HIRC")
    if hirc is not None:
        if hirc[1] < 4:
            raise WwiseBankError("Wwise bank has an invalid HIRC section")
        hirc_object_count = struct.unpack_from("<I", bank_data, hirc[0])[0]

    media_ids = []
    embedded_media_bytes = 0
    didx = section_locations.get("DIDX")
    if didx is not None:
        didx_offset, didx_size = didx
        if didx_size % 12:
            raise WwiseBankError("Wwise bank has an invalid DIDX section")
        data = section_locations.get("DATA")
        if data is None:
            raise WwiseBankError("Wwise bank does not contain a DATA section")
        _data_offset, data_size = data
        for entry_offset in range(didx_offset, didx_offset + didx_size, 12):
            media_id, relative_offset, media_size = struct.unpack_from(
                "<III", bank_data, entry_offset
            )
            if relative_offset + media_size > data_size:
                raise WwiseBankError(f"Embedded media {media_id} is out of bounds")
            media_ids.append(media_id)
            embedded_media_bytes += media_size

    return WwiseBankSummary(
        bank_version=bank_version,
        sections=tuple(sections),
        media_ids=tuple(media_ids),
        embedded_media_bytes=embedded_media_bytes,
        hirc_object_count=hirc_object_count,
    )


def inspect_bank(bank):
    bank = Path(bank).expanduser().resolve()
    if not bank.is_file():
        raise WwiseBankError(f"Wwise bank does not exist: {bank}")
    return inspect_bank_data(bank.read_bytes())


def extract_embedded_media(bank_data):
    didx_offset = bank_data.find(b"DIDX")
    if didx_offset < 0 or didx_offset + 8 > len(bank_data):
        raise WwiseBankError("Wwise bank does not contain a DIDX section")
    didx_size = struct.unpack_from("<I", bank_data, didx_offset + 4)[0]
    if didx_size == 0 or didx_size % 12:
        raise WwiseBankError("Wwise bank has an invalid DIDX section")

    data_offset = bank_data.find(b"DATA", didx_offset + 8 + didx_size)
    if data_offset < 0 or data_offset + 8 > len(bank_data):
        raise WwiseBankError("Wwise bank does not contain a DATA section")
    data_size = struct.unpack_from("<I", bank_data, data_offset + 4)[0]
    data_start = data_offset + 8
    data_end = data_start + data_size
    if data_end > len(bank_data):
        raise WwiseBankError("Wwise bank DATA section is truncated")

    media = []
    for offset in range(didx_offset + 8, didx_offset + 8 + didx_size, 12):
        media_id, relative_offset, media_size = struct.unpack_from(
            "<III", bank_data, offset
        )
        media_start = data_start + relative_offset
        media_end = media_start + media_size
        if media_start < data_start or media_end > data_end:
            raise WwiseBankError(f"Embedded media {media_id} is out of bounds")
        media.append(EmbeddedMedia(media_id, bank_data[media_start:media_end]))
    return media


def read_embedded_media(bank):
    bank = Path(bank).expanduser().resolve()
    if not bank.is_file():
        raise WwiseBankError(f"Wwise bank does not exist: {bank}")
    return extract_embedded_media(bank.read_bytes())


def extract_bank(bank, output_directory, *, limit=None, overwrite=False):
    if limit is not None and limit <= 0:
        raise WwiseBankError("Media limit must be positive")
    media = read_embedded_media(bank)
    if limit is not None:
        media = media[:limit]

    output_directory = Path(output_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    outputs = []
    for item in media:
        output = output_directory / f"{item.media_id}.wem"
        if output.exists() and not overwrite:
            raise WwiseBankError(
                f"Output already exists: {output}; pass --overwrite to replace it"
            )
        output.write_bytes(item.data)
        outputs.append(output)
    return outputs


def resolve_decoder(decoder="vgmstream-cli"):
    decoder = str(decoder)
    decoder_path = shutil.which(decoder)
    expanded_decoder = Path(decoder).expanduser()
    if decoder_path is None and expanded_decoder.is_file():
        decoder_path = str(expanded_decoder.resolve())
    if decoder_path is None:
        raise AudioConversionError(
            "vgmstream-cli was not found. Install vgmstream or pass --decoder."
        )
    return decoder_path


def convert_audio(
    source,
    output,
    *,
    decoder="vgmstream-cli",
    overwrite=False,
    runner=subprocess.run,
):
    source = Path(source).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if not source.is_file():
        raise AudioConversionError(f"Input audio does not exist: {source}")
    if output.exists() and not overwrite:
        raise AudioConversionError(
            f"Output already exists: {output}; pass --overwrite to replace it"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    result = runner(
        [resolve_decoder(decoder), "-o", str(output), str(source)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AudioConversionError(
            f"Unable to convert {source.name}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    if not output.is_file():
        raise AudioConversionError(f"Decoder did not create output: {output}")
    return output


def extract_and_convert_bank(
    bank,
    output_directory,
    *,
    limit=None,
    decoder="vgmstream-cli",
    overwrite=False,
):
    sources = extract_bank(
        bank,
        output_directory,
        limit=limit,
        overwrite=overwrite,
    )
    return [
        convert_audio(
            source,
            source.with_suffix(".wav"),
            decoder=decoder,
            overwrite=overwrite,
        )
        for source in sources
    ]
