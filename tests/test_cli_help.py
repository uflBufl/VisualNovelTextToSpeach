import os
import subprocess
import sys
import unittest


class CliHelpSmokeTest(unittest.TestCase):
    def test_gui_entry_points_print_help_without_starting_native_ui(self):
        environment = dict(os.environ)
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")
        for module, program in (
            ("vntts.app", "vntts-app"),
            ("vntts.authoring.workbench_ui", "vntts-authoring-workbench"),
            ("vntts.calibration", "vntts-calibrate"),
            (
                "vntts.authoring.failure_reference_audit_ui",
                "vntts-reference-audit",
            ),
        ):
            with self.subTest(module=module):
                completed = subprocess.run(
                    (sys.executable, "-m", module, "--help"),
                    check=False,
                    capture_output=True,
                    text=True,
                    env=environment,
                    timeout=15,
                )

                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(f"usage: {program}", completed.stdout)
                self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
