import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch

import vntts.authoring.cli_silence_comparison as silence_cli
from vntts.authoring.cli import main
from vntts.authoring.cli_silence_comparison import (
    SilenceComparisonError,
)


class AuthoringCliSilenceComparisonTest(unittest.TestCase):
    def test_top_level_parser_translates_family_domain_errors(self):
        errors = StringIO()
        with (
            patch.object(
                silence_cli,
                "load_silence_comparison",
                side_effect=SilenceComparisonError("comparison authority changed"),
            ),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["silence-comparison-check", "comparison"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("comparison authority changed", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
