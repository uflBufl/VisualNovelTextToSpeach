"""Leaf loading primitives for authoritative generation state inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from vntts_artifacts.voice_generation_queue import (
    VoiceGenerationQueue,
    VoiceGenerationQueueError,
)

from vntts.authoring.generation_lease import BulkGenerationError


def load_stable_generation_queue(queue_path):
    """Load a queue from one immutable byte snapshot and return its SHA-256."""
    queue_path = Path(queue_path)
    try:
        payload = queue_path.read_bytes()
    except OSError as error:
        raise BulkGenerationError(str(error)) from error
    digest = hashlib.sha256(payload).hexdigest()
    try:
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / "queue.jsonl"
            snapshot.write_bytes(payload)
            queue = VoiceGenerationQueue.load(snapshot)
    except (OSError, VoiceGenerationQueueError) as error:
        raise BulkGenerationError(str(error)) from error
    return queue, digest


__all__ = ["load_stable_generation_queue"]
