import os
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, PropertyMock, patch

import numpy as np
import soundfile as sf

from tests.audio_output_fixtures import FakeAudioModule, FakeOutputStream
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.authoring.pcm_playback import (
    PcmClip,
    PcmPlaybackError,
    PersistentPcmPlayer,
)
from vntts.system_audio import AudioDeviceChangedError


class PersistentPcmPlayerTest(unittest.TestCase):
    def create_player(self):
        audio = FakeAudioModule()
        player = PersistentPcmPlayer(audio)
        self.addCleanup(player.close)
        return audio, player

    def test_load_resamples_and_normalizes_channels_once(self):
        _audio, player = self.create_player()
        with TemporaryDirectory() as directory:
            mono = Path(directory) / "mono-24k.wav"
            stereo = Path(directory) / "stereo-48k.wav"
            sf.write(mono, np.linspace(-0.5, 0.5, 2_400), 24_000, subtype="PCM_16")
            sf.write(
                stereo,
                np.column_stack(
                    (np.linspace(-0.5, 0.5, 4_800), np.linspace(0.5, -0.5, 4_800))
                ),
                48_000,
                subtype="PCM_16",
            )

            prepared_mono = player.load(mono)
            prepared_stereo = player.load(stereo)

        self.assertEqual(prepared_mono.sample_rate, 48_000)
        self.assertEqual(prepared_mono.samples.shape, (4_800, 2))
        np.testing.assert_allclose(
            prepared_mono.samples[:, 0], prepared_mono.samples[:, 1]
        )
        self.assertEqual(prepared_stereo.samples.shape, (4_800, 2))

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "POSIX FIFO"
    )
    def test_load_refuses_fifo_swap_and_recovers(self):
        _audio, player = self.create_player()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "mono.wav"
            sf.write(path, np.linspace(-0.5, 0.5, 2_400), 24_000, subtype="PCM_16")
            gate: RegularFileGate = {"path": path, "armed": True}
            with (
                swap_at_acquisition(self, gate),
                self.assertRaisesRegex(PcmPlaybackError, "Unable to decode mono.wav"),
            ):
                player.load(path)
            self.assertFalse(gate["armed"], "regular file must be swapped at open")
            clip = player.load(path)
        self.assertEqual(clip.sample_rate, 48_000)
        self.assertEqual(clip.samples.shape, (4_800, 2))

    def test_load_bytes_uses_the_same_preparation(self):
        _audio, player = self.create_player()
        payload = BytesIO()
        sf.write(payload, np.linspace(-0.5, 0.5, 2_400), 24_000, format="WAV")

        clip = player.load_bytes(payload.getvalue(), name="captured.wav")

        self.assertEqual(clip.sample_rate, 48_000)
        self.assertEqual(clip.samples.shape, (4_800, 2))

    def test_first_frames_and_reverse_switch_use_the_same_warm_stream(self):
        audio, player = self.create_player()
        first = PcmClip(
            np.array([[0.1, -0.1], [0.2, -0.2], [0.3, -0.3]], np.float32),
            player.sample_rate,
        )
        second = PcmClip(
            np.array([[0.7, 0.6], [0.5, 0.4], [0.3, 0.2]], np.float32),
            player.sample_rate,
        )

        player.play(first)
        np.testing.assert_array_equal(audio.stream.pump(2), first.samples[:2])
        player.play(second)
        np.testing.assert_array_equal(audio.stream.pump(2), second.samples[:2])

        self.assertTrue(audio.stream.started)

    def test_completion_waits_for_dac_and_stale_end_cannot_finish_new_clip(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        old_token = player.play(clip)
        audio.stream.pump(480)
        self.assertFalse(player.snapshot().finished)

        new_token = player.play(clip)
        audio.stream.time += audio.latency + 1
        snapshot = player.snapshot()

        self.assertNotEqual(old_token, new_token)
        self.assertEqual(snapshot.token, new_token)
        self.assertFalse(snapshot.finished)

    def test_pause_seek_resume_and_underflow_are_explicit(self):
        audio, player = self.create_player()
        samples = np.arange(20, dtype=np.float32).reshape(10, 2) / 20
        clip = PcmClip(samples, player.sample_rate)
        player.play(clip)
        np.testing.assert_array_equal(audio.stream.pump(3), samples[:3])

        player.pause()
        np.testing.assert_array_equal(audio.stream.pump(2), np.zeros((2, 2)))
        player.seek(6)
        player.resume()
        np.testing.assert_array_equal(audio.stream.pump(4), samples[6:])
        self.assertTrue(player.snapshot().started)

        player.play(clip)
        audio.stream.pump(2, underflow=True)
        self.assertTrue(player.snapshot().underflowed)

    def test_attempt_failures_survive_pause_seek_and_resume_until_new_play(self):
        for failure in ("underflow", "clock", "callback"):
            with self.subTest(failure=failure):
                audio, player = self.create_player()
                clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
                token = player.play(clip)
                if failure == "underflow":
                    audio.stream.pump(48, underflow=True)
                elif failure == "clock":
                    with patch.object(
                        FakeOutputStream,
                        "time",
                        create=True,
                        new_callable=PropertyMock,
                        side_effect=RuntimeError("device clock failed"),
                    ):
                        player.snapshot()
                else:
                    audio.stream.callback(
                        np.empty((48, 2), np.float32),
                        48,
                        Mock(outputBufferDacTime="invalid DAC time"),
                        None,
                    )
                failed = player.snapshot()
                self.assertTrue(failed.underflowed or failed.error)

                player.pause()
                self.assertEqual(player.snapshot().token, token)
                seek_token = player.seek(48)
                resume_token = player.resume()
                self.assertLess(token, seek_token)
                self.assertLess(seek_token, resume_token)
                self.assertFalse(player.snapshot().started)
                audio.stream.pump(432)
                audio.stream.time += audio.latency + 0.001
                completed = player.snapshot()
                self.assertEqual(completed.token, resume_token)
                self.assertTrue(completed.finished)
                self.assertEqual(completed.position_frames, clip.frames)
                self.assertEqual(completed.underflowed, failed.underflowed)
                self.assertEqual(completed.error, failed.error)

                player.play(clip)
                self.assertFalse(player.snapshot().underflowed)
                self.assertIsNone(player.snapshot().error)
                audio.stream.pump(480)
                audio.stream.time += audio.latency + 0.001
                self.assertTrue(player.snapshot().finished)

                player.play(clip)
                audio.stream.pump(48, underflow=True)
                player.stop()
                stopped = player.snapshot()
                self.assertFalse(stopped.underflowed)
                self.assertIsNone(stopped.error)
                self.assertEqual(stopped.position_frames, 0)
                self.assertFalse(stopped.started or stopped.playing or stopped.finished)

    def test_close_aborts_and_closes_stream(self):
        audio = FakeAudioModule()
        player = PersistentPcmPlayer(audio)

        player.close()

        self.assertTrue(audio.stream.aborted)
        self.assertTrue(audio.stream.closed)

    def test_device_change_replays_onset_with_same_token_and_attempt_failure(self):
        audio, player = self.create_player()
        samples = np.arange(480 * 2, dtype=np.float32).reshape(480, 2) / 1000
        clip = PcmClip(samples, player.sample_rate)
        token = player.play(clip)
        old_stream = audio.stream
        old_stream.pump(48, underflow=True)
        with patch.object(
            FakeOutputStream,
            "time",
            create=True,
            new_callable=PropertyMock,
            side_effect=RuntimeError("clock failed"),
        ):
            player.snapshot()
        old_stream.device_changed = True

        snapshot = player.snapshot()

        self.assertIsNot(audio.stream, old_stream)
        self.assertTrue(old_stream.aborted and old_stream.closed)
        self.assertEqual(snapshot.token, token)
        self.assertEqual(snapshot.position_frames, 0)
        self.assertFalse(snapshot.started or snapshot.finished)
        self.assertTrue(snapshot.playing and snapshot.underflowed)
        self.assertEqual(snapshot.error, "clock failed")
        np.testing.assert_array_equal(audio.stream.pump(48), samples[:48])
        player.play(clip)
        self.assertFalse(player.snapshot().underflowed)
        self.assertIsNone(player.snapshot().error)

    def test_play_rejects_invalid_logical_pcm(self):
        _audio, player = self.create_player()
        for samples in (
            np.empty((0, 2), np.float32),
            np.ones((48,), np.float32),
            np.ones((48, 3), np.float32),
            np.full((48, 2), np.nan, np.float32),
        ):
            with self.subTest(shape=samples.shape):
                with self.assertRaises(PcmPlaybackError):
                    player.play(PcmClip(samples, 24_000))

    def test_change_between_device_check_and_clock_read_replays_without_error(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        token = player.play(clip)
        old_stream = audio.stream
        old_stream.pump(48)

        def changed_clock(stream):
            if stream is old_stream:
                old_stream.device_changed = True
                raise AudioDeviceChangedError("Changed while reading time")
            return stream._clock

        with patch.object(
            FakeOutputStream,
            "time",
            property(
                changed_clock,
                lambda stream, value: setattr(stream, "_clock", value),
            ),
            create=True,
        ):
            snapshot = player.snapshot()
        self.assertEqual(snapshot.token, token)
        self.assertEqual(snapshot.position_frames, 0)
        self.assertIsNone(snapshot.error)

    def test_injected_sounddevice_module_uses_shared_output(self):
        audio = FakeAudioModule()
        with patch(
            "vntts.system_audio.get_system_audio_output", return_value=audio
        ) as shared:
            player = PersistentPcmPlayer(ModuleType("sounddevice"))
        self.addCleanup(player.close)
        shared.assert_called_once_with()
        self.assertIs(player.audio_module, audio)

    def test_device_change_converts_cached_clip_and_keeps_logical_frame_units(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        token = player.play(clip)
        audio.stream.pump(48)
        audio.stream.device_changed = True
        audio.device = {"default_samplerate": 24_000, "max_output_channels": 1}

        snapshot = player.snapshot()

        self.assertEqual(player.sample_rate, 24_000)
        self.assertEqual(audio.stream.channels, 1)
        self.assertEqual(snapshot.total_frames, clip.frames)
        self.assertEqual(snapshot.token, token)
        audio.stream.pump(120)
        audio.stream.time += audio.latency
        self.assertEqual(player.snapshot().position_frames, 240)
        player.pause()
        player.seek(288)
        player.resume()
        audio.stream.pump(96)
        audio.stream.time += audio.latency + 0.001
        completed = player.snapshot()
        self.assertTrue(completed.finished)
        self.assertEqual(completed.position_frames, clip.frames)
        player.play(clip, position_frames=240)
        self.assertEqual(player.snapshot().position_frames, 240)
        self.assertEqual(player.snapshot().total_frames, clip.frames)

    def test_unavailable_device_reports_error_and_next_play_retries(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        player.play(clip)
        audio.stream.device_changed = True
        with patch.object(
            audio, "query_devices", side_effect=RuntimeError("No output device")
        ):
            failed = player.snapshot()
        self.assertIn("No output device", failed.error)
        self.assertFalse(failed.playing or failed.finished)
        self.assertIsNone(player.stream)

        player.play(clip)

        self.assertIsNone(player.snapshot().error)
        np.testing.assert_array_equal(audio.stream.pump(48), clip.samples[:48])

    def test_device_change_after_queued_end_replays_but_completed_clip_stays_stopped(
        self,
    ):
        for completed in (False, True):
            with self.subTest(completed=completed):
                audio, player = self.create_player()
                clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
                token = player.play(clip)
                audio.stream.pump(480)
                if completed:
                    audio.stream.time += audio.latency + 0.001
                    self.assertTrue(player.snapshot().finished)
                audio.stream.device_changed = True

                snapshot = player.snapshot()

                self.assertEqual(snapshot.token, token)
                self.assertEqual(snapshot.finished, completed)
                self.assertEqual(snapshot.playing, not completed)
                self.assertEqual(snapshot.position_frames, 480 if completed else 0)

    def test_close_attempts_each_release_once_and_preserves_first_failure(self):
        for failed_phase in ("stop", "abort", "close"):
            with self.subTest(failed_phase=failed_phase):
                audio = FakeAudioModule()
                player = PersistentPcmPlayer(audio)
                primary = RuntimeError(f"{failed_phase} failed")
                failures = {failed_phase: primary}
                if failed_phase == "stop":
                    failures["abort"] = KeyboardInterrupt("abort cleanup failed")
                if failed_phase != "close":
                    failures["close"] = SystemExit("close cleanup failed")
                released = []

                def release(phase):
                    released.append(phase)
                    if phase in failures:
                        raise failures[phase]

                with (
                    patch.object(player, "stop", side_effect=lambda: release("stop")),
                    patch.object(
                        audio.stream, "abort", side_effect=lambda: release("abort")
                    ),
                    patch.object(
                        audio.stream, "close", side_effect=lambda: release("close")
                    ),
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        player.close()
                    player.close()
                self.assertIs(caught.exception, primary)
                self.assertEqual(released, ["stop", "abort", "close"])
                expected_notes = []
                if failed_phase == "stop":
                    expected_notes.append(
                        "Output stream abort failed: abort cleanup failed"
                    )
                if failed_phase != "close":
                    expected_notes.append(
                        "Output stream cleanup failed: close cleanup failed"
                    )
                self.assertEqual(getattr(primary, "__notes__", []), expected_notes)

    def test_startup_failure_survives_a_stream_cleanup_failure(self):
        for cleanup_error in (
            None,
            RuntimeError("device refused cleanup"),
            SystemExit("device refused cleanup"),
        ):
            with self.subTest(cleanup_error=cleanup_error):
                audio = FakeAudioModule()
                startup_error = RuntimeError("device refused startup")
                with (
                    patch.object(FakeOutputStream, "start", side_effect=startup_error),
                    patch.object(
                        FakeOutputStream, "close", side_effect=cleanup_error
                    ) as close,
                    self.assertRaisesRegex(
                        PcmPlaybackError,
                        "Unable to open the output stream: device refused startup",
                    ) as caught,
                ):
                    PersistentPcmPlayer(audio)

                close.assert_called_once_with()
                self.assertIs(caught.exception.__cause__, startup_error)
                if cleanup_error is not None:
                    self.assertIn("device refused cleanup", startup_error.__notes__[0])


if __name__ == "__main__":
    unittest.main()
