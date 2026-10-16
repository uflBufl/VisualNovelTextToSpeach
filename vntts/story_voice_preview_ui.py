"""Cancellable playback of a planned voice without leaving the story summary."""

from collections.abc import Sequence
from hashlib import sha256
from threading import Event
from typing import cast

from PySide6.QtCore import QEvent, QMetaObject, QObject, QThreadPool, Signal
from PySide6.QtMultimedia import QMediaPlayer

from vntts.async_ui import LatestTaskRunner
from vntts.path_safety import read_regular_bytes
from vntts.pregeneration_audition import VoiceAuditionCancelled, VoiceAuditionError
from vntts.pregeneration_audition_ui import (
    _PrefetchVoiceAuditionPreviewer,
    _PreviewAudio,
    _VoiceAuditionPreviewer,
)
from vntts.pregeneration_voices import VoiceGroup, VoicePlan
from vntts.qt_audio import QtPcmPlayer


class StoryVoicePreview(QObject):
    statusChanged = Signal(str)
    activeChanged = Signal(bool)

    def __init__(
        self,
        service: _VoiceAuditionPreviewer,
        parent: QObject,
        *,
        player: QtPcmPlayer | None = None,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.session: _VoiceAuditionPreviewer | None = None
        self.player = player
        self.runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.runner.finished.connect(self._finished)
        self.preparation_runner = LatestTaskRunner(self, thread_pool=thread_pool)
        self.preparation_session: _PrefetchVoiceAuditionPreviewer | None = None
        self.preparation_cancellation = Event()
        self._preparation_cleanup: tuple[QMetaObject.Connection, ...] = ()
        self.cancellation = Event()
        self.active = False
        if self.player is not None:
            self._connect_player()

    def _connect_player(self) -> None:
        assert self.player is not None
        self.player.errorOccurred.connect(lambda _code, message: self._failed(message))
        self.player.mediaStatusChanged.connect(self._media_changed)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Hide:
            self.stop()
            self.cancel_preparation()
        return super().eventFilter(watched, event)

    def prepare(
        self, requests: Sequence[tuple[VoicePlan, VoiceGroup, str, str]]
    ) -> None:
        self.cancel_preparation()
        if (
            not requests
            or getattr(self.service, "supports_prefetch", False) is not True
        ):
            return
        session = cast(
            _PrefetchVoiceAuditionPreviewer, getattr(self.service, "new_session")()
        )
        self.preparation_session = session
        self.preparation_cancellation = Event()
        self._preparation_cleanup = (
            self.destroyed.connect(self.preparation_cancellation.set),
            self.destroyed.connect(session.close),
        )
        self.preparation_runner.start(
            self._prepare_requests,
            session,
            requests,
            self.preparation_cancellation,
        )

    @staticmethod
    def _prepare_requests(
        session: _PrefetchVoiceAuditionPreviewer,
        requests: Sequence[tuple[VoicePlan, VoiceGroup, str, str]],
        cancellation: Event,
    ) -> None:
        for plan, group, source, text in requests:
            if cancellation.is_set():
                return
            try:
                session.generate(
                    plan,
                    group,
                    source,
                    text=text,
                    cancel_event=cancellation,
                    automatic=True,
                )
            except VoiceAuditionCancelled:
                return
            except OSError, ValueError, VoiceAuditionError:
                continue

    def cancel_preparation(self) -> None:
        self.preparation_cancellation.set()
        self.preparation_runner.cancel()
        for connection in self._preparation_cleanup:
            QObject.disconnect(connection)
        self._preparation_cleanup = ()
        if self.preparation_session is not None:
            self.preparation_session.close()
            self.preparation_session = None

    def play(self, plan: VoicePlan, group: VoiceGroup, source: str, text: str) -> None:
        self.stop()
        if self.session is None:
            self.session = (
                getattr(self.service, "new_session")()
                if getattr(self.service, "supports_prefetch", False) is True
                else self.service
            )
        self.cancellation = Event()
        self.active = True
        self.activeChanged.emit(True)
        self.statusChanged.emit("Preparing selected voice preview...")
        try:
            peek = getattr(self.session, "peek", None)
            ready = (
                peek(plan, group, source, text=text)
                if getattr(self.session, "supports_prefetch", False) is True
                and callable(peek)
                else None
            )
            if ready is None and self.session is not self.service:
                ready = getattr(self.service, "peek")(plan, group, source, text=text)
            if ready is None and self.preparation_session is not None:
                ready = getattr(self.preparation_session, "peek")(
                    plan, group, source, text=text
                )
        except (OSError, ValueError, VoiceAuditionError) as error:
            self._finished(None, error)
            return
        if ready is not None:
            self._finished(ready, None)
            return
        self.runner.start(
            self.session.generate,
            plan,
            group,
            source,
            text=text,
            cancel_event=self.cancellation,
        )

    def _finished(self, preview: _PreviewAudio | None, error: Exception | None) -> None:
        if not self.active:
            return
        if error is not None:
            self._failed(f"Unable to preview this voice: {error}")
            return
        try:
            if preview is None:
                raise ValueError("Voice preview is unavailable")
            payload = read_regular_bytes(preview.path)
            if sha256(payload).hexdigest() != preview.audio_sha256:
                raise ValueError("Voice preview changed after validation")
            if self.player is None:
                self.player = QtPcmPlayer(self)
                self._connect_player()
            if self.player.play_bytes(payload, source=str(preview.path)) is None:
                self._failed("Unable to start voice preview playback.")
                return
            if self.active:
                self.statusChanged.emit("Playing selected voice preview.")
        except (OSError, ValueError) as failure:
            self._failed(f"Unable to play voice preview: {failure}")

    def _media_changed(self, status: object) -> None:
        if self.active and status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.stop()
            self.statusChanged.emit("Voice preview finished. Play again to compare.")

    def _failed(self, message: str) -> None:
        if self.active:
            self.stop()
            self.statusChanged.emit(message)

    def stop(self) -> None:
        was_active = self.active
        self.active = False
        self.cancellation.set()
        self.runner.cancel()
        if self.session is not None and self.session is not self.service:
            self.session.cancel()
        if self.player is not None:
            self.player.stop()
        if was_active:
            self.activeChanged.emit(False)

    def close(self) -> None:
        self.stop()
        self.cancel_preparation()
        if self.session is not None and self.session is not self.service:
            self.session.close()
        self.session = None
