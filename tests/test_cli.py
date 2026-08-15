import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from vntts.cli import (
    CLIReportResult,
    cli_error,
    cli_exit_code,
    cli_message,
    cli_messages,
    cli_success,
)


class CLITest(unittest.TestCase):
    def test_messages_use_requested_stream_and_exit_code(self):
        output = StringIO()
        errors = StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli_message("progress", exit_code=7), 7)
            self.assertEqual(cli_success("done"), 0)
            self.assertEqual(cli_error("failed"), 1)
            self.assertEqual(cli_messages(("first", "second")), 0)

        self.assertEqual(output.getvalue(), "progress\ndone\nfirst\nsecond\n")
        self.assertEqual(errors.getvalue(), "failed\n")

    def test_report_result_is_named_unpackable_and_maps_to_exit_code(self):
        report_path = Path("report.json")
        result = CLIReportResult(False, report_path)

        successful, unpacked_path = result

        self.assertFalse(successful)
        self.assertEqual(result, (False, report_path))
        self.assertEqual(unpacked_path, report_path)
        self.assertEqual(result.report_path, report_path)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(cli_exit_code(True), 0)
        self.assertEqual(cli_exit_code(False), 1)


if __name__ == "__main__":
    unittest.main()
