"""Validated, content-free progress from a source-audio publisher's stdout."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Thread
from typing import IO

from vntts.json_types import decode_json

PROGRESS_PREFIX = "R1999_PROGRESS "


@dataclass(frozen=True)
class SourceAudioProgress:
    stage: str
    processed: int
    total: int
    state: str


def parse_source_audio_progress(payload: str) -> SourceAudioProgress | None:
    if len(payload) > 4096:
        return None
    try:
        document = decode_json(payload)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    processed, total = document.get("processed"), document.get("total")
    stage, state = document.get("stage"), document.get("state")
    if (
        document.get("schema") != "r1999.source-audio-progress"
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or stage not in ("duration", "semantics", "references", "portraits")
        or state not in ("analyzing", "publishing")
        or type(processed) is not int
        or type(total) is not int
        or not 0 <= processed <= total <= 2**31 - 1
        or state == "publishing"
        and stage != "portraits"
        and processed != total
    ):
        return None
    return SourceAudioProgress(str(stage), processed, total, str(state))


class SourceAudioProgressReader:
    """Drain stdout independently while communicate owns the stderr pipe."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._output: list[str] = []
        self._pending: deque[SourceAudioProgress] = deque(maxlen=64)
        self._error: Exception | None = None
        self._thread = Thread(
            target=self._read, name="vntts-source-audio-progress", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> None:
        line_start = True
        try:
            with self._stream:
                while piece := self._stream.readline(4096 + len(PROGRESS_PREFIX) + 1):
                    progress = (
                        parse_source_audio_progress(piece[len(PROGRESS_PREFIX) :])
                        if line_start
                        and piece.startswith(PROGRESS_PREFIX)
                        and piece.endswith("\n")
                        else None
                    )
                    if progress is None:
                        self._output.append(piece)
                    else:
                        self._pending.append(progress)
                    line_start = piece.endswith("\n")
        except Exception as error:
            self._error = error

    def report(self, callback: Callable[[SourceAudioProgress], None]) -> None:
        if self._error is not None:
            raise self._error
        while True:
            try:
                progress = self._pending.popleft()
            except IndexError:
                return
            callback(progress)

    def finish(self) -> None:
        if self._thread.ident is None:
            self._stream.close()
            return
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("Source-audio progress pipe did not close")
        if self._error is not None:
            raise self._error

    def output(self) -> str:
        return "".join(self._output)
