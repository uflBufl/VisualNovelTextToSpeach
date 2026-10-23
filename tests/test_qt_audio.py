import unittest
from unittest.mock import Mock, patch

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QUrl
from PySide6.QtWidgets import QApplication, QWidget

from vntts.authoring.pcm_playback import PcmClip, PcmPlaybackError, PlaybackSnapshot
from vntts.qt_audio import QtPcmPlayer


class _PcmPlayer:
    def __init__(self):
        self.clip = PcmClip(np.ones((4, 2), np.float32), 48_000)
        self.token = 0
        self.snapshot_value = PlaybackSnapshot(0, 0, 4, False, True, False, False, None)
        self.stopped = self.closed = False

    def load_bytes(self, payload, *, name):
        self.loaded = payload, name
        return self.clip

    def play(self, clip):
        self.played = clip
        self.token += 1
        self.snapshot_value = PlaybackSnapshot(
            self.token, 0, 4, False, True, False, False, None
        )
        return self.token

    def snapshot(self):
        return self.snapshot_value

    def stop(self):
        self.stopped = True
        self.token += 1
        self.snapshot_value = PlaybackSnapshot(
            self.token, 0, 0, False, False, False, False, None
        )

    def close(self):
        self.closed = True


class QtAudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_runtime_factory_errors_are_reported_and_retry_uses_a_fresh_player(self):
        for error in (
            RuntimeError("Rust audio worker is missing; rebuild native/audio-worker"),
            PermissionError("Rust audio worker is not executable"),
            RuntimeError("Rust audio worker exited before completion"),
            RuntimeError("Rust audio worker protocol mismatch"),
            TimeoutError("Rust audio worker startup timed out"),
        ):
            with self.subTest(error=str(error)):
                pcm = _PcmPlayer()
                factory = Mock(side_effect=[error, pcm])
                player = QtPcmPlayer(player_factory=factory)
                failures = []
                player.errorOccurred.connect(
                    lambda _code, message: failures.append(message)
                )
                self.assertIsNone(player.play_bytes(b"audio", "test.wav"))
                self.assertEqual(failures, [str(error)])
                self.assertEqual(player.errorString(), str(error))
                self.assertFalse(player._timer.isActive())
                self.assertIsNone(player._token)
                self.assertIs(player.play_bytes(b"audio", "test.wav"), pcm.clip)
                self.assertTrue(player._timer.isActive())
                player.stop()
                player.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_poll_failure_preserves_error_when_stop_also_fails_and_can_retry(self):
        broken = _PcmPlayer()
        broken.close = Mock(wraps=broken.close)
        recovered = _PcmPlayer()
        player = QtPcmPlayer(player_factory=Mock(side_effect=[broken, recovered]))
        failures = []
        player.errorOccurred.connect(lambda _code, message: failures.append(message))
        player.play_bytes(b"audio", "test.wav")
        with (
            patch.object(
                broken, "snapshot", side_effect=RuntimeError("worker crashed")
            ),
            patch.object(broken, "stop", side_effect=RuntimeError("worker is closed")),
        ):
            player._poll()
        self.assertEqual(failures, ["worker crashed"])
        self.assertTrue(broken.closed)
        self.assertFalse(player._timer.isActive())
        self.assertIsNone(player._token)
        self.assertIs(player.play_bytes(b"audio", "test.wav"), recovered.clip)
        player.stop()
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        broken.close.assert_called_once_with()

    def test_stop_failure_reports_once_and_retry_discards_the_failed_player(self):
        broken = _PcmPlayer()
        recovered = _PcmPlayer()
        player = QtPcmPlayer(player_factory=Mock(side_effect=[broken, recovered]))
        failures = []
        player.errorOccurred.connect(lambda _code, message: failures.append(message))
        with patch("vntts.support.record_audio_lifecycle") as lifecycle:
            player.play_bytes(b"audio", "test.wav")
            with patch.object(
                broken, "stop", side_effect=RuntimeError("worker stop timed out")
            ):
                player.stop()
            player.stop()
        self.assertEqual(failures, ["worker stop timed out"])
        self.assertEqual(
            [call.kwargs["outcome"] for call in lifecycle.call_args_list],
            ["queued", "failed"],
        )
        self.assertTrue(broken.closed)
        self.assertFalse(player._timer.isActive())
        self.assertIsNone(player._token)
        self.assertIs(player.play_bytes(b"audio", "test.wav"), recovered.clip)
        player.stop()
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_availability_check_skips_native_runtime_for_injected_players(self):
        player = QtPcmPlayer(player_factory=_PcmPlayer)
        with patch("vntts.rust_audio.check_audio_runtime") as check:
            player.check_available()
        check.assert_not_called()
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_pcm_adapter_reports_only_clean_dac_completion(self):
        pcm = _PcmPlayer()
        pcm.device_name = "Test output"
        player = QtPcmPlayer(player_factory=lambda: pcm)
        finished = []
        failures = []
        player.mediaStatusChanged.connect(finished.append)
        player.errorOccurred.connect(lambda _error, message: failures.append(message))

        with patch("vntts.support.record_audio_lifecycle") as lifecycle:
            clip = player.play_bytes(b"exact audio", "memory:test.wav")
            pcm.snapshot_value = PlaybackSnapshot(
                pcm.token, 4, 4, True, False, True, False, None
            )
            player._poll()
        self.assertEqual(
            [call.kwargs["outcome"] for call in lifecycle.call_args_list],
            ["queued", "started", "finished"],
        )
        self.assertEqual(
            lifecycle.call_args_list[0].kwargs["device_name"], "Test output"
        )
        self.assertEqual(
            len({call.kwargs["player_id"] for call in lifecycle.call_args_list}), 1
        )
        self.assertEqual(
            len({call.kwargs["attempt_id"] for call in lifecycle.call_args_list}), 1
        )

        self.assertIs(clip, pcm.clip)
        self.assertEqual(pcm.loaded, (b"exact audio", "memory:test.wav"))
        self.assertEqual(finished, [QtPcmPlayer.MediaStatus.EndOfMedia])
        self.assertEqual(failures, [])

        player.play_bytes(b"exact audio", "memory:test.wav")
        pcm.snapshot_value = PlaybackSnapshot(
            pcm.token, 4, 4, True, False, True, True, None
        )
        player._poll()
        self.assertEqual(len(finished), 1)
        self.assertEqual(failures, ["Audio output underflowed; replay the sample"])
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_pcm_adapter_times_out_without_start_and_can_replay(self):
        pcm = _PcmPlayer()
        now = [0.0]
        player = QtPcmPlayer(player_factory=lambda: pcm, clock=lambda: now[0])
        failures = []
        finished = []
        player.errorOccurred.connect(lambda _error, message: failures.append(message))
        player.mediaStatusChanged.connect(finished.append)

        player.play_bytes(b"audio", "test.wav")
        now[0] = 4.99
        player._poll()
        self.assertEqual(failures, [])

        now[0] = 5.0
        player._poll()
        self.assertEqual(
            failures, ["Audio output did not start within 5 seconds; replay the sample"]
        )
        self.assertTrue(pcm.stopped)
        self.assertFalse(player._timer.isActive())

        player.play()
        pcm.snapshot_value = PlaybackSnapshot(
            pcm.token, 4, 4, True, False, True, False, None
        )
        player._poll()
        self.assertEqual(
            failures, ["Audio output did not start within 5 seconds; replay the sample"]
        )
        self.assertEqual(finished, [QtPcmPlayer.MediaStatus.EndOfMedia])
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_pcm_adapter_reports_invalidated_playback_token(self):
        pcm = _PcmPlayer()
        player = QtPcmPlayer(player_factory=lambda: pcm)
        failures = []
        player.errorOccurred.connect(lambda _error, message: failures.append(message))

        player.play_bytes(b"audio", "test.wav")
        pcm.snapshot_value = PlaybackSnapshot(
            pcm.token + 1, 0, 4, False, True, False, False, None
        )
        player._poll()

        self.assertEqual(
            failures, ["Audio playback was interrupted unexpectedly; replay the sample"]
        )
        self.assertTrue(pcm.stopped)
        self.assertFalse(player._timer.isActive())
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_pcm_adapter_ignores_finished_snapshot_after_started_slot_replays(self):
        pcm = _PcmPlayer()
        player = QtPcmPlayer(player_factory=lambda: pcm)
        failures = []
        finished = []
        replayed = [False]
        player.errorOccurred.connect(lambda _error, message: failures.append(message))
        player.mediaStatusChanged.connect(finished.append)

        def replay_when_started(state):
            if state == QtPcmPlayer.PlaybackState.PlayingState and not replayed[0]:
                replayed[0] = True
                player.stop()
                player.play()

        player.playbackStateChanged.connect(replay_when_started)
        player.play_bytes(b"audio", "test.wav")
        pcm.snapshot_value = PlaybackSnapshot(
            pcm.token, 4, 4, True, False, True, False, None
        )
        player._poll()

        self.assertTrue(replayed[0])
        self.assertEqual(failures, [])
        self.assertEqual(finished, [])
        self.assertTrue(player._timer.isActive())

        pcm.snapshot_value = PlaybackSnapshot(
            pcm.token, 4, 4, True, False, True, False, None
        )
        player._poll()
        self.assertEqual(finished, [QtPcmPlayer.MediaStatus.EndOfMedia])
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_deleting_player_or_owner_closes_pcm_stream(self):
        for delete_owner in (False, True):
            with self.subTest(delete_owner=delete_owner):
                owner = QWidget()
                pcm = _PcmPlayer()
                player = QtPcmPlayer(owner, player_factory=lambda: pcm)

                def check_cleanup_before_terminal(_operation, **details):
                    if details["outcome"] == "interrupted":
                        self.assertTrue(pcm.stopped if delete_owner else pcm.closed)

                with patch(
                    "vntts.support.record_audio_lifecycle",
                    side_effect=check_cleanup_before_terminal,
                ) as lifecycle:
                    player.play_bytes(b"audio", "test.wav")
                    (owner if delete_owner else player).deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertEqual(
                    [call.kwargs["outcome"] for call in lifecycle.call_args_list],
                    ["queued", "interrupted"],
                )
                self.assertEqual(
                    len(
                        {call.kwargs["attempt_id"] for call in lifecycle.call_args_list}
                    ),
                    1,
                )
                self.assertTrue(pcm.closed)
                if not delete_owner:
                    owner.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_stop_records_one_terminal_for_queued_or_started_attempt(self):
        for started in (False, True):
            with self.subTest(started=started):
                pcm = _PcmPlayer()
                player = QtPcmPlayer(player_factory=lambda: pcm)
                finished = []
                player.mediaStatusChanged.connect(finished.append)

                def check_stop_before_terminal(_operation, **details):
                    if details["outcome"] == "stopped":
                        self.assertTrue(pcm.stopped)
                        self.assertFalse(player._timer.isActive())
                        self.assertIsNone(player._token)
                        self.assertEqual(
                            player._lifecycle["attempt_id"], details["attempt_id"]
                        )

                with patch(
                    "vntts.support.record_audio_lifecycle",
                    side_effect=check_stop_before_terminal,
                ) as lifecycle:
                    player.play_bytes(b"audio", "private-dialogue.wav")
                    if started:
                        pcm.snapshot_value = PlaybackSnapshot(
                            pcm.token, 1, 4, True, True, False, False, None
                        )
                        player._poll()
                    player.stop()
                    player.stop()
                    player._poll()
                outcomes = [call.kwargs["outcome"] for call in lifecycle.call_args_list]
                self.assertEqual(
                    outcomes,
                    ["queued", "started", "stopped"]
                    if started
                    else ["queued", "stopped"],
                )
                self.assertEqual(
                    lifecycle.call_args_list[-1].kwargs["reason"],
                    "stop" if started else "cancelled-before-start",
                )
                self.assertNotIn("private-dialogue", str(lifecycle.call_args_list))
                self.assertEqual(finished, [])
                player.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_replacement_uses_new_attempt_and_stable_player_identity(self):
        pcm = _PcmPlayer()
        player = QtPcmPlayer(player_factory=lambda: pcm)
        states = []
        player.playbackStateChanged.connect(states.append)
        with patch("vntts.support.record_audio_lifecycle") as lifecycle:
            player.play_bytes(b"first", "first.wav")
            player.play_bytes(b"second", "second.wav")
            player.stop()
        events = [call.kwargs for call in lifecycle.call_args_list]
        self.assertEqual(
            [event["outcome"] for event in events],
            ["queued", "interrupted", "queued", "stopped"],
        )
        self.assertEqual(events[1]["reason"], "replaced")
        self.assertEqual(events[0]["attempt_id"], events[1]["attempt_id"])
        self.assertEqual(events[2]["attempt_id"], events[3]["attempt_id"])
        self.assertNotEqual(events[0]["attempt_id"], events[2]["attempt_id"])
        self.assertEqual(len({event["player_id"] for event in events}), 1)
        self.assertEqual(states, [QtPcmPlayer.PlaybackState.StoppedState])
        other = QtPcmPlayer(player_factory=_PcmPlayer)
        self.assertNotEqual(
            player._lifecycle["player_id"], other._lifecycle["player_id"]
        )
        player.deleteLater()
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_failed_attempt_does_not_also_record_stop(self):
        for failure in ("device", "underflow", "start", "decode"):
            with self.subTest(failure=failure):
                pcm = _PcmPlayer()
                player = QtPcmPlayer(player_factory=lambda: pcm)
                failures = []
                player.errorOccurred.connect(
                    lambda _error, message: failures.append(message)
                )
                with patch("vntts.support.record_audio_lifecycle") as lifecycle:
                    if failure == "start":
                        with patch.object(
                            pcm,
                            "play",
                            side_effect=PcmPlaybackError("Device unavailable"),
                        ):
                            player.play_bytes(b"audio", "test.wav")
                    elif failure == "decode":
                        with patch.object(
                            pcm,
                            "load_bytes",
                            side_effect=PcmPlaybackError("Invalid PCM"),
                        ):
                            player.play_bytes(b"audio", "test.wav")
                    else:
                        player.play_bytes(b"audio", "test.wav")
                        pcm.snapshot_value = PlaybackSnapshot(
                            pcm.token,
                            4,
                            4,
                            True,
                            False,
                            True,
                            failure == "underflow",
                            "Device unavailable" if failure == "device" else None,
                        )
                        player._poll()
                    player.stop()
                    player._poll()
                events = [call.kwargs for call in lifecycle.call_args_list]
                self.assertEqual(
                    [event["outcome"] for event in events].count("failed"), 1
                )
                self.assertFalse(
                    {"stopped", "interrupted", "finished"}
                    & {event["outcome"] for event in events}
                )
                self.assertEqual(len({event["attempt_id"] for event in events}), 1)
                self.assertEqual(len(failures), 1)
                player.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_hiding_owner_ancestor_interrupts_once_without_completion(self):
        owner = QWidget()
        service = QObject(owner)
        pcm = _PcmPlayer()
        player = QtPcmPlayer(service, player_factory=lambda: pcm)
        finished = []
        player.mediaStatusChanged.connect(finished.append)
        with patch("vntts.support.record_audio_lifecycle") as lifecycle:
            player.play_bytes(b"audio", "test.wav")
            QCoreApplication.sendEvent(owner, QEvent(QEvent.Type.Hide))
            player.stop()
            owner.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(
            [call.kwargs["outcome"] for call in lifecycle.call_args_list],
            ["queued", "interrupted"],
        )
        self.assertEqual(lifecycle.call_args_list[-1].kwargs["reason"], "owner-hidden")
        self.assertTrue(pcm.stopped)
        self.assertTrue(pcm.closed)
        self.assertEqual(finished, [])

    def test_source_change_interrupts_queued_attempt_once(self):
        pcm = _PcmPlayer()
        player = QtPcmPlayer(player_factory=lambda: pcm)
        with patch("vntts.support.record_audio_lifecycle") as lifecycle:
            player.play_bytes(b"audio", "test.wav")
            player.setSource(QUrl())
            player.stop()
        self.assertEqual(
            [call.kwargs["outcome"] for call in lifecycle.call_args_list],
            ["queued", "interrupted"],
        )
        self.assertEqual(
            lifecycle.call_args_list[-1].kwargs["reason"], "source-replaced"
        )
        player.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
