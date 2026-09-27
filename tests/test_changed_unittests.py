import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.run_changed_unittests import changed_paths, select_test_modules


class ChangedUnittestsTest(unittest.TestCase):
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
            }
            modules = {}
            for module, source in sources.items():
                path = root / f"{module}.py"
                path.write_text(source, encoding="utf-8")
                modules[module] = path

            self.assertEqual(
                select_test_modules({"vntts/base.py"}, modules),
                (["tests.test_consumer", "tests.test_follow"], None),
            )
            self.assertEqual(
                select_test_modules({"tests/test_consumer.py"}, modules),
                (["tests.test_consumer", "tests.test_follow"], None),
            )
            self.assertIsNone(select_test_modules({"uv.lock"}, modules)[0])


if __name__ == "__main__":
    unittest.main()
