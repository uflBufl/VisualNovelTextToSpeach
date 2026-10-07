import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vntts.authoring.listening as listening_module
from tests.listening_fixtures import write_model_reports
from tests.test_authoring_listening_import import write_listening_fixture
from vntts.authoring.listening import (
    aggregate_listening_report,
    create_listening_session_from_reports,
    ensure_listening_report,
    load_listening_session,
    record_trial_preference,
)
from vntts.authoring.listening_import import import_listening_session


class ListeningOperationCountTest(unittest.TestCase):
    @staticmethod
    def _key_captures(capture):
        return sum(
            Path(call.args[0]).name == ".blind-key.json"
            for call in capture.call_args_list
        )

    def _run_with_key_budget(self, action, maximum):
        with patch.object(
            listening_module,
            "capture_authority_file",
            wraps=listening_module.capture_authority_file,
        ) as capture:
            result = action()
        captures = self._key_captures(capture)
        self.assertGreater(captures, 0)
        self.assertLessEqual(captures, maximum)
        return result

    def _current_session(self, root):
        return create_listening_session_from_reports(
            write_model_reports(root, item_count=1), root / "current-session", seed=5
        )

    def _legacy_session(self, root):
        return (
            import_listening_session(
                write_listening_fixture(root), root / "legacy-app-data"
            ).destination
            / "session.json"
        )

    def test_public_operations_bound_key_capture_counts_for_current_and_legacy(self):
        for kind in ("current", "legacy"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                root = Path(directory)
                session_path = (
                    self._current_session(root)
                    if kind == "current"
                    else self._legacy_session(root)
                )
                report_path = session_path.with_name("report.json")

                loaded = self._run_with_key_budget(
                    lambda: load_listening_session(session_path), 1
                )
                self.assertTrue(loaded["trials"])

                report = self._run_with_key_budget(
                    lambda: aggregate_listening_report(session_path, report_path), 1
                )
                self.assertTrue(report_path.is_file())

                cached = self._run_with_key_budget(
                    lambda: ensure_listening_report(session_path, report_path), 1
                )
                self.assertEqual(cached["session"], report["session"])

                report_path.unlink()
                rebuilt = self._run_with_key_budget(
                    lambda: ensure_listening_report(session_path, report_path), 2
                )
                self.assertTrue(report_path.is_file())
                self.assertEqual(rebuilt["session"], str(session_path.resolve()))

                session_document = json.loads(session_path.read_text(encoding="utf-8"))
                session_document["trials"][0]["rating"] = None
                session_document["completed_count"] = 0
                session_path.write_text(
                    json.dumps(session_document, sort_keys=True), encoding="utf-8"
                )
                report_path.unlink()
                trial_id = session_document["trials"][0]["trial_id"]
                saved = self._run_with_key_budget(
                    lambda: record_trial_preference(
                        session_path,
                        trial_id,
                        "tie",
                        report_path=report_path,
                    ),
                    2,
                )
                self.assertEqual(saved["trials"][0]["rating"]["preference"], "tie")
                self.assertTrue(report_path.is_file())


if __name__ == "__main__":
    unittest.main()
