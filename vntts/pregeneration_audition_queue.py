"""One application-owned renderer with bounded, session-owned audition work."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from stat import S_ISREG
from threading import Event, Lock
from time import monotonic

from vntts.authoring.advisory_lock import AdvisoryLockBusyError, exclusive_advisory_lock
from vntts.document_identity import canonical_document_sha256, is_lowercase_sha256
from vntts.pregeneration_audition import (
    ProgressReporter,
    VoiceAuditionCancelled,
    VoiceAuditionError,
    VoiceAuditionPreview,
    VoiceAuditionPreviewService,
    VoiceAuditionReference,
    _Cancellation,
    _count_audition,
    _validate_request,
    measure_audition_phase,
)
from vntts.pregeneration_voices import VoiceGroup, VoicePlan
from vntts.settings import AppSettings
from vntts.support import performance_operation_id, record_background_operation

PreviewInput = tuple[VoiceGroup, str, str | None]


def _context_key(
    plan: VoicePlan, group: VoiceGroup, source: str, text: str | None
) -> str:
    candidate = _validate_request(plan, group, source)
    return canonical_document_sha256(
        {
            "manifest": plan.voice_manifest_sha256,
            "group": group.group_id,
            "context": group.decision_context_sha256,
            "source": source,
            "references": candidate.reference_sha256s,
            "transcript": candidate.reference_transcript,
            "text": group.sample_text if text is None else text,
            "backend": plan.synthesis_backend,
            "model": plan.synthesis_model,
            "language": plan.synthesis_language,
            "profile": plan.synthesis_profile,
            "controls": plan.synthesis_controls_sha256,
        }
    )


@dataclass
class _Job:
    session: VoiceAuditionSession
    key: str
    plan: VoicePlan
    group: VoiceGroup
    source: str
    text: str | None
    foreground: bool = False
    automatic: bool = False
    cancel: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    preview: VoiceAuditionPreview | None = None
    error: Exception | None = None
    progress: ProgressReporter | None = None
    operation_id: str = field(
        default_factory=lambda: performance_operation_id.get() or token_hex(16)
    )
    queued_at: float = field(default_factory=monotonic)


class VoiceAuditionCoordinator:
    """Keep model lifetime independent of windows; schedule between renders."""

    def __init__(
        self,
        service: VoiceAuditionPreviewService,
        *,
        background_available: Callable[[], bool] = lambda: True,
        max_cache_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self.service = service
        self.background_available = background_available
        self.max_cache_bytes = max_cache_bytes
        self._lock = Lock()
        self._cache_lock = Lock()
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voice-audition"
        )
        self._pending: list[_Job] = []
        self._running: _Job | None = None
        self._draining = False
        self._closed = False
        self._suspended = False
        self._sessions: set[VoiceAuditionSession] = set()
        self._warmup_cancel: Event | None = None
        self._warmup_session: VoiceAuditionSession | None = None

    def session(self) -> VoiceAuditionSession:
        with self._lock:
            if self._closed:
                raise VoiceAuditionError("Voice audition coordinator is closed")
            session = VoiceAuditionSession(self)
            self._sessions.add(session)
            return session

    def suspend(self) -> None:
        """Explicit unload/reading drops speculation without stopping another owner."""
        with self._lock:
            self._suspended = True
            self._cancel_warmup()
            for session in self._sessions:
                session._ready.clear()
                self._cancel_session(session, background_only=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel_warmup()
            for session in self._sessions:
                self._cancel_session(session)
        self._pool.shutdown(wait=True)
        self.service.close()

    def cancel_warmup(self) -> None:
        with self._lock:
            self._cancel_warmup()

    def _cancel_warmup(self) -> None:
        if self._warmup_cancel is not None:
            self._warmup_cancel.set()

    def _warmup(
        self, session: VoiceAuditionSession, settings: AppSettings, cancel: Event
    ) -> None:
        try:
            with self._lock:
                if (
                    cancel.is_set()
                    or self._closed
                    or session._closed
                    or self._suspended
                    or not self.background_available()
                ):
                    return
            self.service.warmup(settings, cancel_event=cancel)
        except Exception:
            # Missing installed assets leave the normal explicit preview available.
            pass
        finally:
            with self._lock:
                if self._warmup_cancel is cancel:
                    self._warmup_cancel = None
                    self._warmup_session = None

    def _cancel_session(
        self, session: VoiceAuditionSession, *, background_only: bool = False
    ) -> None:
        for key, job in tuple(session._jobs.items()):
            if not background_only or not job.foreground:
                job.cancel.set()
                if job is not self._running:
                    if job in self._pending:
                        self._pending.remove(job)
                    job.error = VoiceAuditionCancelled(
                        "Voice audition generation was cancelled"
                    )
                    job.done.set()
                    session._jobs.pop(key, None)

    def _submit(self, job: _Job) -> None:
        self._pending.append(job)
        job.session._jobs[job.key] = job
        if not self._draining:
            self._draining = True
            self._pool.submit(self._drain)

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._draining = False
                    return
                foreground = next(
                    (job for job in self._pending if job.foreground), None
                )
                job = foreground or self._pending[0]
                self._pending.remove(job)
                self._running = job
            ready_stamp: tuple[tuple[object, ...], ...] = ()
            operation_token = performance_operation_id.set(job.operation_id)
            record_background_operation(
                "pregeneration-audition-queue-wait",
                (monotonic() - job.queued_at) * 1000,
                "complete",
                operation_id=job.operation_id,
                timing_scope="queue-wait",
            )
            try:
                ready_stamp = self._run_job(job)
            except Exception as error:
                job.error = error
            finally:
                performance_operation_id.reset(operation_token)
            with self._lock:
                if (
                    job.preview is not None
                    and job.error is None
                    and not job.cancel.is_set()
                    and not job.session._closed
                ):
                    job.session._ready[job.key] = (job.preview, ready_stamp)
                if job.error is not None and not isinstance(
                    job.error, VoiceAuditionCancelled
                ):
                    job.session._failures.add(job.key)
                elif job.preview is not None:
                    job.session._failures.discard(job.key)
                if job.session._jobs.get(job.key) is job:
                    job.session._jobs.pop(job.key, None)
                self._running = None
                job.done.set()
                protected = self._protected_preview_ids()
                if job.preview is not None:
                    protected.add(job.preview.identity)
            self._prune(protected)

    def _run_job(self, job: _Job) -> tuple[tuple[object, ...], ...]:
        if job.cancel.is_set():
            raise VoiceAuditionCancelled("Voice audition generation was cancelled")
        stamp = self.service.authorization_stamp(job.plan, job.group, job.source)
        preview = self.service.lookup(
            job.plan,
            job.group,
            job.source,
            text=job.text,
            cancel_event=job.cancel,
        )
        if preview is None:
            with self._lock:
                if not job.foreground and (
                    self._suspended
                    or not self.background_available()
                    or not (job.automatic or self.service.can_prefetch(job.plan))
                ):
                    raise VoiceAuditionCancelled(
                        "Background voice preparation is paused"
                    )
            preview = self.service.generate(
                job.plan,
                job.group,
                job.source,
                text=job.text,
                cancel_event=job.cancel,
                progress=job.progress,
            )
        job.preview = preview
        if stamp != self.service.authorization_stamp(job.plan, job.group, job.source):
            raise VoiceAuditionError("Voice inputs changed during preview preparation")
        return stamp + self.service.audio_stamp(preview)

    def _protected_preview_ids(self) -> set[str]:
        return {
            preview.identity
            for session in self._sessions
            for key, (preview, _stamp) in session._ready.items()
            if key in session._focus_keys or key == session._manual_key
        }

    def _prune(self, protected: set[str]) -> None:
        """Only audition WAVs are disposable; never follow aliases or touch voices."""
        try:
            if self.service.root.is_symlink() or self.service.root.is_junction():
                return
            entries: list[tuple[float, Path, int]] = []
            for path in self.service.root.glob("*.wav"):
                stat = path.stat(follow_symlinks=False)
                if is_lowercase_sha256(path.stem) and S_ISREG(stat.st_mode):
                    entries.append((stat.st_mtime, path, stat.st_size))
            total = sum(size for _, _, size in entries)
            for _, path, size in sorted(entries):
                if total <= self.max_cache_bytes:
                    break
                if path.stem in protected:
                    continue
                try:
                    with self._cache_lock, self._lock:
                        if path.stem in self._protected_preview_ids():
                            continue
                        with exclusive_advisory_lock(path.with_suffix(".lock")):
                            path.unlink(missing_ok=True)
                            path.with_suffix(".json").unlink(missing_ok=True)
                    total -= size
                except AdvisoryLockBusyError:
                    continue
        except OSError, ValueError:
            pass


class VoiceAuditionSession:
    """A window can abandon its requests without closing the shared renderer."""

    supports_prefetch = True

    def __init__(self, owner: VoiceAuditionCoordinator) -> None:
        self.owner = owner
        self._jobs: dict[str, _Job] = {}
        self._ready: dict[
            str, tuple[VoiceAuditionPreview, tuple[tuple[object, ...], ...]]
        ] = {}
        self._closed = False
        self._focus_keys: set[str] = set()
        self._manual_key: str | None = None
        self._failures: set[str] = set()

    @property
    def backend(self) -> object | None:
        return self.owner.service.backend

    @property
    def preparing(self) -> bool:
        with self.owner._lock:
            return any(not job.cancel.is_set() for job in self._jobs.values())

    def can_prepare_automatically(self) -> bool:
        with self.owner._lock:
            return bool(
                not self._closed
                and not self.owner._closed
                and not self.owner._suspended
                and self.owner.background_available()
            )

    def warmup(self, settings: AppSettings) -> None:
        with self.owner._lock:
            self.owner._cancel_warmup()
            if (
                self._closed
                or self.owner._closed
                or not self.owner.background_available()
                or settings.speech_backend != "moss-tts"
            ):
                return
            # Continue is a new explicit preparation request after an unload/read.
            self.owner._suspended = False
            cancel = Event()
            self.owner._warmup_cancel = cancel
            self.owner._warmup_session = self
            self.owner._pool.submit(self.owner._warmup, self, settings, cancel)

    @measure_audition_phase("request")
    def generate(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
        cancel_event: _Cancellation | None = None,
        progress: ProgressReporter | None = None,
        automatic: bool = False,
    ) -> VoiceAuditionPreview:
        with self.owner._lock:
            if self._closed or self.owner._closed:
                raise VoiceAuditionError("Voice audition session is closed")
        key = _context_key(plan, group, candidate_source_id, text)
        stamp = self.owner.service.authorization_stamp(plan, group, candidate_source_id)
        if not automatic:
            with self.owner._lock:
                self._manual_key = key
                self._failures.discard(key)
        # Saved audio does not queue behind an unrelated model render.
        with self.owner._cache_lock:
            preview = self.owner.service.lookup(
                plan, group, candidate_source_id, text=text, cancel_event=cancel_event
            )
            if preview is not None:
                if stamp != self.owner.service.authorization_stamp(
                    plan, group, candidate_source_id
                ):
                    raise VoiceAuditionError(
                        "Voice inputs changed during preview preparation"
                    )
                with self.owner._lock:
                    if not self._closed:
                        self._ready[key] = (
                            preview,
                            stamp + self.owner.service.audio_stamp(preview),
                        )
                return preview
        with self.owner._lock:
            if self._closed or self.owner._closed:
                raise VoiceAuditionError("Voice audition session is closed")
            if automatic:
                if self.owner._suspended or not self.owner.background_available():
                    raise VoiceAuditionCancelled(
                        "Background voice preparation is paused"
                    )
            else:
                self.owner._suspended = False
            job = self._jobs.get(key)
            if job is None or job.cancel.is_set():
                job = _Job(
                    self,
                    key,
                    plan,
                    group,
                    candidate_source_id,
                    text,
                    foreground=not automatic,
                    automatic=automatic,
                    progress=progress,
                )
                self.owner._submit(job)
            else:
                job.foreground = job.foreground or not automatic
                job.automatic = job.automatic or automatic
                job.progress = progress
        return _wait_for_preview(job, cancel_event)

    def peek(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> VoiceAuditionPreview | None:
        key = _context_key(plan, group, candidate_source_id, text)
        with self.owner._lock:
            ready = self._ready.get(key)
        if ready is None:
            return None
        try:
            if self.owner.service.authorization_unchanged(ready[1]):
                return ready[0]
        except OSError, ValueError, VoiceAuditionError:
            pass
        with self.owner._lock:
            self._ready.pop(key, None)
        return None

    @measure_audition_phase("restore-cache")
    def restore_cached(
        self,
        plan: VoicePlan,
        requests: Sequence[PreviewInput],
        *,
        cancel_event: _Cancellation | None = None,
    ) -> None:
        """Admit existing disk samples in a worker without loading a TTS engine."""
        _count_audition("candidate_count", len(requests))
        for group, source, text in requests:
            if cancel_event is not None and cancel_event.is_set():
                raise VoiceAuditionCancelled("Cached audition loading was cancelled")
            with self.owner._lock:
                if self._closed or self.owner._closed:
                    return
            try:
                self._restore_cached_preview(plan, group, source, text, cancel_event)
            except VoiceAuditionCancelled:
                raise
            except OSError, ValueError, VoiceAuditionError:
                continue

    def _restore_cached_preview(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        source: str,
        text: str | None,
        cancel_event: _Cancellation | None,
    ) -> None:
        key = _context_key(plan, group, source, text)
        with self.owner._cache_lock:
            if not self.owner.service.has_cached_preview(
                plan, group, source, text=text
            ):
                return
            stamp = self.owner.service.authorization_stamp(plan, group, source)
            preview = self.owner.service.lookup(
                plan, group, source, text=text, cancel_event=cancel_event
            )
            if preview is None or not self.owner.service.authorization_unchanged(stamp):
                return
            if cancel_event is not None and cancel_event.is_set():
                raise VoiceAuditionCancelled("Cached audition loading was cancelled")
            ready = (preview, stamp + self.owner.service.audio_stamp(preview))
            with self.owner._lock:
                if not self._closed and not self.owner._closed:
                    self._ready[key] = ready

    def preparation_state(
        self,
        plan: VoicePlan,
        group: VoiceGroup,
        candidate_source_id: str,
        *,
        text: str | None = None,
    ) -> str:
        if self.peek(plan, group, candidate_source_id, text=text) is not None:
            return "ready"
        key = _context_key(plan, group, candidate_source_id, text)
        with self.owner._lock:
            job = self._jobs.get(key)
            if job is not None and not job.cancel.is_set():
                return "preparing" if self.owner._running is job else "queued"
            return "failed" if key in self._failures else "not-prepared"

    def prefetch(
        self,
        plan: VoicePlan,
        requests: Sequence[PreviewInput],
        *,
        automatic: bool = False,
    ) -> None:
        selected = tuple(dict.fromkeys(requests))[:3]
        keys = {
            _context_key(plan, group, source, text) for group, source, text in selected
        }
        with self.owner._lock:
            if self._closed or self.owner._closed:
                return
            self._focus_keys = keys
            if self._manual_key not in keys:
                self._manual_key = None
            self._cancel_displaced_jobs(keys)
            if self.owner._suspended or not self.owner.background_available():
                return
            for group, source, text in selected:
                key = _context_key(plan, group, source, text)
                if key in self._ready or key in self._failures:
                    continue
                job = self._jobs.get(key)
                if job is None or job.cancel.is_set():
                    self.owner._submit(
                        _Job(self, key, plan, group, source, text, automatic=automatic)
                    )
                elif automatic:
                    job.automatic = True
            # Hovered/current targets precede stale speculative neighbours.
            pending = self.owner._pending
            prioritized = [
                self._jobs[key]
                for key in (
                    _context_key(plan, group, source, text)
                    for group, source, text in selected
                )
                if key in self._jobs and self._jobs[key] in pending
            ]
            self.owner._pending = prioritized + [
                job for job in pending if job not in prioritized
            ]

    def _cancel_displaced_jobs(self, keys: set[str]) -> None:
        for key, job in tuple(self._jobs.items()):
            if key not in keys and not job.foreground:
                job.cancel.set()
                if job in self.owner._pending:
                    self.owner._pending.remove(job)
                    job.error = VoiceAuditionCancelled(
                        "Voice audition preparation was displaced"
                    )
                    job.done.set()
                    self._jobs.pop(key, None)

    def reference_audio(
        self, plan: VoicePlan, group: VoiceGroup, source: str
    ) -> VoiceAuditionReference | None:
        return self.owner.service.reference_audio(plan, group, source)

    def cancel(self) -> None:
        with self.owner._lock:
            if self.owner._warmup_session is self:
                self.owner._cancel_warmup()
            self.owner._cancel_session(self)

    def close(self) -> None:
        with self.owner._lock:
            self._closed = True
            if self.owner._warmup_session is self:
                self.owner._cancel_warmup()
            self.owner._cancel_session(self)
            self._ready.clear()
            self.owner._sessions.discard(self)

    def new_session(self) -> VoiceAuditionSession:
        return self.owner.session()


def _wait_for_preview(
    job: _Job, cancel_event: _Cancellation | None
) -> VoiceAuditionPreview:
    while not job.done.wait(0.05):
        if cancel_event is not None and cancel_event.is_set():
            job.cancel.set()
            raise VoiceAuditionCancelled("Voice audition generation was cancelled")
    if cancel_event is not None and cancel_event.is_set():
        raise VoiceAuditionCancelled("Voice audition generation was cancelled")
    if job.error is not None:
        raise job.error
    assert job.preview is not None
    return job.preview
