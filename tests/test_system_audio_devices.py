import ctypes
import json
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch

from vntts import system_audio_devices as devices


def write_pointer(destination, value):
    destination._obj.value = value


class MacProbeTest(unittest.TestCase):
    def setUp(self):
        self.uid = "headphones-A"
        self.properties = {
            (1, "dOut", "glob"): struct.pack("=I", 7),
            (1, "dev#", "glob"): struct.pack("=I", 7),
            (7, "stm#", "outp"): struct.pack("=I", 8),
            (7, "uid ", "glob"): bytes(ctypes.c_void_p(123)),
            (7, "livn", "glob"): struct.pack("=I", 1),
            (8, "sfmt", "glob"): struct.pack("=d8I", 48_000, 1, 1, 8, 1, 8, 2, 32, 0),
        }
        audio, cf = Mock(), Mock()

        def get_property(device, address):
            value = address._obj
            selector = value.selector.to_bytes(4, "big").decode()
            scope = value.scope.to_bytes(4, "big").decode()
            return self.properties[device, selector, scope]

        def get_size(device, address, _qualifier_size, _qualifier, size):
            write_pointer(size, len(get_property(device, address)))
            return 0

        def get_data(device, address, _qualifier_size, _qualifier, size, data):
            value = get_property(device, address)
            ctypes.memmove(data, value, len(value))
            write_pointer(size, len(value))
            return 0

        def string_data(_pointer, buffer, _capacity, _encoding):
            buffer.value = self.uid.encode()
            return True

        audio.AudioObjectGetPropertyDataSize.side_effect = get_size
        audio.AudioObjectGetPropertyData.side_effect = get_data
        cf.CFStringGetLength.return_value = 100
        cf.CFStringGetMaximumSizeForEncoding.return_value = 400
        cf.CFStringGetCString.side_effect = string_data
        with patch.object(ctypes, "CDLL", side_effect=[audio, cf]):
            self.probe = devices._MacOutputProbe()
        self.audio, self.cf = audio, cf

    def test_uid_replacement_with_same_numeric_id_and_format_changes(self):
        original = self.probe()
        self.assertEqual(original, self.probe())
        self.uid = "headphones-B"
        replacement = self.probe()
        self.assertNotEqual(original, replacement)
        self.properties[8, "sfmt", "glob"] = struct.pack(
            "=d8I", 24_000, 1, 1, 4, 1, 4, 1, 32, 0
        )
        self.assertNotEqual(replacement, self.probe())
        self.assertEqual(self.cf.CFRelease.call_count, 4)

    def test_no_default_is_known_state_but_native_failure_raises(self):
        self.properties[1, "dOut", "glob"] = struct.pack("=I", 0)
        self.assertIsNone(self.probe()[1])
        self.audio.AudioObjectGetPropertyDataSize.side_effect = None
        self.audio.AudioObjectGetPropertyDataSize.return_value = -1
        with self.assertRaises(OSError):
            self.probe()

    def test_cf_string_released_when_conversion_fails(self):
        self.cf.CFStringGetCString.side_effect = None
        self.cf.CFStringGetCString.return_value = False
        with self.assertRaises(OSError):
            self.probe()
        self.cf.CFRelease.assert_called_once()


class WindowsProbeTest(unittest.TestCase):
    def setUp(self):
        self.ole = Mock()
        self.ole.CoInitializeEx.return_value = 0

        def create(_clsid, _outer, _context, _iid, output):
            write_pointer(output, 10)
            return 0

        self.ole.CoCreateInstance.side_effect = create
        with patch.object(ctypes, "WinDLL", return_value=self.ole, create=True):
            self.probe = devices._WindowsOutputProbe()
        self.released = []
        self.buffers = []
        self.endpoint_ids = {20: "{unique-A}", 21: "{unique-B}"}
        self.default_status = 0
        self.failure = None
        self.roles = []

        def method(pointer, index, *_types):
            def invoke(_self, *arguments):
                identity = pointer.value
                if index == 2:
                    self.released.append(identity)
                    return 0
                if self.failure == (identity, index):
                    return -2147467259
                if identity == 10 and index == 4:
                    self.roles.append(arguments[1])
                    if not self.default_status:
                        write_pointer(arguments[-1], 20 + arguments[1])
                    return self.default_status
                if identity == 10 and index == 3:
                    write_pointer(arguments[-1], 30)
                elif identity == 30 and index == 3:
                    write_pointer(arguments[-1], 2)
                elif identity == 30 and index == 4:
                    write_pointer(arguments[-1], 20 + arguments[0])
                elif index == 5:
                    buffer = ctypes.create_unicode_buffer(self.endpoint_ids[identity])
                    self.buffers.append(buffer)
                    write_pointer(arguments[-1], ctypes.addressof(buffer))
                elif index == 6:
                    write_pointer(arguments[-1], 1)
                else:
                    raise AssertionError((identity, index))
                return 0

            return invoke

        patcher = patch.object(devices, "_com_method", side_effect=method)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unique_ids_both_default_roles_topology_and_pointer_prototypes(self):
        original = self.probe()
        self.assertEqual(self.roles, [0, 1])
        self.endpoint_ids[21] = "{unique-C}"
        self.assertNotEqual(original, self.probe())
        self.assertEqual(self.ole.CoTaskMemFree.call_count, 8)
        self.assertEqual(self.ole.CoUninitialize.call_count, 2)
        self.assertEqual(
            self.ole.CoCreateInstance.argtypes[-1], ctypes.POINTER(ctypes.c_void_p)
        )
        self.assertEqual(self.ole.CoTaskMemFree.argtypes, [ctypes.c_void_p])

    def test_no_output_is_distinct_from_api_failure(self):
        self.default_status = -2147023728  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
        self.assertEqual(self.probe()[1], (None, None))
        self.default_status = -2147467259
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.ole.CoUninitialize.call_count, 2)
        self.assertEqual(self.released[-1], 10)

    def test_identity_failure_releases_endpoint_string_collection_and_enumerator(self):
        self.failure = (21, 6)
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.released, [20, 21, 10])
        self.assertEqual(self.ole.CoTaskMemFree.call_count, 2)
        self.ole.CoUninitialize.assert_called_once()
        self.failure = (30, 4)
        self.released.clear()
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.released, [20, 21, 30, 10])

    def test_existing_sta_does_not_acquire_or_release_com_count(self):
        self.ole.CoInitializeEx.return_value = -2147417850  # RPC_E_CHANGED_MODE
        self.probe()
        self.ole.CoUninitialize.assert_not_called()
        self.ole.CoInitializeEx.return_value = 1  # S_FALSE acquires a count.
        self.probe()
        self.ole.CoUninitialize.assert_called_once()

    def test_initialization_failure_does_not_uninitialize(self):
        self.ole.CoInitializeEx.return_value = -2147467259
        with self.assertRaises(OSError):
            self.probe()
        self.ole.CoCreateInstance.assert_not_called()
        self.ole.CoUninitialize.assert_not_called()

    def test_failed_collection_release_does_not_skip_com_cleanup(self):
        release = self.probe._release

        def failing_release(pointer):
            if pointer.value == 30:
                raise OSError("Collection release failed")
            release(pointer)

        with patch.object(self.probe, "_release", side_effect=failing_release):
            with self.assertRaisesRegex(OSError, "Collection release failed"):
                self.probe()
        self.assertEqual(self.released[-1], 10)
        self.ole.CoUninitialize.assert_called_once()


class ComAbiTest(unittest.TestCase):
    def test_vtable_keeps_full_native_pointer_and_32_bit_hresult(self):
        seen = []

        @ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32)
        def callback(pointer, value):
            seen.append((pointer, value))
            return -2147467259

        vtable = (ctypes.c_void_p * 1)(ctypes.cast(callback, ctypes.c_void_p).value)
        interface = ctypes.c_void_p(ctypes.addressof(vtable))
        pointer = ctypes.c_void_p(ctypes.addressof(interface))
        with patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            method = devices._com_method(pointer, 0, ctypes.c_uint32)
            self.assertEqual(method(pointer, 41), -2147467259)
        self.assertEqual(seen, [(pointer.value, 41)])
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertGreater(pointer.value, 0xFFFFFFFF)


class LinuxProbeTest(unittest.TestCase):
    def test_pulse_default_routing_and_format_without_transient_running_state(self):
        sink = {
            "index": 1,
            "name": "headphones",
            "sample_specification": "float32le 2ch 48000Hz",
            "channel_map": "front-left,front-right",
            "state": "RUNNING",
        }
        with (
            patch.object(devices.shutil, "which", return_value="/usr/bin/pactl"),
            patch.object(devices.subprocess, "run") as run,
        ):

            def observation(default):
                run.side_effect = [
                    Mock(stdout=json.dumps({"default_sink_name": default})),
                    Mock(stdout=json.dumps([sink])),
                ]
                return devices._linux_output_fingerprint()

            original = observation("headphones")
            sink["state"] = "IDLE"
            self.assertEqual(original, observation("headphones"))
            self.assertNotEqual(original, observation("speakers"))
            self.assertEqual(run.call_args.kwargs["timeout"], 1)
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_pulse_failure_raises_instead_of_claiming_output_disconnected(self):
        with (
            patch.object(devices.shutil, "which", return_value="pactl"),
            patch.object(
                devices.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("pactl", 1),
            ),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                devices._linux_output_fingerprint()

    def test_alsa_fallback_records_topology_and_config(self):
        with (
            patch.object(devices.shutil, "which", return_value=None),
            patch.object(
                devices.Path,
                "read_bytes",
                side_effect=[b"cards", b"pcm", FileNotFoundError(), b"default"],
            ),
        ):
            self.assertEqual(
                devices._linux_output_fingerprint(),
                ("alsa", b"cards", b"pcm", (None, b"default")),
            )

    def test_missing_alsa_is_a_known_state_and_later_hardware_is_detected(self):
        with (
            patch.object(devices.shutil, "which", return_value=None),
            patch.object(devices.Path, "read_bytes", side_effect=FileNotFoundError()),
        ):
            absent = devices._linux_output_fingerprint()
        self.assertEqual(absent, ("alsa", None, None, (None, None)))
        with (
            patch.object(devices.shutil, "which", return_value=None),
            patch.object(
                devices.Path,
                "read_bytes",
                side_effect=[
                    b"USB",
                    b"playback",
                    FileNotFoundError(),
                    FileNotFoundError(),
                ],
            ),
        ):
            self.assertNotEqual(absent, devices._linux_output_fingerprint())


if __name__ == "__main__":
    unittest.main()
