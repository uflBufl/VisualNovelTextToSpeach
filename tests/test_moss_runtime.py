import unittest
from unittest.mock import patch

from vntts.moss_runtime import RetainedMossRuntime
from vntts.synthesis import SynthesisChunkStream


class _Backend:
    def __init__(self, registry, **options):
        self.registry = registry
        self.options = options
        self.runtime_status = "ready"
        self.stops = 0
        self.shutdowns = 0
        self.loads = 0

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

    def shutdown(self):
        self.shutdowns += 1
        self.runtime_status = None

    def set_volume(self, _volume):
        pass

    def set_generation_profile(self, _profile):
        pass


class RetainedMossRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
