import json
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.verify_windows_release_matrix import main


class VerifyWindowsReleaseMatrixTest(unittest.TestCase):
    def test_bad_matrix_inputs_return_diagnostics_without_tracebacks(self):
        for contents in (
            None,
            "{",
            json.dumps({"version": 1, "required_profiles": [{}]}),
        ):
            with self.subTest(contents=contents), TemporaryDirectory() as directory:
                path = Path(directory) / "matrix.json"
                if contents is not None:
                    path.write_text(contents, encoding="utf-8")
                stderr = StringIO()
                with redirect_stderr(stderr):
                    result = main(
                        ["--matrix", str(path), "--evidence-directory", directory]
                    )
                self.assertEqual(result, 1)
                self.assertTrue(stderr.getvalue().strip())
                self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
