import hashlib
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from vntts.authoring.bulk_generation import ReviewAuthority
from vntts.authoring.cli import main as authoring_main
from vntts.authoring.cohort_review import CohortReviewPlan
from vntts.authoring.pending_resolution import (
    RECOVER_OR_REGENERATE,
    PendingResolutionError,
    PendingResolutionPlan,
    build_pending_regeneration_command,
    build_pending_resolution_plan,
    load_pending_resolution_plan,
    write_pending_resolution_plan,
)
from vntts.authoring.workbench import ReviewItem


class PendingResolutionPlanTest(unittest.TestCase):
    def fixture(self):
        queue_sha256 = "1" * 64
        state_sha256 = "2" * 64
        queue_id = "game:line:1"
        plan = CohortReviewPlan(
            "3" * 64,
            {
                "workspace_id": "resume-1234567890abcdef12345678-1234567890abcdef",
                "workspace_config_fingerprint": "4" * 64,
                "queue_sha256": queue_sha256,
                "state_sha256": state_sha256,
                "blocked_items": [
                    {
                        "queue_id": queue_id,
                        "line_id": "game:line",
                        "reason": "Generation profile must be non-empty text",
                    }
                ],
            },
        )
        item = ReviewItem(
            queue_id=queue_id,
            line_id="game:line",
            speaker="Narrator",
            voice_character="Narrator",
            text="Exact pending text",
            status="generated",
            review_status="pending_review",
            attempts=3,
            seed=2,
            last_error=None,
            audio=None,
            authority=ReviewAuthority(
                queue_sha256=queue_sha256,
                state_sha256=state_sha256,
                item_sha256="5" * 64,
                audio_sha256="6" * 64,
            ),
        )
        return plan, item

    def test_builds_exact_fail_closed_record_without_mutating_authority(self):
        plan, item = self.fixture()
        with (
            patch(
                "vntts.authoring.pending_resolution.build_cohort_review_plan",
                return_value=plan,
            ),
            patch(
                "vntts.authoring.pending_resolution.list_review_items",
                return_value=(item,),
            ),
        ):
            result = build_pending_resolution_plan("workspace")

        self.assertEqual(result.document["blocked_pending_count"], 1)
        self.assertEqual(result.document["action_counts"], {RECOVER_OR_REGENERATE: 1})
        record = result.document["records"][0]
        self.assertEqual(record["queue_id"], item.queue_id)
        self.assertEqual(record["item_sha256"], "5" * 64)
        self.assertEqual(record["audio_sha256"], "6" * 64)
        self.assertEqual(
            record["text_sha256"], hashlib.sha256(item.text.encode()).hexdigest()
        )
        self.assertEqual(record["action"], RECOVER_OR_REGENERATE)

    def test_rejects_state_change_between_cohort_and_resolution_projection(self):
        plan, item = self.fixture()
        changed = SimpleNamespace(**{**item.__dict__})
        changed.authority = ReviewAuthority(
            queue_sha256=item.authority.queue_sha256,
            state_sha256="f" * 64,
            item_sha256=item.authority.item_sha256,
            audio_sha256=item.authority.audio_sha256,
        )
        with (
            patch(
                "vntts.authoring.pending_resolution.build_cohort_review_plan",
                return_value=plan,
            ),
            patch(
                "vntts.authoring.pending_resolution.list_review_items",
                return_value=(changed,),
            ),
            self.assertRaisesRegex(PendingResolutionError, "state changed"),
        ):
            build_pending_resolution_plan("workspace")

    def test_cli_prints_the_canonical_read_only_plan(self):
        plan, item = self.fixture()
        with (
            patch(
                "vntts.authoring.pending_resolution.build_cohort_review_plan",
                return_value=plan,
            ),
            patch(
                "vntts.authoring.pending_resolution.list_review_items",
                return_value=(item,),
            ),
        ):
            expected = build_pending_resolution_plan("workspace")
        output = StringIO()
        with (
            patch(
                "vntts.authoring.cli_generation.build_pending_resolution_plan",
                return_value=expected,
            ),
            redirect_stdout(output),
        ):
            exit_code = authoring_main(["pending-resolution-plan", "workspace"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected.document)

    def test_plan_publication_is_no_replace_and_tamper_evident(self):
        plan, item = self.fixture()
        with (
            patch(
                "vntts.authoring.pending_resolution.build_cohort_review_plan",
                return_value=plan,
            ),
            patch(
                "vntts.authoring.pending_resolution.list_review_items",
                return_value=(item,),
            ),
        ):
            expected = build_pending_resolution_plan("workspace")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pending-plan.json"
            write_pending_resolution_plan(expected, path)
            loaded = load_pending_resolution_plan(path)
            self.assertEqual(loaded.document, expected.document)
            with self.assertRaisesRegex(PendingResolutionError, "output exists"):
                write_pending_resolution_plan(expected, path)

            forged = json.loads(path.read_text(encoding="utf-8"))
            forged["records"][0]["audio_sha256"] = "0" * 64
            path.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaisesRegex(PendingResolutionError, "identity is invalid"):
                load_pending_resolution_plan(path)

    def test_regeneration_command_is_exact_bounded_and_current(self):
        plan, item = self.fixture()
        with (
            patch(
                "vntts.authoring.pending_resolution.build_cohort_review_plan",
                return_value=plan,
            ),
            patch(
                "vntts.authoring.pending_resolution.list_review_items",
                return_value=(item,),
            ),
        ):
            expected = build_pending_resolution_plan("workspace")
        argv = (
            "python",
            "-m",
            "vntts.authoring.cli",
            "generate",
            "--regenerate-existing",
            "--queue-id",
            item.queue_id,
        )
        with (
            patch(
                "vntts.authoring.pending_resolution.build_pending_resolution_plan",
                return_value=expected,
            ),
            patch(
                "vntts.authoring.pending_resolution.generation_command",
                return_value=argv,
            ) as command,
        ):
            result = build_pending_regeneration_command(
                "workspace", expected, batch_index=1, batch_size=10
            )

        self.assertEqual(result.batch_index, 1)
        self.assertEqual(result.batch_count, 1)
        self.assertEqual(result.queue_ids, (item.queue_id,))
        self.assertEqual(result.command, argv)
        command.assert_called_once_with(
            "workspace", queue_ids=(item.queue_id,), regenerate_existing=True
        )

    def test_regeneration_command_rejects_stale_or_out_of_range_plan(self):
        plan, item = self.fixture()
        with (
            patch(
                "vntts.authoring.pending_resolution.build_cohort_review_plan",
                return_value=plan,
            ),
            patch(
                "vntts.authoring.pending_resolution.list_review_items",
                return_value=(item,),
            ),
        ):
            expected = build_pending_resolution_plan("workspace")
        changed = PendingResolutionPlan(
            "f" * 64, {**expected.document, "plan_id": "f" * 64}
        )
        with (
            patch(
                "vntts.authoring.pending_resolution.build_pending_resolution_plan",
                return_value=changed,
            ),
            self.assertRaisesRegex(PendingResolutionError, "authority changed"),
        ):
            build_pending_regeneration_command("workspace", expected, batch_index=1)
        with (
            patch(
                "vntts.authoring.pending_resolution.build_pending_resolution_plan",
                return_value=expected,
            ),
            self.assertRaisesRegex(PendingResolutionError, "exceeds 1"),
        ):
            build_pending_regeneration_command("workspace", expected, batch_index=2)


if __name__ == "__main__":
    unittest.main()
