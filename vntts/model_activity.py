"""Current foreground model activity, independent of runtime installation and Qt."""

from threading import Lock

from vntts.huggingface_assets import AssetProgress

_lock = Lock()
_activities: dict[str, tuple["ModelActivity", AssetProgress]] = {}


class ModelActivity:
    """Only the latest operation for a backend may update or clear its activity."""

    def __init__(self, backend: str) -> None:
        self.backend = backend

    def report(self, event: AssetProgress) -> None:
        with _lock:
            current = _activities.get(self.backend)
            if current is not None and current[0] is self:
                _activities[self.backend] = self, event

    def close(self) -> None:
        with _lock:
            current = _activities.get(self.backend)
            if current is not None and current[0] is self:
                del _activities[self.backend]


def begin_model_activity(
    backend: str, initial: AssetProgress | None = None
) -> ModelActivity:
    activity = ModelActivity(backend)
    with _lock:
        _activities[backend] = activity, initial or AssetProgress("resolving", backend)
    return activity


def get_model_activity(backend: str) -> AssetProgress | None:
    with _lock:
        current = _activities.get(backend)
        return current[1] if current is not None else None


def model_activities() -> dict[str, AssetProgress]:
    with _lock:
        return {backend: current[1] for backend, current in _activities.items()}
