import subprocess
import sys
import unittest


class DependencyCompatibilityTest(unittest.TestCase):
    def test_installed_runtime_modules_import_in_a_fresh_process(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib\n"
                "from vntts.package_self_test import required_modules\n"
                "for name in required_modules:\n"
                "    importlib.import_module(name)\n",
            ],
            check=True,
            timeout=120,
        )


if __name__ == "__main__":
    unittest.main()
