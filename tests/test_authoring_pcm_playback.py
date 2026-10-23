import os
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

from tests.native_clip_fixtures import FakeClipController
from tests.regular_file_fixtures import RegularFileGate, swap_at_acquisition
from vntts.authoring.pcm_playback import PcmClip, PcmPlaybackError, PersistentPcmPlayer
from vntts.rust_clip_player import RustClipController


class PersistentPcmPlayerTest(unittest.TestCase):
    def create_player(self):
        audio = FakeClipController()
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

    def test_new_clip_starts_at_its_first_frame_in_the_same_native_worker(self):
        audio, player = self.create_player()
        first = PcmClip(np.arange(20, dtype=np.float32).reshape(10, 2) / 20, 48_000)
        second = PcmClip(-first.samples, first.sample_rate)
        player.play(first)
        np.testing.assert_array_equal(audio.pump(2), first.samples[:2])
        player.play(second)
        np.testing.assert_array_equal(audio.pump(2), second.samples[:2])
        self.assertEqual(audio.load_count, 2)
        self.assertEqual(audio.reset_count, 0)

    def test_completion_waits_for_native_dac_and_old_end_cannot_finish_new_clip(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        old_token = player.play(clip)
        audio.pump(480)
        self.assertFalse(player.snapshot().finished)
        new_token = player.play(clip)
        audio.time += audio.latency + 1
        snapshot = player.snapshot()
        self.assertGreater(new_token, old_token)
        self.assertEqual(snapshot.token, new_token)
        self.assertFalse(snapshot.finished or snapshot.started)
        audio.pump(480)
        audio.time += audio.latency
        self.assertTrue(player.snapshot().finished)

    def test_pause_seek_resume_and_unpolled_underflow_are_explicit(self):
        audio, player = self.create_player()
        samples = np.arange(20, dtype=np.float32).reshape(10, 2) / 20
        clip = PcmClip(samples, player.sample_rate)
        token = player.play(clip)
        np.testing.assert_array_equal(audio.pump(3, underflow=True), samples[:3])
        player.pause()
        self.assertEqual(player.snapshot().token, token)
        self.assertTrue(player.snapshot().underflowed)
        np.testing.assert_array_equal(audio.pump(2), np.zeros((2, 2)))
        seek_token = player.seek(6)
        resume_token = player.resume()
        self.assertGreater(resume_token, seek_token)
        audio.state["started"] = True
        self.assertFalse(player.snapshot().started)
        np.testing.assert_array_equal(audio.pump(4), samples[6:])
        self.assertTrue(player.snapshot().started)

    def test_attempt_failures_survive_controls_until_new_play_or_stop(self):
        for failure in ("underflow", "native-error", "status"):
            with self.subTest(failure=failure):
                audio, player = self.create_player()
                clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
                token = player.play(clip)
                audio.pump(48, underflow=failure == "underflow")
                if failure == "native-error":
                    audio.state["error"] = "native output failed"
                if failure == "status":
                    with patch.object(
                        audio, "status", side_effect=RuntimeError("device clock failed")
                    ):
                        player.snapshot()
                failed = player.snapshot()
                self.assertTrue(failed.underflowed or failed.error)
                player.pause()
                self.assertEqual(player.snapshot().token, token)
                seek_token = player.seek(48)
                resume_token = player.resume()
                self.assertLess(token, seek_token)
                self.assertLess(seek_token, resume_token)
                audio.pump(432)
                audio.time += audio.latency + 0.001
                completed = player.snapshot()
                self.assertTrue(completed.finished)
                self.assertEqual(completed.position_frames, clip.frames)
                self.assertEqual(completed.underflowed, failed.underflowed)
                self.assertEqual(completed.error, failed.error)
                player.play(clip)
                self.assertFalse(player.snapshot().underflowed)
                self.assertIsNone(player.snapshot().error)
                audio.pump(48, underflow=True)
                player.stop()
                stopped = player.snapshot()
                self.assertFalse(
                    stopped.underflowed
                    or stopped.started
                    or stopped.playing
                    or stopped.finished
                )
                self.assertIsNone(stopped.error)
                self.assertEqual(stopped.position_frames, 0)

    def test_producer_starvation_is_not_reported_as_device_underflow(self):
        audio, player = self.create_player()
        player.play(PcmClip(np.ones((480, 2), np.float32), player.sample_rate))
        audio.state["producer_starvation_count"] = 2
        self.assertFalse(player.snapshot().underflowed)
        self.assertEqual(audio.status()["producer_starvation_count"], 2)

    def test_completed_clip_can_seek_and_replay_after_native_buffer_was_released(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        player.play(clip)
        audio.pump(480)
        audio.time += audio.latency
        self.assertTrue(player.snapshot().finished)
        player.seek(240)
        self.assertEqual(audio.load_count, 2)
        self.assertEqual(player.snapshot().position_frames, 240)
        self.assertFalse(player.snapshot().playing)
        player.resume()
        np.testing.assert_array_equal(audio.pump(240), clip.samples[240:])
        audio.time += audio.latency
        self.assertTrue(player.snapshot().finished)
        player.resume()
        self.assertEqual(audio.load_count, 3)
        np.testing.assert_array_equal(audio.pump(48), clip.samples[:48])

    def test_device_change_replays_onset_with_same_token_and_attempt_failure(self):
        audio, player = self.create_player()
        clip = PcmClip(np.arange(960, dtype=np.float32).reshape(480, 2) / 1000, 48_000)
        token = player.play(clip)
        audio.pump(48, underflow=True)
        player.snapshot()
        audio.device_changed = True
        snapshot = player.snapshot()
        self.assertEqual(audio.reset_count, 1)
        self.assertEqual(snapshot.token, token)
        self.assertEqual(snapshot.position_frames, 0)
        self.assertFalse(snapshot.started or snapshot.finished)
        self.assertTrue(snapshot.playing and snapshot.underflowed)
        np.testing.assert_array_equal(audio.pump(48), clip.samples[:48])

    def test_device_change_replays_after_registry_already_cancelled_native_clip(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        token = player.play(clip)
        audio.pump(48, underflow=True)
        audio.stop()
        audio.device_changed = True
        snapshot = player.snapshot()
        self.assertEqual(snapshot.token, token)
        self.assertTrue(snapshot.playing)
        self.assertFalse(snapshot.finished)
        self.assertTrue(snapshot.underflowed)
        self.assertEqual(audio.load_count, 2)
        np.testing.assert_array_equal(audio.pump(48), clip.samples[:48])

    def test_change_between_check_and_status_replays_without_spurious_error(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        token = player.play(clip)
        audio.pump(48)
        original = audio.status

        def changed_status():
            if not audio.reset_count:
                audio.device_changed = True
                raise RuntimeError("Changed while reading status")
            return original()

        with patch.object(audio, "status", side_effect=changed_status):
            snapshot = player.snapshot()
        self.assertEqual(snapshot.token, token)
        self.assertEqual(snapshot.position_frames, 0)
        self.assertIsNone(snapshot.error)

    def test_device_change_converts_cached_clip_and_preserves_logical_frames(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        token = player.play(clip)
        audio.pump(48)
        audio.device_changed = True
        audio.device = {"default_samplerate": 24_000, "max_output_channels": 1}
        snapshot = player.snapshot()
        self.assertEqual(player.sample_rate, 24_000)
        self.assertEqual(audio.channels, 1)
        self.assertEqual(snapshot.total_frames, clip.frames)
        self.assertEqual(snapshot.token, token)
        audio.pump(120)
        audio.time += audio.latency
        self.assertEqual(player.snapshot().position_frames, 240)
        player.pause()
        player.seek(288)
        player.resume()
        audio.pump(96)
        audio.time += audio.latency + 0.001
        self.assertTrue(player.snapshot().finished)
        player.play(clip, position_frames=240)
        self.assertEqual(player.snapshot().position_frames, 240)
        self.assertEqual(player.snapshot().total_frames, clip.frames)

    def test_unavailable_device_reports_error_and_next_play_retries(self):
        audio, player = self.create_player()
        clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
        player.play(clip)
        audio.device_changed = True
        with patch.object(
            audio, "query_device", side_effect=RuntimeError("No output device")
        ):
            failed = player.snapshot()
        self.assertIn("No output device", failed.error)
        self.assertFalse(failed.playing or failed.finished)
        player.play(clip)
        self.assertIsNone(player.snapshot().error)
        np.testing.assert_array_equal(audio.pump(48), clip.samples[:48])

    def test_device_change_after_queued_end_replays_only_uncompleted_clip(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                audio, player = self.create_player()
                clip = PcmClip(np.ones((480, 2), np.float32), player.sample_rate)
                token = player.play(clip)
                audio.pump(480)
                if completed:
                    audio.time += audio.latency + 0.001
                    self.assertTrue(player.snapshot().finished)
                audio.device_changed = True
                snapshot = player.snapshot()
                self.assertEqual(snapshot.token, token)
                self.assertEqual(snapshot.finished, completed)
                self.assertEqual(snapshot.playing, not completed)
                self.assertEqual(snapshot.position_frames, 480 if completed else 0)

    def test_play_rejects_invalid_pcm_before_loading_native_buffer(self):
        audio, player = self.create_player()
        for samples in (
            np.empty((0, 2), np.float32),
            np.ones((48,), np.float32),
            np.ones((48, 3), np.float32),
            np.full((48, 2), np.nan, np.float32),
        ):
            with self.subTest(shape=samples.shape), self.assertRaises(PcmPlaybackError):
                player.play(PcmClip(samples, 24_000))
        self.assertEqual(audio.load_count, 0)

    def test_default_monitor_runtime_failure_is_a_playback_error(self):
        with patch(
            "vntts.system_audio.get_system_audio_output",
            side_effect=RuntimeError("Rust audio worker is missing; run cargo build"),
        ):
            with self.assertRaisesRegex(
                PcmPlaybackError, "Rust audio worker is missing"
            ):
                PersistentPcmPlayer()

    def test_default_baselines_monitor_before_creating_dedicated_controller(self):
        order = []
        audio = FakeClipController()
        shared = Mock()
        shared.query_devices.side_effect = lambda **kwargs: order.append("baseline")

        def create_controller():
            order.append("controller")
            return audio

        with (
            patch("vntts.system_audio.get_system_audio_output", return_value=shared),
            patch(
                "vntts.authoring.pcm_playback.RustClipController",
                side_effect=create_controller,
            ),
        ):
            player = PersistentPcmPlayer()
        self.addCleanup(player.close)
        self.assertEqual(order, ["baseline", "controller"])

    def test_close_attempts_native_close_and_preserves_stop_failure(self):
        audio, player = self.create_player()
        primary = RuntimeError("stop failed")
        with (
            patch.object(audio, "stop", side_effect=primary) as stop,
            patch.object(
                audio, "close", side_effect=SystemExit("cleanup failed")
            ) as close,
        ):
            with self.assertRaises(RuntimeError) as caught:
                player.close()
            player.close()
        self.assertIs(caught.exception, primary)
        stop.assert_called_once_with()
        close.assert_called_once_with()
        self.assertIn("cleanup failed", primary.__notes__[0])

    def test_query_failure_closes_controller_and_preserves_primary_failure(self):
        audio = FakeClipController()
        with (
            patch.object(audio, "query_device", side_effect=RuntimeError("no device")),
            patch.object(
                audio, "close", side_effect=SystemExit("cleanup failed")
            ) as close,
        ):
            with self.assertRaisesRegex(PcmPlaybackError, "no device") as caught:
                PersistentPcmPlayer(audio)
        close.assert_called_once_with()
        self.assertIn("cleanup failed", caught.exception.__notes__[0])


class RustClipControllerTest(unittest.TestCase):
    def worker(self):
        worker = Mock()
        worker.device_changed = Event()
        return worker

    def finished_event(self, *, sample_rate=24000, channels=1):
        return {
            "event": "finished",
            "id": 1,
            "outcome": "complete",
            "frames_played": 2,
            "position_frames": 2,
            "audible_position_frames": 2,
            "total_frames": 2,
            "sample_rate": sample_rate,
            "channels": channels,
            "started": True,
            "playing": False,
            "paused": False,
            "finished": True,
            "underflow_count": 0,
            "producer_starvation_count": 0,
            "callback_count": 1,
            "elapsed_ms": 1.0,
            "actual_sample_rate": sample_rate,
            "output_latency_seconds": 0.01,
            "error": None,
        }

    def test_protocol_preserves_pcm_and_ignores_interleaved_native_events(self):
        worker = self.worker()
        worker.receive.side_effect = [
            {"event": "device", "default_samplerate": 48000, "max_output_channels": 2},
            {"event": "loaded", "id": 1},
            {"event": "started", "id": 1},
            {"event": "first_callback", "id": 1},
            self.finished_event(sample_rate=48000, channels=2),
            {
                "event": "status",
                "id": 1,
                "position_frames": 2,
                "audible_position_frames": 2,
                "total_frames": 2,
                "sample_rate": 48000,
                "channels": 2,
                "started": True,
                "playing": False,
                "paused": False,
                "finished": True,
                "underflow_count": 0,
                "producer_starvation_count": 0,
                "error": None,
            },
            {"event": "loaded", "id": 2},
        ]
        controller = RustClipController(lambda: worker)
        self.addCleanup(controller.close)
        controller.query_device()
        samples = np.array([[0.1, -0.1], [0.2, -0.2]], np.float32)
        controller.load(samples, 48000, 2, 0.1)
        np.testing.assert_array_equal(
            np.frombuffer(worker._send_bounded.call_args.args[1], "<f4").reshape(2, 2),
            samples,
        )
        self.assertEqual(worker.active_id, 1)
        controller.start()
        self.assertTrue(controller.status()["finished"])
        self.assertIsNone(worker.active_id)
        controller.load(samples, 48000, 2, 0.1)
        self.assertEqual(worker._send_bounded.call_args.args[0]["id"], 2)
        self.assertEqual(
            [call.args[0]["op"] for call in worker.send.call_args_list],
            ["query_device", "start", "status"],
        )

    def test_finished_summary_is_validated_once_with_distinct_native_counters(self):
        worker = self.worker()
        worker.process.pid = 123
        finished = self.finished_event()
        finished["underflow_count"] = 1
        finished["producer_starvation_count"] = 3
        worker.receive.side_effect = [
            {"event": "loaded", "id": 1},
            finished,
            finished,
            {"event": "device", "default_samplerate": 48000},
        ]
        controller = RustClipController(lambda: worker)
        self.addCleanup(controller.close)
        controller.load(np.zeros((2, 1), np.float32), 24000, 1, 0.1)
        with patch("vntts.rust_clip_player.record_audio_lifecycle") as record:
            controller.stop()
            controller.query_device()
        record.assert_called_once()
        self.assertEqual(record.call_args.args, ("rust-pcm-summary",))
        self.assertEqual(record.call_args.kwargs["underflow_count"], 1)
        self.assertEqual(record.call_args.kwargs["producer_starvation_count"], 3)
        self.assertEqual(record.call_args.kwargs["worker_pid"], 123)

    def test_invalid_finished_counters_fail_closed_without_success_summary(self):
        worker = self.worker()
        finished = self.finished_event()
        finished["producer_starvation_count"] = -1
        worker.receive.side_effect = [{"event": "loaded", "id": 1}, finished]
        controller = RustClipController(lambda: worker)
        controller.load(np.zeros((2, 1), np.float32), 24000, 1, 0.1)
        with patch("vntts.rust_clip_player.record_audio_lifecycle") as record:
            with self.assertRaisesRegex(RuntimeError, "producer_starvation_count"):
                controller.stop()
        record.assert_not_called()
        worker.close.assert_called_once_with()

    def test_stale_event_fails_closed(self):
        worker = self.worker()
        worker.receive.side_effect = [{"event": "loaded", "id": 2}]
        controller = RustClipController(lambda: worker)
        with self.assertRaisesRegex(RuntimeError, "another clip"):
            controller.load(np.zeros((2, 1), np.float32), 24000, 1, 0.1)
        worker.close.assert_called_once_with()

    def test_terminal_racing_control_consumes_ack_before_the_next_command(self):
        worker = self.worker()
        worker.receive.side_effect = [
            {"event": "loaded", "id": 1},
            self.finished_event(),
            {"event": "resumed", "id": 1, "finished": True},
            {"event": "device", "default_samplerate": 48000},
        ]
        controller = RustClipController(lambda: worker)
        self.addCleanup(controller.close)
        controller.load(np.zeros((2, 1), np.float32), 24000, 1, 0.1)
        self.assertFalse(controller.resume())
        self.assertEqual(controller.query_device()["default_samplerate"], 48000)

    def test_invalid_native_status_fails_closed(self):
        for field, value in (
            ("audible_position_frames", 3),
            ("total_frames", 3),
            ("started", "yes"),
            ("sample_rate", 48000),
            ("error", 1),
        ):
            with self.subTest(field=field):
                worker = self.worker()
                status = {
                    "event": "status",
                    "id": 1,
                    "position_frames": 2,
                    "audible_position_frames": 1,
                    "total_frames": 2,
                    "sample_rate": 24000,
                    "channels": 1,
                    "started": True,
                    "playing": True,
                    "paused": False,
                    "finished": False,
                    "underflow_count": 0,
                    "producer_starvation_count": 0,
                    "error": None,
                }
                status[field] = value
                worker.receive.side_effect = [{"event": "loaded", "id": 1}, status]
                controller = RustClipController(lambda: worker)
                controller.load(np.zeros((2, 1), np.float32), 24000, 1, 0.1)
                with self.assertRaises(RuntimeError):
                    controller.status()
                worker.close.assert_called_once_with()

    def test_stop_consumes_already_finished_event_without_waiting_for_second_finish(
        self,
    ):
        worker = self.worker()
        worker.receive.side_effect = [
            {"event": "loaded", "id": 1},
            self.finished_event(),
        ]
        controller = RustClipController(lambda: worker)
        self.addCleanup(controller.close)
        controller.load(np.zeros((2, 1), np.float32), 24000, 1, 0.1)
        controller.stop()
        controller.stop()
        self.assertEqual(worker.send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
