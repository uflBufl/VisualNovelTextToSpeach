import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from durable_file import atomic_write_text

from vntts.playback import MAX_REPLAY_AUDIO_BYTES, ReplayAudio


@dataclass(frozen=True)
class DialogueHistoryEntry:
    id: str
    recorded_at: str
    character: str
    text: str


class DialogueHistory:
    def __init__(
        self,
        *,
        maximum_entries: int = 1000,
        maximum_audio_bytes: int = MAX_REPLAY_AUDIO_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be positive")
        self.maximum_entries = maximum_entries
        self.maximum_audio_bytes = maximum_audio_bytes
        self._audio: dict[str, dict[int, ReplayAudio | None]] = {}
        self._audio_bytes = 0
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: list[DialogueHistoryEntry] = []
        self._last_utterance: tuple[str, int] | None = None
        self._last_ordinal = 0
        self._lock = RLock()

    def add(
        self,
        character: str | None,
        text: str | None,
        *,
        utterance: tuple[str, int] | None = None,
        ordinal: int | None = None,
    ) -> DialogueHistoryEntry | None:
        character = (character or "Narrator").strip() or "Narrator"
        text = " ".join((text or "").split())
        if not text:
            return None
        with self._lock:
            part = 1 if ordinal is None else ordinal
            if utterance is not None and utterance == self._last_utterance:
                previous = self._entries[-1]
                if part > self._last_ordinal:
                    self._entries[-1] = replace(
                        previous, text=f"{previous.text} {text}"
                    )
                    self._last_ordinal = part
                    if previous.id in self._audio:
                        self._audio[previous.id][part] = None
                return self._entries[-1]
            entry = DialogueHistoryEntry(
                id=uuid4().hex,
                recorded_at=self.clock().isoformat(),
                character=character,
                text=text,
            )
            self._entries.append(entry)
            self._audio[entry.id] = {part: None}
            if len(self._entries) > self.maximum_entries:
                self._drop_audio(self._entries.pop(0).id)
            self._last_utterance = utterance
            self._last_ordinal = part
            return entry

    def retain_audio(
        self, entry_id: str, ordinal: int | None, clip: ReplayAudio
    ) -> None:
        with self._lock:
            parts = self._audio.get(entry_id)
            part = 1 if ordinal is None else ordinal
            if parts is None or part not in parts:
                return
            previous = parts[part]
            if previous is not None:
                if previous.complete or (
                    not clip.complete and previous.pcm.nbytes >= clip.pcm.nbytes
                ):
                    return
                self._audio_bytes -= previous.pcm.nbytes
            parts[part] = clip
            self._audio_bytes += clip.pcm.nbytes
            while self._audio_bytes > self.maximum_audio_bytes and self._audio:
                self._drop_audio(next(iter(self._audio)))

    def _drop_audio(self, entry_id: str) -> None:
        parts = self._audio.pop(entry_id, {})
        self._audio_bytes -= sum(
            clip.pcm.nbytes for clip in parts.values() if clip is not None
        )

    def recording(self, entry_id: str) -> tuple[ReplayAudio, ...]:
        with self._lock:
            parts = self._audio.get(entry_id)
            if not parts or None in parts.values():
                return ()
            return tuple(clip for clip in parts.values() if clip is not None)

    def snapshot(self) -> list[DialogueHistoryEntry]:
        with self._lock:
            return list(self._entries)

    def search(self, query: str | None = "") -> list[DialogueHistoryEntry]:
        query = " ".join((query or "").split()).casefold()
        entries = self.snapshot()
        if not query:
            return entries
        return [
            entry
            for entry in entries
            if query in entry.character.casefold() or query in entry.text.casefold()
        ]

    def export(self, path: str | Path) -> Path:
        path = Path(path).expanduser()
        entries = self.snapshot()
        if path.suffix.casefold() == ".json":
            content = json.dumps(
                {
                    "version": 1,
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "entries": [asdict(entry) for entry in entries],
                },
                ensure_ascii=False,
                indent=2,
            )
        else:
            content = "\n\n".join(
                f"[{entry.recorded_at}] {entry.character}\n{entry.text}"
                for entry in entries
            )
        atomic_write_text(path, content + ("\n" if content else ""))
        return path
