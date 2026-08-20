import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from vntts.authoring.cli import main as authoring_main
from vntts.authoring.failure_regeneration import (
    FailureRegenerationError,
    FailureRegenerationPlan,
    build_failure_regeneration_command,
    build_failure_regeneration_plan,
    load_failure_regeneration_plan,
    write_failure_regeneration_plan,
)


class FailureRegenerationPlanTest(unittest.TestCase):
    def fixture(self):
        queue_id = "game:line:1"
        item = {
            "status": "failed",
            "attempts": 7,
            "seed": 6,
            "last_error": "legacy limit",
        }
        repair = {
            "queue_sha256": "1" * 64,
            "state_sha256": "2" * 64,
            "records": [
                {
                    "queue_id": queue_id,
                    "line_id": "game:line",
                    "failure_kind": "missed_eos_audio_limit",
                    "attempts": 7,
                    "seed": 6,
                    "action": "provenance_recovery_or_regeneration",
                }
            ],
        }
        workspace = {
            "workspace_id": "resume-1234567890abcdef12345678-1234567890abcdef",
            "config_fingerprint": "3" * 64,
        }
        return queue_id, item, repair, workspace

    def build(self):
        queue_id, item, repair, workspace = self.fixture()
        with (
            patch(
                "vntts.authoring.failure_regeneration._load_workspace",
                return_value=(Path("/workspace"), workspace),
            ),
            patch(
                "vntts.authoring.failure_regeneration.generation_failure_repair_plan",
                return_value=repair,
            ),
            patch(
                "vntts.authoring.failure_regeneration.load_generation_state",
                return_value={"items": {queue_id: item}},
            ),
        ):
            return build_failure_regeneration_plan("workspace")

    def test_builds_exact_legacy_failure_plan(self):
        expected = self.build()
        record = expected.document["records"][0]

        self.assertEqual(expected.document["failure_count"], 1)
        self.assertEqual(record["queue_id"], "game:line:1")
        self.assertEqual(record["attempts"], 7)
        self.assertEqual(record["seed"], 6)
        self.assertEqual(len(record["item_sha256"]), 64)

    def test_plan_publication_is_no_replace_and_tamper_evident(self):
        expected = self.build()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "failure-plan.json"
            write_failure_regeneration_plan(expected, path)
            self.assertEqual(
                load_failure_regeneration_plan(path).document, expected.document
            )
            with self.assertRaisesRegex(FailureRegenerationError, "output exists"):
                write_failure_regeneration_plan(expected, path)
            forged = json.loads(path.read_text(encoding="utf-8"))
            forged["records"][0]["attempts"] = 8
            path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(
                FailureRegenerationError, "identity is invalid"
            ):
                load_failure_regeneration_plan(path)

    def test_command_is_exact_single_attempt_and_rejects_stale_plan(self):
        expected = self.build()
        argv = ("python", "generate", "--queue-id", "game:line:1")
        with (
            patch(
                "vntts.authoring.failure_regeneration.build_failure_regeneration_plan",
                return_value=expected,
            ),
            patch(
                "vntts.authoring.failure_regeneration.generation_command",
                return_value=argv,
            ) as command,
        ):
            result = build_failure_regeneration_command(
                "workspace", expected, batch_index=1, batch_size=10
            )

        self.assertEqual(result.queue_ids, ("game:line:1",))
        self.assertEqual(result.command, argv)
        command.assert_called_once_with(
            "workspace",
            queue_ids=("game:line:1",),
            regenerate_existing=True,
            retries=0,
            seed=0,
        )
        stale = FailureRegenerationPlan(
            "f" * 64, {**expected.document, "plan_id": "f" * 64}
        )
        with (
            patch(
                "vntts.authoring.failure_regeneration.build_failure_regeneration_plan",
                return_value=stale,
            ),
            self.assertRaisesRegex(FailureRegenerationError, "authority changed"),
        ):
            build_failure_regeneration_command("workspace", expected, batch_index=1)

    def test_cli_prints_plan_and_bounded_command(self):
        expected = self.build()
        output = StringIO()
        with (
            patch(
                "vntts.authoring.cli.build_failure_regeneration_plan",
                return_value=expected,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                authoring_main(["failure-regeneration-plan", "workspace"]), 0
            )
        self.assertEqual(json.loads(output.getvalue()), expected.document)


if __name__ == "__main__":
    unittest.main()
