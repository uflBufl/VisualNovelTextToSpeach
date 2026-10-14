"""Headless OS fingerprints; these probes never open or reset PortAudio."""

from __future__ import annotations

import ctypes
import json
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import UUID


def create_default_output_probe() -> Callable[[], object]:
    """Return a probe; missing output is a value, failed observation raises."""
    if sys.platform == "darwin":
        return _MacOutputProbe()
    if sys.platform == "win32":
        return _WindowsOutputProbe()
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

    def __call__(self) -> object:
        default = struct.unpack("=I", self._property(1, "dOut"))[0]
        ids = self._property(1, "dev#")
        devices = []
        for (device,) in struct.iter_unpack("=I", ids):
            streams = self._property(device, "stm#", "outp")
            if not streams and device != default:
                continue
            uid = self._uid(device)
            alive = struct.unpack("=I", self._property(device, "livn"))[0]
            formats = tuple(
                (stream, struct.unpack("=d8I", self._property(stream, "sfmt"))[:-1])
                for (stream,) in struct.iter_unpack("=I", streams)
            )
            devices.append((device, uid, alive, formats))
        if default and not any(device[0] == default for device in devices):
            raise OSError("CoreAudio default output disappeared during observation")
        return ("coreaudio", default or None, tuple(sorted(devices)))


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
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
    def __init__(self) -> None:
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

    def __call__(self) -> object:
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
            defaults: list[tuple[str, int] | None] = []
            for role in (0, 1):
                device = ctypes.c_void_p()
                try:
                    status = _com_method(
                        enumerator,
                        4,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_void_p),
                    )(enumerator, 0, role, ctypes.byref(device))
                    if status & 0xFFFFFFFF == 0x80070490:
                        defaults.append(None)
                    else:
                        _check_hresult(status, "GetDefaultAudioEndpoint")
                        defaults.append(self._identity(device))
                finally:
                    self._release(device)
            _check_hresult(
                _com_method(
                    enumerator,
                    3,
                    ctypes.c_int,
                    ctypes.c_uint32,
                    ctypes.POINTER(ctypes.c_void_p),
                )(enumerator, 0, 1, ctypes.byref(collection)),
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
                    devices.append(self._identity(device))
                finally:
                    self._release(device)
            return "mmdevice", tuple(defaults), tuple(sorted(devices))
        finally:
            try:
                self._release(collection)
            finally:
                try:
                    self._release(enumerator)
                finally:
                    if initialized:
                        self.ole.CoUninitialize()


def _linux_output_fingerprint() -> object:
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
        return "pulse", info["default_sink_name"] or None, topology
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
    return "alsa", values[0], values[1], tuple(values[2:])
