import unittest
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from vntts.moss_runtime import RetainedMossRuntime, _is_moss_backend, _LockedStream
from vntts.speech_worker import IsolatedSpeechBackend
from vntts.synthesis import SynthesisChunkStream, SynthesisRequest
from vntts.voices import CharacterVoiceRegistry


class _Backend:
    def __init__(self, registry, **options):
        self.registry = registry
        self.narrator_reference = options.get("narrator_reference")
        self.startup_cancellation = options.get("startup_cancellation")
        self.startup_progress = options.get("startup_progress")
        self.options = options
        self.runtime_status = "ready"
        self.stops = 0
        self.shutdowns = 0
        self.loads = 0
        self.play_started = Event()
        self.release_playback = Event()

    def render(self, _request):
        registry = self.registry

        def chunks():
            if False:
                yield None
            return registry

        return SynthesisChunkStream(chunks())

    def load(self):
        self.loads += 1
        self.runtime_status = "ready"

    def stop(self):
        self.stops += 1
        return False

    def set_live_mode_active(self, active):
        return bool(active)

    def shutdown(self):
        self.shutdowns += 1
        self.runtime_status = None

    def set_volume(self, _volume):
        pass

    def set_generation_profile(self, _profile):
        pass

    def play_prepared(self, prepared, *, playback_guard=None):
        self.play_started.set()
        self.release_playback.wait(1)
        return prepared


class RetainedMossRuntimeTests(unittest.TestCase):
    @patch("vntts.moss_cpp_backend.moss_cpp_requested", return_value=False)
    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=False)
    def test_mlx_backend_bypasses_the_native_runtime_contract(self, *_):
        for entrypoint in ("backend_for", "__call__"):
            with self.subTest(entrypoint=entrypoint):
                runtime = RetainedMossRuntime()
                registry = CharacterVoiceRegistry()
                backend = getattr(runtime, entrypoint)(
                    registry,
                    model_name="shraey/MOSS-TTS-Local-Transformer-v1.5-MLX-int8",
                    defer_startup=True,
                )
                self.addCleanup(backend.shutdown)

                self.assertIsInstance(backend, IsolatedSpeechBackend)
                self.assertIs(backend.registry, registry)
                self.assertFalse(hasattr(backend, "runtime_status"))
                self.assertIsNone(backend.process)
                self.assertIsNone(runtime.backend)

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_native_backend_still_requires_the_native_runtime_contract(self, _):
        runtime = RetainedMossRuntime(
            "/tmp/vntts-moss-runtime-test",
            backend_factory=lambda *_args, **_options: object(),
        )
        with self.assertRaisesRegex(TypeError, "factory returned an invalid backend"):
            runtime.backend_for(CharacterVoiceRegistry(), model_name="model.gguf")

    def test_stream_close_failure_still_releases_the_operation_lock(self):
        finalized = Event()

        def chunks():
            try:
                yield None
            finally:
                finalized.set()
                raise RuntimeError("generator cleanup failed")

        lock = Lock()
        lock.acquire()
        stream = _LockedStream(SynthesisChunkStream(chunks()), lock)
        next(stream)

        with self.assertRaisesRegex(RuntimeError, "generator cleanup failed"):
            stream.close()

        self.assertTrue(finalized.is_set())
        self.assertTrue(lock.acquire(blocking=False))
        stream.close()  # Closing again must not release another owner's lock.
        self.assertTrue(lock.locked())
        lock.release()

    def test_backend_guard_requires_mutable_lease_fields(self):
        for field in (
            "registry",
            "narrator_reference",
            "startup_cancellation",
            "startup_progress",
        ):
            with self.subTest(field=field):
                backend = _Backend("registry")
                delattr(backend, field)
                self.assertFalse(_is_moss_backend(backend))

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_reuses_one_backend_until_model_changes_or_runtime_shuts_down(self, _):
        created = []

        def factory(registry, **options):
            backend = _Backend(registry, **options)
            created.append(backend)
            return backend

        runtime = RetainedMossRuntime(
            "/tmp/vntts-moss-runtime-test", backend_factory=factory
        )
        first = runtime.backend_for("first", model_name="model-a")
        first.shutdown()
        second = runtime.backend_for("second", model_name="model-a")

        self.assertEqual(len(created), 1)
        self.assertEqual(second.render(None).collect(), "second")
        self.assertTrue(runtime.loaded)

        runtime.unload()
        self.assertFalse(runtime.loaded)
        runtime.backend_for("third", model_name="model-a")
        self.assertTrue(runtime.loaded)
        self.assertEqual(created[0].loads, 2)

        runtime.backend_for("fourth", model_name="model-b")
        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].shutdowns, 2)
        runtime.shutdown()
        self.assertEqual(created[1].shutdowns, 1)

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_recreates_backend_when_language_changes(self, _):
        created = []

        def factory(registry, **options):
            backend = _Backend(registry, **options)
            created.append(backend)
            return backend

        runtime = RetainedMossRuntime(
            "/tmp/vntts-moss-runtime-test", backend_factory=factory
        )
        with patch("vntts.moss_runtime.record_native_speech") as record:
            first = runtime.backend_for(
                "first", model_name="model-a", language="English"
            )
            same_language = runtime.backend_for(
                "second", model_name="model-a", language="English"
            )
            self.assertEqual(len(created), 1)
            self.assertEqual(same_language.render(None).collect(), "second")
            record.assert_not_called()
            changed_language = runtime.backend_for(
                "third", model_name="model-a", language="Russian"
            )
            record.assert_called_once_with(
                operation="runtime-replace",
                reason="configuration-change",
                server_pid=None,
            )

        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].shutdowns, 1)
        self.assertEqual(created[0].options["language"], "English")
        self.assertEqual(created[1].options["language"], "Russian")
        self.assertEqual(changed_language.render(None).collect(), "third")
        first.shutdown()
        runtime.shutdown()

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_old_lease_restores_its_model_and_language_after_another_owner(self, _):
        for model, language in (("model-b", "English"), ("model-a", "Russian")):
            with self.subTest(model=model, language=language):
                created = []

                def factory(registry, **options):
                    backend = _Backend(registry, **options)
                    created.append(backend)
                    return backend

                runtime = RetainedMossRuntime(
                    "/tmp/vntts-moss-runtime-test", backend_factory=factory
                )
                self.addCleanup(runtime.shutdown)
                first = runtime.backend_for(
                    "first", model_name="model-a", language="English"
                )
                runtime.backend_for("second", model_name=model, language=language)
                request = SynthesisRequest(voice="Narrator", text="Test.")
                self.assertEqual(first.render(request).collect(), "first")
                self.assertEqual(len(created), 3)
                self.assertEqual(created[-1].options["model_name"], "model-a")
                self.assertEqual(created[-1].options["language"], "English")
                self.assertEqual([value.shutdowns for value in created], [1, 1, 0])
                self.assertEqual(first.render(request).collect(), "first")
                self.assertEqual(len(created), 3)
                runtime.shutdown()
                with self.assertRaisesRegex(RuntimeError, "unloaded"):
                    first.render(request)
                self.assertFalse(runtime._operation_lock.locked())

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_lease_reconfiguration_uses_current_render_startup_cancellation(self, _):
        for cancelled in (None, False, True):
            with self.subTest(cancelled=cancelled):
                seen = []

                def factory(registry, **options):
                    cancellation = options.get("startup_cancellation")
                    seen.append(cancellation)
                    if cancellation is not None and cancellation.is_set():
                        raise RuntimeError("startup cancelled")
                    return _Backend(registry, **options)

                runtime = RetainedMossRuntime(
                    "/tmp/vntts-moss-runtime-test", backend_factory=factory
                )
                self.addCleanup(runtime.shutdown)
                stale = Event()
                first = runtime.backend_for(
                    "first", model_name="model-a", startup_cancellation=stale
                )
                stale.set()
                runtime.backend_for("second", model_name="model-b")
                current = None if cancelled is None else Event()
                if cancelled:
                    current.set()
                request = SynthesisRequest(
                    voice="Narrator", text="Test.", cancellation=current
                )
                if cancelled:
                    with self.assertRaisesRegex(RuntimeError, "startup cancelled"):
                        first.render(request)
                else:
                    self.assertEqual(first.render(request).collect(), "first")
                    self.assertIs(runtime.backend.startup_cancellation, current)
                self.assertIs(seen[-1], current)
                self.assertFalse(runtime._operation_lock.locked())
                if cancelled:
                    self.assertIsNone(runtime.backend)
                    retry = Event()
                    self.assertEqual(
                        first.render(
                            SynthesisRequest(
                                voice="Narrator", text="Test.", cancellation=retry
                            )
                        ).collect(),
                        "first",
                    )
                    self.assertIs(seen[-1], retry)
                    self.assertEqual(runtime.backend.options["model_name"], "model-a")

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_cached_playback_does_not_hold_the_generation_lock(self, _):
        runtime = RetainedMossRuntime(
            "/tmp/vntts-moss-runtime-test", backend_factory=_Backend
        )
        lease = runtime.backend_for("first", model_name="model-a")
        prepared = SimpleNamespace(payload=SimpleNamespace(cached_audio=object()))
        playback = Thread(target=lease.play_prepared, args=(prepared,))
        playback.start()
        self.assertTrue(runtime._backend.play_started.wait(0.5))

        loaded = Event()
        probe = Thread(target=lambda: (lease.load(), loaded.set()))
        probe.start()
        self.assertTrue(loaded.wait(0.5))

        runtime._backend.release_playback.set()
        playback.join(1)
        probe.join(1)
        runtime.shutdown()

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_live_stop_does_not_wait_for_offline_render(self, _):
        runtime = RetainedMossRuntime(
            "/tmp/vntts-moss-runtime-test", backend_factory=_Backend
        )
        lease = runtime.backend_for("first", model_name="model-a")
        stream = lease.render(None)
        finished = Event()
        result = []

        def change_mode():
            result.append(lease.set_live_mode_active(False))
            finished.set()

        probe = Thread(target=change_mode, daemon=True)
        probe.start()
        try:
            self.assertTrue(finished.wait(0.5))
            self.assertEqual(result, [False])
        finally:
            stream.close()
            probe.join(1)
            runtime.shutdown()

    @patch("vntts.moss_runtime.moss_cpp_requested", return_value=True)
    def test_offline_openmoss_installs_and_loads_without_manual_preload(self, _):
        created = []

        def factory(registry, **options):
            backend = _Backend(registry, **options)
            created.append(backend)
            return backend

        runtime = RetainedMossRuntime(
            "/tmp/vntts-moss-runtime-test", backend_factory=factory
        )

        runtime.benchmark_backend(
            "moss-tts",
            "voices",
            None,
            model_name="model-a",
            terms_accepted=False,
            allow_gated_model_access=False,
        )

        self.assertTrue(created[0].options["allow_download"])
        self.assertNotIn("terms_accepted", created[0].options)
        self.assertNotIn("allow_gated_model_access", created[0].options)
        self.assertTrue(runtime.loaded)
        runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
