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
class WwiseActionReference:
    action_id: int
    action_type: int
    target_id: int


@dataclass(frozen=True)
class WwiseEventRoute:
    event_id: int
    action_ids: tuple[int, ...]
    actions: tuple[WwiseActionReference, ...]
    sound_ids: tuple[int, ...]
    media_ids: tuple[int, ...]


@dataclass(frozen=True)
class WwiseBankSummary:
    bank_version: int | None
    sections: tuple[str, ...]
    media_ids: tuple[int, ...]
    embedded_media_bytes: int
    hirc_object_count: int | None
    event_routes: tuple[WwiseEventRoute, ...] = ()

    @property
    def media_count(self):
        return len(self.media_ids)

    @property
    def event_count(self):
        return len(self.event_routes)


@dataclass(frozen=True)
class _HircObject:
    object_type: int
    object_id: int
    payload: bytes


def _read_varint(data, offset):
    value = 0
    for _index in range(11):
        if offset >= len(data):
            raise WwiseBankError("Wwise HIRC variable integer is truncated")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise WwiseBankError("Wwise HIRC variable integer is too long")


def _parse_hirc_objects(payload):
    if len(payload) < 4:
        raise WwiseBankError("Wwise bank has an invalid HIRC section")
    object_count = struct.unpack_from("<I", payload)[0]
    objects = []
    offset = 4
    for index in range(object_count):
        if offset + 5 > len(payload):
            raise WwiseBankError(f"Wwise HIRC object {index} header is truncated")
        object_type = payload[offset]
        object_size = struct.unpack_from("<I", payload, offset + 1)[0]
        object_start = offset + 5
        object_end = object_start + object_size
        if object_size < 4 or object_end > len(payload):
            raise WwiseBankError(f"Wwise HIRC object {index} is truncated")
        object_id = struct.unpack_from("<I", payload, object_start)[0]
        objects.append(
            _HircObject(
                object_type=object_type,
                object_id=object_id,
                payload=payload[object_start + 4 : object_end],
            )
        )
        offset = object_end
    if offset != len(payload) and any(payload[offset:]):
        raise WwiseBankError("Wwise HIRC section has trailing data")
    return tuple(objects)


def _parse_event_action_ids(payload, bank_version):
    offset = 0
    if bank_version is not None and bank_version > 154:
        offset = 7
    if bank_version is not None and bank_version <= 122:
        if offset + 4 > len(payload):
            raise WwiseBankError("Wwise event action count is truncated")
        count = struct.unpack_from("<I", payload, offset)[0]
        offset += 4
    else:
        count, offset = _read_varint(payload, offset)
    end = offset + count * 4
    if end > len(payload):
        raise WwiseBankError("Wwise event action list is truncated")
    return tuple(
        struct.unpack_from("<I", payload, offset + index * 4)[0]
        for index in range(count)
    )


def _parse_action(action, bank_version):
    payload = action.payload
    if bank_version is not None and bank_version <= 56:
        if len(payload) < 20:
            raise WwiseBankError(f"Wwise action {action.object_id} is truncated")
        action_type = struct.unpack_from("<I", payload)[0]
        target_id = struct.unpack_from("<I", payload, 4)[0]
    else:
        if len(payload) < 7:
            raise WwiseBankError(f"Wwise action {action.object_id} is truncated")
        action_type = struct.unpack_from("<H", payload)[0]
        target_id = struct.unpack_from("<I", payload, 2)[0]
    return WwiseActionReference(action.object_id, action_type, target_id)


def _parse_sound_media_id(sound, bank_version):
    payload = sound.payload
    stream_type_size = 4 if bank_version is not None and bank_version <= 89 else 1
    source_id_offset = 4 + stream_type_size
    if source_id_offset + 4 > len(payload):
        raise WwiseBankError(f"Wwise sound {sound.object_id} is truncated")
    return struct.unpack_from("<I", payload, source_id_offset)[0]


def _skip_parameter_node_effects(payload, offset, bank_version):
    if offset + 2 > len(payload):
        return None
    offset += 1
    effect_count = payload[offset]
    offset += 1
    if effect_count:
        offset += 1
        effect_size = 7 if bank_version <= 145 else 6
        offset += effect_count * effect_size
    if bank_version > 136:
        if offset + 2 > len(payload):
            return None
        offset += 1
        metadata_count = payload[offset]
        offset += 1 + metadata_count * 6
    return offset


def _parse_parameter_node_parent_id(payload, bank_version, *, offset=0):
    if bank_version is None or bank_version < 90:
        return None
    offset = _skip_parameter_node_effects(payload, offset, bank_version)
    if offset is None:
        return None
    if bank_version <= 145:
        offset += 1
    if offset + 8 > len(payload):
        return None
    return struct.unpack_from("<I", payload, offset + 4)[0]


def _parse_sound_parent_id(sound, bank_version):
    if bank_version is None or bank_version < 90:
        return None
    stream_type = sound.payload[4] if len(sound.payload) > 4 else None
    if stream_type is None:
        return None
    node_offset = 9
    if bank_version <= 112:
        node_offset += 4
        if stream_type != 2:
            node_offset += 4
        node_offset += 5
    elif bank_version <= 150:
        node_offset += 5
    else:
        node_offset += 9
    return _parse_parameter_node_parent_id(
        sound.payload,
        bank_version,
        offset=node_offset,
    )


def _is_descendant_of(node_id, ancestor_id, parent_ids):
    visited = set()
    current = node_id
    while current and current not in visited:
        if current == ancestor_id:
            return True
        visited.add(current)
        current = parent_ids.get(current)
    return False


def parse_hirc_event_routes(payload, bank_version):
    """Parse the event -> action -> sound/media relationships from HIRC.

    The field layout follows the versioned HIRC schema used by wwiser. Unknown
    HIRC object types remain safely skippable because every object is framed.
    """
    objects = _parse_hirc_objects(payload)
    actions = {
        item.object_id: _parse_action(item, bank_version)
        for item in objects
        if item.object_type == 0x03
    }
    sound_objects = {
        item.object_id: item for item in objects if item.object_type == 0x02
    }
    sounds = {
        sound_id: _parse_sound_media_id(item, bank_version)
        for sound_id, item in sound_objects.items()
    }
    parent_ids = {
        sound_id: parent_id
        for sound_id, item in sound_objects.items()
        if (parent_id := _parse_sound_parent_id(item, bank_version)) is not None
    }
    for item in objects:
        if item.object_type not in {0x05, 0x06, 0x07, 0x09}:
            continue
        parent_id = _parse_parameter_node_parent_id(item.payload, bank_version)
        if parent_id is not None:
            parent_ids[item.object_id] = parent_id
    routes = []
    for item in objects:
        if item.object_type != 0x04:
            continue
        action_ids = _parse_event_action_ids(item.payload, bank_version)
        route_actions = tuple(
            actions[action_id] for action_id in action_ids if action_id in actions
        )
        targets = {action.target_id for action in route_actions}
        sound_ids = tuple(
            sound_id
            for sound_id in sounds
            if any(
                _is_descendant_of(sound_id, target_id, parent_ids)
                for target_id in targets
            )
        )
        media_ids = tuple(dict.fromkeys(sounds[sound_id] for sound_id in sound_ids))
        routes.append(
            WwiseEventRoute(
                item.object_id,
                action_ids,
                route_actions,
                sound_ids,
                media_ids,
            )
        )
    return tuple(routes)


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
    event_routes = ()
    hirc = section_locations.get("HIRC")
    if hirc is not None:
        if hirc[1] < 4:
            raise WwiseBankError("Wwise bank has an invalid HIRC section")
        hirc_object_count = struct.unpack_from("<I", bank_data, hirc[0])[0]
        event_routes = parse_hirc_event_routes(
            bank_data[hirc[0] : hirc[0] + hirc[1]],
            bank_version,
        )

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
        event_routes=event_routes,
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
        [resolve_decoder(decoder), "-i", "-o", str(output), str(source)],
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
