"""Ownership and conservative lifetime claims for app-managed speech runtimes."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from vntts_artifacts.atomic_io import atomic_write_json

from vntts import application_directories
from vntts.authoring.advisory_lock import AdvisoryLockBusyError, exclusive_advisory_lock
from vntts.authoring.generation_lease import inspect_process_status
from vntts.runtime_paths import RUNTIME_ENVIRONMENT_VARIABLES
from vntts.services.tts_engine import TTSConfigurationError

OWNER_SCHEMA = "vntts.managed-runtime-generation-v1"


def read_record(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except OSError, ValueError:
        return {}


def owned_generation(backend, runtime):
    """Recognise exact owned directories, never aliases into user/bundled data."""
    if backend not in RUNTIME_ENVIRONMENT_VARIABLES:
        return None
    base = (
        application_directories.get_local_data_directory().resolve()
        / "speech-runtimes"
        / backend
    )
    runtime = Path(runtime).absolute()
    try:
        recipe, folder, generation, environment = runtime.relative_to(base).parts
    except ValueError:
        return None
    if (
        not re.fullmatch(r"[0-9a-f]{64}", recipe)
        or folder != "generations"
        or environment != "environment"
        or not re.fullmatch(r"[0-9a-f]{32}", generation)
    ):
        return None
    candidate = base / recipe / folder / generation
    for path in (
        base.parent,
        base,
        base / recipe,
        candidate.parent,
        candidate,
        runtime,
    ):
        if path.is_symlink() or path.is_junction():
            return None
    if read_record(candidate / "owner.json") != {
        "schema": OWNER_SCHEMA,
        "backend": backend,
        "recipe": recipe,
        "generation": generation,
    }:
        return None
    return candidate


class RuntimeUse:
    """Keep a backend and every launched child visible even after a parent crash."""

    def __init__(self, generation):
        self.path = generation / "users" / f"{uuid4().hex}.json"
        if self.path.parent.is_symlink() or self.path.parent.is_junction():
            raise TTSConfigurationError("Speech runtime usage directory is an alias.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.document = {"parent_pid": os.getpid(), "children": [], "launching": False}
        self.processes = []
        self._save()

    def _save(self):
        atomic_write_json(self.path, self.document)

    def begin_launch(self):
        self.document["launching"] = True
        self._save()

    def launched(self, process):
        if process is not None:
            self.processes.append(process)
            self.document["children"].append(process.pid)
        self.document["launching"] = False
        self._save()

    def close(self):
        # Never release a child whose shutdown could not be confirmed.
        try:
            uncertain = self.document["launching"] or any(
                p.poll() is None for p in self.processes
            )
        except OSError:
            uncertain = True
        if uncertain:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass  # A stale claim is reclaimed only once both processes are dead.


def claim_runtime(backend, runtime):
    generation = owned_generation(backend, runtime)
    if generation is None:
        return None
    guard = generation.parent.parent / "installation.lock"
    try:
        with exclusive_advisory_lock(guard):
            if (
                owned_generation(backend, runtime) != generation
                or not Path(runtime).is_dir()
            ):
                raise TTSConfigurationError(
                    "Speech runtime changed before startup. Retry."
                )
            return RuntimeUse(generation)
    except AdvisoryLockBusyError as error:
        raise AdvisoryLockBusyError(
            "Speech runtime maintenance is in progress in another window. Retry when it finishes."
        ) from error


def _in_use(generation):
    users = generation / "users"
    if users.is_symlink() or users.is_junction():
        return True
    if not users.exists():
        return False
    for path in users.iterdir():
        record = read_record(path)
        parent, children = record.get("parent_pid"), record.get("children")
        # ponytail: an interrupted launch with no recorded child PID stays protected;
        # add an OS process-tree identity only if orphaned storage becomes material.
        if record.get("launching") is not False or not isinstance(children, list):
            return True
        if any(
            type(pid) is not int or not 0 < pid <= 0x7FFFFFFF
            for pid in [parent, *children]
        ):
            return True
        if any(inspect_process_status(pid) != "dead" for pid in [parent, *children]):
            return True
    return False


def remove_inactive_generation(backend, generation):
    """Caller holds this recipe's installation guard."""
    if owned_generation(backend, generation / "environment") != generation or _in_use(
        generation
    ):
        return False
    shutil.rmtree(generation)
    return True


def cleanup_managed_runtimes(backend, keep, *, progress=None):
    """Remove only recognised inactive copies after a good replacement exists."""
    use = claim_runtime(backend, keep)
    if use is None:
        return
    try:
        _cleanup_managed_runtimes(backend, keep, progress=progress)
    finally:
        use.close()


def _cleanup_managed_runtimes(backend, keep, *, progress=None):
    progress = progress or (lambda _message: None)
    keep_generation = owned_generation(backend, keep)
    if keep_generation is None:
        return
    base = keep_generation.parents[2]
    removed = 0
    for location in base.iterdir():
        if (
            not re.fullmatch(r"[0-9a-f]{64}", location.name)
            or location.is_symlink()
            or location.is_junction()
        ):
            continue
        try:
            with exclusive_advisory_lock(location / "installation.lock"):
                generations = location / "generations"
                if (
                    not generations.is_dir()
                    or generations.is_symlink()
                    or generations.is_junction()
                ):
                    continue
                selected = read_record(location / "verified.json").get("generation")
                for generation in generations.iterdir():
                    if generation == keep_generation:
                        continue
                    if (
                        location == keep_generation.parent.parent
                        and generation.name == selected
                    ):
                        continue  # Preserve a newer publication from a concurrent caller.
                    if not remove_inactive_generation(backend, generation):
                        continue
                    removed += 1
                    if generation.name == selected:
                        (location / "verified.json").unlink(missing_ok=True)
        except AdvisoryLockBusyError:
            continue
        except OSError as error:
            progress(
                f"Speech runtime is ready; unused-copy cleanup was deferred: {error}"
            )
    if removed:
        progress(
            f"Removed {removed} unused speech runtime copies. Dependencies can be downloaded again if needed."
        )
