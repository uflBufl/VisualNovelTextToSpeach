import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from threading import RLock
from uuid import uuid4


@dataclass(frozen=True)
class DialogueHistoryEntry:
    id: str
    recorded_at: str
    character: str
    text: str


class DialogueHistory:
    def __init__(self, *, maximum_entries=1000, clock=None):
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be positive")
        self.maximum_entries = maximum_entries
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.entries = []
        self.active_entry_id = None
        self.lock = RLock()

    def add(self, character, text):
        character = (character or "Narrator").strip() or "Narrator"
        text = " ".join((text or "").split())
        if not text:
            self.finish_current()
            return None
        with self.lock:
            active = self._active_entry()
            if (
                active is not None
                and active.character == character
                and self._is_continuation(active.text, text)
            ):
                if active.text == text:
                    return active
                updated = replace(active, text=text)
                self.entries[-1] = updated
                return updated
            entry = DialogueHistoryEntry(
                id=uuid4().hex,
                recorded_at=self.clock().isoformat(),
                character=character,
                text=text,
            )
            self.entries.append(entry)
            if len(self.entries) > self.maximum_entries:
                self.entries = self.entries[-self.maximum_entries :]
            self.active_entry_id = entry.id
            return entry

    def finish_current(self):
        with self.lock:
            self.active_entry_id = None

    def snapshot(self):
        with self.lock:
            return list(self.entries)

    def search(self, query=""):
        query = " ".join((query or "").split()).casefold()
        entries = self.snapshot()
        if not query:
            return entries
        return [
            entry
            for entry in entries
            if query in entry.character.casefold() or query in entry.text.casefold()
        ]

    def export(self, path):
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
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(f"{path.suffix}.tmp")
        temporary_path.write_text(content + ("\n" if content else ""), encoding="utf-8")
        temporary_path.replace(path)
        return path

    def _active_entry(self):
        if not self.entries or self.entries[-1].id != self.active_entry_id:
            return None
        return self.entries[-1]

    @staticmethod
    def _is_continuation(previous, current):
        if previous.startswith(current) or current.startswith(previous):
            return True
        return SequenceMatcher(None, previous, current).ratio() >= 0.65
