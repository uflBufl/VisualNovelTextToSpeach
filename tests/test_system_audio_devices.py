import ctypes
import json
import struct
import subprocess
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

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
        original = self.probe()
        self.properties[1, "dOut", "glob"] = struct.pack("=I", 0)
        self.assertIsNone(self.probe().selected)
        self.properties[1, "dOut", "glob"] = struct.pack("=I", 7)
        self.assertEqual(original, self.probe())
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

    def test_unrelated_inventory_does_not_change_selected_or_read_its_format(self):
        original = self.probe()
        self.properties[1, "dev#", "glob"] = struct.pack("=2I", 7, 9)
        self.properties[9, "stm#", "outp"] = struct.pack("=I", 10)
        self.properties[9, "uid ", "glob"] = bytes(ctypes.c_void_p(123))
        self.properties[9, "livn", "glob"] = struct.pack("=I", 1)
        changed = self.probe()
        self.assertEqual(original.selected, changed.selected)
        self.assertNotEqual(original.inventory, changed.inventory)
        self.properties[9, "livn", "glob"] = struct.pack("=I", 0)
        self.assertEqual(original.selected, self.probe().selected)

    def test_native_id_alive_and_selected_stream_changes_are_detected(self):
        original = self.probe()
        self.properties[7, "livn", "glob"] = struct.pack("=I", 0)
        self.assertNotEqual(original.selected, self.probe().selected)
        self.properties[7, "livn", "glob"] = struct.pack("=I", 1)
        self.properties[7, "stm#", "outp"] = struct.pack("=I", 10)
        self.properties[10, "sfmt", "glob"] = self.properties[8, "sfmt", "glob"]
        self.assertNotEqual(original.selected, self.probe().selected)
        for (_device, selector, scope), value in list(self.properties.items()):
            if _device == 7:
                self.properties[9, selector, scope] = value
        self.properties[1, "dev#", "glob"] = struct.pack("=I", 9)
        self.properties[1, "dOut", "glob"] = struct.pack("=I", 9)
        self.assertNotEqual(original.selected, self.probe().selected)


class WindowsProbeTest(unittest.TestCase):
    def setUp(self):
        self.ole = Mock()
        self.ole.CoInitializeEx.return_value = 0
        self.ole.PropVariantClear.return_value = 0

        def create(_clsid, _outer, _context, _iid, output):
            write_pointer(output, 10)
            return 0

        self.ole.CoCreateInstance.side_effect = create
        self.winmm, self.dsound = Mock(), Mock()
        self.winmm.waveInGetNumDevs.return_value = 2
        self.winmm.waveOutGetNumDevs.return_value = 2
        self.winmm.waveInGetDevCapsW.return_value = 0
        self.winmm.waveOutGetDevCapsW.return_value = 0
        self.winmm.waveOutMessage.side_effect = self._wave_message
        self.dsound.GetDeviceID.side_effect = self._default_guid
        self.probe = self.make_probe(13)
        self.released = []
        self.buffers = []
        self.endpoint_ids = {20: "{unique-A}", 21: "{unique-B}"}
        self.endpoint_guids = {
            20: UUID("00000000-0000-0000-0000-000000000020"),
            21: UUID("00000000-0000-0000-0000-000000000021"),
        }
        self.states = {20: 1, 21: 1}
        self.default_status = 0
        self.default = 20
        self.preferred = 0
        self.failure = None
        self.roles = []
        self.mix_format = struct.pack("<HHIIHHH", 3, 2, 48000, 384000, 8, 32, 0)
        patcher = patch.object(devices, "_com_method", side_effect=self._com_method)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.dict(devices.os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_probe(self, host):
        libraries = {"ole32": self.ole, "winmm": self.winmm, "dsound": self.dsound}
        with patch.object(
            ctypes, "WinDLL", side_effect=libraries.__getitem__, create=True
        ):
            return devices._WindowsOutputProbe(host)

    def _buffer(self, value, output):
        buffer = (
            ctypes.create_unicode_buffer(value)
            if isinstance(value, str)
            else ctypes.create_string_buffer(value)
        )
        self.buffers.append(buffer)
        write_pointer(output, ctypes.addressof(buffer))

    def _com_method(self, pointer, index, *_types):
        return lambda _self, *arguments: self._invoke_com(pointer, index, *arguments)

    def _invoke_com(self, pointer, index, *arguments):
        identity = pointer.value
        if index == 2:
            self.released.append(identity)
            return 0
        if self.failure == (identity, index):
            return -2147467259
        if identity == 10 and index == 4:
            self.roles.append(arguments[1])
            if not self.default_status:
                write_pointer(arguments[-1], self.default)
            return self.default_status
        if identity == 10 and index == 5:
            target = next(
                key for key, value in self.endpoint_ids.items() if value == arguments[0]
            )
            write_pointer(arguments[-1], target)
        elif identity == 10 and index == 3:
            self.assertEqual(arguments[1], 0xF)
            write_pointer(arguments[-1], 30)
        elif identity == 30 and index == 3:
            write_pointer(arguments[-1], len(self.endpoint_ids))
        elif identity == 30 and index == 4:
            write_pointer(arguments[-1], sorted(self.endpoint_ids)[arguments[0]])
        elif identity in self.endpoint_ids and index == 3:
            write_pointer(arguments[-1], 40)
        elif identity in self.endpoint_ids and index == 4:
            write_pointer(arguments[-1], identity + 40)
        elif identity in self.endpoint_ids and index == 5:
            self._buffer(self.endpoint_ids[identity], arguments[-1])
        elif identity in self.endpoint_ids and index == 6:
            write_pointer(arguments[-1], self.states[identity])
        elif identity == 40 and index == 8:
            self._buffer(self.mix_format, arguments[-1])
        elif identity in (60, 61) and index == 5:
            self.assertEqual(arguments[0]._obj.pid, 4)
            value = arguments[-1]._obj
            value.vt = 31
            buffer = ctypes.create_unicode_buffer(
                str(self.endpoint_guids[identity - 40])
            )
            self.buffers.append(buffer)
            value.payload[0] = ctypes.addressof(buffer)
        else:
            raise AssertionError((identity, index))
        return 0

    def _wave_message(self, pointer, message, first, second):
        if message == 0x2015:
            ctypes.c_uint32.from_address(first).value = self.preferred
        elif message == 0x0812:
            identifier = (
                self.endpoint_ids[20 + (pointer.value or 0)].encode("utf-16-le")
                + b"\0\0"
            )
            ctypes.c_uint32.from_address(first).value = len(identifier)
        elif message == 0x0811:
            identifier = (
                self.endpoint_ids[20 + (pointer.value or 0)].encode("utf-16-le")
                + b"\0\0"
            )
            ctypes.memmove(first, identifier, len(identifier))
        else:
            raise AssertionError(message)
        return 0

    def _default_guid(self, source, output):
        self.assertEqual(
            UUID(bytes_le=bytes(source._obj)),
            UUID("def00000-9c6d-47ed-aaf1-4dda8f2b5c03"),
        )
        ctypes.memmove(output, self.endpoint_guids[self.default].bytes_le, 16)
        return 0

    def test_wasapi_observes_only_multimedia_and_keeps_inventory_separate(self):
        original = self.probe()
        self.assertEqual(self.roles, [1])
        self.endpoint_ids[21] = "{unique-C}"
        changed = self.probe()
        self.assertEqual(original.selected, changed.selected)
        self.assertNotEqual(original.inventory, changed.inventory)
        self.assertEqual(self.ole.CoTaskMemFree.call_count, 8)
        self.assertEqual(self.ole.CoUninitialize.call_count, 2)
        self.assertEqual(
            self.ole.CoCreateInstance.argtypes[-1], ctypes.POINTER(ctypes.c_void_p)
        )
        self.assertEqual(self.ole.CoTaskMemFree.argtypes, [ctypes.c_void_p])

    def test_format_default_and_selected_availability_changes(self):
        original = self.probe()
        self.mix_format = struct.pack("<HHIIHHH", 3, 1, 24000, 96000, 4, 32, 0)
        formatted = self.probe()
        self.assertNotEqual(original.selected, formatted.selected)
        self.assertEqual(original.inventory, formatted.inventory)
        self.default = 21
        self.assertNotEqual(formatted.selected, self.probe().selected)
        self.states[21] = 2
        unavailable = self.probe()
        self.assertIsNone(unavailable.selected[-1])
        self.assertNotEqual(formatted.selected, unavailable.selected)

    def test_no_output_and_return_are_distinct_from_api_failure(self):
        original = self.probe()
        self.default_status = -2147023728  # HRESULT_FROM_WIN32(ERROR_NOT_FOUND)
        self.assertIsNone(self.probe().selected)
        self.default_status = 0
        self.assertEqual(original, self.probe())
        self.default_status = -2147467259
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.ole.CoUninitialize.call_count, 4)
        self.assertEqual(self.released[-1], 10)

    def test_identity_failure_releases_endpoint_string_collection_and_enumerator(self):
        self.failure = (21, 6)
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.released, [20, 21, 30, 10])
        self.assertEqual(self.ole.CoTaskMemFree.call_count, 2)
        self.ole.CoUninitialize.assert_called_once()
        self.failure = (30, 4)
        self.released.clear()
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.released, [30, 10])

    def test_format_failure_releases_client_selected_device_and_com(self):
        self.failure = (40, 8)
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.released, [20, 21, 40, 20, 30, 10])
        self.assertEqual(self.ole.CoTaskMemFree.call_count, 3)
        self.ole.CoUninitialize.assert_called_once()

    def test_invalid_format_size_still_frees_format_buffer(self):
        self.mix_format = self.mix_format[:16] + struct.pack("<H", 4097)
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.ole.CoTaskMemFree.call_count, 4)
        self.assertEqual(self.released, [20, 21, 40, 20, 30, 10])

    def test_mme_resolves_preferred_wave_output_without_assuming_console_role(self):
        self.probe = self.make_probe(2)
        original = self.probe()
        self.assertEqual(self.roles, [])
        self.preferred = 1
        self.assertNotEqual(original.selected, self.probe().selected)
        self.assertEqual(self.winmm.waveOutMessage.argtypes[2], ctypes.c_size_t)

    def test_mme_override_uses_successful_caps_order_including_input_mapper(self):
        self.probe = self.make_probe(2)
        devices.os.environ["PA_RECOMMENDED_OUTPUT_DEVICE"] = "5suffix"
        self.assertEqual(self.probe().selected[1], self.endpoint_ids[21])
        self.winmm.waveInGetDevCapsW.side_effect = lambda index, *_: (
            1 if index == 0 else 0
        )
        devices.os.environ["PA_RECOMMENDED_OUTPUT_DEVICE"] = "4"
        self.assertEqual(self.probe().selected[1], self.endpoint_ids[21])
        self.winmm.waveOutGetDevCapsW.side_effect = lambda index, *_: (
            1 if index == 0 else 0
        )
        devices.os.environ["PA_RECOMMENDED_OUTPUT_DEVICE"] = "3"
        self.assertEqual(self.probe().selected[1], self.endpoint_ids[21])

    def test_mme_inapplicable_override_keeps_preferred_and_failure_raises(self):
        self.probe = self.make_probe(2)
        self.preferred = 1
        for override in ("0", "999", "-1", "9" * 32):
            with self.subTest(override=override):
                devices.os.environ["PA_RECOMMENDED_OUTPUT_DEVICE"] = override
                self.assertEqual(self.probe().selected[1], self.endpoint_ids[21])
        devices.os.environ["PA_RECOMMENDED_OUTPUT_DEVICE"] = "5"
        self.winmm.waveInGetDevCapsW.return_value = 7
        with self.assertRaises(OSError):
            self.probe()
        del devices.os.environ["PA_RECOMMENDED_OUTPUT_DEVICE"]
        self.winmm.waveOutMessage.side_effect = None
        self.winmm.waveOutMessage.return_value = 1
        with self.assertRaises(OSError):
            self.probe()
        self.winmm.waveOutGetNumDevs.return_value = 0
        self.assertIsNone(self.probe().selected)

    def test_directsound_guid_maps_default_to_endpoint_and_clears_properties(self):
        self.probe = self.make_probe(1)
        original = self.probe()
        self.assertEqual(self.roles, [])
        self.default = 21
        self.assertNotEqual(original.selected, self.probe().selected)
        self.assertEqual(self.ole.PropVariantClear.call_count, 4)
        self.assertIn(60, self.released)
        self.assertIn(61, self.released)
        self.failure = (61, 5)
        with self.assertRaises(OSError):
            self.probe()
        self.assertEqual(self.released[-3:], [21, 30, 10])
        self.assertEqual(self.ole.PropVariantClear.call_count, 6)

    def test_directsound_missing_output_and_native_failure(self):
        self.probe = self.make_probe(1)
        self.states = {20: 2, 21: 2}
        self.assertIsNone(self.probe().selected)
        self.dsound.GetDeviceID.assert_not_called()
        self.states[20] = 1
        self.dsound.GetDeviceID.side_effect = None
        self.dsound.GetDeviceID.return_value = -2005401480  # DSERR_NODRIVER
        self.assertIsNone(self.probe().selected)
        self.dsound.GetDeviceID.return_value = -2147467259
        with self.assertRaises(OSError):
            self.probe()

    def test_directsound_does_not_read_properties_of_disconnected_endpoints(self):
        self.probe = self.make_probe(1)
        self.states[21] = 4
        self.failure = (61, 5)
        self.assertIsNotNone(self.probe().selected)
        self.assertEqual(self.ole.PropVariantClear.call_count, 1)

    def test_unknown_backend_does_not_guess_a_role(self):
        for host in (None, 8, 11):
            with self.subTest(host=host), self.assertRaises(OSError):
                self.make_probe(host)

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
    def test_pulse_selected_format_and_route_are_separate_from_inventory(self):
        sinks = [
            {
                "index": index,
                "name": name,
                "sample_specification": "float32le 2ch 48000Hz",
                "channel_map": "front-left,front-right",
            }
            for index, name in enumerate(("headphones", "speakers"))
        ]
        with (
            patch.object(devices.shutil, "which", return_value="pactl"),
            patch.object(devices.subprocess, "run") as run,
        ):

            def observe(default="headphones"):
                run.side_effect = [
                    Mock(stdout=json.dumps({"default_sink_name": default})),
                    Mock(stdout=json.dumps(sinks)),
                ]
                return devices._linux_output_fingerprint()

            original = observe()
            sinks[1]["sample_specification"] = "float32le 1ch 24000Hz"
            self.assertEqual(original, observe())
            sinks[1]["index"] = 5
            inventory = observe()
            self.assertEqual(original.selected, inventory.selected)
            self.assertNotEqual(original.inventory, inventory.inventory)
            sinks[0]["sample_specification"] = "float32le 1ch 24000Hz"
            self.assertNotEqual(original.selected, observe().selected)
            self.assertNotEqual(original.selected, observe("speakers").selected)
            self.assertIsNone(observe("").selected)
            with self.assertRaises(OSError):
                observe("missing-sink")

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
            self.assertNotEqual(original, observation(""))
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
                devices.OutputDeviceSnapshot(
                    ("alsa", b"cards", b"pcm", (None, b"default")),
                    ("alsa", b"cards", b"pcm", (None, b"default")),
                ),
            )

    def test_missing_alsa_is_a_known_state_and_later_hardware_is_detected(self):
        with (
            patch.object(devices.shutil, "which", return_value=None),
            patch.object(devices.Path, "read_bytes", side_effect=FileNotFoundError()),
        ):
            absent = devices._linux_output_fingerprint()
        self.assertEqual(
            absent,
            devices.OutputDeviceSnapshot(None, ("alsa", None, None, (None, None))),
        )
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
