import unittest

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from vntts.authoring.pcm_playback import PcmClip, PlaybackSnapshot
from vntts.qt_audio import QtPcmPlayer, play_audio_bytes


class _PcmPlayer:
    def __init__(self):
        self.clip = PcmClip(np.ones((4, 2), np.float32), 48_000)
        self.snapshot_value = PlaybackSnapshot(1, 0, 4, False, True, False, False, None)
        self.stopped = self.closed = False

    def load_bytes(self, payload, *, name):
        self.loaded = payload, name
        return self.clip

    def play(self, clip):
        self.played = clip
        return 1

    def snapshot(self):
        return self.snapshot_value

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class QtAudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_pcm_adapter_reports_only_clean_dac_completion(self):
        pcm = _PcmPlayer()
        player = QtPcmPlayer(player_factory=lambda: pcm)
        finished = []
        failures = []
        player.mediaStatusChanged.connect(finished.append)
        player.errorOccurred.connect(lambda _error, message: failures.append(message))

        clip = play_audio_bytes(player, None, b"exact audio", "memory:test.wav")
        pcm.snapshot_value = PlaybackSnapshot(1, 4, 4, True, False, True, False, None)
        player._poll()

        self.assertIs(clip, pcm.clip)
        self.assertEqual(pcm.loaded, (b"exact audio", "memory:test.wav"))
        self.assertEqual(finished, [QtPcmPlayer.MediaStatus.EndOfMedia])
        self.assertEqual(failures, [])

        play_audio_bytes(player, None, b"exact audio", "memory:test.wav")
        pcm.snapshot_value = PlaybackSnapshot(1, 4, 4, True, False, True, True, None)
        player._poll()
        self.assertEqual(len(finished), 1)
        self.assertEqual(failures, ["Audio output underflowed; replay the sample"])
        player._close()

    def test_deleting_player_or_owner_closes_pcm_stream(self):
        for delete_owner in (False, True):
            with self.subTest(delete_owner=delete_owner):
                owner = QWidget()
                pcm = _PcmPlayer()
                player = QtPcmPlayer(owner, player_factory=lambda: pcm)
                player.play_bytes(b"audio", "test.wav")
                (owner if delete_owner else player).deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertTrue(pcm.closed)
                if not delete_owner:
                    owner.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
