import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

import vntts.pregeneration_audition as audition_module
from tests.pregeneration_fixtures import FakeBackend, ambiguous_fixture, clean_wav_bytes
from vntts.pregeneration_audition import (
    VoiceAuditionCancelled,
    VoiceAuditionError,
    VoiceAuditionPreviewService,
)
from vntts.pregeneration_audition_queue import (
    VoiceAuditionCoordinator,
    _wait_for_preview,
)
from vntts.settings import AppSettings
from vntts.speech_worker import FatalSpeechWorkerError
from vntts.support import PerformanceLog


class VoiceAuditionQueueTest(unittest.TestCase):
    def test_warm_batch_reuses_admission_and_correlates_worker_costs(self):
        with TemporaryDirectory() as directory:
            plan, group, _, _, owner = self.fixture(Path(directory))
            requests = tuple(
                (group, candidate.source_id, None) for candidate in group.candidates
            )
            performance = PerformanceLog()
            with patch("vntts.support.performance_log", performance):
                for _, source, _ in requests:
                    owner.session().generate(plan, group, source)
            events = performance.report()["events"]
            request_ids = {
                event["operation_id"]
                for event in events
                if event["operation"] == "pregeneration-audition-request"
            }
            self.assertEqual(len(request_ids), len(requests))
            for event in events:
                self.assertIn(event["operation_id"], request_ids)
            root = owner.service.root
            owner.close()
            restored = VoiceAuditionCoordinator(VoiceAuditionPreviewService(root))
            self.addCleanup(restored.close)
            session = restored.session()
            with (
                patch.object(
                    audition_module.CharacterVoiceRegistry,
                    "from_document",
                    wraps=audition_module.CharacterVoiceRegistry.from_document,
                ) as parse,
                patch.object(
                    audition_module,
                    "analyze_reference_bytes",
                    wraps=audition_module.analyze_reference_bytes,
                ) as preflight,
            ):
                session.restore_cached(plan, requests)
                for _, source, _ in requests:
                    self.assertIsNotNone(session.peek(plan, group, source))
                parse.assert_called_once()
                expected = {
                    checksum
                    for candidate in group.candidates
                    for checksum in candidate.reference_sha256s
                }
                self.assertEqual(preflight.call_count, len(expected))
                session.close()
                restored.session().restore_cached(plan, requests)
                parse.assert_called_once()
                self.assertEqual(preflight.call_count, len(expected))

    def test_empty_cache_never_loads_authority_and_reports_correlated_misses(self):
        with TemporaryDirectory() as directory:
            plan, group, _, _, owner = self.fixture(Path(directory))
            session = owner.session()
            requests = tuple(
                (group, candidate.source_id, None) for candidate in group.candidates
            )
            performance = PerformanceLog()
            with (
                patch.object(owner.service, "authorization_stamp") as stamp,
                patch.object(audition_module, "capture_authority_file") as capture,
                patch("vntts.support.performance_log", performance),
            ):
                session.restore_cached(plan, requests)
            stamp.assert_not_called()
            capture.assert_not_called()
            events = performance.report()["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["cache_misses"], len(requests))
            self.assertEqual(events[0]["candidate_count"], len(requests))
            self.assertEqual(events[0]["timing_scope"], "inclusive")
            self.assertTrue(events[0]["operation_id"])
            self.assertIsNone(owner.service.backend)

    def test_warmup_starts_early_and_suspend_cancels_its_startup(self):
        service = Mock()
        entered = Event()

        def warmup(_settings, *, cancel_event):
            entered.set()
            self.assertTrue(cancel_event.wait(5))

        service.warmup.side_effect = warmup
        owner = VoiceAuditionCoordinator(service)
        self.addCleanup(owner.close)
        owner.session().warmup(AppSettings(speech_backend="moss-tts"))
        self.assertTrue(entered.wait(5))
        owner.suspend()
        self.wait(owner)
        service.warmup.assert_called_once()

    def test_warmup_respects_reading_and_closed_session_before_launch(self):
        for close in (False, True):
            with self.subTest(close=close):
                service = Mock()
                owner = VoiceAuditionCoordinator(service)
                self.addCleanup(owner.close)
                session = owner.session()
                release = Event()
                owner._pool.submit(lambda: release.wait(5))
                session.warmup(AppSettings(speech_backend="moss-tts"))
                if close:
                    session.close()
                else:
                    owner.background_available = lambda: False
                release.set()
                self.wait(owner)
                service.warmup.assert_not_called()

    def test_new_model_request_cancels_queued_warmup(self):
        service = Mock()
        owner = VoiceAuditionCoordinator(service)
        self.addCleanup(owner.close)
        session = owner.session()
        release = Event()
        owner._pool.submit(lambda: release.wait(5))
        session.warmup(AppSettings(speech_backend="moss-tts", tts_model="first"))
        session.warmup(AppSettings(speech_backend="moss-tts", tts_model="second"))
        release.set()
        self.wait(owner)
        service.warmup.assert_called_once()
        self.assertEqual(service.warmup.call_args.args[0].tts_model, "second")

    def fixture(self, root):
        plan, group, manifest = ambiguous_fixture(root)
        group = replace(group, alternate_sample_text="A different preview phrase.")
        plan = replace(plan, groups=(group,))
        backend = FakeBackend("moss-tts")
        service = VoiceAuditionPreviewService(
            root / "previews", backend_factory=lambda *_args, **_kwargs: backend
        )
        owner = VoiceAuditionCoordinator(service)
        self.addCleanup(owner.close)
        return plan, group, manifest, backend, owner

    def wait(self, owner):
        owner._pool.submit(lambda: None).result(timeout=5)

    def test_prefetched_game_phrase_and_manual_custom_text_use_exact_inputs(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            game_phrase = "Morning, traveller."
            custom_phrase = "Моя собственная реплика!"
            session.prefetch(plan, ((group, source, game_phrase),), automatic=True)
            self.wait(owner)
            self.assertEqual(
                session.preparation_state(plan, group, source, text=game_phrase),
                "ready",
            )
            self.assertIsNone(
                session.preparation_error(plan, group, source, text=game_phrase)
            )
            prepared = session.peek(plan, group, source, text=game_phrase)
            self.assertEqual(prepared.text, game_phrase)
            self.assertEqual(
                session.generate(plan, group, source, text=game_phrase),
                replace(prepared, reused=True),
            )
            edited = session.generate(plan, group, source, text=custom_phrase)
            self.assertEqual(edited.text, custom_phrase)
            self.assertNotEqual(edited.identity, prepared.identity)
            self.assertEqual(
                [request.text for request in backend.requests],
                [game_phrase, custom_phrase],
            )

    def test_automatic_preparation_can_start_a_cold_model(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            self.assertIsNone(owner.service.backend)
            self.assertEqual(backend.requests, [])
            preview = session.generate(
                plan, group, group.candidates[0].source_id, automatic=True
            )
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(
                session.peek(plan, group, group.candidates[0].source_id), preview
            )

    def test_explicit_preview_joins_shared_startup_and_takes_the_next_render(self):
        for same_key in (False, True):
            with self.subTest(same_key=same_key), TemporaryDirectory() as directory:
                plan, group, _, backend, owner = self.fixture(Path(directory))
                session = owner.session()
                source = group.candidates[0].source_id
                selected_text = None if same_key else group.alternate_sample_text
                entered, release, joined = Event(), Event(), Event()
                progress = []

                def factory(*_args, **kwargs):
                    kwargs["startup_progress"]("Loading model weights...")
                    entered.set()
                    self.assertTrue(release.wait(5))
                    kwargs["startup_progress"]("Model weights loaded.")
                    return backend

                def wait_for_preview(job, cancel_event):
                    joined.set()
                    return _wait_for_preview(job, cancel_event)

                with (
                    patch.object(
                        owner.service, "backend_factory", side_effect=factory
                    ) as start,
                    ThreadPoolExecutor() as pool,
                ):
                    try:
                        session.prefetch(plan, ((group, source, None),), automatic=True)
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(
                            session.preparation_progress(plan, group, source),
                            "Loading model weights...",
                        )
                        with patch(
                            "vntts.pregeneration_audition_queue._wait_for_preview",
                            side_effect=wait_for_preview,
                        ):
                            future = pool.submit(
                                session.generate,
                                plan,
                                group,
                                source,
                                text=selected_text,
                                progress=progress.append,
                            )
                            self.assertTrue(joined.wait(5))
                            self.assertEqual(progress[-1], "Loading model weights...")
                            self.assertEqual(
                                session.preparation_progress(
                                    plan, group, source, text=selected_text
                                ),
                                "Loading model weights...",
                            )
                            release.set()
                            preview = future.result(timeout=5)
                    finally:
                        release.set()
                    self.wait(owner)
                    start.assert_called_once()
                self.assertIn("Model weights loaded.", progress)
                self.assertEqual(
                    preview.text, group.sample_text if same_key else selected_text
                )
                self.assertEqual(
                    [request.text for request in backend.requests],
                    [group.sample_text]
                    if same_key
                    else [selected_text, group.sample_text],
                )
                self.assertIsNone(session.preparation_progress(plan, group, source))
                self.assertIsNone(session.preparation_error(plan, group, source))

    def test_closing_background_owner_preserves_startup_for_waiting_preview(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            background, foreground = owner.session(), owner.session()
            source = group.candidates[0].source_id
            entered, release, joined = Event(), Event(), Event()

            def factory(*_args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                self.assertFalse(kwargs["startup_cancellation"].is_set())
                return backend

            def wait_for_preview(job, cancel_event):
                joined.set()
                return _wait_for_preview(job, cancel_event)

            with (
                patch.object(
                    owner.service, "backend_factory", side_effect=factory
                ) as start,
                ThreadPoolExecutor() as pool,
            ):
                try:
                    background.prefetch(plan, ((group, source, None),), automatic=True)
                    self.assertTrue(entered.wait(5))
                    with patch(
                        "vntts.pregeneration_audition_queue._wait_for_preview",
                        side_effect=wait_for_preview,
                    ):
                        future = pool.submit(
                            foreground.generate,
                            plan,
                            group,
                            source,
                            text=group.alternate_sample_text,
                        )
                        self.assertTrue(joined.wait(5))
                        background.close()
                        release.set()
                        self.assertEqual(
                            future.result(timeout=5).text, group.alternate_sample_text
                        )
                finally:
                    release.set()
                self.wait(owner)
                start.assert_called_once()
            self.assertEqual(
                [request.text for request in backend.requests],
                [group.alternate_sample_text],
            )
            self.assertIsNone(owner.service.lookup(plan, group, source))

    def test_cancelling_waiting_preview_keeps_original_preparation(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            entered, release, joined, cancel = Event(), Event(), Event(), Event()
            progress = []

            def factory(*_args, **kwargs):
                kwargs["startup_progress"]("Loading model weights...")
                entered.set()
                self.assertTrue(release.wait(5))
                self.assertFalse(kwargs["startup_cancellation"].is_set())
                kwargs["startup_progress"]("Model weights loaded.")
                return backend

            def wait_for_preview(job, cancel_event):
                joined.set()
                return _wait_for_preview(job, cancel_event)

            with (
                patch.object(
                    owner.service, "backend_factory", side_effect=factory
                ) as start,
                ThreadPoolExecutor() as pool,
            ):
                try:
                    session.prefetch(plan, ((group, source, None),), automatic=True)
                    self.assertTrue(entered.wait(5))
                    with patch(
                        "vntts.pregeneration_audition_queue._wait_for_preview",
                        side_effect=wait_for_preview,
                    ):
                        future = pool.submit(
                            session.generate,
                            plan,
                            group,
                            source,
                            text=group.alternate_sample_text,
                            cancel_event=cancel,
                            progress=progress.append,
                        )
                        self.assertTrue(joined.wait(5))
                        cancel.set()
                        with self.assertRaises(VoiceAuditionCancelled):
                            future.result(timeout=2)
                        cancelled_progress = list(progress)
                        release.set()
                finally:
                    release.set()
                self.wait(owner)
                start.assert_called_once()
            self.assertEqual(progress, cancelled_progress)
            self.assertEqual(
                [request.text for request in backend.requests], [group.sample_text]
            )
            self.assertIsNotNone(session.peek(plan, group, source))

    def test_waiting_preview_receives_shared_fatal_startup_failure_without_restart(
        self,
    ):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            entered, release, joined = Event(), Event(), Event()

            def factory(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                raise FatalSpeechWorkerError("CUDA device-side assert triggered")

            def wait_for_preview(job, cancel_event):
                joined.set()
                return _wait_for_preview(job, cancel_event)

            with (
                patch.object(
                    owner.service, "backend_factory", side_effect=factory
                ) as start,
                ThreadPoolExecutor() as pool,
            ):
                try:
                    session.prefetch(plan, ((group, source, None),), automatic=True)
                    self.assertTrue(entered.wait(5))
                    with patch(
                        "vntts.pregeneration_audition_queue._wait_for_preview",
                        side_effect=wait_for_preview,
                    ):
                        future = pool.submit(
                            session.generate,
                            plan,
                            group,
                            source,
                            text=group.alternate_sample_text,
                        )
                        self.assertTrue(joined.wait(5))
                        release.set()
                        with self.assertRaisesRegex(
                            VoiceAuditionError, "CUDA device-side assert"
                        ):
                            future.result(timeout=5)
                finally:
                    release.set()
                self.wait(owner)
                start.assert_called_once()
            self.assertFalse(session.can_prepare_automatically())
            self.assertEqual(backend.requests, [])
            session.generate(plan, group, source, text=group.alternate_sample_text)
            self.assertTrue(session.can_prepare_automatically())

    def test_queued_automatic_preparation_stops_for_reading_or_suspension(self):
        for suspend in (False, True):
            with self.subTest(suspend=suspend), TemporaryDirectory() as directory:
                plan, group, _, backend, owner = self.fixture(Path(directory))
                session = owner.session()
                entered, release, queued = Event(), Event(), Event()

                def block():
                    entered.set()
                    self.assertTrue(release.wait(5))

                submit = owner._submit

                def submitted(job):
                    submit(job)
                    queued.set()

                owner._pool.submit(block)
                with ThreadPoolExecutor() as pool:
                    with patch.object(owner, "_submit", side_effect=submitted):
                        future = pool.submit(
                            session.generate,
                            plan,
                            group,
                            group.candidates[0].source_id,
                            automatic=True,
                        )
                        try:
                            self.assertTrue(entered.wait(5))
                            self.assertTrue(queued.wait(5))
                            if suspend:
                                owner.suspend()
                            else:
                                owner.background_available = lambda: False
                        finally:
                            release.set()
                        with self.assertRaises(VoiceAuditionCancelled):
                            future.result(timeout=5)
                self.wait(owner)
                self.assertEqual(backend.requests, [])

    def test_automatic_preparation_never_clears_suspension_but_reuses_cache(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            first = session.generate(plan, group, group.candidates[0].source_id)
            owner.suspend()
            cached = session.generate(
                plan, group, group.candidates[0].source_id, automatic=True
            )
            self.assertEqual(cached.path, first.path)
            with self.assertRaises(VoiceAuditionCancelled):
                session.generate(
                    plan,
                    group,
                    group.candidates[0].source_id,
                    text=group.alternate_sample_text,
                    automatic=True,
                )
            self.assertEqual(len(backend.requests), 1)

    def test_automatic_or_manual_request_promotes_queued_cold_prefetch(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic), TemporaryDirectory() as directory:
                plan, group, _, backend, owner = self.fixture(Path(directory))
                session = owner.session()
                entered, release, joined = Event(), Event(), Event()

                def block():
                    entered.set()
                    self.assertTrue(release.wait(5))

                def wait_for_preview(job, cancel_event):
                    joined.set()
                    return _wait_for_preview(job, cancel_event)

                owner._pool.submit(block)
                session.prefetch(plan, ((group, group.candidates[0].source_id, None),))
                with ThreadPoolExecutor() as pool:
                    with patch(
                        "vntts.pregeneration_audition_queue._wait_for_preview",
                        side_effect=wait_for_preview,
                    ):
                        future = pool.submit(
                            session.generate,
                            plan,
                            group,
                            group.candidates[0].source_id,
                            automatic=automatic,
                        )
                        try:
                            self.assertTrue(entered.wait(5))
                            self.assertTrue(joined.wait(5))
                        finally:
                            release.set()
                        preview = future.result(timeout=5)
                self.assertEqual(preview.text, group.sample_text)
                self.assertEqual(
                    preview.candidate_source_id, group.candidates[0].source_id
                )
                self.assertEqual(
                    session.peek(plan, group, group.candidates[0].source_id), preview
                )
                self.assertEqual(len(backend.requests), 1)

    def test_editor_prefetch_starts_cold_with_bounded_prioritized_states(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            entered, release = Event(), Event()
            backend.on_render = lambda: (entered.set(), release.wait(5))
            source = group.candidates[0].source_id
            groups = tuple(
                replace(group, group_id=str(index) * 64, sample_text=text)
                for index, text in enumerate(
                    ("First.", "Second.", "Third.", "Fourth."), 1
                )
            )
            plan = replace(plan, groups=groups)
            requests = tuple((value, source, None) for value in groups)
            try:
                session.prefetch(plan, requests, automatic=True)
                self.assertTrue(entered.wait(5))
                self.assertEqual(
                    session.preparation_state(plan, groups[0], source),
                    "preparing",
                )
                self.assertEqual(
                    session.preparation_state(plan, groups[1], source),
                    "queued",
                )
                self.assertEqual(
                    session.preparation_state(plan, groups[3], source),
                    "not-prepared",
                )
                session.prefetch(
                    plan, (requests[2], requests[1], requests[3]), automatic=True
                )
            finally:
                release.set()
            self.wait(owner)
            self.assertEqual(
                [request.text for request in backend.requests],
                ["First.", "Third.", "Second.", "Fourth."],
            )
            self.assertIsNotNone(session.peek(plan, groups[0], source))
            self.assertEqual(
                session.preparation_state(plan, groups[3], source), "ready"
            )

    def test_changing_prefetch_window_preserves_running_preview_for_foreground_reuse(
        self,
    ):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            entered, release, joined = Event(), Event(), Event()

            def block_first_render():
                entered.set()
                self.assertTrue(release.wait(5))

            backend.on_render = block_first_render
            first = ((group, source, None),)
            other = ((group, source, group.alternate_sample_text),)

            def wait_for_preview(job, cancel_event):
                joined.set()
                return _wait_for_preview(job, cancel_event)

            with ThreadPoolExecutor() as pool:
                try:
                    session.prefetch(plan, first, automatic=True)
                    self.assertTrue(entered.wait(5))
                    session.prefetch(plan, other, automatic=True)
                    session.prefetch(plan, first, automatic=True)
                    self.assertEqual(
                        session.preparation_state(plan, group, source), "preparing"
                    )
                    with patch(
                        "vntts.pregeneration_audition_queue._wait_for_preview",
                        side_effect=wait_for_preview,
                    ):
                        future = pool.submit(session.generate, plan, group, source)
                        self.assertTrue(joined.wait(5))
                        release.set()
                        generated = future.result(timeout=5)
                finally:
                    release.set()
            self.wait(owner)
            self.assertEqual(
                [request.text for request in backend.requests],
                [group.sample_text],
            )
            self.assertEqual(session.preparation_state(plan, group, source), "ready")
            preview = session.peek(plan, group, source)
            self.assertEqual(preview, generated)
            self.assertTrue(preview.path.exists())
            self.assertTrue(session.generate(plan, group, source).reused)
            self.assertEqual(
                session.preparation_state(
                    plan, group, source, text=group.alternate_sample_text
                ),
                "not-prepared",
            )

    def test_leaving_focus_keeps_saved_samples_reusable_after_session_close(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            first = session.generate(plan, group, source)
            session.prefetch(
                plan, ((group, source, group.alternate_sample_text),), automatic=True
            )
            self.wait(owner)
            self.assertEqual(session.peek(plan, group, source), first)
            self.assertEqual(session.preparation_state(plan, group, source), "ready")
            session.close()
            self.assertIsNotNone(owner.service.backend)
            with self.assertRaisesRegex(VoiceAuditionError, "closed"):
                session.generate(plan, group, source)
            reopened = owner.session()
            self.assertTrue(first.path.exists())
            self.assertTrue(reopened.generate(plan, group, source).reused)
            self.assertTrue(
                reopened.generate(
                    plan, group, source, text=group.alternate_sample_text
                ).reused
            )
            self.assertEqual(len(backend.requests), 2)

    def test_foreground_waits_for_running_preparation_then_precedes_pending_background(
        self,
    ):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            entered, release, queued = Event(), Event(), Event()
            pending_text = "Another background phrase."
            progress = []

            def block():
                entered.set()
                self.assertTrue(release.wait(5))

            def wait_for_preview(job, cancel_event):
                queued.set()
                return _wait_for_preview(job, cancel_event)

            backend.on_render = block
            with ThreadPoolExecutor() as pool:
                try:
                    session.prefetch(
                        plan,
                        ((group, source, None), (group, source, pending_text)),
                        automatic=True,
                    )
                    self.assertTrue(entered.wait(5))
                    with patch(
                        "vntts.pregeneration_audition_queue._wait_for_preview",
                        side_effect=wait_for_preview,
                    ):
                        future = pool.submit(
                            session.generate,
                            plan,
                            group,
                            source,
                            text=group.alternate_sample_text,
                            progress=progress.append,
                        )
                        self.assertTrue(queued.wait(5))
                        self.assertEqual(
                            progress,
                            ["Waiting for the current voice preview to finish..."],
                        )
                        self.assertEqual(
                            session.preparation_progress(
                                plan, group, source, text=group.alternate_sample_text
                            ),
                            progress[-1],
                        )
                        release.set()
                        preview = future.result(timeout=5)
                finally:
                    release.set()
            self.wait(owner)
            self.assertEqual(
                [request.text for request in backend.requests],
                [group.sample_text, group.alternate_sample_text, pending_text],
            )
            self.assertEqual(preview.text, group.alternate_sample_text)
            self.assertIsNotNone(session.peek(plan, group, source))

    def test_failed_prediction_is_visible_without_automatic_retry_loop(self):
        with TemporaryDirectory() as directory:
            plan, group, _, _, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            requests = ((group, source, None),)
            with patch.object(
                owner.service, "generate", side_effect=RuntimeError("model unavailable")
            ) as generate:
                for _ in range(3):
                    session.prefetch(plan, requests, automatic=True)
                    self.wait(owner)
                self.assertEqual(
                    session.preparation_state(plan, group, source), "failed"
                )
                self.assertEqual(
                    session.preparation_error(plan, group, source), "model unavailable"
                )
                self.assertIsNone(
                    session.preparation_error(plan, group, source, text="Other phrase")
                )
                generate.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                    session.generate(plan, group, source)
                self.assertEqual(generate.call_count, 2)
            session.generate(plan, group, source)
            self.assertEqual(session.preparation_state(plan, group, source), "ready")
            self.assertIsNone(session.preparation_error(plan, group, source))

    def test_fatal_worker_failure_stops_background_work_until_explicit_retry(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            other_session = owner.session()
            source = group.candidates[0].source_id
            cached = session.generate(plan, group, source)
            failed_text = group.alternate_sample_text
            queued_text = "Another queued preview."
            entered, release = Event(), Event()

            def fail():
                entered.set()
                self.assertTrue(release.wait(5))
                raise FatalSpeechWorkerError("CUDA device-side assert triggered")

            backend.on_render = fail
            try:
                session.prefetch(
                    plan,
                    ((group, source, failed_text), (group, source, queued_text)),
                    automatic=True,
                )
                self.assertTrue(entered.wait(5))
                other_session.prefetch(
                    plan, ((group, source, "Other window."),), automatic=True
                )
            finally:
                release.set()
            self.wait(owner)
            self.assertEqual(len(backend.requests), 2)
            self.assertEqual(
                session.preparation_state(plan, group, source, text=failed_text),
                "failed",
            )
            self.assertIn(
                "CUDA device-side assert",
                session.preparation_error(plan, group, source, text=failed_text),
            )
            self.assertFalse(session.preparing)
            self.assertFalse(other_session.preparing)
            self.assertFalse(session.can_prepare_automatically())
            self.assertEqual(session.peek(plan, group, source), cached)
            session.prefetch(plan, ((group, source, queued_text),), automatic=True)
            with self.assertRaises(VoiceAuditionCancelled):
                other_session.generate(
                    plan, group, source, text=queued_text, automatic=True
                )
            self.wait(owner)
            self.assertEqual(len(backend.requests), 2)
            backend.on_render = None
            retried = session.generate(plan, group, source, text=failed_text)
            self.assertEqual(retried.text, failed_text)
            self.assertTrue(session.can_prepare_automatically())
            self.assertEqual(
                session.preparation_state(plan, group, source, text=failed_text),
                "ready",
            )

    def test_recoverable_failure_does_not_stop_other_background_previews(self):
        with TemporaryDirectory() as directory:
            plan, group, _, _, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            generate = owner.service.generate

            def fail_first(plan, group, source, **kwargs):
                if kwargs["text"] is None:
                    raise VoiceAuditionError("Model unavailable")
                return generate(plan, group, source, **kwargs)

            with patch.object(owner.service, "generate", side_effect=fail_first):
                session.prefetch(
                    plan,
                    (
                        (group, source, None),
                        (group, source, group.alternate_sample_text),
                    ),
                    automatic=True,
                )
                self.wait(owner)
            self.assertTrue(session.can_prepare_automatically())
            self.assertIsNotNone(
                session.peek(plan, group, source, text=group.alternate_sample_text)
            )

    def test_restore_cached_inventory_admits_audio_without_starting_model(self):
        with TemporaryDirectory() as directory:
            plan, group, _, _, owner = self.fixture(Path(directory))
            source = group.candidates[0].source_id
            original = owner.session().generate(plan, group, source)
            root = owner.service.root
            owner.close()
            factory = Mock(
                side_effect=AssertionError("cached inventory must not start a model")
            )
            restored_owner = VoiceAuditionCoordinator(
                VoiceAuditionPreviewService(root, backend_factory=factory)
            )
            self.addCleanup(restored_owner.close)
            session = restored_owner.session()
            session.restore_cached(
                plan, ((group, source, None), (group, source, "Missing."))
            )
            self.assertEqual(session.preparation_state(plan, group, source), "ready")
            self.assertEqual(
                session.peek(plan, group, source).identity, original.identity
            )
            self.assertEqual(
                session.preparation_state(plan, group, source, text="Missing."),
                "not-prepared",
            )
            self.assertIsNone(restored_owner.service.backend)
            factory.assert_not_called()
            cancel = Event()
            session.close()
            session = restored_owner.session()
            lookup = restored_owner.service.lookup

            def cancelled(*args, **kwargs):
                preview = lookup(*args, **kwargs)
                cancel.set()
                return preview

            with patch.object(restored_owner.service, "lookup", side_effect=cancelled):
                with self.assertRaises(VoiceAuditionCancelled):
                    session.restore_cached(
                        plan, ((group, source, None),), cancel_event=cancel
                    )
            self.assertIsNone(session.peek(plan, group, source))

    def test_prefetch_never_starts_a_cold_model(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            session.prefetch(plan, ((group, group.candidates[0].source_id, None),))
            self.wait(owner)
            self.assertEqual(backend.requests, [])
            self.assertIsNone(owner.service.backend)
            owner.close()

    def test_cached_playback_does_not_wait_for_another_render(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            first = session.generate(plan, group, group.candidates[0].source_id)
            entered, release = Event(), Event()

            def block():
                entered.set()
                self.assertTrue(release.wait(5))

            backend.on_render = block
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(
                    session.generate,
                    plan,
                    group,
                    group.candidates[0].source_id,
                    text=group.alternate_sample_text,
                )
                try:
                    self.assertTrue(entered.wait(5))
                    cached = pool.submit(
                        session.generate, plan, group, group.candidates[0].source_id
                    ).result(timeout=2)
                    self.assertEqual(cached.path, first.path)
                    self.assertTrue(cached.reused)
                finally:
                    release.set()
                future.result(timeout=5)
            owner.close()

    def test_ready_admission_is_invalidated_by_reference_or_manifest_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, manifest, _, owner = self.fixture(root)
            session = owner.session()
            session.generate(plan, group, group.candidates[0].source_id)
            self.assertIsNotNone(
                session.peek(plan, group, group.candidates[0].source_id)
            )
            reference = manifest.parent / "references" / "rhiannon.wav"
            reference.write_bytes(clean_wav_bytes(amplitude=0.2))
            self.assertIsNone(session.peek(plan, group, group.candidates[0].source_id))
            with self.assertRaises(VoiceAuditionError):
                session.generate(plan, group, group.candidates[0].source_id)
            owner.close()

    def test_display_and_decision_metadata_do_not_invalidate_synthesis(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            first = session.generate(plan, group, group.candidates[0].source_id)
            candidates = tuple(
                replace(
                    candidate,
                    recommendation="Another recommendation",
                    source_titles=("Another label",),
                )
                for candidate in group.candidates
            )
            updated = replace(
                group,
                group_id="a" * 64,
                decision_context_sha256="b" * 64,
                candidates=candidates,
                candidate_inventory=candidates,
            )
            changed_plan = replace(
                plan, groups=(updated,), synthesis_controls_sha256="c" * 64
            )
            second = session.generate(changed_plan, updated, candidates[0].source_id)
            self.assertEqual(first.identity, second.identity)
            self.assertEqual(second.group_id, updated.group_id)
            self.assertEqual(len(backend.requests), 1)
            owner.close()

    def test_metadata_change_readmits_ready_once_and_rejects_late_input_changes(self):
        for mutate in (False, True):
            with self.subTest(mutate=mutate), TemporaryDirectory() as directory:
                plan, group, manifest, backend, owner = self.fixture(Path(directory))
                session = owner.session()
                first = session.generate(plan, group, group.candidates[0].source_id)
                second = session.generate(
                    plan,
                    group,
                    group.candidates[0].source_id,
                    text=group.alternate_sample_text,
                )
                self.assertTrue(first.path.samefile(second.path))
                metadata = first.path.stat()
                # Hard-link creation changes ctime only on some filesystems.
                os.utime(
                    first.path,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
                )
                lookup = owner.service.lookup

                def recheck(*args, **kwargs):
                    result = lookup(*args, **kwargs)
                    if mutate:
                        manifest.write_bytes(manifest.read_bytes() + b"\n")
                    return result

                with (
                    patch.object(
                        owner.service, "lookup", side_effect=recheck
                    ) as readmit,
                    patch.object(
                        owner.service,
                        "_ensure_preview_backend",
                        side_effect=AssertionError("ready recheck acquired model"),
                    ),
                ):
                    source = group.candidates[0].source_id
                    self.assertIsNone(
                        session.peek(plan, group, source, revalidate=False)
                    )
                    readmit.assert_not_called()
                    for _ in range(2):
                        self.assertEqual(
                            session.peek(plan, group, source),
                            None if mutate else first,
                        )
                    readmit.assert_called_once()
                self.assertEqual(len(backend.requests), 2)

    def test_ready_audio_or_sidecar_mutation_invalidates_memory_preview(self):
        for suffix in (".wav", ".json"):
            with self.subTest(suffix=suffix), TemporaryDirectory() as directory:
                plan, group, _, _, owner = self.fixture(Path(directory))
                session = owner.session()
                preview = session.generate(plan, group, group.candidates[0].source_id)
                preview.path.with_suffix(suffix).write_bytes(b"changed")
                self.assertIsNone(
                    session.peek(plan, group, group.candidates[0].source_id)
                )
                owner.close()

    def test_language_and_transcript_change_the_preview(self):
        with TemporaryDirectory() as directory:
            plan, group, _, _, owner = self.fixture(Path(directory))
            session = owner.session()
            first = session.generate(plan, group, group.candidates[0].source_id)
            changed = replace(plan, synthesis_language="French")
            self.assertIsNone(
                session.peek(changed, group, group.candidates[0].source_id)
            )
            second = session.generate(changed, group, group.candidates[0].source_id)
            self.assertNotEqual(first.identity, second.identity)
            candidate = replace(
                group.candidates[0], reference_transcript="The original recording."
            )
            updated = replace(
                group, candidates=(candidate,), candidate_inventory=(candidate,)
            )
            third = session.generate(
                replace(plan, groups=(updated,)), updated, candidate.source_id
            )
            self.assertNotEqual(first.identity, third.identity)
            owner.close()

    def test_suspend_cancels_only_speculation_and_closed_session_leaves_other_session_alive(
        self,
    ):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            session.generate(plan, group, group.candidates[0].source_id)
            owner.suspend()
            self.assertIsNone(session.peek(plan, group, group.candidates[0].source_id))
            session.prefetch(
                plan,
                ((group, group.candidates[0].source_id, group.alternate_sample_text),),
            )
            self.wait(owner)
            self.assertEqual(len(backend.requests), 1)
            session.close()
            other = owner.session()
            self.assertTrue(
                other.generate(plan, group, group.candidates[0].source_id).reused
            )
            owner.close()

    def test_cancelling_a_waiter_keeps_the_running_preview_reusable(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            entered, release, cancel = Event(), Event(), Event()

            def block():
                entered.set()
                self.assertTrue(release.wait(5))

            backend.on_render = block
            with ThreadPoolExecutor() as pool:
                future = pool.submit(
                    session.generate,
                    plan,
                    group,
                    group.candidates[0].source_id,
                    cancel_event=cancel,
                )
                try:
                    self.assertTrue(entered.wait(5))
                    cancel.set()
                    with self.assertRaises(VoiceAuditionCancelled):
                        future.result(timeout=2)
                finally:
                    release.set()
            self.wait(owner)
            self.assertIsNotNone(
                session.peek(plan, group, group.candidates[0].source_id)
            )
            self.assertTrue(
                session.generate(plan, group, group.candidates[0].source_id).reused
            )
            self.assertEqual(len(backend.requests), 1)
            owner.close()

    def test_explicit_cancellation_still_discards_running_background_preparation(self):
        for action in ("cancel", "close", "suspend"):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                plan, group, _, backend, owner = self.fixture(Path(directory))
                session = owner.session()
                source = group.candidates[0].source_id
                entered, release = Event(), Event()

                def block():
                    entered.set()
                    self.assertTrue(release.wait(5))

                backend.on_render = block
                try:
                    session.prefetch(plan, ((group, source, None),), automatic=True)
                    self.assertTrue(entered.wait(5))
                    getattr(owner if action == "suspend" else session, action)()
                finally:
                    release.set()
                self.wait(owner)
                self.assertIsNone(session.peek(plan, group, source))
                self.assertIsNone(owner.service.lookup(plan, group, source))

    def test_cancelling_pending_preview_only_stops_work_without_another_waiter(self):
        for waiter_count in (1, 2):
            with self.subTest(waiters=waiter_count), TemporaryDirectory() as directory:
                plan, group, _, backend, owner = self.fixture(Path(directory))
                session = owner.session()
                source = group.candidates[0].source_id
                release = Event()
                cancellations = [Event() for _ in range(waiter_count)]
                joined = [Event() for _ in range(waiter_count)]

                def wait_for_preview(job, cancel_event):
                    joined[cancellations.index(cancel_event)].set()
                    return _wait_for_preview(job, cancel_event)

                owner._pool.submit(lambda: release.wait(5))
                with ThreadPoolExecutor() as pool:
                    try:
                        with patch(
                            "vntts.pregeneration_audition_queue._wait_for_preview",
                            side_effect=wait_for_preview,
                        ):
                            futures = [
                                pool.submit(
                                    session.generate,
                                    plan,
                                    group,
                                    source,
                                    cancel_event=cancel,
                                )
                                for cancel in cancellations
                            ]
                            for event in joined:
                                self.assertTrue(event.wait(5))
                            cancellations[0].set()
                            with self.assertRaises(VoiceAuditionCancelled):
                                futures[0].result(timeout=2)
                            release.set()
                            if waiter_count == 2:
                                futures[1].result(timeout=5)
                    finally:
                        release.set()
                self.wait(owner)
                self.assertEqual(len(backend.requests), waiter_count - 1)

    def test_cached_input_mutation_is_rejected_before_playback(self):
        with TemporaryDirectory() as directory:
            plan, group, manifest, _, owner = self.fixture(Path(directory))
            session = owner.session()
            session.generate(plan, group, group.candidates[0].source_id)
            lookup = owner.service.lookup

            def changed(*args, **kwargs):
                result = lookup(*args, **kwargs)
                manifest.write_bytes(manifest.read_bytes() + b"\n")
                return result

            with patch.object(owner.service, "lookup", side_effect=changed):
                with self.assertRaisesRegex(VoiceAuditionError, "inputs changed"):
                    session.generate(plan, group, group.candidates[0].source_id)
            owner.close()

    def test_completed_work_keeps_generated_previews_and_identity_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan, group, _, backend, owner = self.fixture(root)
            session = owner.session()
            first = session.generate(plan, group, group.candidates[0].source_id)
            second = session.generate(
                plan,
                group,
                group.candidates[0].source_id,
                text=group.alternate_sample_text,
            )
            sidecars = {
                preview.path.with_suffix(".json"): preview.path.with_suffix(
                    ".json"
                ).read_bytes()
                for preview in (first, second)
            }
            session.close()
            owner.close()
            self.assertTrue(first.path.samefile(second.path))
            for sidecar, payload in sidecars.items():
                self.assertEqual(sidecar.read_bytes(), payload)
            restored = VoiceAuditionCoordinator(
                VoiceAuditionPreviewService(
                    owner.service.root,
                    backend_factory=Mock(
                        side_effect=AssertionError("regenerated saved audio")
                    ),
                )
            )
            self.addCleanup(restored.close)
            for text in (None, group.alternate_sample_text):
                self.assertTrue(
                    restored.session()
                    .generate(plan, group, group.candidates[0].source_id, text=text)
                    .reused
                )
            self.assertEqual(len(backend.requests), 2)
