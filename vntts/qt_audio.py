"""Qt signals around the persistent sounddevice authoring player."""

from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Protocol
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QMediaPlayer as _QtMediaPlayer

from vntts.authoring.pcm_playback import (
    PcmClip,
    PcmPlaybackError,
    PersistentPcmPlayer,
    PlaybackSnapshot,
)

PLAYBACK_START_TIMEOUT_SECONDS = 5.0


class _PcmPlayer(Protocol):
    def load(self, path: str | Path) -> PcmClip: ...

    def load_bytes(self, payload: bytes, *, name: str) -> PcmClip: ...

    def play(self, clip: PcmClip) -> int: ...

    def stop(self) -> None: ...

    def snapshot(self) -> PlaybackSnapshot: ...

    def close(self) -> None: ...


class QtPcmPlayer(QObject):
    """QMediaPlayer-shaped adapter backed by stable persistent PCM playback."""

    Error = _QtMediaPlayer.Error
    MediaStatus = _QtMediaPlayer.MediaStatus
    PlaybackState = _QtMediaPlayer.PlaybackState

    errorOccurred = Signal(object, str)
    mediaStatusChanged = Signal(object)
    playbackStateChanged = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        player_factory: Callable[[], _PcmPlayer] = PersistentPcmPlayer,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        super().__init__(parent)
        self._player_factory = player_factory
        self._player: _PcmPlayer | None = None
        self._source: Path | None = None
        self._clip: PcmClip | None = None
        self._token: int | None = None
        self._started = False
        self._started_at: float | None = None
        self._error = ""
        self._lifecycle = {
            "player_id": uuid4().hex,
            "owner": type(parent).__name__ if parent is not None else "authoring",
        }
        self._clock = clock
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        owner = parent
        while owner is not None:
            owner.installEventFilter(self)
            owner = owner.parent()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.Hide, QEvent.Type.DeferredDelete}:
            self._stop(
                "interrupted",
                reason="owner-hidden"
                if event.type() == QEvent.Type.Hide
                else "owner-deleted",
            )
        return super().eventFilter(watched, event)

    def setSource(self, source: QUrl) -> None:
        self._stop("interrupted", reason="source-replaced")
        self._source = Path(source.toLocalFile()) if not source.isEmpty() else None
        self._clip = None

    def play(self) -> None:
        self._begin_attempt()
        try:
            player = self._ensure_player()
            if self._clip is None:
                if self._source is None:
                    raise PcmPlaybackError("Playback source is empty")
                self._clip = player.load(self._source)
            self._start(player, self._clip)
        except PcmPlaybackError as error:
            self._fail(str(error))

    def play_bytes(self, payload: bytes, source: str) -> PcmClip | None:
        self._begin_attempt()
        try:
            player = self._ensure_player()
            self._source = None
            self._clip = player.load_bytes(payload, name=source)
            self._start(player, self._clip)
            return self._clip
        except PcmPlaybackError as error:
            self._fail(str(error))
            return None

    def stop(self) -> None:
        self._stop(
            "stopped", reason="stop" if self._started else "cancelled-before-start"
        )

    def _stop(self, outcome: str, *, reason: str, emit_state: bool = True) -> None:
        active = self._token is not None
        self._token = None
        self._started = False
        self._started_at = None
        self._timer.stop()
        try:
            if self._player is not None:
                self._player.stop()
        finally:
            self._finish_attempt(outcome, reason=reason)
        if active and emit_state:
            self.playbackStateChanged.emit(self.PlaybackState.StoppedState)

    def _begin_attempt(self) -> None:
        if "attempt_id" in self._lifecycle:
            self._stop("interrupted", reason="replaced", emit_state=False)
        self._lifecycle["attempt_id"] = uuid4().hex

    def _finish_attempt(self, outcome: str, *, reason: str | None = None) -> None:
        if "attempt_id" in self._lifecycle:
            self._record_playback(outcome, reason=reason)
            self._lifecycle.pop("attempt_id")

    def errorString(self) -> str:
        return self._error

    def _ensure_player(self) -> _PcmPlayer:
        if self._player is None:
            pcm = self._player_factory()
            lifecycle = self._lifecycle

            def close_pcm() -> None:
                from vntts.support import record_audio_lifecycle

                try:
                    pcm.close()
                finally:
                    if "attempt_id" in lifecycle:
                        record_audio_lifecycle(
                            "authoring-playback",
                            **lifecycle,
                            outcome="interrupted",
                            reason="destroyed",
                            device_name=getattr(pcm, "device_name", None),
                        )
                        lifecycle.pop("attempt_id")

            # A QObject's own bound slot is disconnected during its destruction.
            # Retain the PCM callback owner until the native stream is closed.
            self.destroyed.connect(close_pcm)
            self._player = pcm
        return self._player

    def _start(self, player: _PcmPlayer, clip: PcmClip) -> None:
        self._error = ""
        self._started = False
        self._started_at = self._clock()
        self._token = player.play(clip)
        self._record_playback("queued", clip=clip)
        self._timer.start()

    def _poll(self) -> None:
        if self._token is None:
            return
        assert self._player is not None
        snapshot = self._player.snapshot()
        assert self._started_at is not None
        if snapshot.token != self._token:
            self._fail("Audio playback was interrupted unexpectedly; replay the sample")
            return
        if snapshot.error:
            self._fail(snapshot.error)
            return
        if snapshot.started and not self._started:
            self._started = True
            self._record_playback("started")
            self.playbackStateChanged.emit(self.PlaybackState.PlayingState)
            if self._token != snapshot.token:
                return
        if (
            not self._started
            and self._clock() - self._started_at >= PLAYBACK_START_TIMEOUT_SECONDS
        ):
            self._fail("Audio output did not start within 5 seconds; replay the sample")
            return
        if not snapshot.finished:
            return
        if snapshot.underflowed:
            self._fail("Audio output underflowed; replay the sample")
            return
        self._token = None
        self._timer.stop()
        self._finish_attempt("finished")
        self.mediaStatusChanged.emit(self.MediaStatus.EndOfMedia)
        self.playbackStateChanged.emit(self.PlaybackState.StoppedState)

    def _fail(self, message: str) -> None:
        self._stop("failed", reason=message)
        self._error = message
        self.errorOccurred.emit(self.Error.ResourceError, message)

    def _record_playback(
        self, outcome: str, *, clip: PcmClip | None = None, reason: str | None = None
    ) -> None:
        from vntts.support import record_audio_lifecycle

        record_audio_lifecycle(
            "authoring-playback",
            **self._lifecycle,
            outcome=outcome,
            reason=reason,
            device_name=getattr(self._player, "device_name", None),
            sample_rate=clip.sample_rate if clip is not None else None,
            channels=clip.samples.shape[1] if clip is not None else None,
        )


__all__ = ["QtPcmPlayer"]
