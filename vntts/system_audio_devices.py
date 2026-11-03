"""Headless OS fingerprints; these probes never open or reset PortAudio."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID


@dataclass(frozen=True)
class OutputDeviceSnapshot:
    selected: object
    inventory: object


def create_default_output_probe(
    host_api_type: int | None = None,
) -> Callable[[], OutputDeviceSnapshot]:
    """Return a probe; missing output is a value, failed observation raises."""
    if sys.platform == "darwin":
        return _MacOutputProbe()
    if sys.platform == "win32":
        return _WindowsOutputProbe(host_api_type)
    if sys.platform.startswith("linux"):
        return _linux_output_fingerprint
    raise OSError(f"System audio device probing is unsupported on {sys.platform}")


class _PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


class _MacOutputProbe:
    def __init__(self) -> None:
        self.audio = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
        self.cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        arguments: list[Any] = [
            ctypes.c_uint32,
            ctypes.POINTER(_PropertyAddress),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.audio.AudioObjectGetPropertyDataSize.argtypes = arguments
        self.audio.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self.audio.AudioObjectGetPropertyData.argtypes = arguments + [ctypes.c_void_p]
        self.audio.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self.cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self.cf.CFStringGetLength.restype = ctypes.c_long
        self.cf.CFStringGetMaximumSizeForEncoding.argtypes = [
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        self.cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.restype = None

    def _property(self, device: int, selector: str, scope: str = "glob") -> bytes:
        address = _PropertyAddress(
            int.from_bytes(selector.encode(), "big"),
            int.from_bytes(scope.encode(), "big"),
            0,
        )
        size = ctypes.c_uint32()
        status = self.audio.AudioObjectGetPropertyDataSize(
            device, ctypes.byref(address), 0, None, ctypes.byref(size)
        )
        if status:
            raise OSError(f"CoreAudio {selector} size failed: {status}")
        if size.value > 1_048_576:
            raise OSError("CoreAudio returned an invalid property size")
        buffer = ctypes.create_string_buffer(size.value)
        status = self.audio.AudioObjectGetPropertyData(
            device, ctypes.byref(address), 0, None, ctypes.byref(size), buffer
        )
        if status or size.value > len(buffer):
            raise OSError(f"CoreAudio {selector} read failed: {status}")
        return buffer.raw[: size.value]

    def _uid(self, device: int) -> str:
        value = ctypes.c_void_p.from_buffer_copy(self._property(device, "uid "))
        if not value.value:
            raise OSError("CoreAudio returned an empty device UID")
        try:
            length = self.cf.CFStringGetLength(value)
            capacity = self.cf.CFStringGetMaximumSizeForEncoding(length, 0x08000100)
            if not 0 <= capacity <= 1_048_576:
                raise OSError("CoreAudio returned an invalid device UID size")
            buffer = ctypes.create_string_buffer(capacity + 1)
            if not self.cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
                raise OSError("CoreAudio device UID conversion failed")
            return buffer.value.decode("utf-8")
        finally:
            self.cf.CFRelease(value)

    def __call__(self) -> OutputDeviceSnapshot:
        default = struct.unpack("=I", self._property(1, "dOut"))[0]
        ids = self._property(1, "dev#")
        devices = []
        for (device,) in struct.iter_unpack("=I", ids):
            streams = self._property(device, "stm#", "outp")
            if not streams and device != default:
                continue
            uid = self._uid(device)
            alive = struct.unpack("=I", self._property(device, "livn"))[0]
            devices.append((device, uid, alive))
        if default and not any(device[0] == default for device in devices):
            raise OSError("CoreAudio default output disappeared during observation")
        selected = None
        if default:
            identity = next(device for device in devices if device[0] == default)
            streams = self._property(default, "stm#", "outp")
            formats = tuple(
                (stream, struct.unpack("=d8I", self._property(stream, "sfmt"))[:-1])
                for (stream,) in struct.iter_unpack("=I", streams)
            )
            selected = ("coreaudio", *identity, formats)
        return OutputDeviceSnapshot(selected, ("coreaudio", tuple(sorted(devices))))


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_uint16),
        ("reserved", ctypes.c_uint16 * 3),
        ("payload", ctypes.c_void_p * 2),
    ]


def _check_hresult(status: int, operation: str) -> None:
    if ctypes.c_int32(status).value < 0:
        raise OSError(f"{operation} failed: HRESULT 0x{status & 0xFFFFFFFF:08x}")


def _com_method(
    pointer: ctypes.c_void_p, index: int, *arguments: Any
) -> Callable[..., int]:
    if not pointer.value:
        raise OSError("MMDevice returned an empty interface")
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return cast(
        Callable[..., int],
        getattr(ctypes, "WINFUNCTYPE")(ctypes.c_int32, ctypes.c_void_p, *arguments)(
            vtable[index]
        ),
    )


class _WindowsOutputProbe:
    def __init__(self, host_api_type: int | None = None) -> None:
        if host_api_type not in (1, 2, 13):
            raise OSError(f"Unsupported Windows PortAudio host API: {host_api_type}")
        self.host_api_type = host_api_type
        self.ole = getattr(ctypes, "WinDLL")("ole32")
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole.CoInitializeEx.restype = ctypes.c_int32
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoCreateInstance.argtypes = [
            ctypes.POINTER(_GUID),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.ole.CoCreateInstance.restype = ctypes.c_int32
        self.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole.CoTaskMemFree.restype = None
        self.ole.PropVariantClear.argtypes = [ctypes.c_void_p]
        self.ole.PropVariantClear.restype = ctypes.c_int32
        if host_api_type == 2:
            self.winmm = getattr(ctypes, "WinDLL")("winmm")
            self.winmm.waveOutMessage.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_size_t,
                ctypes.c_size_t,
            ]
            self.winmm.waveOutMessage.restype = ctypes.c_uint32
            for direction in ("In", "Out"):
                count = getattr(self.winmm, f"wave{direction}GetNumDevs")
                count.argtypes, count.restype = [], ctypes.c_uint32
                caps = getattr(self.winmm, f"wave{direction}GetDevCapsW")
                caps.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint32]
                caps.restype = ctypes.c_uint32
        if host_api_type == 1:
            self.dsound = getattr(ctypes, "WinDLL")("dsound")
            self.dsound.GetDeviceID.argtypes = [
                ctypes.POINTER(_GUID),
                ctypes.POINTER(_GUID),
            ]
            self.dsound.GetDeviceID.restype = ctypes.c_int32

    def _release(self, pointer: ctypes.c_void_p) -> None:
        if pointer.value:
            _com_method(pointer, 2)(pointer)

    def _identity(self, device: ctypes.c_void_p) -> tuple[str, int]:
        identifier = ctypes.c_void_p()
        try:
            _check_hresult(
                _com_method(device, 5, ctypes.POINTER(ctypes.c_void_p))(
                    device, ctypes.byref(identifier)
                ),
                "IMMDevice.GetId",
            )
            if not identifier.value:
                raise OSError("MMDevice returned an empty endpoint ID")
            name = ctypes.wstring_at(identifier)
            state = ctypes.c_uint32()
            _check_hresult(
                _com_method(device, 6, ctypes.POINTER(ctypes.c_uint32))(
                    device, ctypes.byref(state)
                ),
                "IMMDevice.GetState",
            )
            return name, state.value
        finally:
            if identifier.value:
                self.ole.CoTaskMemFree(identifier)

    def _mix_format(self, device: ctypes.c_void_p) -> bytes:
        client, value = ctypes.c_void_p(), ctypes.c_void_p()
        iid = _GUID.from_buffer_copy(
            UUID("1cb9ad4c-dbfa-4c32-b178-c2f568a703b2").bytes_le
        )
        try:
            _check_hresult(
                _com_method(
                    device,
                    3,
                    ctypes.POINTER(_GUID),
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                )(device, ctypes.byref(iid), 23, None, ctypes.byref(client)),
                "IMMDevice.Activate(IAudioClient)",
            )
            _check_hresult(
                _com_method(client, 8, ctypes.POINTER(ctypes.c_void_p))(
                    client, ctypes.byref(value)
                ),
                "IAudioClient.GetMixFormat",
            )
            if not value.value:
                raise OSError("IAudioClient returned an empty mix format")
            header = ctypes.string_at(value, 18)  # Packed WAVEFORMATEX includes cbSize.
            extra = struct.unpack_from("<H", header, 16)[0]
            if extra > 4096:
                raise OSError("IAudioClient returned an invalid mix format size")
            return header + ctypes.string_at(value.value + 18, extra)
        finally:
            try:
                if value.value:
                    self.ole.CoTaskMemFree(value)
            finally:
                self._release(client)

    def _wave_message(self, device: int, message: int, first: int, second: int) -> None:
        status = self.winmm.waveOutMessage(
            ctypes.c_void_p(device), message, first, second
        )
        if status:
            raise OSError(f"waveOutMessage(0x{message:x}) failed: {status}")

    def _mme_endpoint(self) -> str | None:
        if not self.winmm.waveOutGetNumDevs():
            return None
        override = os.environ.get("PA_RECOMMENDED_OUTPUT_DEVICE", "")
        wave_id = None
        if override and len(override.encode()) < 32:
            # PA uses atoi and counts only successful caps, inputs before outputs.
            match = re.match(r"\s*([+-]?[0-9]+)", override)
            recommended = int(match[1]) if match else 0
            local_index = 0
            for direction in ("In", "Out"):
                count = getattr(self.winmm, f"wave{direction}GetNumDevs")()
                for index in range(-1, count) if count else ():
                    native_id = index & 0xFFFFFFFF
                    caps = ctypes.create_string_buffer(80 if direction == "In" else 84)
                    status = getattr(self.winmm, f"wave{direction}GetDevCapsW")(
                        native_id, caps, len(caps)
                    )
                    if status == 7:  # MMSYSERR_NOMEM also aborts PA initialization.
                        raise OSError("waveGetDevCapsW failed: out of memory")
                    if status:
                        continue
                    if direction == "Out" and local_index == recommended:
                        wave_id = native_id
                    local_index += 1
        if wave_id is None or wave_id == 0xFFFFFFFF:
            preferred, flags = ctypes.c_uint32(0xFFFFFFFF), ctypes.c_uint32()
            self._wave_message(
                0xFFFFFFFF, 0x2015, ctypes.addressof(preferred), ctypes.addressof(flags)
            )  # DRVM_MAPPER_PREFERRED_GET
            wave_id = preferred.value
        size = ctypes.c_uint32()
        self._wave_message(wave_id, 0x0812, ctypes.addressof(size), 0)
        if not 2 <= size.value <= 1_048_576 or size.value % 2:
            raise OSError("waveOut returned an invalid endpoint ID size")
        buffer = ctypes.create_string_buffer(size.value)
        self._wave_message(wave_id, 0x0811, ctypes.addressof(buffer), size.value)
        if not buffer.raw.endswith(b"\0\0"):
            raise OSError("waveOut returned an unterminated endpoint ID")
        identifier = buffer.raw.decode("utf-16-le").rstrip("\0")
        if not identifier:
            raise OSError("waveOut returned an empty endpoint ID")
        return identifier

    def _directsound_guid(self, device: ctypes.c_void_p) -> UUID:
        store = ctypes.c_void_p()
        value = _PropVariant()
        key = _PropertyKey(
            _GUID.from_buffer_copy(
                UUID("1da5d803-d492-4edd-8c23-e0c0ffee7f0e").bytes_le
            ),
            4,
        )
        try:
            _check_hresult(
                _com_method(
                    device, 4, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
                )(device, 0, ctypes.byref(store)),
                "IMMDevice.OpenPropertyStore",
            )
            _check_hresult(
                _com_method(store, 5, ctypes.POINTER(_PropertyKey), ctypes.c_void_p)(
                    store, ctypes.byref(key), ctypes.byref(value)
                ),
                "IPropertyStore.GetValue",
            )
            if value.vt != 31 or not value.payload[0]:  # VT_LPWSTR
                raise OSError("MMDevice returned an invalid DirectSound endpoint GUID")
            return UUID(ctypes.wstring_at(value.payload[0]))
        finally:
            try:
                _check_hresult(
                    self.ole.PropVariantClear(ctypes.byref(value)), "PropVariantClear"
                )
            finally:
                self._release(store)

    def _selected(
        self, enumerator: ctypes.c_void_p, endpoints: list[tuple[str, int, UUID | None]]
    ) -> object:
        device = ctypes.c_void_p()
        try:
            if self.host_api_type == 13:
                status = _com_method(
                    enumerator,
                    4,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p),
                )(enumerator, 0, 1, ctypes.byref(device))  # WASAPI uses eMultimedia.
            else:
                if self.host_api_type == 2:
                    identifier = self._mme_endpoint()
                    if identifier is None:
                        return None
                else:
                    if not any(state == 1 for _, state, _ in endpoints):
                        return None
                    default = _GUID.from_buffer_copy(
                        UUID("def00000-9c6d-47ed-aaf1-4dda8f2b5c03").bytes_le
                    )
                    concrete = _GUID()
                    status = self.dsound.GetDeviceID(
                        ctypes.byref(default), ctypes.byref(concrete)
                    )
                    if status & 0xFFFFFFFF == 0x88780078:  # DSERR_NODRIVER
                        return None
                    _check_hresult(status, "DirectSound.GetDeviceID")
                    guid = UUID(bytes_le=bytes(concrete))
                    identifier = next(
                        (name for name, _, native in endpoints if native == guid), None
                    )
                    if identifier is None:
                        raise OSError(
                            "DirectSound default endpoint disappeared during observation"
                        )
                status = _com_method(
                    enumerator, 5, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)
                )(enumerator, identifier, ctypes.byref(device))
            if status & 0xFFFFFFFF == 0x80070490:  # ERROR_NOT_FOUND
                return None
            _check_hresult(status, "Resolve selected audio endpoint")
            identity = self._identity(device)
            return (
                "mmdevice",
                *identity,
                self._mix_format(device) if identity[1] == 1 else None,
            )
        finally:
            self._release(device)

    def __call__(self) -> OutputDeviceSnapshot:
        status = self.ole.CoInitializeEx(None, 0)
        # An existing STA is usable too; only successful calls acquire a COM count.
        if status & 0xFFFFFFFF != 0x80010106:
            _check_hresult(status, "CoInitializeEx")
        initialized = ctypes.c_int32(status).value >= 0
        enumerator, collection = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            clsid = _GUID.from_buffer_copy(
                UUID("bcde0395-e52f-467c-8e3d-c4579291692e").bytes_le
            )
            iid = _GUID.from_buffer_copy(
                UUID("a95664d2-9614-4f35-a746-de8db63617e6").bytes_le
            )
            _check_hresult(
                self.ole.CoCreateInstance(
                    ctypes.byref(clsid),
                    None,
                    1,
                    ctypes.byref(iid),
                    ctypes.byref(enumerator),
                ),
                "CoCreateInstance(MMDeviceEnumerator)",
            )
            _check_hresult(
                _com_method(
                    enumerator,
                    3,
                    ctypes.c_int,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p),
                )(enumerator, 0, 0xF, ctypes.byref(collection)),
                "EnumAudioEndpoints",
            )
            count = ctypes.c_uint32()
            _check_hresult(
                _com_method(collection, 3, ctypes.POINTER(ctypes.c_uint32))(
                    collection, ctypes.byref(count)
                ),
                "IMMDeviceCollection.GetCount",
            )
            devices = []
            for index in range(count.value):
                device = ctypes.c_void_p()
                try:
                    _check_hresult(
                        _com_method(
                            collection,
                            4,
                            ctypes.c_uint32,
                            ctypes.POINTER(ctypes.c_void_p),
                        )(collection, index, ctypes.byref(device)),
                        "IMMDeviceCollection.Item",
                    )
                    identity = self._identity(device)
                    guid = (
                        self._directsound_guid(device)
                        if self.host_api_type == 1 and identity[1] == 1
                        else None
                    )
                    devices.append((*identity, guid))
                finally:
                    self._release(device)
            selected = self._selected(enumerator, devices)
            inventory = tuple(sorted((name, state) for name, state, _ in devices))
            return OutputDeviceSnapshot(selected, ("mmdevice", inventory))
        finally:
            try:
                self._release(collection)
            finally:
                try:
                    self._release(enumerator)
                finally:
                    if initialized:
                        self.ole.CoUninitialize()


def _linux_output_fingerprint() -> OutputDeviceSnapshot:
    pactl = shutil.which("pactl")
    if pactl:

        def query(*arguments: str) -> Any:
            result = subprocess.run(
                [pactl, "--format=json", *arguments],
                capture_output=True,
                text=True,
                check=True,
                timeout=1,
            )
            return json.loads(result.stdout)

        info = query("info")
        sinks = query("list", "sinks")
        # Sink RUNNING/IDLE/SUSPENDED changes during playback are not device changes.
        topology = tuple(
            sorted(
                (
                    sink["index"],
                    sink["name"],
                    sink["sample_specification"],
                    sink["channel_map"],
                )
                for sink in sinks
            )
        )
        default = info["default_sink_name"]
        selected = next((sink for sink in topology if sink[1] == default), None)
        if default and selected is None:
            raise OSError("PulseAudio default sink disappeared during observation")
        return OutputDeviceSnapshot(
            ("pulse", selected) if selected is not None else None,
            ("pulse", tuple((sink[0], sink[1]) for sink in topology)),
        )
    # shortcut: ALSA topology/config only; install pactl for Pulse/PipeWire routing.
    values: list[bytes | None] = []
    for path in (
        Path("/proc/asound/cards"),
        Path("/proc/asound/pcm"),
        Path("/etc/asound.conf"),
        Path.home() / ".asoundrc",
    ):
        try:
            values.append(path.read_bytes())
        except FileNotFoundError:
            values.append(None)
    fingerprint = ("alsa", values[0], values[1], tuple(values[2:]))
    return OutputDeviceSnapshot(fingerprint if any(values[:2]) else None, fingerprint)
