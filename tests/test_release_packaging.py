import importlib.util
import runpy
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCY_COLLECTION_PATH = (
    PROJECT_ROOT / "packaging" / "pyinstaller" / "dependency_collection.py"
)
dependency_collection_spec = importlib.util.spec_from_file_location(
    "dependency_collection", DEPENDENCY_COLLECTION_PATH
)
dependency_collection = importlib.util.module_from_spec(dependency_collection_spec)
dependency_collection_spec.loader.exec_module(dependency_collection)


class ReleasePackagingTest(unittest.TestCase):
    def test_bundled_x11_worker_dispatches_before_desktop_imports(self):
        worker = ModuleType("vntts.x11_capture_worker")
        worker.main = Mock(return_value=0)
        with (
            patch.dict(sys.modules, {"vntts.x11_capture_worker": worker}),
            patch.object(
                sys, "argv", ["VisualNovelTextToSpeech", "--x11-capture-worker"]
            ),
        ):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(PROJECT_ROOT / "vntts/app.py"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 0)
        worker.main.assert_called_once_with()

    def test_shared_dependency_collector_preserves_inventory(self):
        def copy_distribution_metadata(distribution):
            if distribution == "torchcodec":
                raise RuntimeError("metadata unavailable")
            return [(f"{distribution}-metadata", "metadata")]

        packages = "r1999extractor TTS coqpit gruut ko_speech_tools trainer rapidocr onnxruntime".split()
        distributions = (
            "coqui-tts coqpit gruut ko-speech-tools torch torchaudio torchcodec "
            "trainer transformers reverse1999-extractor"
        ).split()
        datas, binaries, hidden_imports = [], [], []
        with (
            patch.object(
                dependency_collection,
                "collect_data_files",
                return_value=[("vntts/icons/lucide/play.svg", "vntts/icons/lucide")],
            ) as collect_data_files,
            patch.object(
                dependency_collection,
                "collect_all",
                side_effect=lambda package: (
                    [(f"{package}-data", "data")],
                    [(f"{package}-binary", "binary")],
                    [f"{package}.import"],
                ),
            ) as collect_all,
            patch.object(
                dependency_collection,
                "copy_metadata",
                side_effect=copy_distribution_metadata,
            ) as copy_metadata,
        ):
            dependency_collection.collect_packaged_dependencies(
                datas, binaries, hidden_imports
            )

        collect_data_files.assert_called_once_with(
            "vntts", includes=["icons/lucide/*.svg", "icons/lucide/LICENSE"]
        )
        self.assertEqual(
            collect_all.call_args_list, [call(package) for package in packages]
        )
        self.assertEqual(
            copy_metadata.call_args_list, [call(item) for item in distributions]
        )
        self.assertEqual(
            datas,
            [("vntts/icons/lucide/play.svg", "vntts/icons/lucide")]
            + [(f"{name}-data", "data") for name in packages]
            + [
                (f"{name}-metadata", "metadata")
                for name in distributions
                if name != "torchcodec"
            ],
        )
        self.assertEqual(binaries, [(f"{name}-binary", "binary") for name in packages])
        self.assertEqual(hidden_imports, [f"{name}.import" for name in packages])


if __name__ == "__main__":
    unittest.main()
