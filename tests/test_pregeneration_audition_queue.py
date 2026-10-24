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
            self.assertTrue(session.can_prepare_automatically())
            self.assertEqual(backend.requests, [])
            preview = session.generate(
                plan, group, group.candidates[0].source_id, automatic=True
            )
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(
                session.peek(plan, group, group.candidates[0].source_id), preview
            )

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
                self.assertEqual(owner._suspended, suspend)

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
            self.assertTrue(owner._suspended)
            with self.assertRaises(VoiceAuditionCancelled):
                session.generate(
                    plan,
                    group,
                    group.candidates[0].source_id,
                    text=group.alternate_sample_text,
                    automatic=True,
                )
            self.assertTrue(owner._suspended)
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
                    self.assertEqual(job.foreground, not automatic)
                    self.assertEqual(job.automatic, automatic)
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
                        future.result(timeout=5)
                self.assertEqual(len(backend.requests), 1)

    def test_automatic_preparation_waits_while_reading_and_after_suspension(self):
        with TemporaryDirectory() as directory:
            _, _, _, _, owner = self.fixture(Path(directory))
            session = owner.session()
            owner.background_available = lambda: False
            self.assertFalse(session.can_prepare_automatically())
            owner.background_available = lambda: True
            self.assertTrue(session.can_prepare_automatically())
            owner.suspend()
            self.assertFalse(session.can_prepare_automatically())

    def test_automatic_preparation_rejects_a_closed_session_or_owner(self):
        for close_owner in (False, True):
            with (
                self.subTest(close_owner=close_owner),
                TemporaryDirectory() as directory,
            ):
                _, _, _, _, owner = self.fixture(Path(directory))
                session = owner.session()
                if close_owner:
                    owner.close()
                else:
                    session.close()
                self.assertFalse(session.can_prepare_automatically())

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
            self.assertIsNone(session.peek(plan, groups[0], source))
            self.assertEqual(
                session.preparation_state(plan, groups[3], source), "ready"
            )

    def test_returning_to_cancelled_running_preview_enqueues_a_replacement(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            source = group.candidates[0].source_id
            entered, release = Event(), Event()

            def block_first_render():
                entered.set()
                self.assertTrue(release.wait(5))

            backend.on_render = block_first_render
            first = ((group, source, None),)
            other = ((group, source, group.alternate_sample_text),)
            try:
                session.prefetch(plan, first, automatic=True)
                self.assertTrue(entered.wait(5))
                cancelled = owner._running
                session.prefetch(plan, other, automatic=True)
                session.prefetch(plan, first, automatic=True)
                self.assertTrue(cancelled.cancel.is_set())
                with owner._lock:
                    self.assertEqual(len(owner._pending), 1)
                    self.assertIsNot(owner._pending[0], cancelled)
                self.assertEqual(
                    session.preparation_state(plan, group, source), "queued"
                )
            finally:
                release.set()
            self.wait(owner)
            self.assertEqual(
                [request.text for request in backend.requests],
                [group.sample_text, group.sample_text],
            )
            self.assertEqual(session.preparation_state(plan, group, source), "ready")
            preview = session.peek(plan, group, source)
            self.assertIsNotNone(preview)
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
            reopened = owner.session()
            self.assertTrue(first.path.exists())
            self.assertTrue(reopened.generate(plan, group, source).reused)
            self.assertTrue(
                reopened.generate(
                    plan, group, source, text=group.alternate_sample_text
                ).reused
            )
            self.assertEqual(len(backend.requests), 2)

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

    def test_foreground_and_prefetch_share_one_render_and_ready_is_not_a_decision(self):
        with TemporaryDirectory() as directory:
            plan, group, _, backend, owner = self.fixture(Path(directory))
            session = owner.session()
            preview = session.generate(plan, group, group.candidates[0].source_id)
            session.prefetch(plan, ((group, group.candidates[0].source_id, None),))
            self.wait(owner)
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(
                session.peek(plan, group, group.candidates[0].source_id), preview
            )
            session.close()
            self.assertIsNotNone(owner.service.backend)
            with self.assertRaisesRegex(VoiceAuditionError, "closed"):
                session.generate(plan, group, group.candidates[0].source_id)
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

    def test_shared_link_rechecks_ready_once_and_rejects_late_input_changes(self):
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
                    for _ in range(2):
                        self.assertEqual(
                            session.peek(plan, group, group.candidates[0].source_id),
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

    def test_cancellation_does_not_publish_a_ready_preview(self):
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
            self.assertIsNone(session.peek(plan, group, group.candidates[0].source_id))
            owner.close()

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
