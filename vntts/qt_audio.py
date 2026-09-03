"""Qt signals around the persistent sounddevice authoring player."""

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer as _QtMediaPlayer

from vntts.authoring.pcm_playback import PcmPlaybackError, PersistentPcmPlayer


class QtPcmPlayer(QObject):
    """QMediaPlayer-shaped adapter backed by stable persistent PCM playback."""

    Error = _QtMediaPlayer.Error
    MediaStatus = _QtMediaPlayer.MediaStatus
    PlaybackState = _QtMediaPlayer.PlaybackState

    errorOccurred = Signal(object, str)
    mediaStatusChanged = Signal(object)
    playbackStateChanged = Signal(object)

    def __init__(self, parent=None, *, player_factory=PersistentPcmPlayer):
        super().__init__(parent)
        self._player_factory = player_factory
        self._player = None
        self._source = None
        self._clip = None
        self._token = None
        self._started = False
        self._error = ""
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        self.destroyed.connect(self._close)

    def setSource(self, source):
        self.stop()
        self._source = Path(source.toLocalFile()) if not source.isEmpty() else None
        self._clip = None

    def play(self):
        try:
            player = self._ensure_player()
            if self._clip is None:
                if self._source is None:
                    raise PcmPlaybackError("Playback source is empty")
                self._clip = player.load(self._source)
            self._start(player, self._clip)
        except PcmPlaybackError as error:
            self._fail(str(error))

    def play_bytes(self, payload, source):
        try:
            player = self._ensure_player()
            self._source = None
            self._clip = player.load_bytes(payload, name=source)
            self._start(player, self._clip)
            return self._clip
        except PcmPlaybackError as error:
            self._fail(str(error))
            return None

    def stop(self):
        active = self._token is not None
        self._token = None
        self._started = False
        self._timer.stop()
        if self._player is not None:
            self._player.stop()
        if active:
            self.playbackStateChanged.emit(self.PlaybackState.StoppedState)

    def errorString(self):
        return self._error

    def _ensure_player(self):
        if self._player is None:
            self._player = self._player_factory()
        return self._player

    def _start(self, player, clip):
        self._error = ""
        self._started = False
        self._token = player.play(clip)
        self._timer.start()

    def _poll(self):
        snapshot = self._player.snapshot()
        if self._token is None or snapshot.token != self._token:
            return
        if snapshot.error:
            self._fail(snapshot.error)
            return
        if snapshot.started and not self._started:
            self._started = True
            self.playbackStateChanged.emit(self.PlaybackState.PlayingState)
        if not snapshot.finished:
            return
        if snapshot.underflowed:
            self._fail("Audio output underflowed; replay the sample")
            return
        self._token = None
        self._timer.stop()
        self.mediaStatusChanged.emit(self.MediaStatus.EndOfMedia)
        self.playbackStateChanged.emit(self.PlaybackState.StoppedState)

    def _fail(self, message):
        self.stop()
        self._error = message
        self.errorOccurred.emit(self.Error.ResourceError, message)

    def _close(self, *_args):
        if self._player is not None:
            self._player.close()
            self._player = None


def play_audio_bytes(player, _parent, payload, source):
    return player.play_bytes(payload, source)


def release_audio_buffer(player, _clip):
    player.setSource(QUrl())


__all__ = ["QtPcmPlayer", "play_audio_bytes", "release_audio_buffer"]
