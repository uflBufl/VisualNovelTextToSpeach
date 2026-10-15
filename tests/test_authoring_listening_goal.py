import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.listening_fixtures import write_listening_fixture, write_model_reports
from vntts.authoring.listening import (
    EVALUATION_GOALS,
    ModelListeningError,
    aggregate_listening_report,
    create_listening_session,
    create_listening_session_from_reports,
    ensure_listening_report,
    load_listening_session,
    record_trial_preference,
)
from vntts.authoring.listening_cli import main as listening_main
from vntts.authoring.listening_import import (
    ListeningImportError,
    import_listening_session,
    inspect_listening_session,
)


class ListeningEvaluationGoalTest(unittest.TestCase):
    def _inputs(self, root):
        reports = write_model_reports(root, item_count=1)
        benchmark = root / "benchmark.json"
        benchmark.write_text(
            json.dumps(
                {
                    "schema": "vntts.voice-model-benchmark",
                    "schema_version": 1,
                    "reports": [str(path) for path in reports],
                }
            ),
            encoding="utf-8",
        )
        return reports, benchmark

    def test_both_creation_routes_persist_default_and_explicit_goals(self):
        for route in ("benchmark", "reports"):
            for goal in (None, *EVALUATION_GOALS):
                with (
                    self.subTest(route=route, goal=goal),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    reports, benchmark = self._inputs(root)
                    create, source = (
                        (create_listening_session, benchmark)
                        if route == "benchmark"
                        else (create_listening_session_from_reports, reports)
                    )
                    options = {} if goal is None else {"evaluation_goal": goal}
                    session = create(source, root / "session", **options)
                    self.assertEqual(
                        load_listening_session(session)["evaluation_goal"],
                        goal or "synthesis-quality",
                    )

    def test_both_cli_creation_commands_forward_the_goal(self):
        for command in ("start", "start-reports"):
            for goal in (None, "overall-preference"):
                with (
                    self.subTest(command=command, goal=goal),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    reports, benchmark = self._inputs(root)
                    source = (
                        ["--benchmark", str(benchmark)]
                        if command == "start"
                        else ["--reports", *(str(path) for path in reports)]
                    )
                    output = root / "session"
                    options = [] if goal is None else ["--evaluation-goal", goal]
                    with redirect_stdout(StringIO()):
                        result = listening_main(
                            [command, *source, "--output", str(output), *options]
                        )
                    self.assertEqual(result, 0)
                    self.assertEqual(
                        load_listening_session(output / "session.json")[
                            "evaluation_goal"
                        ],
                        goal or "synthesis-quality",
                    )

    def test_invalid_goal_cannot_create_a_session(self):
        for route in ("benchmark", "reports"):
            for goal in (None, "unknown", "", [], 1):
                with (
                    self.subTest(route=route, goal=goal),
                    TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    reports, benchmark = self._inputs(root)
                    create, source = (
                        (create_listening_session, benchmark)
                        if route == "benchmark"
                        else (create_listening_session_from_reports, reports)
                    )
                    output = root / "session"
                    with self.assertRaisesRegex(ModelListeningError, "evaluation goal"):
                        create(source, output, evaluation_goal=goal)
                    self.assertFalse(output.exists())

    def test_invalid_stored_goal_blocks_loading_rating_and_reporting(self):
        for goal in (None, "unknown", "", [], 1):
            with self.subTest(goal=goal), TemporaryDirectory() as directory:
                root = Path(directory)
                reports, _benchmark = self._inputs(root)
                path = create_listening_session_from_reports(reports, root / "session")
                document = json.loads(path.read_text(encoding="utf-8"))
                trial_id = document["trials"][0]["trial_id"]
                document["evaluation_goal"] = goal
                path.write_text(json.dumps(document), encoding="utf-8")
                before = path.read_bytes()
                report_path = path.with_name("report.json")
                for operation in (
                    lambda: load_listening_session(path),
                    lambda: record_trial_preference(path, trial_id, "a"),
                    lambda: aggregate_listening_report(path, report_path),
                ):
                    with self.assertRaises(ModelListeningError):
                        operation()
                    self.assertEqual(path.read_bytes(), before)
                    self.assertFalse(report_path.exists())

    def test_invalid_legacy_goal_is_rejected_before_preservation_import(self):
        for goal in (None, "unknown", "", [], 1):
            with self.subTest(goal=goal), TemporaryDirectory() as directory:
                root = Path(directory)
                source = write_listening_fixture(root)
                path = source / "session.json"
                document = json.loads(path.read_text(encoding="utf-8"))
                document["evaluation_goal"] = goal
                path.write_text(json.dumps(document), encoding="utf-8")
                output = root / "app-data"
                for operation in (
                    lambda: inspect_listening_session(source),
                    lambda: import_listening_session(source, output),
                ):
                    with self.assertRaisesRegex(
                        ListeningImportError, "evaluation goal"
                    ):
                        operation()
                    self.assertFalse(output.exists())

    def test_saved_rating_and_cached_report_retain_the_session_goal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports, _benchmark = self._inputs(root)
            path = create_listening_session_from_reports(
                reports, root / "session", evaluation_goal="overall-preference"
            )
            session = load_listening_session(path)
            saved = record_trial_preference(
                path,
                session["trials"][0]["trial_id"],
                "a",
                report_path=path.with_name("report.json"),
            )
            self.assertEqual(saved["evaluation_goal"], "overall-preference")
            self.assertEqual(
                ensure_listening_report(path)["evaluation_goal"], "overall-preference"
            )
            document = json.loads(path.read_text(encoding="utf-8"))
            for goal in ("synthesis-quality", None):
                if goal is None:
                    document.pop("evaluation_goal")
                else:
                    document["evaluation_goal"] = goal
                path.write_text(json.dumps(document), encoding="utf-8")
                report = ensure_listening_report(path)
                self.assertEqual(report["evaluation_goal"], goal)
                self.assertEqual(report["completed_trials"], 1)
                self.assertEqual(report["models"][0]["preference"]["wins"], 1)

    def test_goal_less_current_session_resumes_without_inventing_a_goal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reports, _benchmark = self._inputs(root)
            path = create_listening_session_from_reports(reports, root / "session")
            document = json.loads(path.read_text(encoding="utf-8"))
            document.pop("evaluation_goal")
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_listening_session(path)
            self.assertNotIn("evaluation_goal", loaded)
            saved = record_trial_preference(
                path, loaded["trials"][0]["trial_id"], "tie"
            )
            self.assertNotIn("evaluation_goal", saved)
            self.assertNotIn("evaluation_goal", json.loads(path.read_text()))
            self.assertIsNone(aggregate_listening_report(path)["evaluation_goal"])

    def test_goal_less_legacy_session_and_cached_report_preserve_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            imported = import_listening_session(
                write_listening_fixture(root), root / "app-data"
            ).destination
            before = {
                path.relative_to(imported): path.read_bytes()
                for path in imported.rglob("*")
                if path.is_file()
            }
            session = load_listening_session(imported / "session.json")
            report = ensure_listening_report(imported / "session.json")
            self.assertNotIn("evaluation_goal", session)
            self.assertIsNone(report.get("evaluation_goal"))
            self.assertEqual(
                {
                    path.relative_to(imported): path.read_bytes()
                    for path in imported.rglob("*")
                    if path.is_file()
                },
                before,
            )
