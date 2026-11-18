import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.run_changed_unittests import changed_paths, select_test_modules


class ChangedUnittestsTest(unittest.TestCase):
    def test_non_python_changes_do_not_enumerate_or_parse_modules(self):
        for changed in (
            set(),
            {"todo.md", "docs/usage.md"},
            {"uv.lock"},
            {"unknown.fixture"},
        ):
            with (
                self.subTest(changed=changed),
                patch("scripts.run_changed_unittests._module_files") as files,
                patch("scripts.run_changed_unittests._reverse_dependencies") as imports,
            ):
                selected, reason = select_test_modules(changed)
                if changed & {"uv.lock", "unknown.fixture"}:
                    self.assertIsNone(selected)
                    self.assertIsNotNone(reason)
                else:
                    self.assertEqual((selected, reason), ([], None))
                files.assert_not_called()
                imports.assert_not_called()

    def test_changed_python_import_errors_still_require_full_suite(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.py"
            path.write_text("def unfinished(\n", encoding="utf-8")
            selected, reason = select_test_modules(
                {"vntts/bad.py"}, {"vntts.bad": path}
            )
        self.assertIsNone(selected)
        self.assertIn("cannot map Python imports", reason)

    def test_branch_local_changes_and_transitive_imports(self):
        with patch(
            "scripts.run_changed_unittests._git",
            side_effect=(
                b"base-commit\n",
                b"vntts/base.py\0",
                b"tests/test_follow.py\0",
            ),
        ):
            self.assertEqual(
                changed_paths("origin/main"),
                {"vntts/base.py", "tests/test_follow.py"},
            )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {
                "vntts.base": "VALUE = 1\n",
                "vntts.consumer": "from vntts.base import VALUE\n",
                "tests.test_consumer": "from vntts.consumer import VALUE\n",
                "tests.test_follow": "from tests.test_consumer import VALUE\n",
                "vntts.gui": "from vntts.base import VALUE\n",
                "vntts.platform": (
                    "def launch(): return ['python', '-m', 'vntts.gui']\n"
                    "def permissions(): return True\n"
                ),
                "tests.test_launch": "from vntts.platform import launch\n",
                "tests.test_permissions": "from vntts.platform import permissions\n",
                "vntts.metadata": "FIELD = 'vntts.gui'\n",
                "tests.test_metadata": "from vntts.metadata import FIELD\n",
                "vntts.facade": (
                    "from importlib import import_module as load\n"
                    "MODULES = {'screen': 'vntts.gui'}\n"
                    "def __getattr__(name): return load(MODULES[name])\n"
                ),
                "vntts.facade.other": "VALUE = 2\n",
                "tests.test_export": "from vntts.facade import screen\n",
                "tests.test_other": "from vntts.facade.other import VALUE\n",
                "tests.test_child": "COMMAND = ['python', '-m', 'vntts.gui']\n",
                "tests.test_ci": "WORKFLOW = '.github/workflows/example.yml'\n",
            }
            modules = {}
            for module, source in sources.items():
                path = root / f"{module}.py"
                path.write_text(source, encoding="utf-8")
                modules[module] = path

            self.assertEqual(
                select_test_modules({"vntts/base.py"}, modules),
                (
                    [
                        "tests.test_child",
                        "tests.test_consumer",
                        "tests.test_export",
                        "tests.test_follow",
                        "tests.test_launch",
                    ],
                    None,
                ),
            )
            self.assertEqual(
                select_test_modules({"tests/test_consumer.py"}, modules),
                (["tests.test_consumer", "tests.test_follow"], None),
            )
            self.assertIsNone(select_test_modules({"uv.lock"}, modules)[0])
            self.assertEqual(
                select_test_modules({"vntts/gui.py"}, modules),
                (
                    [
                        "tests.test_child",
                        "tests.test_export",
                        "tests.test_launch",
                        "tests.test_metadata",
                    ],
                    None,
                ),
            )
            self.assertEqual(
                select_test_modules({".github/workflows/example.yml"}, modules),
                (["tests.test_ci"], None),
            )


if __name__ == "__main__":
    unittest.main()
